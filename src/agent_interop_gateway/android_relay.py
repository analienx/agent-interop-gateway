from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from .android_adb import AdbError, AndroidAdb, InjectionUnavailable, InjectionUncertain
from .chatgpt_android import (
    CHATGPT_PACKAGE,
    extract_explicit_delegation,
    extract_text_candidates,
    looks_like_delegation,
)

RESULT_PREFIX = "[local delegation result]"


@dataclass(slots=True)
class RelayConfig:
    gateway_url: str
    token: str | None
    interval: float
    arm: bool
    risk: str
    capabilities: list[str]
    natural_trigger: bool
    max_result_chars: int
    gateway_wait_seconds: float


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    fingerprint: str
    delegation_id: str
    task: str
    status: str
    result_json: str | None


class RelayLedger:
    """Crash-safe relay ledger providing at-most-once UI injection semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS relay (
                    fingerprint TEXT PRIMARY KEY,
                    delegation_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def get(self, fingerprint: str) -> LedgerRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint, delegation_id, task, status, result_json "
                "FROM relay WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return LedgerRecord(*row) if row else None

    def claim(self, fingerprint: str, task: str) -> LedgerRecord:
        delegation_id = str(uuid5(NAMESPACE_URL, f"aigw-android-relay:{fingerprint}"))
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO relay"
                "(fingerprint, delegation_id, task, status, result_json, updated_at) "
                "VALUES(?, ?, ?, 'pending', NULL, ?)",
                (fingerprint, delegation_id, task, now),
            )
        record = self.get(fingerprint)
        if record is None:
            raise RuntimeError("failed to persist relay claim")
        return record

    def update(self, fingerprint: str, status: str, result: dict | None = None) -> None:
        payload = json.dumps(result, ensure_ascii=False) if result is not None else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE relay SET status = ?, result_json = COALESCE(?, result_json), "
                "updated_at = ? WHERE fingerprint = ?",
                (status, payload, time.time(), fingerprint),
            )

    def resumable(self, limit: int = 20) -> list[LedgerRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT fingerprint, delegation_id, task, status, result_json "
                "FROM relay WHERE status IN ('pending', 'result_ready') "
                "ORDER BY updated_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [LedgerRecord(*row) for row in rows]


def _fingerprint(task: str) -> str:
    normalized = " ".join(task.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _gateway_json_request(
    config: RelayConfig,
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    prefer_async: bool = False,
    timeout: float = 30,
) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if prefer_async:
        headers["Prefer"] = "respond-async"
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"gateway unavailable: {exc.reason}") from exc


def submit_gateway(config: RelayConfig, task: str, delegation_id: str) -> dict:
    body = {
        "protocol": "aigw/1",
        "id": delegation_id,
        "task": task,
        "origin": {"surface": "chatgpt-android-adb"},
        "capabilities": config.capabilities,
        "risk": config.risk,
        "routing": {
            "preference": "local_first",
            "allow_fallback": True,
            "fallback_on_failure": config.risk == "read",
        },
    }
    base = config.gateway_url.rstrip("/")
    result = _gateway_json_request(
        config,
        base + "/v1/delegations",
        method="POST",
        body=body,
        prefer_async=True,
    )
    deadline = time.monotonic() + max(config.gateway_wait_seconds, 0)
    while result.get("state") in {"queued", "running"}:
        if time.monotonic() >= deadline:
            raise RuntimeError("delegation is still running; it will be resumed with the same id")
        time.sleep(min(1.0, max(0.1, deadline - time.monotonic())))
        result = _gateway_json_request(
            config,
            base + f"/v1/delegations/{delegation_id}",
            timeout=15,
        )
    return result


def format_result(result: dict, max_chars: int = 6000, delegation_id: str | None = None) -> str:
    prefix = RESULT_PREFIX
    if delegation_id:
        prefix = f"[local delegation result {delegation_id[-8:]}]"
    if result.get("state") == "succeeded":
        output = str(result.get("stdout") or "").strip()
        if not output:
            output = json.dumps(result.get("payload") or {}, ensure_ascii=False)
        rendered = f"{prefix} {output}".strip()
    else:
        error = (
            result.get("error") or result.get("stderr") or result.get("state") or "unknown error"
        )
        rendered = f"{prefix} FAILED: {error}"
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + " ...[result truncated by mobile relay]"
    return rendered


def _task_from_candidate(text: str, natural_trigger: bool) -> str | None:
    explicit = extract_explicit_delegation(text)
    if explicit:
        return explicit
    if natural_trigger and looks_like_delegation(text):
        return text.strip()
    return None


def _resume_record(
    bridge: AndroidAdb,
    config: RelayConfig,
    ledger: RelayLedger,
    record: LedgerRecord,
) -> None:
    result: dict
    if record.status == "pending":
        result = submit_gateway(config, record.task, record.delegation_id)
        ledger.update(record.fingerprint, "result_ready", result)
    elif record.status == "result_ready" and record.result_json:
        result = json.loads(record.result_json)
    else:
        return

    rendered = format_result(result, config.max_result_chars, record.delegation_id)
    ledger.update(record.fingerprint, "injecting")
    try:
        receipt = bridge.inject_text_message(rendered, expected_package=CHATGPT_PACKAGE)
    except InjectionUnavailable as exc:
        ledger.update(record.fingerprint, "result_ready")
        print(f"result retained until injection becomes available: {exc}", flush=True)
        return
    except InjectionUncertain as exc:
        ledger.update(record.fingerprint, "injection_uncertain")
        print(f"injection uncertain; manual review required: {exc}", flush=True)
        return
    except AdbError as exc:
        ledger.update(record.fingerprint, "injection_uncertain")
        print(f"injection failed closed; manual review required: {exc}", flush=True)
        return

    if receipt.confirmed:
        ledger.update(record.fingerprint, "injected")
        print(f"result injected and confirmed via {receipt.send_method}", flush=True)


def run_relay(args: argparse.Namespace) -> None:
    if args.arm and not args.capability:
        raise SystemExit("--arm requires at least one explicit --capability")
    bridge = AndroidAdb(args.adb, args.serial)
    bridge.ensure_device()
    config = RelayConfig(
        gateway_url=args.gateway_url,
        token=args.token or os.environ.get("AIGW_TOKEN"),
        interval=max(args.interval, 0.5),
        arm=args.arm,
        risk=args.risk,
        capabilities=args.capability,
        natural_trigger=args.natural_trigger,
        max_result_chars=args.max_result_chars,
        gateway_wait_seconds=args.gateway_wait_seconds,
    )
    ledger = RelayLedger(args.state_db)
    state = "ARMED" if config.arm else "DRY RUN"
    trigger = "natural heuristics" if config.natural_trigger else "explicit 'delegate locally ...'"
    print(f"Android relay started: {state}; trigger={trigger}.", flush=True)

    previous_keys: set[tuple[str, str, tuple[int, int, int, int] | None]] = set()
    foreground_baselined = False
    while True:
        try:
            if bridge.current_package() != CHATGPT_PACKAGE:
                previous_keys.clear()
                foreground_baselined = False
                time.sleep(max(config.interval, 1.0))
                continue

            if config.arm:
                outstanding = ledger.resumable(limit=1)
                if outstanding:
                    _resume_record(bridge, config, ledger, outstanding[0])

            candidates = extract_text_candidates(bridge.dump_ui())
            current_keys = {(item.text, item.resource_id, item.bounds) for item in candidates}
            if not foreground_baselined:
                # Never execute transcript content merely because the relay was started or
                # restarted while an old conversation was already visible.
                previous_keys = current_keys
                foreground_baselined = True
                time.sleep(config.interval)
                continue
            for candidate in candidates:
                key = (candidate.text, candidate.resource_id, candidate.bounds)
                if key in previous_keys or candidate.text.startswith(RESULT_PREFIX):
                    continue
                task = _task_from_candidate(candidate.text, config.natural_trigger)
                if not task:
                    continue
                print(f"delegation candidate: {task}", flush=True)
                if not config.arm:
                    continue
                fingerprint = _fingerprint(task)
                record = ledger.claim(fingerprint, task)
                if record.status in {"injected", "injecting", "injection_uncertain"}:
                    continue
                _resume_record(bridge, config, ledger, record)
            previous_keys = current_keys
            time.sleep(config.interval)
        except (AdbError, RuntimeError) as exc:
            print(f"relay error: {exc}", flush=True)
            time.sleep(max(config.interval, 2.0))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aigw-android-relay",
        description="Experimental semantic-UI relay from ChatGPT Android to AIGW",
    )
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8765")
    parser.add_argument("--token")
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--risk", choices=["read", "write", "privileged"], default="read")
    parser.add_argument(
        "--state-db",
        default=str(Path.home() / ".agent-interop-gateway" / "android-relay.sqlite3"),
    )
    parser.add_argument("--max-result-chars", type=int, default=6000)
    parser.add_argument("--gateway-wait-seconds", type=float, default=20.0)
    parser.add_argument(
        "--natural-trigger",
        action="store_true",
        help="allow broad language heuristics; explicit delegation phrases are safer",
    )
    parser.add_argument(
        "--arm",
        action="store_true",
        help="actually execute matching requests; default is observation-only",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        run_relay(args)
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()

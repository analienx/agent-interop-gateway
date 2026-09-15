from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .android_adb import AdbError, AndroidAdb
from .chatgpt_android import CHATGPT_PACKAGE, extract_text_candidates, looks_like_delegation

RESULT_PREFIX = "[local delegation result]"


@dataclass(slots=True)
class RelayConfig:
    gateway_url: str
    token: str | None
    interval: float
    arm: bool
    risk: str
    capabilities: list[str]


def submit_gateway(config: RelayConfig, task: str) -> dict:
    body = {
        "protocol": "aigw/1",
        "task": task,
        "origin": {"surface": "chatgpt-android-adb"},
        "capabilities": config.capabilities,
        "risk": config.risk,
        "routing": {"preference": "local_first", "allow_fallback": True},
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    request = urllib.request.Request(
        config.gateway_url.rstrip("/") + "/v1/delegations",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway HTTP {exc.code}: {detail}") from exc


def format_result(result: dict) -> str:
    if result.get("state") == "succeeded":
        output = str(result.get("stdout") or "").strip()
        if not output:
            output = json.dumps(result.get("payload") or {}, ensure_ascii=False)
        return f"{RESULT_PREFIX} {output}".strip()
    error = result.get("error") or result.get("stderr") or result.get("state") or "unknown error"
    return f"{RESULT_PREFIX} FAILED: {error}"


def run_relay(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    config = RelayConfig(
        gateway_url=args.gateway_url,
        token=args.token or os.environ.get("AIGW_TOKEN"),
        interval=args.interval,
        arm=args.arm,
        risk=args.risk,
        capabilities=args.capability,
    )
    seen: set[str] = set()
    state = "ARMED: matching requests may execute." if config.arm else "DRY RUN: no work executes."
    print(f"Android relay started. {state}", flush=True)

    while True:
        try:
            if bridge.current_package() != CHATGPT_PACKAGE:
                time.sleep(max(config.interval, 1.0))
                continue
            candidates = extract_text_candidates(bridge.dump_ui())
            for candidate in candidates:
                text = candidate.text.strip()
                if not text or text in seen or text.startswith(RESULT_PREFIX):
                    continue
                seen.add(text)
                if not looks_like_delegation(text):
                    continue
                print(f"candidate: {text}", flush=True)
                if not config.arm:
                    continue
                result = submit_gateway(config, text)
                rendered = format_result(result)
                try:
                    bridge.inject_text_message(rendered)
                    print("result injected into exposed composer", flush=True)
                except AdbError as exc:
                    print(f"result ready but composer injection unavailable: {exc}", flush=True)
                    print(rendered, flush=True)
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
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--capability", action="append", default=[])
    parser.add_argument("--risk", choices=["read", "write", "privileged"], default="read")
    parser.add_argument(
        "--arm",
        action="store_true",
        help="actually submit matching text; without this flag the relay is observation-only",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    run_relay(args)


if __name__ == "__main__":
    main()

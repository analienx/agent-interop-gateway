from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass

from .android_adb import AdbError, AndroidAdb, UiNode

CHATGPT_PACKAGE = "com.openai.chatgpt"
EXPLICIT_TRIGGER_PATTERNS = [
    re.compile(r"^\s*delegate\s+locally\b[\s:,-]*(.+)$", re.IGNORECASE),
    re.compile(r"^\s*local\s+delegate\b[\s:,-]*(.+)$", re.IGNORECASE),
    re.compile(
        r"^\s*delegate\s+(?:this\s+)?to\s+(?:the\s+)?local\s+machine\b[\s:,-]*(.+)$", re.IGNORECASE
    ),
]


@dataclass(slots=True)
class TranscriptCandidate:
    text: str
    resource_id: str
    class_name: str
    bounds: tuple[int, int, int, int] | None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_text_candidates(nodes: list[UiNode]) -> list[TranscriptCandidate]:
    """Return de-duplicated human-readable text exposed by the current UI tree."""
    candidates: list[TranscriptCandidate] = []
    seen: set[tuple[str, str, tuple[int, int, int, int] | None]] = set()
    for node in nodes:
        text = _normalize_text(node.text)
        if len(text) < 2:
            continue
        key = (text, node.resource_id, node.bounds)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            TranscriptCandidate(
                text=text,
                resource_id=node.resource_id,
                class_name=node.class_name,
                bounds=node.bounds,
            )
        )
    return candidates


def extract_explicit_delegation(text: str) -> str | None:
    normalized = _normalize_text(text)
    for pattern in EXPLICIT_TRIGGER_PATTERNS:
        match = pattern.match(normalized)
        if match:
            task = _normalize_text(match.group(1))
            return task or None
    return None


def looks_like_delegation(text: str) -> bool:
    if extract_explicit_delegation(text):
        return True
    patterns = [
        r"\bdelegate\b",
        r"\bon my (?:local )?(?:machine|computer|pc)\b",
        r"\brun (?:this|it) locally\b",
        r"\buse (?:the )?local (?:machine|executor|agent)\b",
    ]
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def _snapshot(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    serial = bridge.ensure_device()
    package = bridge.current_package()
    payload = {"serial": serial, "package": package, "nodes": bridge.semantic_snapshot()}
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _probe(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    serial = bridge.ensure_device()
    package = bridge.current_package()
    nodes = bridge.dump_ui()
    payload = {
        "serial": serial,
        "package": package,
        "chatgpt_foreground": package == CHATGPT_PACKAGE,
        "editor_exposed": bridge.find_editor(nodes) is not None,
        "send_exposed": bridge.find_send(nodes) is not None,
        "text_candidates": [x.text for x in extract_text_candidates(nodes)],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _devices(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    payload = [
        {"serial": item.serial, "state": item.state, "details": item.details}
        for item in bridge.list_devices()
    ]
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _inject(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    if bridge.current_package() != CHATGPT_PACKAGE and not args.force:
        raise SystemExit("ChatGPT is not the foreground package; pass --force to override")
    receipt = bridge.inject_text_message(
        args.text,
        expected_package=None if args.force else CHATGPT_PACKAGE,
    )
    print(json.dumps({"confirmed": receipt.confirmed, "method": receipt.send_method}))


def _watch(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    bridge.ensure_device()
    previous: set[tuple[str, str, tuple[int, int, int, int] | None]] = set()
    print("Watching semantic UI text. No action is executed by this command.")
    while True:
        try:
            current = extract_text_candidates(bridge.dump_ui())
            current_keys = {(item.text, item.resource_id, item.bounds) for item in current}
            for candidate in current:
                key = (candidate.text, candidate.resource_id, candidate.bounds)
                if key in previous:
                    continue
                explicit = extract_explicit_delegation(candidate.text)
                marker = " [explicit-delegation]" if explicit else ""
                if not explicit and looks_like_delegation(candidate.text):
                    marker = " [delegation-hint]"
                print(candidate.text + marker, flush=True)
            previous = current_keys
            time.sleep(max(args.interval, 0.25))
        except AdbError as exc:
            print(f"ADB error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigw-chatgpt-android")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("devices").set_defaults(func=_devices)
    sub.add_parser("snapshot").set_defaults(func=_snapshot)
    sub.add_parser("probe").set_defaults(func=_probe)
    inject = sub.add_parser("inject")
    inject.add_argument("text")
    inject.add_argument("--force", action="store_true")
    inject.set_defaults(func=_inject)
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=float, default=1.5)
    watch.set_defaults(func=_watch)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except AdbError as exc:
        raise SystemExit(f"ADB error: {exc}") from exc


if __name__ == "__main__":
    main()

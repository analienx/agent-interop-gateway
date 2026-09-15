from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass

from .android_adb import AdbError, AndroidAdb, UiNode

CHATGPT_PACKAGE = "com.openai.chatgpt"


@dataclass(slots=True)
class TranscriptCandidate:
    text: str
    resource_id: str


def extract_text_candidates(nodes: list[UiNode]) -> list[TranscriptCandidate]:
    """Return human-readable text exposed by the current ChatGPT Android UI.

    ChatGPT does not publish a stable accessibility schema for conversation roles, so this
    function deliberately does not pretend it can distinguish user vs assistant reliably.
    Consumers must de-duplicate and apply an explicit trigger/classifier before execution.
    """
    candidates: list[TranscriptCandidate] = []
    seen: set[str] = set()
    for node in nodes:
        text = node.text.strip()
        if not text or text in seen:
            continue
        if len(text) < 2:
            continue
        seen.add(text)
        candidates.append(TranscriptCandidate(text=text, resource_id=node.resource_id))
    return candidates


def looks_like_delegation(text: str) -> bool:
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
    package = bridge.current_package()
    payload = {"package": package, "nodes": bridge.semantic_snapshot()}
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _probe(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    package = bridge.current_package()
    nodes = bridge.dump_ui()
    payload = {
        "package": package,
        "chatgpt_foreground": package == CHATGPT_PACKAGE,
        "editor_exposed": bridge.find_editor(nodes) is not None,
        "send_exposed": bridge.find_send(nodes) is not None,
        "text_candidates": [x.text for x in extract_text_candidates(nodes)],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _inject(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    if bridge.current_package() != CHATGPT_PACKAGE and not args.force:
        raise SystemExit("ChatGPT is not the foreground package; pass --force to override")
    bridge.inject_text_message(args.text)


def _watch(args: argparse.Namespace) -> None:
    bridge = AndroidAdb(args.adb, args.serial)
    previous: set[str] = set()
    print("Watching semantic UI text. No action is executed by this command.")
    while True:
        try:
            current = extract_text_candidates(bridge.dump_ui())
            for candidate in current:
                if candidate.text in previous:
                    continue
                marker = " [delegation?]" if looks_like_delegation(candidate.text) else ""
                print(candidate.text + marker, flush=True)
            previous = {x.text for x in current}
            time.sleep(args.interval)
        except AdbError as exc:
            print(f"ADB error: {exc}", flush=True)
            time.sleep(max(args.interval, 2.0))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigw-chatgpt-android")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot").set_defaults(func=_snapshot)
    sub.add_parser("probe").set_defaults(func=_probe)
    inject = sub.add_parser("inject")
    inject.add_argument("text")
    inject.add_argument("--force", action="store_true")
    inject.set_defaults(func=_inject)
    watch = sub.add_parser("watch")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.set_defaults(func=_watch)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

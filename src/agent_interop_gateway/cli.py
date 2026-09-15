from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

from .config import load_config


def _request(url: str, method: str = "GET", body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {payload}") from exc


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .api import create_app

    config = load_config(args.config)
    host = args.host or config.host
    if host not in {"127.0.0.1", "::1", "localhost"} and not config.token:
        raise SystemExit("refusing non-loopback bind without AIGW_TOKEN")
    uvicorn.run(
        create_app(config),
        host=host,
        port=args.port or config.port,
        reload=False,
    )


def _submit(args: argparse.Namespace) -> None:
    base = args.url.rstrip("/")
    body = {
        "protocol": "aigw/1",
        "task": args.task,
        "capabilities": args.capability,
        "risk": args.risk,
        "routing": {
            "preference": args.preference,
            "executor": args.executor,
            "allow_fallback": not args.no_fallback,
        },
        "origin": {"surface": args.surface},
    }
    token = args.token or os.environ.get("AIGW_TOKEN")
    result = _request(f"{base}/v1/delegations", "POST", body, token)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aigw", description="Agent Interop Gateway")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the local gateway")
    serve.add_argument("--config")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.set_defaults(func=_serve)

    submit = sub.add_parser("submit", help="submit a delegation")
    submit.add_argument("task")
    submit.add_argument("--url", default="http://127.0.0.1:8765")
    submit.add_argument("--token")
    submit.add_argument("--capability", action="append", default=[])
    submit.add_argument("--risk", choices=["read", "write", "privileged"], default="read")
    submit.add_argument(
        "--preference",
        choices=["local_first", "lowest_cost", "quality_first", "specific"],
        default="local_first",
    )
    submit.add_argument("--executor")
    submit.add_argument("--no-fallback", action="store_true")
    submit.add_argument("--surface", default="cli")
    submit.set_defaults(func=_submit)

    token = sub.add_parser("token", help="generate a bearer token")
    token.set_defaults(func=lambda _args: print(secrets.token_urlsafe(32)))

    health = sub.add_parser("health", help="check a gateway")
    health.add_argument("--url", default="http://127.0.0.1:8765")
    health.set_defaults(
        func=lambda args: print(
            json.dumps(_request(args.url.rstrip("/") + "/health"), indent=2)
        )
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

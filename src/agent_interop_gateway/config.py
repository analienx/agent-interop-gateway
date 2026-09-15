from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ExecutorConfig:
    name: str
    type: str
    argv: list[str] = field(default_factory=list)
    capabilities: set[str] = field(default_factory=set)
    cost_tier: int = 0
    quality_tier: int = 0
    priority: int = 100
    enabled: bool = True
    cwd: str | None = None
    allowed_commands: set[str] = field(default_factory=set)
    allowed_env: set[str] = field(default_factory=set)


@dataclass(slots=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    allow_write: bool = False
    allow_privileged: bool = False
    max_output_chars: int = 50_000
    executors: list[ExecutorConfig] = field(default_factory=list)


def _default_path() -> Path:
    default = Path.home() / ".agent-interop-gateway" / "gateway.toml"
    return Path(os.environ.get("AIGW_CONFIG", default))


def _executor_from_dict(raw: dict[str, Any]) -> ExecutorConfig:
    return ExecutorConfig(
        name=str(raw["name"]),
        type=str(raw.get("type", "agent_process")),
        argv=[str(x) for x in raw.get("argv", [])],
        capabilities={str(x) for x in raw.get("capabilities", [])},
        cost_tier=int(raw.get("cost_tier", 0)),
        quality_tier=int(raw.get("quality_tier", 0)),
        priority=int(raw.get("priority", 100)),
        enabled=bool(raw.get("enabled", True)),
        cwd=str(raw["cwd"]) if raw.get("cwd") else None,
        allowed_commands={str(x) for x in raw.get("allowed_commands", [])},
        allowed_env={str(x) for x in raw.get("allowed_env", [])},
    )


def load_config(path: str | Path | None = None) -> GatewayConfig:
    config_path = Path(path) if path else _default_path()
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

    gateway = raw.get("gateway", {})
    executors = [_executor_from_dict(x) for x in raw.get("executors", [])]
    if not executors:
        executors = [
            ExecutorConfig(
                name="echo",
                type="echo",
                capabilities={"demo"},
                cost_tier=0,
                quality_tier=0,
                priority=1000,
            )
        ]

    return GatewayConfig(
        host=str(os.environ.get("AIGW_HOST", gateway.get("host", "127.0.0.1"))),
        port=int(os.environ.get("AIGW_PORT", gateway.get("port", 8765))),
        token=os.environ.get("AIGW_TOKEN", gateway.get("token")),
        allow_write=_env_bool("AIGW_ALLOW_WRITE", gateway.get("allow_write", False)),
        allow_privileged=_env_bool(
            "AIGW_ALLOW_PRIVILEGED", gateway.get("allow_privileged", False)
        ),
        max_output_chars=int(gateway.get("max_output_chars", 50_000)),
        executors=executors,
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}

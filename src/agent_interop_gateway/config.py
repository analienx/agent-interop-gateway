from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


EXECUTOR_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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
    allowed_risks: set[str] = field(default_factory=lambda: {"read"})
    inherit_env: bool = False
    allow_path_lookup: bool = False
    allowed_cwds: set[str] = field(default_factory=set)
    max_parallel: int = 1
    max_attempts: int = 1
    retry_backoff_seconds: float = 1.0
    retry_exit_codes: set[int] = field(default_factory=set)
    transient_stderr_patterns: list[str] = field(default_factory=list)
    kill_grace_seconds: float = 2.0


@dataclass(slots=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str | None = None
    allow_write: bool = False
    allow_privileged: bool = False
    max_output_chars: int = 50_000
    max_task_chars: int = 20_000
    max_metadata_bytes: int = 16_384
    max_request_bytes: int = 131_072
    max_concurrent_delegations: int = 4
    max_queued_delegations: int = 64
    result_ttl_seconds: int = 86_400
    state_db: str | None = None
    executors: list[ExecutorConfig] = field(default_factory=list)


def _default_path() -> Path:
    default = Path.home() / ".agent-interop-gateway" / "gateway.toml"
    return Path(os.environ.get("AIGW_CONFIG", default))


def _default_state_db() -> Path:
    return Path.home() / ".agent-interop-gateway" / "state.sqlite3"


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
        allowed_risks={str(x) for x in raw.get("allowed_risks", ["read"])},
        inherit_env=bool(raw.get("inherit_env", False)),
        allow_path_lookup=bool(raw.get("allow_path_lookup", False)),
        allowed_cwds={str(x) for x in raw.get("allowed_cwds", [])},
        max_parallel=int(raw.get("max_parallel", 1)),
        max_attempts=int(raw.get("max_attempts", 1)),
        retry_backoff_seconds=float(raw.get("retry_backoff_seconds", 1.0)),
        retry_exit_codes={int(x) for x in raw.get("retry_exit_codes", [])},
        transient_stderr_patterns=[str(x) for x in raw.get("transient_stderr_patterns", [])],
        kill_grace_seconds=float(raw.get("kill_grace_seconds", 2.0)),
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

    state_db_raw = os.environ.get("AIGW_STATE_DB", gateway.get("state_db"))
    if state_db_raw is None:
        state_db_raw = str(_default_state_db())

    config = GatewayConfig(
        host=str(os.environ.get("AIGW_HOST", gateway.get("host", "127.0.0.1"))),
        port=int(os.environ.get("AIGW_PORT", gateway.get("port", 8765))),
        token=os.environ.get("AIGW_TOKEN", gateway.get("token")),
        allow_write=_env_bool("AIGW_ALLOW_WRITE", gateway.get("allow_write", False)),
        allow_privileged=_env_bool("AIGW_ALLOW_PRIVILEGED", gateway.get("allow_privileged", False)),
        max_output_chars=int(gateway.get("max_output_chars", 50_000)),
        max_task_chars=int(gateway.get("max_task_chars", 20_000)),
        max_metadata_bytes=int(gateway.get("max_metadata_bytes", 16_384)),
        max_request_bytes=int(gateway.get("max_request_bytes", 131_072)),
        max_concurrent_delegations=int(gateway.get("max_concurrent_delegations", 4)),
        max_queued_delegations=int(gateway.get("max_queued_delegations", 64)),
        result_ttl_seconds=int(gateway.get("result_ttl_seconds", 86_400)),
        state_db=str(Path(state_db_raw).expanduser()) if state_db_raw else None,
        executors=executors,
    )
    validate_config(config)
    return config


def validate_config(config: GatewayConfig) -> None:
    if not 1 <= config.port <= 65535:
        raise ConfigError("gateway port must be between 1 and 65535")
    for name, value in {
        "max_output_chars": config.max_output_chars,
        "max_task_chars": config.max_task_chars,
        "max_metadata_bytes": config.max_metadata_bytes,
        "max_request_bytes": config.max_request_bytes,
        "max_concurrent_delegations": config.max_concurrent_delegations,
        "max_queued_delegations": config.max_queued_delegations,
        "result_ttl_seconds": config.result_ttl_seconds,
    }.items():
        if value <= 0:
            raise ConfigError(f"{name} must be positive")
    if config.max_request_bytes < config.max_task_chars:
        raise ConfigError("max_request_bytes must be >= max_task_chars")
    if config.max_queued_delegations < config.max_concurrent_delegations:
        raise ConfigError("max_queued_delegations must be >= max_concurrent_delegations")

    names: set[str] = set()
    valid_types = {"echo", "agent_process", "structured_process"}
    for executor in config.executors:
        if not EXECUTOR_NAME.fullmatch(executor.name) or executor.name in names:
            raise ConfigError(
                f"executor names must be unique and match [A-Za-z0-9._-]{{1,64}}: {executor.name!r}"
            )
        names.add(executor.name)
        if executor.type not in valid_types:
            raise ConfigError(f"unknown executor type {executor.type!r}")
        if executor.enabled and executor.type == "agent_process" and not executor.argv:
            raise ConfigError(f"enabled agent_process {executor.name!r} requires argv")
        if (
            executor.enabled
            and executor.type == "structured_process"
            and not executor.allowed_commands
        ):
            raise ConfigError(
                f"enabled structured_process {executor.name!r} requires allowed_commands"
            )
        if executor.cost_tier < 0 or executor.quality_tier < 0:
            raise ConfigError(f"executor {executor.name!r} cost/quality tiers cannot be negative")
        if executor.max_parallel <= 0 or executor.max_attempts <= 0:
            raise ConfigError(
                f"executor {executor.name!r} concurrency/attempt limits must be positive"
            )
        if executor.retry_backoff_seconds < 0 or executor.kill_grace_seconds < 0:
            raise ConfigError(f"executor {executor.name!r} timing values cannot be negative")
        if not executor.allowed_risks or not executor.allowed_risks.issubset(
            {"read", "write", "privileged"}
        ):
            raise ConfigError(f"executor {executor.name!r} has invalid allowed_risks")
        for pattern in executor.transient_stderr_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"executor {executor.name!r} has invalid transient stderr regex: {pattern!r}"
                ) from exc

    if config.token is not None and (
        len(config.token) < 24
        or config.token != config.token.strip()
        or any(ord(char) < 33 or ord(char) > 126 for char in config.token)
    ):
        raise ConfigError("configured bearer token must be >=24 printable ASCII characters")
    if not _is_loopback_host(config.host) and not config.token:
        raise ConfigError("a bearer token is required when binding beyond loopback")
    if (config.allow_write or config.allow_privileged) and not config.token:
        raise ConfigError(
            "a bearer token is required when write or privileged delegations are enabled"
        )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"localhost", "ip6-localhost"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}

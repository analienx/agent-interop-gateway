from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AigwModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    PRIVILEGED = "privileged"


class DelegationState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class Durability(StrEnum):
    VOLATILE = "volatile"
    COMMITTED = "committed"
    UNCERTAIN = "uncertain"


class RoutingPreference(StrEnum):
    LOCAL_FIRST = "local_first"
    LOWEST_COST = "lowest_cost"
    QUALITY_FIRST = "quality_first"
    SPECIFIC = "specific"


class Origin(AigwModel):
    surface: str = Field(default="unknown", min_length=1, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=256)
    actor: str | None = Field(default=None, max_length=256)


class Action(AigwModel):
    kind: Literal["process"]
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str | None = Field(default=None, max_length=4096)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 8192 for item in value):
            raise ValueError("argv entries must be non-empty and <= 8192 characters")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 64:
            raise ValueError("at most 64 environment entries are allowed")
        for key, item in value.items():
            if not key or len(key) > 256 or len(item) > 8192:
                raise ValueError("invalid environment entry")
        return value


class Routing(AigwModel):
    preference: RoutingPreference = RoutingPreference.LOCAL_FIRST
    executor: str | None = Field(default=None, max_length=128)
    allow_fallback: bool = True
    fallback_on_failure: bool = False


class DelegationRequest(AigwModel):
    protocol: Literal["aigw/1"] = "aigw/1"
    id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=128)
    task: str = Field(min_length=1, max_length=100_000)
    origin: Origin = Field(default_factory=Origin)
    capabilities: list[str] = Field(default_factory=list, max_length=128)
    risk: Risk = Risk.READ
    routing: Routing = Field(default_factory=Routing)
    action: Action | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            item = item.strip()
            if not item or len(item) > 128:
                raise ValueError("capabilities must be non-empty and <= 128 characters")
            if item not in seen:
                normalized.append(item)
                seen.add(item)
        return normalized


class ExecutorAttempt(AigwModel):
    executor: str
    attempt: int = Field(ge=1)
    started_at: datetime
    completed_at: datetime
    exit_code: int | None = None
    error: str | None = None
    transient: bool = False


class DelegationResult(AigwModel):
    protocol: Literal["aigw/1"] = "aigw/1"
    delegation_id: str
    state: DelegationState
    executor: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: list[ExecutorAttempt] = Field(default_factory=list)
    replayed: bool = False
    durability: Durability = Durability.VOLATILE

    @classmethod
    def queued(cls, request_id: str) -> DelegationResult:
        return cls(delegation_id=request_id, state=DelegationState.QUEUED)

    def mark_started(self, executor: str) -> None:
        self.state = DelegationState.RUNNING
        self.executor = executor
        self.started_at = datetime.now(UTC)

    def mark_completed(self, *, ok: bool, exit_code: int | None = None) -> None:
        self.state = DelegationState.SUCCEEDED if ok else DelegationState.FAILED
        self.exit_code = exit_code
        self.completed_at = datetime.now(UTC)

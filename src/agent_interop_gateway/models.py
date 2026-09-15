from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


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


class RoutingPreference(StrEnum):
    LOCAL_FIRST = "local_first"
    LOWEST_COST = "lowest_cost"
    QUALITY_FIRST = "quality_first"
    SPECIFIC = "specific"


class Origin(BaseModel):
    surface: str = "unknown"
    conversation_id: str | None = None
    actor: str | None = None


class Action(BaseModel):
    kind: Literal["process"]
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class Routing(BaseModel):
    preference: RoutingPreference = RoutingPreference.LOCAL_FIRST
    executor: str | None = None
    allow_fallback: bool = True


class DelegationRequest(BaseModel):
    protocol: Literal["aigw/1"] = "aigw/1"
    id: str = Field(default_factory=lambda: str(uuid4()))
    task: str = Field(min_length=1)
    origin: Origin = Field(default_factory=Origin)
    capabilities: list[str] = Field(default_factory=list)
    risk: Risk = Risk.READ
    routing: Routing = Field(default_factory=Routing)
    action: Action | None = None
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DelegationResult(BaseModel):
    protocol: Literal["aigw/1"] = "aigw/1"
    delegation_id: str
    state: DelegationState
    executor: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

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

"""Typed Agent Foundry v3 client boundary for the gateway.

The gateway is an authenticated transport/translation adapter, not a
scheduler or model router. This module defines the typed v3 request/response
schemas and the abstract client interface that forwards explicit Foundry
operations (prepare/attach/execute/status/cancel/read-result/quarantine)
plus cursor/paginated bulk reads.

Ownership reminders (see FOUNDRY_EXECUTION_SUBSTRATE_V3):

- Model/account/cost routing belongs to Cline Model Optimizer. The v3 path
  carries no model, cost-tier, or account selection fields; any such field
  is rejected with an explicit error.
- General shell/filesystem mutation is NOT a v3 tool. The closed v3 write
  set is: prepare, attach, execute, cancel, quarantine (+ typed approval
  decisions recorded against jobs where the Foundry deployment supports
  them). This module exposes no shell, no filesystem mutation, and no
  scheduler.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

# States mirror the Foundry v3 durable machine (agent-foundry PR #2).
JOB_STATES = frozenset(
    {
        "accepted",
        "preparing",
        "ready",
        "running",
        "succeeded",
        "failed",
        "cancel_requested",
        "cancelled",
        "interrupted",
        "unknown_outcome",
        "quarantined",
    }
)

TERMINAL_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "interrupted", "unknown_outcome", "quarantined"}
)

# Closed v3 write set. Anything else (shell, filesystem mutation,
# model/account selection) must not be added here.
V3_WRITE_OPS = ("prepare", "attach", "execute", "cancel", "quarantine")

# Fields owned by Cline Model Optimizer. Rejected on the v3 path.
FORBIDDEN_V3_FIELDS = frozenset(
    {
        "model",
        "model_name",
        "model_id",
        "account",
        "account_alias",
        "account_id",
        "cost_tier",
        "cost",
        "price",
        "quota",
        "routing",
        "route",
    }
)

# Bulk-read surfaces from supervisor/mcp/FOUNDRY_CONTRACT.md (config PR #37).
BULK_RESOURCES = (
    "projects",
    "jobs",
    "attempts",
    "activity",
    "artifacts",
    "approvals",
    "health",
    "evidence",
)


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def check_no_model_routing_fields(body: dict[str, Any]) -> str | None:
    """Return an error message when a v3 body carries router-owned fields."""
    found = sorted(FORBIDDEN_V3_FIELDS.intersection(body))
    if found:
        names = ", ".join(found)
        return (
            f"v3 path rejects model/cost/account selection fields ({names}); "
            "model and account routing is owned by Cline Model Optimizer"
        )
    return None


# -- typed v3 schemas ----------------------------------------------------


class V3PrepareRequest(BaseModel):
    project: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    source_digest: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    policy_hash: str = ""
    objective: str = ""
    authority_ref: str = ""
    actor: str = "gateway"

    model_config = {"extra": "forbid"}


class V3AttachRequest(BaseModel):
    manifest: dict[str, Any]
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = "gateway"

    model_config = {"extra": "forbid"}


class V3ExecuteRequest(BaseModel):
    command_profile: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    authority_ref: str = Field(min_length=1)
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)
    native_identity: str | None = None
    actor: str = "gateway"

    model_config = {"extra": "forbid"}


class V3CancelRequest(BaseModel):
    reason: str = Field(min_length=1)
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = "gateway"

    model_config = {"extra": "forbid"}


class V3QuarantineRequest(BaseModel):
    reason: str = Field(min_length=1)
    expected_generation: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1)
    actor: str = "gateway"

    model_config = {"extra": "forbid"}


class V3WriteResponse(BaseModel):
    protocol: Literal["foundry/v3"] = "foundry/v3"
    op: str
    job_id: str
    state: str
    generation: int
    attempt_id: str
    request_hash: str
    policy: dict[str, Any]
    replayed: bool = False


class V3StatusResponse(BaseModel):
    protocol: Literal["foundry/v3"] = "foundry/v3"
    job: dict[str, Any]
    events: list[dict[str, Any]]
    cursor: int


class V3ResultResponse(BaseModel):
    protocol: Literal["foundry/v3"] = "foundry/v3"
    job_id: str
    state: str
    generation: int
    attempt_id: str
    attempt_count: int
    result: dict[str, Any] | None = None
    artifact_digests: list[str] = Field(default_factory=list)
    quarantine_reason: str | None = None
    cursor: int


class V3PageResponse(BaseModel):
    protocol: Literal["foundry/v3"] = "foundry/v3"
    resource: str
    items: list[dict[str, Any]]
    next_cursor: str | None = None


# -- client boundary ------------------------------------------------------


class FoundryError(RuntimeError):
    """Typed Foundry failure surfaced through the adapter."""

    def __init__(self, message: str, *, code: str = "foundry_error"):
        super().__init__(message)
        self.code = code


class IdempotencyConflict(FoundryError):
    def __init__(self, message: str = "idempotency key belongs to a different request"):
        super().__init__(message, code="idempotency_conflict")


class StaleGeneration(FoundryError):
    def __init__(self, message: str):
        super().__init__(message, code="stale_generation")


class IllegalTransition(FoundryError):
    def __init__(self, message: str):
        super().__init__(message, code="illegal_transition")


class JobNotFound(FoundryError):
    def __init__(self, job_id: str):
        super().__init__(f"job not found: {job_id}", code="job_not_found")


class JobNotReady(FoundryError):
    def __init__(self, job_id: str, state: str):
        super().__init__(f"job {job_id} is {state!r}, not terminal", code="job_not_ready")


@dataclass
class FoundryClient(ABC):
    """Abstract typed boundary to a Foundry v3 deployment.

    Implementations forward explicit operations; they never schedule work,
    select models/accounts, or execute shell commands. Bulk reads are
    cursor-paginated.
    """

    @abstractmethod
    def prepare_job(self, request: V3PrepareRequest) -> V3WriteResponse: ...

    @abstractmethod
    def attach_artifact(self, job_id: str, request: V3AttachRequest) -> V3WriteResponse: ...

    @abstractmethod
    def execute_job(self, job_id: str, request: V3ExecuteRequest) -> V3WriteResponse: ...

    @abstractmethod
    def job_status(self, job_id: str, after_cursor: int = 0) -> V3StatusResponse: ...

    @abstractmethod
    def cancel_job(self, job_id: str, request: V3CancelRequest) -> V3WriteResponse: ...

    @abstractmethod
    def read_result(self, job_id: str) -> V3ResultResponse: ...

    @abstractmethod
    def quarantine_job(self, job_id: str, request: V3QuarantineRequest) -> V3WriteResponse: ...

    @abstractmethod
    def bulk_read(
        self,
        resource: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        job_id: str | None = None,
    ) -> V3PageResponse: ...


# -- synthetic in-memory client (tests / offline adapter use) --------------


@dataclass
class _JobRecord:
    job_id: str
    project: str
    ref: str
    source_digest: str
    generation: int
    state: str
    policy_hash: str
    objective: str
    authority_ref: str
    command_profile: str = ""
    native_identity: str | None = None
    result: dict[str, Any] | None = None
    quarantine_reason: str | None = None
    attempt_id: str = ""
    attempt_count: int = 1
    created_at: str = ""
    updated_at: str = ""


@dataclass
class InMemoryFoundryClient(FoundryClient):
    """Synthetic Foundry v3 deployment for tests and offline adapter use.

    Mirrors the durable semantics of the agent-foundry reference
    implementation (idempotent mutating calls with request hashes,
    generation fencing, monotonic event cursors, terminal immutability,
    quarantine from any non-terminal state) without importing it, so the
    gateway stays a decoupled transport adapter.
    """

    jobs: dict[str, _JobRecord] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    attachments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    idempotency: dict[tuple[str, str], tuple[str, dict[str, Any]]] = field(default_factory=dict)
    _seq: int = 0

    # -- internals --------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _policy(self, op: str) -> dict[str, Any]:
        return {
            "decision": "forwarded",
            "op": op,
            "route_owner": "cline-model-optimizer",
            "scheduler": "none: gateway is a transport adapter",
        }

    def _record(
        self, job: _JobRecord, *, from_state: str, to_state: str, actor: str, reason: str
    ) -> dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "job_id": job.job_id,
            "attempt_id": job.attempt_id,
            "generation": job.generation,
            "ts": self._now(),
            "actor": actor,
            "from_state": from_state,
            "to_state": to_state,
            "reason": reason,
        }
        self.events.append(event)
        return event

    def _job_dict(self, job: _JobRecord) -> dict[str, Any]:
        return {
            "job_id": job.job_id,
            "project": job.project,
            "ref": job.ref,
            "source_digest": job.source_digest,
            "generation": job.generation,
            "state": job.state,
            "policy_hash": job.policy_hash,
            "objective": job.objective,
            "authority_ref": job.authority_ref,
            "command_profile": job.command_profile,
            "native_identity": job.native_identity,
            "attempt_id": job.attempt_id,
            "attempt_count": job.attempt_count,
            "quarantine_reason": job.quarantine_reason,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
        }

    def _idem_begin(self, scope: str, key: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not key:
            raise FoundryError("idempotency_key is required", code="missing_idempotency_key")
        digest = canonical_hash(payload)
        slot = (scope, key)
        if slot in self.idempotency:
            stored_hash, stored_response = self.idempotency[slot]
            if stored_hash != digest:
                raise IdempotencyConflict()
            return {**stored_response, "replayed": True}
        return None

    def _idem_store(
        self, scope: str, key: str, payload: dict[str, Any], response: dict[str, Any]
    ) -> None:
        self.idempotency[(scope, key)] = (canonical_hash(payload), response)

    def _get(self, job_id: str) -> _JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise JobNotFound(job_id) from None

    def _check_generation(self, job: _JobRecord, expected: int) -> None:
        if job.generation != expected:
            raise StaleGeneration(
                f"stale generation: have {job.generation}, call carried {expected}"
            )

    def _transition(
        self, job: _JobRecord, to_state: str, *, actor: str, reason: str, expected_generation: int
    ) -> None:
        self._check_generation(job, expected_generation)
        if job.state in TERMINAL_STATES:
            raise IllegalTransition(f"terminal job {job.job_id} in {job.state!r} is immutable")
        if to_state == "quarantined":
            if job.state not in JOB_STATES - TERMINAL_STATES:
                raise IllegalTransition(f"job in {job.state!r} cannot be quarantined")
        elif to_state == "cancel_requested" and job.state not in ("preparing", "ready", "running"):
            raise IllegalTransition(f"job in {job.state!r} cannot be cancelled")
        elif to_state == "running" and job.state not in ("ready", "preparing", "accepted"):
            raise IllegalTransition(f"job in {job.state!r} cannot execute")
        from_state = job.state
        job.state = to_state
        job.updated_at = self._now()
        self._record(job, from_state=from_state, to_state=to_state, actor=actor, reason=reason)

    def _response(
        self, op: str, job: _JobRecord, request_hash: str, *, replayed: bool = False
    ) -> V3WriteResponse:
        return V3WriteResponse(
            op=op,
            job_id=job.job_id,
            state=job.state,
            generation=job.generation,
            attempt_id=job.attempt_id,
            request_hash=request_hash,
            policy=self._policy(op),
            replayed=replayed,
        )

    # -- typed writes -----------------------------------------------------

    def prepare_job(self, request: V3PrepareRequest) -> V3WriteResponse:
        payload = {"op": "prepare", **request.model_dump()}
        replay = self._idem_begin(f"prepare:{request.project}", request.idempotency_key, payload)
        if replay is not None:
            job = self._get(replay["job_id"])
            return V3WriteResponse(**replay)
        now = self._now()
        job = _JobRecord(
            job_id=str(uuid.uuid4()),
            project=request.project,
            ref=request.ref,
            source_digest=request.source_digest,
            generation=1,
            state="accepted",
            policy_hash=request.policy_hash,
            objective=request.objective,
            authority_ref=request.authority_ref,
            attempt_id=str(uuid.uuid4()),
            attempt_count=1,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.job_id] = job
        self.attachments[job.job_id] = []
        self._record(
            job, from_state="accepted", to_state="accepted",
            actor=request.actor, reason="job accepted",
        )
        response = self._response("prepare", job, canonical_hash(payload))
        self._idem_store(
            f"prepare:{request.project}", request.idempotency_key,
            payload, response.model_dump(),
        )
        return response

    def attach_artifact(self, job_id: str, request: V3AttachRequest) -> V3WriteResponse:
        payload = {"op": "attach", "job_id": job_id, **request.model_dump()}
        replay = self._idem_begin(f"job:{job_id}", request.idempotency_key, payload)
        if replay is not None:
            job = self._get(replay["job_id"])
            _ = job
            return V3WriteResponse(**replay)
        job = self._get(job_id)
        manifest = request.manifest
        digest = str(manifest.get("payload_digest", ""))
        if not digest:
            self._transition(job, "quarantined", actor=request.actor,
                             reason="artifact verification failed: missing payload_digest",
                             expected_generation=request.expected_generation)
            job.quarantine_reason = "missing payload_digest"
            raise FoundryError("artifact manifest requires payload_digest", code="artifact_invalid")
        self._check_generation(job, request.expected_generation)
        if job.state in TERMINAL_STATES:
            raise IllegalTransition(
                f"terminal job {job_id} in {job.state!r} cannot attach artifacts"
            )
        self.attachments[job_id].append(manifest)
        self._record(job, from_state=job.state, to_state=job.state, actor=request.actor,
                     reason=f"artifact attached {digest[:12]}")
        response = self._response("attach", job, canonical_hash(payload))
        self._idem_store(f"job:{job_id}", request.idempotency_key, payload, response.model_dump())
        return response

    def execute_job(self, job_id: str, request: V3ExecuteRequest) -> V3WriteResponse:
        payload = {"op": "execute", "job_id": job_id, **request.model_dump()}
        replay = self._idem_begin(f"job:{job_id}", request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**replay)
        job = self._get(job_id)
        identity = (request.native_identity or "").strip()
        if not identity:
            raise FoundryError("a native identity is required before reporting running",
                               code="missing_native_identity")
        self._check_generation(job, request.expected_generation)
        if job.state in TERMINAL_STATES:
            raise IllegalTransition(f"terminal job {job_id} in {job.state!r} is immutable")
        # The synthetic client advances accepted/preparing directly to running
        # to keep the adapter path simple; intermediate states remain visible
        # in the event log for fidelity with the Foundry machine.
        for intermediate in ("preparing", "ready"):
            if job.state == "accepted" and intermediate == "preparing":
                self._transition(job, "preparing", actor=request.actor,
                                 reason="preparing isolated workspace",
                                 expected_generation=job.generation)
            elif job.state == "preparing" and intermediate == "ready":
                self._transition(job, "ready", actor=request.actor,
                                 reason="workspace ready", expected_generation=job.generation)
        job.native_identity = identity
        job.command_profile = request.command_profile
        job.objective = request.objective
        job.authority_ref = request.authority_ref
        self._transition(job, "running", actor=request.actor,
                         reason=f"executing {request.command_profile}",
                         expected_generation=request.expected_generation)
        response = self._response("execute", job, canonical_hash(payload))
        self._idem_store(f"job:{job_id}", request.idempotency_key, payload, response.model_dump())
        return response

    def job_status(self, job_id: str, after_cursor: int = 0) -> V3StatusResponse:
        job = self._get(job_id)
        events = [e for e in self.events if e["job_id"] == job_id and e["seq"] > after_cursor]
        cursor = events[-1]["seq"] if events else after_cursor
        return V3StatusResponse(job=self._job_dict(job), events=events, cursor=cursor)

    def cancel_job(self, job_id: str, request: V3CancelRequest) -> V3WriteResponse:
        payload = {"op": "cancel", "job_id": job_id, **request.model_dump()}
        replay = self._idem_begin(f"job:{job_id}", request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**replay)
        job = self._get(job_id)
        self._transition(job, "cancel_requested", actor=request.actor,
                         reason=request.reason, expected_generation=request.expected_generation)
        response = self._response("cancel", job, canonical_hash(payload))
        self._idem_store(f"job:{job_id}", request.idempotency_key, payload, response.model_dump())
        return response

    def read_result(self, job_id: str) -> V3ResultResponse:
        job = self._get(job_id)
        if job.state not in TERMINAL_STATES:
            raise JobNotReady(job_id, job.state)
        digests = [str(m.get("payload_digest", "")) for m in self.attachments.get(job_id, [])]
        cursor = max((e["seq"] for e in self.events if e["job_id"] == job_id), default=0)
        return V3ResultResponse(
            job_id=job.job_id,
            state=job.state,
            generation=job.generation,
            attempt_id=job.attempt_id,
            attempt_count=job.attempt_count,
            result=job.result,
            artifact_digests=[d for d in digests if d],
            quarantine_reason=job.quarantine_reason,
            cursor=cursor,
        )

    def quarantine_job(self, job_id: str, request: V3QuarantineRequest) -> V3WriteResponse:
        payload = {"op": "quarantine", "job_id": job_id, **request.model_dump()}
        replay = self._idem_begin(f"job:{job_id}", request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**replay)
        job = self._get(job_id)
        self._transition(job, "quarantined", actor=request.actor,
                         reason=request.reason, expected_generation=request.expected_generation)
        job.quarantine_reason = request.reason
        response = self._response("quarantine", job, canonical_hash(payload))
        self._idem_store(f"job:{job_id}", request.idempotency_key, payload, response.model_dump())
        return response

    # -- test/simulation helper (not a wire operation) --------------------

    def settle_running(self, job_id: str, *, outcome: Literal["succeeded", "failed"] = "succeeded",
                       result: dict[str, Any] | None = None, actor: str = "foundry") -> None:
        """Record a synthetic terminal outcome for a running job (tests only)."""
        job = self._get(job_id)
        if job.state != "running":
            raise IllegalTransition(
                f"only a running job can record a result (job is {job.state!r})"
            )
        from_state = job.state
        job.state = outcome
        job.result = result or {}
        job.updated_at = self._now()
        self._record(job, from_state=from_state, to_state=outcome, actor=actor,
                     reason=f"native unit reported {outcome}")

    # -- bulk reads ---------------------------------------------------------

    def bulk_read(self, resource: str, *, cursor: str | None = None,
                  limit: int = 50, job_id: str | None = None) -> V3PageResponse:
        if resource not in BULK_RESOURCES:
            raise FoundryError(f"unknown bulk resource: {resource}", code="unknown_resource")
        limit = max(1, min(limit, 200))
        offset = 0
        if cursor:
            try:
                offset = max(0, int(cursor))
            except ValueError:
                raise FoundryError(f"invalid cursor: {cursor!r}", code="invalid_cursor") from None
        rows = self._resource_rows(resource, job_id=job_id)
        page = rows[offset : offset + limit]
        next_cursor = str(offset + len(page)) if offset + len(page) < len(rows) else None
        return V3PageResponse(resource=resource, items=page, next_cursor=next_cursor)

    def _resource_rows(self, resource: str, *, job_id: str | None) -> list[dict[str, Any]]:
        if resource == "projects":
            seen: dict[str, dict[str, Any]] = {}
            for job in self.jobs.values():
                entry = seen.setdefault(
                    job.project,
                    {
                        "project": job.project,
                        "execution_mode": "foundry_isolated",
                        "repository": job.project,
                        "ref": job.ref,
                        "readiness": "ready",
                        "required_artifact": None,
                        "last_job": None,
                    },
                )
                entry["last_job"] = job.job_id
            return [seen[k] for k in sorted(seen)]
        if resource == "jobs":
            rows = [self._job_dict(j) for j in self.jobs.values()]
            rows.sort(key=lambda r: r["created_at"])
            return rows
        if resource == "attempts":
            rows = [
                {
                    "attempt_id": j.attempt_id,
                    "job_id": j.job_id,
                    "generation": j.generation,
                    "state": j.state,
                    "actor": "gateway",
                    "source_digest": j.source_digest,
                    "artifact_digests": [
                        str(m.get("payload_digest", ""))
                        for m in self.attachments.get(j.job_id, [])
                    ],
                    "policy_hash": j.policy_hash,
                    "cursor": 0,
                }
                for j in self.jobs.values()
            ]
            if job_id:
                rows = [r for r in rows if r["job_id"] == job_id]
            return rows
        if resource == "activity":
            rows = [
                {
                    "timestamp": e["ts"],
                    "project": self.jobs[e["job_id"]].project if e["job_id"] in self.jobs else "",
                    "job_id": e["job_id"],
                    "attempt_id": e["attempt_id"],
                    "agent": e["actor"],
                    "source": "foundry",
                    "action": f"{e['from_state']}->{e['to_state']}",
                    "outcome": e["to_state"],
                    "correlation_id": f"{e['job_id']}:{e['seq']}",
                }
                for e in self.events
            ]
            if job_id:
                rows = [r for r in rows if r["job_id"] == job_id]
            return rows
        if resource == "artifacts":
            rows = []
            for jid, manifests in self.attachments.items():
                if job_id and jid != job_id:
                    continue
                for manifest in manifests:
                    rows.append({"job_id": jid, **manifest})
            return rows
        if resource == "approvals":
            return list(self.approvals)
        if resource == "health":
            return [
                {
                    "plane": "foundry",
                    "available": True,
                    "build": "synthetic-inmemory",
                    "policy_revision": "v3",
                    "reason": "synthetic adapter; live Foundry deployment not yet bound",
                }
            ]
        if resource == "evidence":
            rows = []
            for jid, job in self.jobs.items():
                if job_id and jid != job_id:
                    continue
                if job.result is not None:
                    rows.append(
                        {
                            "job_id": jid,
                            "name": "result",
                            "digest": canonical_hash(job.result),
                            "reference": f"foundry/v3/jobs/{jid}/result",
                        }
                    )
                for manifest in self.attachments.get(jid, []):
                    rows.append(
                        {
                            "job_id": jid,
                            "name": "artifact",
                            "digest": str(manifest.get("payload_digest", "")),
                            "reference": f"foundry/v3/jobs/{jid}/attachments",
                        }
                    )
            return rows
        raise FoundryError(f"unknown bulk resource: {resource}", code="unknown_resource")

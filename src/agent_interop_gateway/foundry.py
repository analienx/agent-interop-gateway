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
- Artifact payloads are NEVER embedded in JSON and the gateway performs no
  fetch/network/package logic. Attach carries the flat
  ``foundry.artifact/v1`` manifest (exactly the Shiftio/agent-foundry field
  set, no invented schema) plus a typed staged-payload reference and
  verification handoff that a real deployment client stages out-of-band.
  The synthetic in-memory client below only validates schema/governance
  evidence and records the handoff; it never fetches, mounts, or executes.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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

NON_TERMINAL_STATES = JOB_STATES - TERMINAL_STATES

# States in which artifact attachment is legal. Attachments freeze at
# ``execute`` time; ``running``, ``cancel_requested``, and terminal states
# reject attachment so execution evidence always binds the frozen set.
ATTACHABLE_STATES = frozenset({"accepted", "preparing", "ready"})

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

# -- foundry.artifact/v1 manifest constants ---------------------------------
# Mirrors agent-foundry's artifacts module at PR #2 head 31f8240. The gateway
# must not invent another manifest schema: these sets are copied verbatim so
# validation stays compatible with the hardened Foundry contract.

SCHEMA_VERSION = "foundry.artifact/v1"
ALLOWED_KINDS = frozenset({"dir-archive", "oci-image"})

# Named verification profiles. A manifest may reference these instead of
# spelling out argv; the deployment adapter decides what each profile runs.
VERIFICATION_PROFILES = frozenset(
    {
        "sha256-check",
        "digest-check",
        "signature-check",
        "provenance-check",
        "reproducibility-check",
    }
)

# Structured-argv allowlist for offline verification commands. Anything
# capable of egress or shell execution is absent by construction.
ALLOWED_VERIFIER_BINARIES = frozenset(
    {
        "sha256sum",
        "shasum",
        "sha256",
        "cosign",
        "openssl",
        "tar",
        "digest",
    }
)

# Lifecycle-script policy allowlist. Payloads declare how much script
# execution they require; the job-bound policy pins the maximum tolerated.
LIFECYCLE_POLICIES = frozenset(
    {
        "no-scripts",
        "offline-only",
        "hermetic",
        "managed-postinstall",
    }
)

# Exact flat field set of the foundry.artifact/v1 manifest. Unknown fields
# (including smuggled model/account/cost fields) are rejected.
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "producer",
        "source_repo",
        "source_commit",
        "lock_digest",
        "platform",
        "arch",
        "toolchain",
        "payload_digest",
        "payload_bytes",
        "built_at",
        "retention",
        "lifecycle_policy",
        "provenance_ref",
        "verify_commands",
    }
)

# Constraints a job-bound artifact policy may declare. ``source_commit`` is
# always pinned to the job's source digest rather than the policy document.
POLICY_FIELDS = (
    "source_repo",
    "lock_digest",
    "platform",
    "arch",
    "toolchain",
    "lifecycle_policy",
    "provenance_ref",
)

# Keys that would embed (or point at) raw payload bytes inside JSON. Payload
# bytes travel out-of-band via the staged-payload reference only.
INLINE_PAYLOAD_FIELDS = frozenset(
    {
        "payload",
        "payload_b64",
        "payload_base64",
        "payload_bytes_raw",
        "content",
        "content_bytes",
        "data",
        "blob",
        "bytes",
    }
)

_HEX_RE = re.compile(r"^[0-9a-f]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SHELL_METACHARS = (
    ";", "|", "&", "$", "`", "\n", "\r", "<", ">", "(", ")",
    "{", "}", "*", "?", "~", "!",
)
_NETWORK_TOKENS = frozenset(
    {
        "curl", "wget", "ssh", "scp", "ftp", "sftp", "rsync", "git", "npm",
        "pip", "docker", "podman",
    }
)
_NETWORK_SCHEMES = ("http://", "https://", "ftp://", "sftp://", "ssh://")
_NETWORK_FLAGS = ("--url", "--upload-file", "--remote")


def canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def policy_hash_for(normalized_policy: dict[str, Any]) -> str:
    """Bind a normalized artifact policy to its digest.

    Matches the hardened Foundry prepare semantics: the job-bound policy is
    identified by ``canonical_hash({"artifact_policy": policy})``.
    """
    return canonical_hash({"artifact_policy": normalized_policy})


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


# -- manifest / policy validation (mirrors hardened Foundry) ----------------


def _validate_verify_step(step: object) -> list[str] | str:
    """Validate one verification step.

    Returns the normalized structured argv (a token list), or the named
    profile string when the step is a governed profile reference. Raises
    :class:`ArtifactValidationError` on any violation.
    """
    if isinstance(step, str):
        text = step.strip()
        if not text:
            raise ArtifactValidationError("verify_commands entries must be non-empty")
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text) and text in VERIFICATION_PROFILES:
            return text
        if any(meta in text for meta in _SHELL_METACHARS):
            raise ArtifactValidationError(
                f"verify_commands must be a single argv without shell metacharacters: {step!r}"
            )
        try:
            argv = shlex.split(text, posix=True)
        except ValueError as error:
            raise ArtifactValidationError(
                f"verify_commands entry does not parse: {error}"
            ) from error
        if not argv:
            raise ArtifactValidationError("verify_commands entries must be non-empty")
        step = argv
    if isinstance(step, (list, tuple)):
        argv = list(step)
        if not argv or not all(isinstance(t, str) and t.strip() for t in argv):
            raise ArtifactValidationError(
                "verify_commands argv entries must be non-empty token lists"
            )
        if any(
            any(meta in token for meta in (";", "|", "&", "$", "`", "\n", "\r"))
            for token in argv
        ):
            raise ArtifactValidationError(
                f"verify_commands argv must not contain shell metacharacters: {argv!r}"
            )
        binary = argv[0].rsplit("/", 1)[-1]
        if binary not in ALLOWED_VERIFIER_BINARIES:
            raise ArtifactValidationError(
                f"verify_commands binary {binary!r} is not governed by the adapter allowlist"
            )
        lowered = [token.lower() for token in argv]
        if lowered[0] in _NETWORK_TOKENS:
            raise ArtifactValidationError(
                f"verify_commands must not need network access: {argv!r}"
            )
        for token in lowered[1:]:
            first = token.split("=", 1)[0]
            if (
                token in _NETWORK_TOKENS
                or first in _NETWORK_FLAGS
                or token.startswith(_NETWORK_SCHEMES)
            ):
                raise ArtifactValidationError(
                    f"verify_commands must not need network access: {argv!r}"
                )
        return argv
    raise ArtifactValidationError(
        "verify_commands entries must be named profiles or argv token lists"
    )


def validate_verify_commands(commands: object) -> tuple[Any, ...]:
    """Validate verification steps; return normalized argv/profile tuples."""
    if not isinstance(commands, list) or not commands:
        raise ArtifactValidationError("verify_commands must be a non-empty list")
    normalized: list[Any] = []
    for step in commands:
        validated = _validate_verify_step(step)
        normalized.append(tuple(validated) if isinstance(validated, list) else validated)
    return tuple(normalized)


def validate_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a flat foundry.artifact/v1 manifest; return a normalized copy.

    Rejects unknown fields, inline payload bytes, digest-format violations,
    ungoverned verification steps, and anything outside the hardened schema.
    """
    if not isinstance(raw, dict):
        raise ArtifactValidationError("manifest must be a mapping")
    errors: list[str] = []
    unknown = sorted(k for k in raw if k not in MANIFEST_FIELDS)
    if unknown:
        errors.append(f"manifest has unknown fields: {unknown}")
    inline = sorted(INLINE_PAYLOAD_FIELDS.intersection(raw))
    if inline:
        errors.append(
            f"manifest must not embed payload bytes in JSON ({', '.join(inline)}); "
            "stage payload bytes out-of-band and reference them via staged_payload"
        )
    if raw.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    if raw.get("kind") not in ALLOWED_KINDS:
        errors.append(f"kind must be one of {sorted(ALLOWED_KINDS)}")
    for name in (
        "producer",
        "source_repo",
        "platform",
        "arch",
        "toolchain",
        "built_at",
        "retention",
        "provenance_ref",
    ):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name} must be a non-empty string")
    lifecycle = raw.get("lifecycle_policy", "")
    if isinstance(lifecycle, str) and lifecycle.strip() and lifecycle not in LIFECYCLE_POLICIES:
        errors.append(f"lifecycle_policy must be one of {sorted(LIFECYCLE_POLICIES)}")
    source_commit = raw.get("source_commit", "")
    if not isinstance(source_commit, str) or not _COMMIT_RE.match(source_commit):
        errors.append("source_commit must be hex of length 7..64")
    for name in ("lock_digest", "payload_digest"):
        value = raw.get(name, "")
        if not isinstance(value, str) or not _SHA256_RE.match(value):
            errors.append(f"{name} must be a 64-char lowercase hex sha256")
    payload_bytes = raw.get("payload_bytes")
    if (
        not isinstance(payload_bytes, int)
        or isinstance(payload_bytes, bool)
        or payload_bytes <= 0
    ):
        errors.append("payload_bytes must be a positive integer")
    normalized_commands: tuple[Any, ...] = ()
    try:
        normalized_commands = validate_verify_commands(raw.get("verify_commands"))
    except ArtifactValidationError as error:
        errors.append(str(error))
    if errors:
        raise ArtifactValidationError("; ".join(errors))
    normalized = {k: raw[k] for k in MANIFEST_FIELDS if k in raw}
    normalized["verify_commands"] = [
        list(c) if isinstance(c, tuple) else c for c in normalized_commands
    ]
    return normalized


def validate_artifact_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """Validate a job-bound artifact policy document; return a normalized copy.

    Required keys: ``source_repo``, ``lock_digest``, ``platform``, ``arch``,
    ``toolchain``, ``lifecycle_policy``. ``provenance_ref`` is optional but,
    when declared, is enforced.
    """
    if not isinstance(policy, dict):
        raise PolicyError("artifact_policy must be a mapping")
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    for name in ("source_repo", "platform", "arch", "toolchain", "provenance_ref"):
        if name == "provenance_ref" and "provenance_ref" not in policy:
            continue
        value = policy.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"artifact_policy[{name}] must be a non-empty string")
        else:
            normalized[name] = value
    lock = policy.get("lock_digest", "")
    if not isinstance(lock, str) or not _SHA256_RE.match(lock):
        errors.append("artifact_policy[lock_digest] must be a 64-char lowercase hex sha256")
    else:
        normalized["lock_digest"] = lock
    lifecycle = policy.get("lifecycle_policy", "")
    if lifecycle not in LIFECYCLE_POLICIES:
        errors.append(
            f"artifact_policy[lifecycle_policy] must be one of {sorted(LIFECYCLE_POLICIES)}"
        )
    else:
        normalized["lifecycle_policy"] = lifecycle
    unknown = sorted(k for k in policy if k not in set(POLICY_FIELDS))
    if unknown:
        errors.append(f"artifact_policy has unknown fields: {unknown}")
    if errors:
        raise PolicyError("; ".join(errors))
    return normalized


def check_matches_policy(
    manifest: dict[str, Any],
    *,
    source_commit: str,
    policy: dict[str, Any],
    caller_constraints: dict[str, Any] | None = None,
) -> None:
    """Enforce a job-bound artifact policy against a manifest.

    Every field declared in ``policy`` must equal the manifest value, and
    ``manifest[source_commit]`` must equal the job's source digest. Caller
    constraints may narrow but never widen or contradict the stored policy.
    """
    mismatches: list[str] = []
    if manifest.get("source_commit") != source_commit:
        mismatches.append("source_commit does not match the job request")
    caller_constraints = caller_constraints or {}
    for name in POLICY_FIELDS:
        declared = policy.get(name)
        caller_value = caller_constraints.get(name)
        actual = manifest.get(name)
        if declared is not None:
            if actual != declared:
                mismatches.append(f"{name} does not match the job artifact policy")
            if caller_value is not None and caller_value != declared:
                mismatches.append(
                    f"caller constraint {name} contradicts the job artifact policy"
                )
        elif caller_value is not None:
            if actual != caller_value:
                mismatches.append(f"{name} does not match the caller constraint")
        elif name != "provenance_ref":
            mismatches.append(f"no constraint declared for required field {name}")
    if mismatches:
        raise ArtifactVerificationError("; ".join(mismatches))


# -- typed v3 schemas ----------------------------------------------------


class V3PrepareRequest(BaseModel):
    project: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    source_digest: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    policy_hash: str = ""
    artifact_policy: dict[str, Any] | None = None
    objective: str = ""
    authority_ref: str = ""
    actor: str = "gateway"

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _bind_artifact_policy(self) -> V3PrepareRequest:
        if self.artifact_policy is not None:
            normalized = validate_artifact_policy(self.artifact_policy)
            computed = policy_hash_for(normalized)
            if self.policy_hash and self.policy_hash != computed:
                raise ValueError("policy_hash does not match the artifact_policy digest")
            self.artifact_policy = normalized
            self.policy_hash = computed
        return self


class V3StagedPayloadRef(BaseModel):
    """Typed out-of-band payload reference for attach.

    A real deployment client stages payload bytes outside JSON (content
    store, read-only mount source) and hands the gateway only this
    reference. It never carries payload bytes.
    """

    ref: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    size: int = Field(ge=1)

    model_config = {"extra": "forbid"}

    @field_validator("digest")
    @classmethod
    def _digest_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            raise ValueError("staged_payload.digest must be a 64-char lowercase hex sha256")
        return value


class V3VerificationHandoff(BaseModel):
    """Typed verification handoff for attach.

    Declares the governed named verification profile the deployment client
    verified the staged payload under. The gateway never fetches payloads
    or runs verification commands; the deployment adapter governs and
    executes them.
    """

    profile: str = Field(min_length=1)

    model_config = {"extra": "forbid"}

    @field_validator("profile")
    @classmethod
    def _profile_is_governed(cls, value: str) -> str:
        if value not in VERIFICATION_PROFILES:
            raise ValueError(
                f"verification.profile must be one of {sorted(VERIFICATION_PROFILES)}"
            )
        return value


class V3ArtifactConstraints(BaseModel):
    """Optional caller-supplied artifact constraints for attach.

    May narrow but never widen or contradict the job-bound artifact policy
    persisted at prepare time.
    """

    source_repo: str | None = None
    lock_digest: str | None = None
    platform: str | None = None
    arch: str | None = None
    toolchain: str | None = None
    lifecycle_policy: str | None = None
    provenance_ref: str | None = None

    model_config = {"extra": "forbid"}


class V3AttachRequest(BaseModel):
    manifest: dict[str, Any]
    staged_payload: V3StagedPayloadRef | None = None
    verification: V3VerificationHandoff | None = None
    constraints: V3ArtifactConstraints | None = None
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
    launch_token: str | None = None


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
    frozen_artifact_digests: list[str] = Field(default_factory=list)
    source_digest: str = ""
    policy_hash: str = ""
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


class ArtifactValidationError(FoundryError):
    """A manifest failed schema, digest, or verification-step checks."""

    def __init__(self, message: str):
        super().__init__(message, code="artifact_invalid")


class ArtifactVerificationError(FoundryError):
    """A manifest was well-formed but payload/policy verification failed."""

    def __init__(self, message: str):
        super().__init__(message, code="artifact_verification")


class PolicyError(FoundryError, ValueError):
    """A job-bound artifact policy document was invalid.

    Subclasses :class:`ValueError` so Pydantic request validation surfaces
    policy problems as HTTP 422 instead of an unhandled error.
    """

    def __init__(self, message: str):
        super().__init__(message, code="policy_invalid")


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


_ERROR_BY_CODE: dict[str, type[FoundryError]] = {
    "artifact_invalid": ArtifactValidationError,
    "artifact_verification": ArtifactVerificationError,
    "policy_invalid": PolicyError,
    "idempotency_conflict": IdempotencyConflict,
    "stale_generation": StaleGeneration,
    "illegal_transition": IllegalTransition,
    "job_not_ready": JobNotReady,
}


def raise_stored_failure(stored: dict[str, Any]) -> None:
    """Re-raise a durable failure recorded under an idempotency key."""
    code = str(stored.get("code", "foundry_error"))
    message = str(stored.get("error", "durable request failed"))
    error_type = _ERROR_BY_CODE.get(code, FoundryError)
    if error_type is JobNotReady:
        raise JobNotReady(str(stored.get("job_id", "?")), str(stored.get("state", "?")))
    if error_type is FoundryError:
        raise FoundryError(message, code=code)
    raise error_type(message)


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
class _AttachmentEvidence:
    artifact_digest: str
    manifest: dict[str, Any]
    mount_point: str
    verified_profile: str
    staged_ref: str
    actor: str
    attached_at: str
    generation: int


@dataclass
class _JobRecord:
    job_id: str
    project: str
    ref: str
    source_digest: str
    generation: int
    state: str
    policy_hash: str
    artifact_policy: dict[str, Any]
    frozen_artifact_digests: list[str]
    launch_token: str | None
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
    generation fencing checked before any mutation, monotonic event cursors
    carrying source/artifact/policy evidence, terminal immutability,
    attachment freeze before execution, job-bound artifact policy matching,
    governed verification profiles, quarantine from any non-terminal state)
    without importing it, so the gateway stays a decoupled transport
    adapter.

    It never fetches payload bytes, mounts filesystems, or runs
    verification commands: attach validates the flat foundry.artifact/v1
    manifest and the staged-payload/verification handoff as evidence only.
    """

    jobs: dict[str, _JobRecord] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    attachments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    attachment_evidence: dict[str, list[_AttachmentEvidence]] = field(default_factory=dict)
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

    def _attached_digests(self, job_id: str) -> list[str]:
        return [
            str(m.get("payload_digest", ""))
            for m in self.attachments.get(job_id, [])
            if m.get("payload_digest")
        ]

    def _record(
        self,
        job: _JobRecord,
        *,
        from_state: str,
        to_state: str,
        actor: str,
        reason: str,
        artifact_digests: list[str] | None = None,
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
            "source_digest": job.source_digest,
            "artifact_digests": (
                list(artifact_digests)
                if artifact_digests is not None
                else self._attached_digests(job.job_id)
            ),
            "policy_hash": job.policy_hash,
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
            "artifact_policy": dict(job.artifact_policy),
            "frozen_artifact_digests": list(job.frozen_artifact_digests),
            "launch_token": job.launch_token,
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
            if stored_response.get("failed"):
                raise_stored_failure(stored_response)
            return {**stored_response, "replayed": True}
        return None

    def _idem_store(
        self, scope: str, key: str, payload: dict[str, Any], response: dict[str, Any]
    ) -> None:
        self.idempotency[(scope, key)] = (canonical_hash(payload), response)

    def _idem_store_failure(
        self,
        scope: str,
        key: str,
        payload: dict[str, Any],
        job: _JobRecord | None,
        error: FoundryError,
    ) -> None:
        stored: dict[str, Any] = {
            "failed": True,
            "code": error.code,
            "error": str(error),
        }
        if job is not None:
            stored.update(
                {
                    "job_id": job.job_id,
                    "state": job.state,
                    "generation": job.generation,
                    "attempt_id": job.attempt_id,
                }
            )
        self._idem_store(scope, key, payload, stored)

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

    def _quarantine(
        self, job: _JobRecord, *, actor: str, reason: str, expected_generation: int
    ) -> None:
        self._check_generation(job, expected_generation)
        if job.state in TERMINAL_STATES:
            raise IllegalTransition(f"terminal job {job.job_id} in {job.state!r} is immutable")
        if job.state not in NON_TERMINAL_STATES:
            raise IllegalTransition(f"job in {job.state!r} cannot be quarantined")
        from_state = job.state
        job.state = "quarantined"
        job.quarantine_reason = reason
        job.launch_token = None
        job.updated_at = self._now()
        self._record(
            job, from_state=from_state, to_state="quarantined", actor=actor, reason=reason
        )

    def _transition(
        self, job: _JobRecord, to_state: str, *, actor: str, reason: str, expected_generation: int
    ) -> None:
        self._check_generation(job, expected_generation)
        if job.state in TERMINAL_STATES:
            raise IllegalTransition(f"terminal job {job.job_id} in {job.state!r} is immutable")
        if to_state == "quarantined":
            if job.state not in NON_TERMINAL_STATES:
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
            launch_token=job.launch_token,
        )

    @staticmethod
    def _launch_token(*, scope: str, key: str) -> str:
        """Derive the durable idempotent launch token for an execute call."""
        return canonical_hash({"launch_reservation": True, "scope": scope, "key": key})

    # -- typed writes -----------------------------------------------------

    def prepare_job(self, request: V3PrepareRequest) -> V3WriteResponse:
        payload = {"op": "prepare", **request.model_dump()}
        replay = self._idem_begin(f"prepare:{request.project}", request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**{k: v for k, v in replay.items() if k != "failed"})
        policy = dict(request.artifact_policy or {})
        if policy:
            policy = validate_artifact_policy(policy)
            computed = policy_hash_for(policy)
            if request.policy_hash and request.policy_hash != computed:
                raise PolicyError("policy_hash does not match the artifact_policy digest")
            policy_hash = computed
        else:
            policy_hash = request.policy_hash
        now = self._now()
        job = _JobRecord(
            job_id=str(uuid.uuid4()),
            project=request.project,
            ref=request.ref,
            source_digest=request.source_digest,
            generation=1,
            state="accepted",
            policy_hash=policy_hash,
            artifact_policy=policy,
            frozen_artifact_digests=[],
            launch_token=None,
            objective=request.objective,
            authority_ref=request.authority_ref,
            attempt_id=str(uuid.uuid4()),
            attempt_count=1,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job.job_id] = job
        self.attachments[job.job_id] = []
        self.attachment_evidence[job.job_id] = []
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
        scope = f"job:{job_id}"
        replay = self._idem_begin(scope, request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**{k: v for k, v in replay.items() if k != "failed"})
        job = self._get(job_id)

        # Generation fencing first: a stale attach must not mutate, quarantine,
        # or record events against the current generation.
        try:
            self._check_generation(job, request.expected_generation)
        except StaleGeneration as error:
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)
            raise

        # Attachment window: frozen at execute; running, cancel_requested,
        # and terminal states reject without quarantine (caller error, not
        # payload evidence).
        if job.state not in ATTACHABLE_STATES:
            error = IllegalTransition(
                f"attachments freeze before execution; job is {job.state!r}"
            )
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)
            raise error

        def quarantine_for_failure(error: FoundryError) -> None:
            self._quarantine(
                job, actor=request.actor, reason=f"artifact verification failed: {error}",
                expected_generation=request.expected_generation,
            )
            job.quarantine_reason = str(error)
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)

        try:
            manifest = validate_manifest(request.manifest)
        except FoundryError as error:
            quarantine_for_failure(error)
            raise

        try:
            if request.staged_payload is not None:
                staged = request.staged_payload
                if staged.digest != manifest["payload_digest"]:
                    raise ArtifactVerificationError(
                        "staged_payload.digest does not match the manifest payload_digest"
                    )
                if staged.size != manifest["payload_bytes"]:
                    raise ArtifactVerificationError(
                        "staged_payload.size does not match the manifest payload_bytes"
                    )
            caller_constraints = (
                {
                    k: v
                    for k, v in request.constraints.model_dump().items()
                    if v is not None
                }
                if request.constraints is not None
                else {}
            )
            check_matches_policy(
                manifest,
                source_commit=job.source_digest,
                policy=job.artifact_policy,
                caller_constraints=caller_constraints,
            )
        except FoundryError as error:
            quarantine_for_failure(error)
            raise

        digest = str(manifest["payload_digest"])
        now = self._now()
        if digest not in {str(m.get("payload_digest")) for m in self.attachments[job_id]}:
            self.attachments[job_id].append(manifest)
        verified_profile = request.verification.profile if request.verification else ""
        if not verified_profile:
            named = [
                c for c in manifest.get("verify_commands", []) if isinstance(c, str)
            ]
            verified_profile = named[0] if named else ""
        evidence = _AttachmentEvidence(
            artifact_digest=digest,
            manifest=dict(manifest),
            mount_point=f"staged:{digest[:12]}",
            verified_profile=verified_profile,
            staged_ref=request.staged_payload.ref if request.staged_payload else "",
            actor=request.actor,
            attached_at=now,
            generation=job.generation,
        )
        self.attachment_evidence[job_id].append(evidence)
        self._record(job, from_state=job.state, to_state=job.state, actor=request.actor,
                     reason=f"artifact attached {digest[:12]}")
        response = self._response("attach", job, canonical_hash(payload))
        self._idem_store(scope, request.idempotency_key, payload, response.model_dump())
        return response

    def execute_job(self, job_id: str, request: V3ExecuteRequest) -> V3WriteResponse:
        # Like the hardened Foundry store, the idempotency payload excludes
        # the native identity (and actor): a same-key replay reuses the
        # committed identity instead of relaunching or conflicting.
        payload = {
            "op": "execute",
            "job_id": job_id,
            "command_profile": request.command_profile,
            "objective": request.objective,
            "authority_ref": request.authority_ref,
            "generation": request.expected_generation,
        }
        full_hash_payload = {"op": "execute", "job_id": job_id, **request.model_dump()}
        scope = f"job:{job_id}"
        replay = self._idem_begin(scope, request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**{k: v for k, v in replay.items() if k != "failed"})
        job = self._get(job_id)
        identity = (request.native_identity or "").strip()
        if not identity:
            error = FoundryError(
                "a native identity is required before reporting running",
                code="missing_native_identity",
            )
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)
            raise error
        # Generation fencing before any mutation: a stale execute must not
        # advance states, reserve launches, or append events.
        try:
            self._check_generation(job, request.expected_generation)
        except StaleGeneration as error:
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)
            raise
        if job.state not in ("accepted", "preparing", "ready"):
            error = IllegalTransition(
                f"only a ready job can execute (job is {job.state!r})"
            )
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)
            raise error
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
        # Two-phase launch reservation, mirrored synthetically: derive the
        # durable idempotent token from the idempotency scope/key, record the
        # reservation, then confirm with the native identity in the running
        # transition (atomic here because no external side effect is
        # possible). Retries with the same key reuse the token via the
        # idempotency record above and never relaunch.
        launch_token = self._launch_token(scope=scope, key=request.idempotency_key)
        if job.launch_token is not None and job.launch_token != launch_token:
            error = IdempotencyConflict("a launch reservation is already pending for this job")
            self._idem_store_failure(scope, request.idempotency_key, payload, job, error)
            raise error
        job.launch_token = launch_token
        self._record(job, from_state="ready", to_state="ready", actor=request.actor,
                     reason=f"launch reserved {launch_token[:12]}")
        frozen = self._attached_digests(job_id)
        job.native_identity = identity
        job.command_profile = request.command_profile
        job.objective = request.objective
        job.authority_ref = request.authority_ref
        job.frozen_artifact_digests = frozen
        self._transition(job, "running", actor=request.actor,
                         reason=f"executing {request.command_profile}",
                         expected_generation=request.expected_generation)
        job.launch_token = None
        # Bind the frozen digest set to the running evidence explicitly: the
        # transition above recorded current attachments, which equal frozen.
        response = self._response("execute", job, canonical_hash(full_hash_payload))
        self._idem_store(scope, request.idempotency_key, payload, response.model_dump())
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
            return V3WriteResponse(**{k: v for k, v in replay.items() if k != "failed"})
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
        frozen = list(job.frozen_artifact_digests) or self._attached_digests(job_id)
        cursor = max((e["seq"] for e in self.events if e["job_id"] == job_id), default=0)
        return V3ResultResponse(
            job_id=job.job_id,
            state=job.state,
            generation=job.generation,
            attempt_id=job.attempt_id,
            attempt_count=job.attempt_count,
            result=job.result,
            artifact_digests=list(frozen),
            frozen_artifact_digests=list(frozen),
            source_digest=job.source_digest,
            policy_hash=job.policy_hash,
            quarantine_reason=job.quarantine_reason,
            cursor=cursor,
        )

    def quarantine_job(self, job_id: str, request: V3QuarantineRequest) -> V3WriteResponse:
        payload = {"op": "quarantine", "job_id": job_id, **request.model_dump()}
        replay = self._idem_begin(f"job:{job_id}", request.idempotency_key, payload)
        if replay is not None:
            return V3WriteResponse(**{k: v for k, v in replay.items() if k != "failed"})
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
        frozen = list(job.frozen_artifact_digests) or self._attached_digests(job_id)
        job.frozen_artifact_digests = frozen
        self._record(job, from_state=from_state, to_state=outcome, actor=actor,
                     reason=f"native unit reported {outcome}", artifact_digests=frozen)

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
                    "frozen_artifact_digests": list(j.frozen_artifact_digests),
                    "policy_hash": j.policy_hash,
                    "native_identity": j.native_identity,
                    "launch_token": j.launch_token,
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
                    "source_digest": e.get("source_digest", ""),
                    "artifact_digests": list(e.get("artifact_digests", [])),
                    "policy_hash": e.get("policy_hash", ""),
                    "correlation_id": f"{e['job_id']}:{e['seq']}",
                }
                for e in self.events
            ]
            if job_id:
                rows = [r for r in rows if r["job_id"] == job_id]
            return rows
        if resource == "artifacts":
            rows = []
            for jid, evidence_list in self.attachment_evidence.items():
                if job_id and jid != job_id:
                    continue
                for evidence in evidence_list:
                    rows.append(
                        {
                            "job_id": jid,
                            "manifest": dict(evidence.manifest),
                            "artifact_digest": evidence.artifact_digest,
                            "mount_point": evidence.mount_point,
                            "verified_profile": evidence.verified_profile,
                            "staged_ref": evidence.staged_ref,
                            "actor": evidence.actor,
                            "attached_at": evidence.attached_at,
                            "generation": evidence.generation,
                        }
                    )
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
                for digest in self._attached_digests(jid):
                    rows.append(
                        {
                            "job_id": jid,
                            "name": "artifact",
                            "digest": digest,
                            "reference": f"foundry/v3/jobs/{jid}/attachments",
                        }
                    )
            return rows
        raise FoundryError(f"unknown bulk resource: {resource}", code="unknown_resource")

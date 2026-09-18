"""Deterministic test-only attachment-receipt cryptography.

This module is the ONLY place a synthetic receipt-issuing key exists: the
gateway module holds no issuer signing secrets and never signs receipts.
Synthetic tests inject the matching ``HmacReceiptVerifier`` (built here)
into ``InMemoryFoundryClient`` so the authenticated trust boundary is
exercised end to end with deterministic keys, clocks, and timestamps.

Timestamps are fixed at import time (issued now, expires +6h) so identical
inputs produce byte-identical receipts, which keeps idempotency payloads
stable across repeated calls within a test session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agent_interop_gateway.foundry import (
    RECEIPT_STATEMENT_TYPE,
    HmacReceiptIssuer,
    HmacReceiptVerifier,
    attachment_replay_domain,
    plan_hash_for_plan,
    step_commitment,
)

# Deterministic synthetic issuer/key pair (tests only; never production).
TEST_ISSUER = "synthetic-deployment"
TEST_ISSUER_KEY = b"aigw-synthetic-test-receipt-key"

# Fixed issuance window for the whole test session (deterministic bodies).
_ISSUED = datetime.now(UTC).replace(microsecond=0)
TEST_ISSUED_AT = _ISSUED.strftime("%Y-%m-%dT%H:%M:%SZ")
TEST_EXPIRES_AT = (_ISSUED + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

DEFAULT_MOUNT_HANDLE = "ro-mount:demo:staged-1"
DEFAULT_VERIFIER = "synthetic-adapter/sha256-check"

_SIGNER = HmacReceiptIssuer(issuer=TEST_ISSUER, key=TEST_ISSUER_KEY)


def test_verifier() -> HmacReceiptVerifier:
    """The injected trust store matching the synthetic test issuer."""
    return HmacReceiptVerifier(issuers={TEST_ISSUER: TEST_ISSUER_KEY})


def test_signer() -> HmacReceiptIssuer:
    """The deployment-side signer paired with :func:`test_verifier`."""
    return _SIGNER


def statement_of(receipt: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the canonical statement a receipt's MAC covers.

    This is exactly what the gateway-side verifier computes: the receipt
    fields (minus authentication material) plus the pinned statement type.
    Deployment directories store statements in this shape.
    """
    data = {k: v for k, v in receipt.items() if k not in ("mac", "receipt_ref")}
    data["statement"] = RECEIPT_STATEMENT_TYPE
    data["steps"] = [dict(step) for step in data["steps"]]
    return data


def normalized_plan(plan: list[Any]) -> list[Any]:
    """Normalize a verify_commands plan the way the gateway does."""
    return [list(c) if isinstance(c, (list, tuple)) else c for c in plan]


def plan_commitments(plan: list[Any]) -> list[dict[str, Any]]:
    """Ordered per-step results for a plan (authenticated by the issuer)."""
    return [
        {"index": i, "step": step, "evidence_digest": step_commitment(i, step)}
        for i, step in enumerate(normalized_plan(plan))
    ]


def signed_receipt(
    manifest: dict[str, Any],
    *,
    job_id: str,
    generation: int = 1,
    staged_ref: str = "cas:staged-1",
    mount_handle: str = DEFAULT_MOUNT_HANDLE,
    verifier: str = DEFAULT_VERIFIER,
    plan: list[Any] | None = None,
    statement_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sign an attachment receipt the way a trusted deployment adapter would.

    The statement binds job_id, generation, artifact digest, staged ref,
    mount handle, verifier identity, the complete normalized plan hash, the
    ordered step results, and the issuance/expiry/replay-domain triple.
    ``statement_overrides`` lets tests sign deliberately wrong statements
    (mismatched steps, partial coverage, wrong plan hash) so the gateway's
    binding checks — not MAC failures — reject them. ``plan`` overrides the
    plan the receipt covers (used for signed-but-mismatched coverage).
    """
    effective_plan = plan
    if effective_plan is None:
        effective_plan = manifest.get("verify_commands", ["sha256-check"])
    statement: dict[str, Any] = {
        "job_id": job_id,
        "generation": generation,
        "artifact_digest": manifest["payload_digest"],
        "staged_ref": staged_ref,
        "mount_handle": mount_handle,
        "verifier": verifier,
        "plan_hash": plan_hash_for_plan(normalized_plan(effective_plan)),
        "steps": plan_commitments(effective_plan),
        "issued_at": TEST_ISSUED_AT,
        "expires_at": TEST_EXPIRES_AT,
        "replay_domain": attachment_replay_domain(
            job_id, generation, manifest["payload_digest"]
        ),
    }
    if statement_overrides:
        statement.update(statement_overrides)
    return _SIGNER.issue_statement(statement)

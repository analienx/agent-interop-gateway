"""Contract-compatibility regression tests: gateway vs hardened Foundry PR #2.

Pins the gateway to the agent-foundry contract at head 31f8240 and the
Shiftio foundry.artifact/v1 manifest:

- typed prepare carries the job-bound artifact_policy with policy-hash
  digest semantics;
- attach carries the flat foundry.artifact/v1 manifest plus a typed
  staged-payload reference and one typed deployment attachment receipt,
  never payload bytes;
- events/results mirror source_digest, artifact_digests, policy_hash, and
  frozen attachment evidence;
- launch reservation/native-identity semantics and stable error mapping;
- the synthetic client stays honest: generation fencing before mutation,
  attachment freeze, policy matching, receipt-verified plans (named-only,
  mixed, structured-argv-only) with per-step commitments, and
  no model/account/cost fields.

All Foundry state here is synthetic and in-process via
InMemoryFoundryClient. No live deployment, network egress, model routing,
or host mutation is involved.
"""

import hashlib

from fastapi.testclient import TestClient

from agent_interop_gateway.api import create_app
from agent_interop_gateway.config import ExecutorConfig, GatewayConfig
from agent_interop_gateway.foundry import (
    InMemoryFoundryClient,
    build_attachment_receipt,
    canonical_hash,
    plan_hash_for_plan,
    policy_hash_for,
    step_commitment,
)

SOURCE = "a" * 40
LOCK = "b" * 64
POLICY = {
    "source_repo": "example/synthetic",
    "lock_digest": LOCK,
    "platform": "linux",
    "arch": "x86_64",
    "toolchain": "node-22",
    "lifecycle_policy": "no-scripts",
    "provenance_ref": "synthetic-provenance",
}


def _client(token: str = "secret", foundry: InMemoryFoundryClient | None = None) -> TestClient:
    config = GatewayConfig(
        token=token,
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})],
    )
    app = create_app(config, foundry=foundry or InMemoryFoundryClient())
    return TestClient(app)


def _auth(token: str = "secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_manifest(
    payload: bytes = b"compat-payload",
    source: str = SOURCE,
    lock: str = LOCK,
    **overrides,
):
    manifest = {
        "schema_version": "foundry.artifact/v1",
        "kind": "dir-archive",
        "producer": "synthetic-ci",
        "source_repo": "example/synthetic",
        "source_commit": source,
        "lock_digest": lock,
        "platform": "linux",
        "arch": "x86_64",
        "toolchain": "node-22",
        "payload_digest": hashlib.sha256(payload).hexdigest(),
        "payload_bytes": len(payload),
        "built_at": "2026-09-17T00:00:00+00:00",
        "retention": "test-only",
        "lifecycle_policy": "no-scripts",
        "provenance_ref": "synthetic-provenance",
        "verify_commands": ["sha256-check"],
    }
    manifest.update(overrides)
    return manifest, payload


def staged_handoff(manifest: dict, payload: bytes, ref: str = "cas:staged-1") -> dict:
    return {
        "ref": ref,
        "digest": manifest["payload_digest"],
        "size": len(payload),
    }


DEFAULT_MOUNT_HANDLE = "ro-mount:demo:staged-1"
DEFAULT_VERIFIER = "synthetic-adapter/sha256-check"


def make_receipt(
    manifest: dict,
    *,
    staged_ref: str = "cas:staged-1",
    mount_handle: str = DEFAULT_MOUNT_HANDLE,
    verifier: str = DEFAULT_VERIFIER,
) -> dict:
    """Build a typed attachment receipt the way a deployment adapter would."""
    return build_attachment_receipt(
        manifest,
        staged_ref=staged_ref,
        mount_handle=mount_handle,
        verifier=verifier,
    )


def shape_only_receipt() -> dict:
    """Syntactically valid receipt for manifests whose plan cannot be built."""
    return {
        "artifact_digest": "0" * 64,
        "staged_ref": "cas:staged-1",
        "mount_handle": DEFAULT_MOUNT_HANDLE,
        "verifier": DEFAULT_VERIFIER,
        "plan_hash": "0" * 64,
        "steps": [
            {"index": 0, "step": "sha256-check", "evidence_digest": "1" * 64},
        ],
    }


def prepare_job(client: TestClient, key: str, policy: dict | None = POLICY) -> dict:
    body: dict = {
        "project": "demo",
        "ref": "main",
        "source_digest": SOURCE,
        "idempotency_key": key,
        "objective": "compat check",
        "authority_ref": "compat",
    }
    if policy is not None:
        body["artifact_policy"] = policy
    resp = client.post("/v3/jobs:prepare", headers=_auth(), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def attach_job(
    client: TestClient,
    job_id: str,
    manifest: dict,
    payload: bytes,
    key: str,
    generation: int = 1,
    with_staged: bool = True,
    receipt: dict | str | None = "default",
    staged_ref: str = "cas:staged-1",
    mount_handle: str = DEFAULT_MOUNT_HANDLE,
    verifier: str = DEFAULT_VERIFIER,
    constraints: dict | None = None,
):
    body: dict = {
        "manifest": manifest,
        "expected_generation": generation,
        "idempotency_key": key,
    }
    if receipt == "default":
        body["receipt"] = make_receipt(
            manifest,
            staged_ref=staged_ref,
            mount_handle=mount_handle,
            verifier=verifier,
        )
    elif receipt is not None:
        body["receipt"] = receipt
    if with_staged:
        body["staged_payload"] = staged_handoff(manifest, payload, ref=staged_ref)
    if constraints is not None:
        body["constraints"] = constraints
    return client.post(f"/v3/jobs/{job_id}:attach", headers=_auth(), json=body)


def ready_via_synthetic_path(foundry: InMemoryFoundryClient, job_id: str) -> None:
    """Advance a job through the explicit synthetic readiness transition."""
    foundry.mark_ready_for_test(job_id)


def execute_job(client: TestClient, job_id: str, key: str, identity: str = "native-1"):
    return client.post(
        f"/v3/jobs/{job_id}:execute",
        headers=_auth(),
        json={
            "command_profile": "offline-test",
            "objective": "compat check",
            "authority_ref": "compat",
            "expected_generation": 1,
            "idempotency_key": key,
            "native_identity": identity,
        },
    )


# -- prepare: job-bound artifact policy ------------------------------------


def test_prepare_persists_policy_and_binds_hash():
    with _client() as client:
        body = prepare_job(client, "prep-policy")
        assert body["state"] == "accepted"
        status = client.get(f"/v3/jobs/{body['job_id']}/status", headers=_auth())
        assert status.status_code == 200
        job = status.json()["job"]
        assert job["artifact_policy"]["arch"] == "x86_64"
        assert job["policy_hash"] == policy_hash_for(POLICY)
        assert job["frozen_artifact_digests"] == []
        assert job["launch_token"] is None


def test_prepare_rejects_policy_hash_mismatch():
    with _client() as client:
        resp = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": SOURCE,
                "idempotency_key": "prep-mismatch",
                "policy_hash": "0" * 64,
                "artifact_policy": POLICY,
            },
        )
        assert resp.status_code == 422, resp.text


def test_prepare_rejects_invalid_policy():
    with _client() as client:
        bad_lifecycle = dict(POLICY, lifecycle_policy="curl-pipe")
        resp = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": SOURCE,
                "idempotency_key": "prep-badpol",
                "artifact_policy": bad_lifecycle,
            },
        )
        assert resp.status_code == 422, resp.text

        smuggled = dict(POLICY, model="smuggled")
        resp2 = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": SOURCE,
                "idempotency_key": "prep-smug",
                "artifact_policy": smuggled,
            },
        )
        assert resp2.status_code == 422, resp2.text


# -- attach: manifest + staged payload + deployment receipt ------------------


def test_attach_receipt_records_typed_evidence():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-handoff")
        job_id = prep["job_id"]
        manifest, payload = make_manifest()
        resp = attach_job(client, job_id, manifest, payload, "attach-1")
        assert resp.status_code == 200, resp.text

        rows = client.get(
            f"/v3/artifacts?job_id={job_id}", headers=_auth()
        ).json()["items"]
        assert len(rows) == 1
        row = rows[0]
        assert row["manifest"]["schema_version"] == "foundry.artifact/v1"
        assert row["artifact_digest"] == manifest["payload_digest"]
        # Evidence is recorded verbatim from the deployment receipt: the
        # mount handle is a separate deployment-owned value, never the CAS
        # staged ref and never synthesized.
        assert row["mount_handle"] == DEFAULT_MOUNT_HANDLE
        assert row["mount_handle"] != "cas:staged-1"
        assert row["verifier"] == DEFAULT_VERIFIER
        assert row["plan_hash"] == plan_hash_for_plan(
            [list(c) if isinstance(c, list) else c for c in manifest["verify_commands"]]
        )
        assert row["steps"] == make_receipt(manifest)["steps"]
        assert row["staged_ref"] == "cas:staged-1"
        assert row["generation"] == 1


def test_attach_requires_both_staged_payload_and_receipt():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-nohandoff")
        manifest, payload = make_manifest()
        # Missing staged_payload.
        missing_staged = client.post(
            f"/v3/jobs/{prep['job_id']}:attach",
            headers=_auth(),
            json={
                "manifest": manifest,
                "receipt": make_receipt(manifest),
                "expected_generation": 1,
                "idempotency_key": "attach-no-staged",
            },
        )
        assert missing_staged.status_code == 422, missing_staged.text
        # Missing receipt.
        missing_receipt = client.post(
            f"/v3/jobs/{prep['job_id']}:attach",
            headers=_auth(),
            json={
                "manifest": manifest,
                "staged_payload": staged_handoff(manifest, payload),
                "expected_generation": 1,
                "idempotency_key": "attach-no-receipt",
            },
        )
        assert missing_receipt.status_code == 422, missing_receipt.text
        # Receipt without verified-step evidence.
        receipt_no_steps = make_receipt(manifest)
        receipt_no_steps.pop("steps")
        missing_steps = client.post(
            f"/v3/jobs/{prep['job_id']}:attach",
            headers=_auth(),
            json={
                "manifest": manifest,
                "staged_payload": staged_handoff(manifest, payload),
                "receipt": receipt_no_steps,
                "expected_generation": 1,
                "idempotency_key": "attach-no-steps",
            },
        )
        assert missing_steps.status_code == 422, missing_steps.text
        # Schema rejections must not quarantine or record evidence.
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "accepted"
        assert status["job"]["quarantine_reason"] is None
        rows = client.get(
            f"/v3/artifacts?job_id={prep['job_id']}", headers=_auth()
        ).json()["items"]
        assert rows == []


def test_attach_rejects_remote_path_and_shell_refs():
    with _client() as client:
        for key, bad_ref in (
            ("remote-url", "https://example.invalid/cas/1"),
            ("remote-path", "/mnt/staging/artifact-1"),
            ("remote-shell", "cas:staged-1; rm -rf /"),
            ("remote-dotdot", "cas:../escape"),
        ):
            prep = prepare_job(client, f"prep-{key}")
            manifest, payload = make_manifest()
            resp = attach_job(
                client, prep["job_id"], manifest, payload, f"attach-{key}",
                staged_ref=bad_ref,
            )
            assert resp.status_code == 422, (key, resp.text)
            status = client.get(
                f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
            ).json()
            assert status["job"]["state"] == "accepted", key


def test_attach_mismatched_step_evidence_quarantines():
    with _client() as client:
        prep = prepare_job(client, "prep-profmm")
        manifest, payload = make_manifest()  # declares sha256-check
        forged = make_receipt(manifest)
        forged["steps"] = [
            {
                "index": 0,
                "step": "digest-check",
                "evidence_digest": step_commitment(0, "digest-check"),
            }
        ]
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-profmm",
            receipt=forged,
        )
        assert resp.status_code == 422, resp.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"
        assert "mismatched" in (status["job"]["quarantine_reason"] or "")


def test_attach_rejects_inline_payload_bytes_and_quarantines():
    with _client() as client:
        prep = prepare_job(client, "prep-inline")
        manifest, payload = make_manifest()
        manifest["payload"] = "aGVsbG8="
        resp = attach_job(client, prep["job_id"], manifest, payload, "attach-inline")
        assert resp.status_code == 422, resp.text
        status = client.get(f"/v3/jobs/{prep['job_id']}/status", headers=_auth()).json()
        assert status["job"]["state"] == "quarantined"


def test_attach_staged_mismatch_quarantines_and_replay_is_stable():
    with _client() as client:
        prep = prepare_job(client, "prep-staged")
        manifest, payload = make_manifest()
        body = {
            "manifest": manifest,
            "staged_payload": {
                "ref": "cas:staged-1",
                "digest": "c" * 64,
                "size": len(payload),
            },
            "receipt": make_receipt(manifest),
            "expected_generation": 1,
            "idempotency_key": "attach-staged",
        }
        first = client.post(
            f"/v3/jobs/{prep['job_id']}:attach", headers=_auth(), json=body
        )
        assert first.status_code == 422, first.text
        # Durable failure replay: same key returns the same failure, no KeyError.
        second = client.post(
            f"/v3/jobs/{prep['job_id']}:attach", headers=_auth(), json=body
        )
        assert second.status_code == 422, second.text
        status = client.get(f"/v3/jobs/{prep['job_id']}/status", headers=_auth()).json()
        assert status["job"]["state"] == "quarantined"


def test_attach_policy_mismatch_quarantines():
    with _client() as client:
        prep = prepare_job(client, "prep-polmm")
        manifest, payload = make_manifest(arch="aarch64")
        resp = attach_job(client, prep["job_id"], manifest, payload, "attach-polmm")
        assert resp.status_code == 422, resp.text
        status = client.get(f"/v3/jobs/{prep['job_id']}/status", headers=_auth()).json()
        assert status["job"]["state"] == "quarantined"


def test_attach_caller_constraint_cannot_contradict_policy():
    with _client() as client:
        prep = prepare_job(client, "prep-contra")
        manifest, payload = make_manifest()
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-contra",
            constraints={"platform": "windows"},
        )
        assert resp.status_code == 422, resp.text


def test_attach_source_pin_mismatch_quarantines():
    with _client() as client:
        prep = prepare_job(client, "prep-srcpin")
        manifest, payload = make_manifest(source="c" * 40)
        resp = attach_job(client, prep["job_id"], manifest, payload, "attach-srcpin")
        assert resp.status_code == 422, resp.text
        status = client.get(f"/v3/jobs/{prep['job_id']}/status", headers=_auth()).json()
        assert status["job"]["state"] == "quarantined"


def test_attach_unknown_and_model_fields_rejected():
    with _client() as client:
        prep = prepare_job(client, "prep-unknown")
        manifest, payload = make_manifest(model="smuggled")
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-unknown",
        )
        assert resp.status_code == 422, resp.text


# -- fencing and freeze ------------------------------------------------------


def test_stale_execute_rejects_without_mutation():
    with _client() as client:
        prep = prepare_job(client, "prep-stale-exe")
        before = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        resp = client.post(
            f"/v3/jobs/{prep['job_id']}:execute",
            headers=_auth(),
            json={
                "command_profile": "offline-test",
                "objective": "compat check",
                "authority_ref": "compat",
                "expected_generation": 99,
                "idempotency_key": "e-stale",
                "native_identity": "native-1",
            },
        )
        assert resp.status_code == 409, resp.text
        after = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert after["job"]["state"] == "accepted"
        assert after["cursor"] == before["cursor"]


def test_stale_attach_rejects_without_mutation():
    with _client() as client:
        prep = prepare_job(client, "prep-stale")
        manifest, payload = make_manifest()
        before = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-stale", generation=99
        )
        assert resp.status_code == 409, resp.text
        after = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert after["job"]["state"] == "accepted"
        assert after["job"]["quarantine_reason"] is None
        assert after["cursor"] == before["cursor"]
        rows = client.get(
            f"/v3/artifacts?job_id={prep['job_id']}", headers=_auth()
        ).json()["items"]
        assert rows == []


def test_attach_frozen_once_running_and_terminal():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-freeze")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200
        ready_via_synthetic_path(foundry, prep["job_id"])
        assert execute_job(client, prep["job_id"], "e1").status_code == 200

        manifest2, payload2 = make_manifest(payload=b"second-payload")
        frozen = attach_job(client, prep["job_id"], manifest2, payload2, "a2")
        assert frozen.status_code == 409, frozen.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "running"
        assert status["job"]["frozen_artifact_digests"] == [manifest["payload_digest"]]

        foundry.settle_running(prep["job_id"], outcome="succeeded", result={"ok": True})
        terminal = attach_job(client, prep["job_id"], manifest2, payload2, "a3")
        assert terminal.status_code == 409, terminal.text


# -- governed verification ----------------------------------------------------


def test_ungoverned_verification_rejected_and_quarantines():
    with _client() as client:
        for key, commands in (
            ("gov-curl", ["curl https://example.invalid/check"]),
            ("gov-py", [["python", "-c", "print('hi')"]]),
            ("gov-shell", ["sha256sum --check manifest.sha256; rm -rf /"]),
        ):
            prep = prepare_job(client, f"prep-{key}")
            manifest, payload = make_manifest(verify_commands=commands)
            # The plan cannot be receipt-built because it is ungoverned; a
            # shape-only receipt keeps the failure on manifest validation.
            resp = attach_job(
                client, prep["job_id"], manifest, payload, f"attach-{key}",
                receipt=shape_only_receipt(),
            )
            assert resp.status_code == 422, (key, resp.text)
            status = client.get(
                f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
            ).json()
            assert status["job"]["state"] == "quarantined", key


def test_named_mixed_and_structured_argv_plans_accepted():
    with _client() as client:
        # Named-only plan.
        prep = prepare_job(client, "prep-named")
        manifest, payload = make_manifest(verify_commands=["digest-check"])
        resp = attach_job(client, prep["job_id"], manifest, payload, "attach-named")
        assert resp.status_code == 200, resp.text

        # Mixed named + structured argv plan.
        prep2 = prepare_job(client, "prep-mixed")
        manifest2, payload2 = make_manifest(
            payload=b"mixed-payload",
            verify_commands=[
                "sha256-check",
                ["sha256sum", "--check", "manifest.sha256"],
            ],
        )
        resp2 = attach_job(
            client, prep2["job_id"], manifest2, payload2, "attach-mixed",
        )
        assert resp2.status_code == 200, resp2.text
        rows = client.get(
            f"/v3/artifacts?job_id={prep2['job_id']}", headers=_auth()
        ).json()["items"]
        assert rows[0]["steps"] == [
            {"index": 0, "step": "sha256-check",
             "evidence_digest": step_commitment(0, "sha256-check")},
            {"index": 1,
             "step": ["sha256sum", "--check", "manifest.sha256"],
             "evidence_digest":
                 step_commitment(1, ["sha256sum", "--check", "manifest.sha256"])},
        ]

        # Structured-argv-only plan.
        prep3 = prepare_job(client, "prep-argv-only")
        manifest3, payload3 = make_manifest(
            payload=b"argv-payload",
            verify_commands=[
                ["sha256sum", "--check", "manifest.sha256"],
                ["tar", "-tf", "payload.tar"],
            ],
        )
        resp3 = attach_job(
            client, prep3["job_id"], manifest3, payload3, "attach-argv-only",
        )
        assert resp3.status_code == 200, resp3.text


def test_receipt_evidence_must_not_be_arbitrary_text_or_cas_ref():
    # Arbitrary evidence text is never proof.
    with _client() as client:
        prep = prepare_job(client, "prep-hprof")
        manifest, payload = make_manifest()
        text_evidence = make_receipt(manifest)
        text_evidence["steps"][0]["evidence_digest"] = "attestation:deploy-1"
        resp = client.post(
            f"/v3/jobs/{prep['job_id']}:attach",
            headers=_auth(),
            json={
                "manifest": manifest,
                "staged_payload": staged_handoff(manifest, payload),
                "receipt": text_evidence,
                "expected_generation": 1,
                "idempotency_key": "attach-text",
            },
        )
        assert resp.status_code == 422, resp.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"
    # A CAS ref is never proof.
    with _client() as client:
        prep = prepare_job(client, "prep-hprof-cas")
        manifest, payload = make_manifest()
        cas_evidence = make_receipt(manifest)
        cas_evidence["steps"][0]["evidence_digest"] = "cas:staged-1"
        resp2 = client.post(
            f"/v3/jobs/{prep['job_id']}:attach",
            headers=_auth(),
            json={
                "manifest": manifest,
                "staged_payload": staged_handoff(manifest, payload),
                "receipt": cas_evidence,
                "expected_generation": 1,
                "idempotency_key": "attach-cas",
            },
        )
        assert resp2.status_code == 422, resp2.text
    # A wrong-but-hex commitment is a verification failure (quarantine).
    with _client() as client:
        prep = prepare_job(client, "prep-hprof-wronghex")
        manifest, payload = make_manifest()
        wrong_hex = make_receipt(manifest)
        wrong_hex["steps"][0]["evidence_digest"] = "e" * 64
        resp3 = client.post(
            f"/v3/jobs/{prep['job_id']}:attach",
            headers=_auth(),
            json={
                "manifest": manifest,
                "staged_payload": staged_handoff(manifest, payload),
                "receipt": wrong_hex,
                "expected_generation": 1,
                "idempotency_key": "attach-wronghex",
            },
        )
        assert resp3.status_code == 422, resp3.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"


def test_shiftio_digest_provenance_partial_proof_rejected():
    """Shiftio-style digest+provenance plan with partial proof is rejected."""
    with _client() as client:
        prep = prepare_job(client, "prep-shiftio")
        manifest, payload = make_manifest(
            verify_commands=["digest-check", "provenance-check"],
        )
        # Receipt covers only the digest step: partial proof must never
        # record an attachment, even though the digest step itself matches.
        partial = make_receipt(manifest)
        partial["steps"] = partial["steps"][:1]
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-partial",
            receipt=partial,
        )
        assert resp.status_code == 422, resp.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"
        assert "missing/partial" in (status["job"]["quarantine_reason"] or "")
        rows = client.get(
            f"/v3/artifacts?job_id={prep['job_id']}", headers=_auth()
        ).json()["items"]
        assert rows == []

        # Reordered full-coverage evidence is still rejected (fresh job:
        # the prior quarantine freezes attachments).
        prep2 = prepare_job(client, "prep-shiftio-reordered")
        reordered = make_receipt(manifest)
        reordered["steps"] = list(reversed(reordered["steps"]))
        resp2 = attach_job(
            client, prep2["job_id"], manifest, payload, "attach-reordered",
            receipt=reordered,
        )
        assert resp2.status_code == 422, resp2.text
        status2 = client.get(
            f"/v3/jobs/{prep2['job_id']}/status", headers=_auth()
        ).json()
        assert status2["job"]["state"] == "quarantined"
        assert "reordered" in (status2["job"]["quarantine_reason"] or "")


def test_fabricated_mount_handle_rejected_and_quarantined():
    with _client() as client:
        # Mount handle equal to the staged CAS ref: fabricated, rejected.
        prep = prepare_job(client, "prep-mount-same")
        manifest, payload = make_manifest()
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-mount-same",
            mount_handle="cas:staged-1",
        )
        assert resp.status_code == 422, resp.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"
        assert "mount" in (status["job"]["quarantine_reason"] or "")

        # CAS-styled mount handle: derived from a CAS ref, rejected.
        prep2 = prepare_job(client, "prep-mount-cas")
        m2, p2 = make_manifest(payload=b"cas-mount-payload")
        resp2 = attach_job(
            client, prep2["job_id"], m2, p2, "attach-mount-cas",
            mount_handle="cas:other-store-id",
        )
        assert resp2.status_code == 422, resp2.text
        status2 = client.get(
            f"/v3/jobs/{prep2['job_id']}/status", headers=_auth()
        ).json()
        assert status2["job"]["state"] == "quarantined"

        # Empty mount handle: schema-rejected, no quarantine.
        prep3 = prepare_job(client, "prep-mount-empty")
        m3, p3 = make_manifest(payload=b"empty-mount-payload")
        resp3 = attach_job(
            client, prep3["job_id"], m3, p3, "attach-mount-empty",
            mount_handle="   ",
        )
        assert resp3.status_code == 422, resp3.text
        rows = client.get(
            f"/v3/artifacts?job_id={prep3['job_id']}", headers=_auth()
        ).json()["items"]
        assert rows == []


def test_plan_hash_mismatch_rejected_and_quarantined():
    with _client() as client:
        prep = prepare_job(client, "prep-planhash")
        manifest, payload = make_manifest(verify_commands=["digest-check"])
        receipt = make_receipt(manifest)
        receipt["plan_hash"] = "f" * 64
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-planhash",
            receipt=receipt,
        )
        assert resp.status_code == 422, resp.text
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"
        assert "plan_hash" in (status["job"]["quarantine_reason"] or "")


def test_structured_only_plan_evidence_gaps_rejected():
    """Structured-argv-only plans reject missing/reordered/mismatched steps."""
    plan = [
        ["sha256sum", "--check", "manifest.sha256"],
        ["tar", "-tf", "payload.tar"],
    ]
    with _client() as client:
        prep = prepare_job(client, "prep-argv-gaps")
        manifest, payload = make_manifest(payload=b"gaps", verify_commands=plan)

        # Missing second argv step.
        missing = make_receipt(manifest)
        missing["steps"] = missing["steps"][:1]
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-missing-step",
            receipt=missing,
        )
        assert resp.status_code == 422, resp.text

        # Reordered argv steps (fresh job: prior failure quarantined).
        prep_r = prepare_job(client, "prep-argv-gaps-reordered")
        reordered = make_receipt(manifest)
        reordered["steps"] = list(reversed(reordered["steps"]))
        resp2 = attach_job(
            client, prep_r["job_id"], manifest, payload, "attach-reordered-step",
            receipt=reordered,
        )
        assert resp2.status_code == 422, resp2.text

        # Mismatched argv tokens (fresh job again).
        prep_m = prepare_job(client, "prep-argv-gaps-mismatch")
        mismatched = make_receipt(manifest)
        mismatched["steps"][1]["step"] = ["tar", "-xf", "payload.tar"]
        resp3 = attach_job(
            client, prep_m["job_id"], manifest, payload, "attach-mismatch-step",
            receipt=mismatched,
        )
        assert resp3.status_code == 422, resp3.text

        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["job"]["state"] == "quarantined"
        rows = client.get(
            f"/v3/artifacts?job_id={prep['job_id']}", headers=_auth()
        ).json()["items"]
        assert rows == []


# -- evidence, launch, error mapping -------------------------------------------


def test_events_carry_source_artifact_and_policy_evidence():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-evidence")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200
        ready_via_synthetic_path(foundry, prep["job_id"])
        assert execute_job(client, prep["job_id"], "e1").status_code == 200
        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status["events"], "expected a monotonic event log"
        for event in status["events"]:
            assert event["source_digest"] == SOURCE
            assert event["policy_hash"] == policy_hash_for(POLICY)
        attach_events = [e for e in status["events"] if e["artifact_digests"]]
        assert attach_events
        assert manifest["payload_digest"] in attach_events[-1]["artifact_digests"]
        running = [e for e in status["events"] if e["to_state"] == "running"]
        assert running and running[-1]["artifact_digests"] == [manifest["payload_digest"]]


def test_result_binds_frozen_evidence():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-result")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200
        ready_via_synthetic_path(foundry, prep["job_id"])
        assert execute_job(client, prep["job_id"], "e1").status_code == 200
        foundry.settle_running(prep["job_id"], outcome="succeeded", result={"exit": 0})
        result = client.get(
            f"/v3/jobs/{prep['job_id']}/result", headers=_auth()
        ).json()
        assert result["artifact_digests"] == [manifest["payload_digest"]]
        assert result["frozen_artifact_digests"] == [manifest["payload_digest"]]
        assert result["source_digest"] == SOURCE
        assert result["policy_hash"] == policy_hash_for(POLICY)


def test_execute_requires_identity_and_reserves_launch_token():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-launch")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200
        ready_via_synthetic_path(foundry, prep["job_id"])

        missing = client.post(
            f"/v3/jobs/{prep['job_id']}:execute",
            headers=_auth(),
            json={
                "command_profile": "offline-test",
                "objective": "compat check",
                "authority_ref": "compat",
                "expected_generation": 1,
                "idempotency_key": "e-missing",
            },
        )
        assert missing.status_code == 422, missing.text

        first = execute_job(client, prep["job_id"], "e1", identity="native-1")
        assert first.status_code == 200, first.text
        assert first.json()["state"] == "running"

        status = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        expected_token = canonical_hash(
            {
                "launch_reservation": True,
                "scope": f"job:{prep['job_id']}",
                "key": "e1",
            }
        )
        reservations = [
            e for e in status["events"] if e["reason"] == f"launch reserved {expected_token[:12]}"
        ]
        assert reservations, "expected a durable launch-reservation marker"

        # Same-key replay reuses the identity without relaunching.
        replay = execute_job(client, prep["job_id"], "e1", identity="native-OTHER")
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        status2 = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert status2["job"]["native_identity"] == "native-1"
        assert len(status2["events"]) == len(status["events"])


def test_execute_from_running_rejected():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-rerun")
        ready_via_synthetic_path(foundry, prep["job_id"])
        assert execute_job(client, prep["job_id"], "e1").status_code == 200
        again = execute_job(client, prep["job_id"], "e2")
        assert again.status_code == 409, again.text


def test_execute_before_ready_rejected_without_mutation():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = prepare_job(client, "prep-notready")
        manifest, payload = make_manifest()
        assert attach_job(
            client, prep["job_id"], manifest, payload, "a1"
        ).status_code == 200
        before = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        # Accepted (and preparing) jobs must not execute: only the explicit
        # synthetic readiness transition produces ready.
        resp = execute_job(client, prep["job_id"], "e-early")
        assert resp.status_code == 409, resp.text
        after = client.get(
            f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
        ).json()
        assert after["job"]["state"] == "accepted"
        assert after["job"]["frozen_artifact_digests"] == []
        assert after["cursor"] == before["cursor"]
        # The explicit synthetic path unblocks execute (fresh key: the early
        # attempt stored a durable failure under its own idempotency key).
        ready_via_synthetic_path(foundry, prep["job_id"])
        assert execute_job(client, prep["job_id"], "e-ready").status_code == 200


def test_stable_error_mapping():
    with _client() as client:
        assert (
            client.get("/v3/jobs/does-not-exist/status", headers=_auth()).status_code
            == 404
        )
        assert client.get("/v3/shell", headers=_auth()).status_code == 404
        bad_cursor = client.get("/v3/jobs?cursor=abc", headers=_auth())
        assert bad_cursor.status_code == 422, bad_cursor.text

        prep = prepare_job(client, "prep-errmap")
        not_ready = client.get(
            f"/v3/jobs/{prep['job_id']}/result", headers=_auth()
        )
        assert not_ready.status_code == 409

        conflict = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "other-ref",
                "source_digest": SOURCE,
                "idempotency_key": "prep-errmap",
            },
        )
        assert conflict.status_code == 409


def test_bulk_attempts_and_activity_expose_evidence():
    with _client() as client:
        prep = prepare_job(client, "prep-bulk")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200
        attempts = client.get(
            f"/v3/attempts?job_id={prep['job_id']}", headers=_auth()
        ).json()["items"]
        assert attempts and attempts[0]["source_digest"] == SOURCE
        assert attempts[0]["policy_hash"] == policy_hash_for(POLICY)
        assert manifest["payload_digest"] in attempts[0]["artifact_digests"]
        activity = client.get(
            f"/v3/activity?job_id={prep['job_id']}", headers=_auth()
        ).json()["items"]
        assert activity and activity[0]["source_digest"] == SOURCE

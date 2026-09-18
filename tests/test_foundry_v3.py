"""Synthetic Foundry v3 adapter tests (issue #5).

All Foundry state here is synthetic and in-process via InMemoryFoundryClient.
No live Foundry deployment, network egress, model routing, or host mutation
is involved.

Attach uses strict flat foundry.artifact/v1 manifests plus the typed
staged-payload reference and a typed deployment attachment receipt;
payload bytes are never embedded in JSON.
"""

import hashlib

from fastapi.testclient import TestClient

from agent_interop_gateway.api import create_app
from agent_interop_gateway.config import ExecutorConfig, GatewayConfig
from agent_interop_gateway.foundry import InMemoryFoundryClient, build_attachment_receipt

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


def make_manifest(payload: bytes = b"adapter-payload", **overrides):
    manifest = {
        "schema_version": "foundry.artifact/v1",
        "kind": "dir-archive",
        "producer": "synthetic-ci",
        "source_repo": "example/synthetic",
        "source_commit": SOURCE,
        "lock_digest": LOCK,
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


def attach_body(manifest: dict, payload: bytes, key: str, generation: int = 1) -> dict:
    return {
        "manifest": manifest,
        "staged_payload": {
            "ref": "cas:staged-1",
            "digest": manifest["payload_digest"],
            "size": len(payload),
        },
        "receipt": build_attachment_receipt(
            manifest,
            staged_ref="cas:staged-1",
            mount_handle="ro-mount:demo:staged-1",
            verifier="synthetic-adapter/sha256-check",
        ),
        "expected_generation": generation,
        "idempotency_key": key,
    }


def prepare_body(key: str, source: str = SOURCE, policy: dict | None = POLICY) -> dict:
    body: dict = {
        "project": "demo",
        "ref": "main",
        "source_digest": source,
        "idempotency_key": key,
        "objective": "synthetic check",
        "authority_ref": "issue-5",
    }
    if policy is not None:
        body["artifact_policy"] = policy
    return body


def test_v3_full_lifecycle_prepare_execute_status_result():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json=prepare_body("prep-1"),
        )
        assert prep.status_code == 200, prep.text
        body = prep.json()
        assert body["protocol"] == "foundry/v3"
        assert body["state"] == "accepted"
        assert body["generation"] == 1
        assert body["attempt_id"]
        assert body["request_hash"]
        assert body["policy"]["route_owner"] == "cline-model-optimizer"
        assert body["replayed"] is False
        job_id = body["job_id"]

        manifest, payload = make_manifest()
        attach = client.post(
            f"/v3/jobs/{job_id}:attach",
            headers=_auth(),
            json=attach_body(manifest, payload, "attach-1"),
        )
        assert attach.status_code == 200, attach.text

        foundry.mark_ready_for_test(job_id)
        exe = client.post(
            f"/v3/jobs/{job_id}:execute",
            headers=_auth(),
            json={
                "command_profile": "synthetic-check",
                "objective": "synthetic check",
                "authority_ref": "issue-5",
                "expected_generation": 1,
                "idempotency_key": "exe-1",
                "native_identity": "native-1",
            },
        )
        assert exe.status_code == 200, exe.text
        assert exe.json()["state"] == "running"

        status = client.get(f"/v3/jobs/{job_id}/status", headers=_auth())
        assert status.status_code == 200
        payload_status = status.json()
        assert payload_status["cursor"] >= 1
        assert payload_status["job"]["state"] == "running"
        assert payload_status["job"]["frozen_artifact_digests"] == [
            manifest["payload_digest"]
        ]

        # Not terminal yet: read_result must refuse honestly.
        not_ready = client.get(f"/v3/jobs/{job_id}/result", headers=_auth())
        assert not_ready.status_code == 409

        foundry.settle_running(job_id, outcome="succeeded", result={"ok": True})
        result = client.get(f"/v3/jobs/{job_id}/result", headers=_auth())
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "succeeded"
        assert result.json()["artifact_digests"] == [manifest["payload_digest"]]
        assert result.json()["frozen_artifact_digests"] == [manifest["payload_digest"]]
        assert result.json()["source_digest"] == SOURCE


def test_v3_cancel_and_quarantine():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json=prepare_body("prep-cancel", policy=None),
        ).json()
        job_id = prep["job_id"]
        foundry.mark_ready_for_test(job_id)
        exe = client.post(
            f"/v3/jobs/{job_id}:execute",
            headers=_auth(),
            json={
                "command_profile": "synthetic-check",
                "objective": "cancel path",
                "authority_ref": "issue-5",
                "expected_generation": 1,
                "idempotency_key": "cancel-exe-1",
                "native_identity": "native-cancel-1",
            },
        )
        assert exe.status_code == 200, exe.text
        cancel = client.post(
            f"/v3/jobs/{job_id}:cancel",
            headers=_auth(),
            json={
                "reason": "no longer needed",
                "expected_generation": 1,
                "idempotency_key": "cancel-1",
            },
        )
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["state"] == "cancel_requested"

        prep2 = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json=prepare_body("prep-quar", source="d" * 64, policy=None),
        ).json()
        quar = client.post(
            f"/v3/jobs/{prep2['job_id']}:quarantine",
            headers=_auth(),
            json={
                "reason": "suspect payload",
                "expected_generation": 1,
                "idempotency_key": "quar-1",
            },
        )
        assert quar.status_code == 200, quar.text
        assert quar.json()["state"] == "quarantined"


def test_v3_idempotency_replay_and_conflict():
    with _client() as client:
        first = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json=prepare_body("same-key", source="e" * 64, policy=None),
        )
        assert first.status_code == 200
        second = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json=prepare_body("same-key", source="e" * 64, policy=None),
        )
        assert second.status_code == 200
        assert second.json()["replayed"] is True
        assert second.json()["job_id"] == first.json()["job_id"]

        conflict = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "other-ref",
                "source_digest": "e" * 64,
                "idempotency_key": "same-key",
            },
        )
        assert conflict.status_code == 409


def test_v3_generation_fencing():
    with _client() as client:
        prep = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json=prepare_body("prep-gen", source="f" * 64, policy=None),
        ).json()
        bad = client.post(
            f"/v3/jobs/{prep['job_id']}:cancel",
            headers=_auth(),
            json={"reason": "x", "expected_generation": 99, "idempotency_key": "cancel-gen"},
        )
        assert bad.status_code == 409


def test_v3_rejects_model_cost_account_fields():
    with _client() as client:
        for test_field in ("model", "account", "cost_tier", "routing"):
            resp = client.post(
                "/v3/jobs:prepare",
                headers=_auth(),
                json={
                    "project": "demo",
                    "ref": "main",
                    "source_digest": "0" * 64,
                    "idempotency_key": f"k-{test_field}",
                    test_field: "smuggled",
                },
            )
            assert resp.status_code == 422, (test_field, resp.text)


def test_v3_bulk_reads_paginated_with_cursors():
    with _client() as client:
        for i in range(3):
            client.post(
                "/v3/jobs:prepare",
                headers=_auth(),
                json={
                    "project": f"proj-{i % 2}",
                    "ref": "main",
                    "source_digest": f"{i}" * 64,
                    "idempotency_key": f"bulk-{i}",
                },
            )
        for resource in (
            "projects",
            "jobs",
            "attempts",
            "activity",
            "artifacts",
            "approvals",
            "health",
            "evidence",
        ):
            first = client.get(f"/v3/{resource}?limit=1", headers=_auth())
            assert first.status_code == 200, (resource, first.text)
            page = first.json()
            assert page["protocol"] == "foundry/v3"
            assert page["resource"] == resource
            assert len(page["items"]) <= 1
            if page["next_cursor"] is not None:
                second = client.get(
                    f"/v3/{resource}?limit=1&cursor={page['next_cursor']}", headers=_auth()
                )
                assert second.status_code == 200

        unknown = client.get("/v3/shell", headers=_auth())
        assert unknown.status_code == 404


def test_v3_requires_auth_and_v1_deprecated_headers():
    with _client() as client:
        denied = client.post("/v3/jobs:prepare", json={})
        assert denied.status_code in (401, 422)

        ok = client.post(
            "/v1/delegations",
            headers=_auth(),
            json={"task": "hello", "capabilities": ["demo"]},
        )
        assert ok.status_code == 200
        assert ok.headers.get("Deprecation") == "true"

        health = client.get("/health")
        assert health.status_code == 200


def test_v3_exposes_no_shell_or_filesystem_tool():
    with _client() as client:
        routes = client.get("/openapi.json").json()["paths"]
        names = " ".join(routes)
        assert "shell" not in names
        assert "filesystem" not in names
        assert "exec" not in names.replace(":execute", "")
        v3_paths = [p for p in routes if p.startswith("/v3/")]
        assert v3_paths, "v3 routes must be mounted"

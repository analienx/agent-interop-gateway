"""Synthetic Foundry v3 adapter tests (issue #5).

All Foundry state here is synthetic and in-process via InMemoryFoundryClient.
No live Foundry deployment, network egress, model routing, or host mutation
is involved.
"""

from fastapi.testclient import TestClient

from agent_interop_gateway.api import create_app
from agent_interop_gateway.config import ExecutorConfig, GatewayConfig
from agent_interop_gateway.foundry import InMemoryFoundryClient


def _client(token: str = "secret", foundry: InMemoryFoundryClient | None = None) -> TestClient:
    config = GatewayConfig(
        token=token,
        executors=[ExecutorConfig(name="echo", type="echo", capabilities={"demo"})],
    )
    app = create_app(config, foundry=foundry or InMemoryFoundryClient())
    return TestClient(app)


def _auth(token: str = "secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_v3_full_lifecycle_prepare_execute_status_result():
    foundry = InMemoryFoundryClient()
    with _client(foundry=foundry) as client:
        prep = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": "sha256:" + "a" * 64,
                "idempotency_key": "prep-1",
                "policy_hash": "pol-1",
                "objective": "synthetic check",
                "authority_ref": "issue-5",
            },
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

        attach = client.post(
            f"/v3/jobs/{job_id}:attach",
            headers=_auth(),
            json={
                "manifest": {"payload_digest": "sha256:" + "b" * 64, "kind": "dir-archive"},
                "expected_generation": 1,
                "idempotency_key": "attach-1",
            },
        )
        assert attach.status_code == 200, attach.text

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
        payload = status.json()
        assert payload["cursor"] >= 1
        assert payload["job"]["state"] == "running"

        # Not terminal yet: read_result must refuse honestly.
        not_ready = client.get(f"/v3/jobs/{job_id}/result", headers=_auth())
        assert not_ready.status_code == 409

        foundry.settle_running(job_id, outcome="succeeded", result={"ok": True})
        result = client.get(f"/v3/jobs/{job_id}/result", headers=_auth())
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "succeeded"
        assert result.json()["artifact_digests"] == ["sha256:" + "b" * 64]


def test_v3_cancel_and_quarantine():
    with _client() as client:
        prep = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": "sha256:" + "c" * 64,
                "idempotency_key": "prep-cancel",
            },
        ).json()
        job_id = prep["job_id"]
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
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": "sha256:" + "d" * 64,
                "idempotency_key": "prep-quar",
            },
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
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": "sha256:" + "e" * 64,
                "idempotency_key": "same-key",
            },
        )
        assert first.status_code == 200
        second = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": "sha256:" + "e" * 64,
                "idempotency_key": "same-key",
            },
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
                "source_digest": "sha256:" + "e" * 64,
                "idempotency_key": "same-key",
            },
        )
        assert conflict.status_code == 409


def test_v3_generation_fencing():
    with _client() as client:
        prep = client.post(
            "/v3/jobs:prepare",
            headers=_auth(),
            json={
                "project": "demo",
                "ref": "main",
                "source_digest": "sha256:" + "f" * 64,
                "idempotency_key": "prep-gen",
            },
        ).json()
        bad = client.post(
            f"/v3/jobs/{prep['job_id']}:cancel",
            headers=_auth(),
            json={"reason": "x", "expected_generation": 99, "idempotency_key": "cancel-gen"},
        )
        assert bad.status_code == 409


def test_v3_rejects_model_cost_account_fields():
    with _client() as client:
        for field in ("model", "account", "cost_tier", "routing"):
            resp = client.post(
                "/v3/jobs:prepare",
                headers=_auth(),
                json={
                    "project": "demo",
                    "ref": "main",
                    "source_digest": "sha256:" + "0" * 64,
                    "idempotency_key": f"k-{field}",
                    field: "smuggled",
                },
            )
            assert resp.status_code == 422, (field, resp.text)


def test_v3_bulk_reads_paginated_with_cursors():
    with _client() as client:
        for i in range(3):
            client.post(
                "/v3/jobs:prepare",
                headers=_auth(),
                json={
                    "project": f"proj-{i % 2}",
                    "ref": "main",
                    "source_digest": "sha256:" + f"{i}" * 64,
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

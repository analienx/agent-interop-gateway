"""Contract-compatibility regression tests: gateway vs hardened Foundry PR #2.

Pins the gateway to the agent-foundry contract at head 31f8240 and the
Shiftio foundry.artifact/v1 manifest:

- typed prepare carries the job-bound artifact_policy with policy-hash
  digest semantics;
- attach carries the flat foundry.artifact/v1 manifest plus a typed
  staged-payload reference/verification handoff, never payload bytes;
- events/results mirror source_digest, artifact_digests, policy_hash, and
  frozen attachment evidence;
- launch reservation/native-identity semantics and stable error mapping;
- the synthetic client stays honest: generation fencing before mutation,
  attachment freeze, policy matching, governed verification profiles, and
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
    canonical_hash,
    policy_hash_for,
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
    with_handoff: bool = True,
    constraints: dict | None = None,
):
    body: dict = {
        "manifest": manifest,
        "expected_generation": generation,
        "idempotency_key": key,
    }
    if with_handoff:
        body["staged_payload"] = staged_handoff(manifest, payload)
        body["verification"] = {"profile": "sha256-check"}
    if constraints is not None:
        body["constraints"] = constraints
    return client.post(f"/v3/jobs/{job_id}:attach", headers=_auth(), json=body)


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


# -- attach: manifest + staged handoff --------------------------------------


def test_attach_full_handoff_records_typed_evidence():
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
        assert row["mount_point"] == f"staged:{manifest['payload_digest'][:12]}"
        assert row["verified_profile"] == "sha256-check"
        assert row["staged_ref"] == "cas:staged-1"
        assert row["generation"] == 1


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
            with_handoff=False,
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
            resp = attach_job(
                client, prep["job_id"], manifest, payload, f"attach-{key}",
                with_handoff=False,
            )
            assert resp.status_code == 422, (key, resp.text)
            status = client.get(
                f"/v3/jobs/{prep['job_id']}/status", headers=_auth()
            ).json()
            assert status["job"]["state"] == "quarantined", key


def test_named_profile_and_structured_argv_accepted():
    with _client() as client:
        prep = prepare_job(client, "prep-named")
        manifest, payload = make_manifest(verify_commands=["digest-check"])
        resp = attach_job(
            client, prep["job_id"], manifest, payload, "attach-named",
            with_handoff=False,
        )
        assert resp.status_code == 200, resp.text

        prep2 = prepare_job(client, "prep-argv")
        manifest2, payload2 = make_manifest(
            payload=b"argv-payload",
            verify_commands=[["sha256sum", "--check", "manifest.sha256"]],
        )
        resp2 = attach_job(
            client, prep2["job_id"], manifest2, payload2, "attach-argv",
            with_handoff=False,
        )
        assert resp2.status_code == 200, resp2.text


def test_handoff_profile_must_be_governed():
    with _client() as client:
        prep = prepare_job(client, "prep-hprof")
        manifest, payload = make_manifest()
        body = {
            "manifest": manifest,
            "staged_payload": staged_handoff(manifest, payload),
            "verification": {"profile": "curl-pipe"},
            "expected_generation": 1,
            "idempotency_key": "attach-hprof",
        }
        resp = client.post(
            f"/v3/jobs/{prep['job_id']}:attach", headers=_auth(), json=body
        )
        assert resp.status_code == 422, resp.text


# -- evidence, launch, error mapping -------------------------------------------


def test_events_carry_source_artifact_and_policy_evidence():
    with _client() as client:
        prep = prepare_job(client, "prep-evidence")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200
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
    with _client() as client:
        prep = prepare_job(client, "prep-launch")
        manifest, payload = make_manifest()
        assert attach_job(client, prep["job_id"], manifest, payload, "a1").status_code == 200

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
    with _client() as client:
        prep = prepare_job(client, "prep-rerun")
        assert execute_job(client, prep["job_id"], "e1").status_code == 200
        again = execute_job(client, prep["job_id"], "e2")
        assert again.status_code == 409, again.text


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

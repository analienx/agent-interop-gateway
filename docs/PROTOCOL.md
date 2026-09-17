# AIGW/1 protocol (deprecated) + Foundry v3 adapter contract

> `aigw/1` is **deprecated**. It remains mounted for the Android
> read/relay experiment and returns `Deprecation: true` on every response.
> New integrations must use the typed `foundry/v3` endpoints below.

## Foundry v3 (`foundry/v3`, current)

`aigw/1` is a small JSON contract between a conversational surface/bridge and a user-controlled gateway.

## Request

`POST /v1/delegations`

```json
{
  "protocol": "aigw/1",
  "id": "uuid-or-caller-id",
  "task": "Natural-language work description",
  "origin": {"surface": "chatgpt-android-voice"},
  "capabilities": ["fs.read", "git"],
  "risk": "read",
  "routing": {
    "preference": "local_first",
    "executor": null,
    "allow_fallback": true
  },
  "timeout_seconds": 120,
  "metadata": {}
}
```

Risk values are `read`, `write`, and `privileged`. The reference gateway rejects `write` and `privileged` requests unless the machine owner enables those categories server-side.

Capabilities are opaque strings matched as a required subset of executor capabilities. Recommended conventions include `fs.read`, `fs.write`, `git`, `github`, `shell`, `browser.read`, `browser.write`, `android.ui.read`, and `android.ui.write`.

A bridge may submit a structured `process` action with `argv`, `cwd`, and explicitly allowlisted environment keys. The reference executor uses direct argv execution rather than a shell string, and the configured executor must allowlist the command.

## Response

```json
{
  "protocol": "aigw/1",
  "delegation_id": "...",
  "state": "succeeded",
  "executor": "local-agent",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "payload": {},
  "error": null
}
```

States are `queued`, `running`, `succeeded`, `failed`, and `rejected`.

## Routing

`local_first` uses operator priority first, then cost and quality. `lowest_cost` ranks by cost tier, then quality. `quality_first` ranks by quality tier, then cost. `specific` is intended for explicit executor selection.

Cost and quality are relative integers rather than vendor/model names, keeping the protocol independent of model pricing and product changes.

## Authentication

If `AIGW_TOKEN` or `[gateway].token` is configured, protected routes require `Authorization: Bearer <token>`. The health endpoint remains unauthenticated so local supervisors can check liveness without receiving execution privileges.

All `/v3/*` routes require the same bearer token as `/v1/*`.

## Foundry v3 typed operations

The gateway is an authenticated transport/translation adapter, not a
scheduler or model router. It forwards explicit Foundry operations and
returns idempotency keys, request hashes, policy results, attempt ids, and
states on every write:

| Operation | Endpoint |
| --- | --- |
| prepare | `POST /v3/jobs:prepare` |
| attach artifact | `POST /v3/jobs/{id}:attach` |
| execute | `POST /v3/jobs/{id}:execute` |
| status (cursor) | `GET /v3/jobs/{id}/status?after_cursor=N` |
| cancel | `POST /v3/jobs/{id}:cancel` |
| read result | `GET /v3/jobs/{id}/result` |
| quarantine | `POST /v3/jobs/{id}:quarantine` |

Every mutating call requires an `idempotency_key`. Replays return the stored
response with `replayed: true`; reusing a key with a different payload is a
`409` conflict, while a replayed durable failure re-raises the same error.
The execute idempotency scope excludes the native identity, so a same-key
replay reuses the committed identity instead of relaunching. Mutations carry
`expected_generation` fencing, checked before any state change; stale
callers get `409` without mutating, quarantining, or appending events.
Terminal states are immutable; cancelling an `accepted`
job is rejected (`409`) because the Foundry machine has no
`accepted -> cancel_requested` edge. `read_result` before a terminal state
is a `409`, never a guess.

### Prepare: job-bound artifact policy

`prepare` accepts an optional `artifact_policy` document with the required
keys `source_repo`, `lock_digest`, `platform`, `arch`, `toolchain`,
`lifecycle_policy` (one of `no-scripts`, `offline-only`, `hermetic`,
`managed-postinstall`) and the optional `provenance_ref`. When present it
is validated, persisted on the job, and bound to `policy_hash` as
`sha256(canonical_json({"artifact_policy": policy}))`; an explicitly
passed `policy_hash` must equal that digest (`422` otherwise). Later
attaches enforce the stored policy: callers cannot omit or contradict
declared constraints.

### Attach: flat manifest plus staged-payload handoff

`attach` carries the flat `foundry.artifact/v1` manifest — exactly the
Shiftio/hardened-Foundry field set (`schema_version`, `kind`
(`dir-archive`|`oci-image`), `producer`, `source_repo`, `source_commit`,
`lock_digest`, `platform`, `arch`, `toolchain`, `payload_digest`,
`payload_bytes`, `built_at`, `retention`, `lifecycle_policy`,
`provenance_ref`, `verify_commands`) — plus a **required** typed
`staged_payload` reference (`ref`, `digest`, `size`) and a **required**
`verification` handoff (`profile`, `evidence`). `ref` is an immutable
deployment-owned staging/CAS identifier (`cas:<id>` / `staging:<id>`);
URLs, paths, and shell syntax are rejected (`422`). `profile` must be a
governed profile (`sha256-check`, `digest-check`, `signature-check`,
`provenance-check`, `reproducibility-check`) **declared in the manifest
`verify_commands`**, and `evidence` must be nonempty deployment-confirmed
verification evidence. Optional `constraints` (`source_repo`,
`lock_digest`, `platform`, `arch`, `toolchain`, `lifecycle_policy`,
`provenance_ref`) may narrow but never contradict the stored policy.

Payload bytes are never embedded in JSON: manifest keys such as `payload`,
`content`, `data`, or `blob` are rejected (`422`), and `staged_payload`
digest/size must equal the manifest values. A missing handoff (or missing
`verification.evidence`) is a `422` schema rejection with no quarantine
and no recorded evidence; a mismatched handoff (digest/size/profile)
quarantines the job (`422`, durable under the idempotency key). The
gateway records only the deployment-supplied evidence — the mount handle
is the deployment-owned staged ref and the verified profile/evidence are
the deployment-confirmed handoff values; nothing is synthesized. The gateway performs no
fetch/network/package logic and runs no verification commands — structured
`verify_commands` argv is restricted to the offline allowlist
(`sha256sum`, `shasum`, `sha256`, `cosign`, `openssl`, `tar`, `digest`)
with no shell metacharacters or network tokens. Failed verification
quarantines the job (`422`, durable under the idempotency key).
Attachment is legal only in `accepted`/`preparing`/`ready`; `running`,
`cancel_requested`, and terminal states reject (`409`) so execution
evidence always binds the frozen set.

### Evidence, launch, and error mapping

Events carry `source_digest`, `artifact_digests`, and `policy_hash`;
`read_result` and status snapshots bind `frozen_artifact_digests` (frozen
at execute), `source_digest`, and `policy_hash`. Execute records a durable
launch reservation derived from the idempotency scope/key, then confirms
with the required native identity in the `ready -> running` transition.
Execute runs **only** from `ready`; `accepted`/`preparing` callers get a
`409` with no mutation. The synthetic in-memory client exposes the narrow
test-only `mark_ready_for_test` helper mirroring the deployment's
`accepted -> preparing -> ready` path — tests must invoke it explicitly
because execute never synthesizes readiness.

Stable mapping: `job_not_found`/`unknown_resource` → `404`;
`idempotency_conflict`/`stale_generation`/`illegal_transition`/`job_not_ready`
→ `409`; `artifact_invalid`/`artifact_verification`/`policy_invalid`/
`invalid_cursor`/`missing_idempotency_key`/`missing_native_identity` →
`422`. Request-schema violations (unknown fields, ungoverned profiles)
are also `422`.

Cursor/paginated bulk reads (all `GET /v3/{resource}?limit=&cursor=`):
`projects`, `jobs`, `attempts`, `activity`, `artifacts`, `approvals`,
`health`, `evidence`. No general shell or filesystem mutation exists on the
v3 path by design.

Model, cost-tier, and account selection fields are rejected (`422`) on the
v3 path. Model and account routing is owned exclusively by Cline Model
Optimizer.

## AIGW/1 request (deprecated)

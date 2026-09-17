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
`409` conflict. Mutations carry `expected_generation` fencing; stale
callers get `409`. Terminal states are immutable; cancelling an `accepted`
job is rejected (`409`) because the Foundry machine has no
`accepted -> cancel_requested` edge. `read_result` before a terminal state
is a `409`, never a guess.

Cursor/paginated bulk reads (all `GET /v3/{resource}?limit=&cursor=`):
`projects`, `jobs`, `attempts`, `activity`, `artifacts`, `approvals`,
`health`, `evidence`. No general shell or filesystem mutation exists on the
v3 path by design.

Model, cost-tier, and account selection fields are rejected (`422`) on the
v3 path. Model and account routing is owned exclusively by Cline Model
Optimizer.

## AIGW/1 request (deprecated)

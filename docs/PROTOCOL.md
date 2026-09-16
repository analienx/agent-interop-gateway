# `aigw/1` protocol

`aigw/1` is the provider-neutral contract between a bridge and Agent Interop Gateway. It intentionally describes **work**, **risk**, **capabilities**, and **routing preference** without naming an LLM vendor.

## Submit a delegation

`POST /v1/delegations`

```json
{
  "protocol": "aigw/1",
  "id": "stable-client-generated-id",
  "task": "Inspect the repository and explain the failing CI job.",
  "origin": {
    "surface": "voice",
    "conversation_id": "optional",
    "actor": "optional"
  },
  "capabilities": ["fs.read", "git"],
  "risk": "read",
  "routing": {
    "preference": "local_first",
    "executor": null,
    "allow_fallback": true,
    "fallback_on_failure": false
  },
  "timeout_seconds": 120,
  "metadata": {}
}
```

Unknown JSON fields are rejected. Request IDs, capabilities, argv, environment values, task length, metadata size, and whole request size are bounded server-side.
## Risk

`risk` is one of `read`, `write`, or `privileged`.

A bridge's declaration does not grant permission. The gateway must allow the risk globally and the chosen executor must include it in `allowed_risks`.

Explicit structured process actions cannot be labeled `read`; caller-controlled process execution is inherently state-capable.

## Capabilities

Capabilities are opaque strings interpreted by operator policy, for example `fs.read`, `fs.write`, `git`, `shell`, `browser`, or `home-assistant`. Routing requires the request capability set to be a subset of the executor capability set.

## Routing

`preference` values:

- `local_first` — lowest operator priority number first, then cost/quality tie-breakers.
- `lowest_cost` — lower `cost_tier` first.
- `quality_first` — higher `quality_tier` first.
- `specific` — use the named executor (when supplied) and its configured priority.

`allow_fallback` controls whether an unavailable candidate may be skipped. `fallback_on_failure` allows trying another executor after execution failure and is valid only for read-only requests.

## Structured action

A request may contain:

```json
{
  "action": {
    "kind": "process",
    "argv": ["git", "status", "--short"],
    "cwd": "/workspace/repo",
    "env": {}
  }
}
```

The structured-process executor still enforces command, cwd, environment, risk, and capability policy. No shell string is evaluated.
## Result

A normalized result contains the stable delegation ID, state, selected executor, timestamps, exit code, bounded stdout/stderr, optional payload/error, attempt history, replay flag, and durability.

```json
{
  "protocol": "aigw/1",
  "delegation_id": "stable-client-generated-id",
  "state": "succeeded",
  "executor": "pi",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "payload": {},
  "error": null,
  "attempts": [],
  "replayed": false,
  "durability": "committed"
}
```

States are `queued`, `running`, `succeeded`, `failed`, or `rejected`. Durability is `volatile`, `committed`, or `uncertain`.

## Synchronous versus asynchronous submission

Without a preference header, the POST waits for the delegation result.

With `Prefer: respond-async`, the gateway returns `202 Accepted` after admission and includes:

- `Location: /v1/delegations/{id}`
- `Retry-After: 1`
- a queued/in-progress `DelegationResult` body

Poll `GET /v1/delegations/{id}` until the state becomes terminal. Keep the same ID across all transport retries.

If the gateway admission queue is full, POST returns `503 Service Unavailable` with `Retry-After` and does not start the task.
## Authentication and utility endpoints

When a token is configured, send `Authorization: Bearer <token>`. Non-loopback binds and any write/privileged-enabled gateway require a strong token.

`GET /health` is an unauthenticated liveness endpoint by design. `GET /ready` is authenticated when a token is configured and verifies executor probes plus journal health.

## Idempotency and conflicts

The request ID is an idempotency key. If the ID already exists with the same canonical payload, AIGW returns the current/persisted result with `replayed=true`. If the payload differs, the request is rejected.

For side-effecting work, never generate a fresh ID merely because the HTTP response was lost. Doing so defeats duplicate protection.

## Compatibility

Protocol additions within `aigw/1` should remain backward-compatible where possible. Unknown request fields are intentionally rejected, so new caller fields require a coordinated protocol revision or server update rather than being silently ignored.

The protocol does not define how a conversation surface discovers a delegation. That is bridge-specific and kept out of the gateway contract.

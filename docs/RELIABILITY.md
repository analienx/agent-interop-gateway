# Reliability model

AIGW treats reliability primarily as **duplicate-side-effect prevention**, not as blind retrying. The gateway deliberately behaves differently for read-only and state-changing work.

## Stable delegation identity

Every request has an `id`. Bridges should keep that ID stable while retrying transport, reconnecting, or polling the same logical operation.

AIGW calculates a SHA-256 fingerprint over the canonical request payload excluding `id`.

- same ID + same fingerprint -> replay/in-progress semantics;
- same ID + different fingerprint -> rejected as an ID conflict.

This protects against a bridge accidentally reusing an old ID for different work.

## Durable execution claim

Before execution, a gateway instance claims the delegation in SQLite. The claim records owner and lease expiry. While the lease is valid, another process cannot execute the same delegation.

The owner renews the lease while execution is active. Completion is accepted only if that instance still owns the claim, preventing a stale worker from overwriting a newer owner.

## Read versus write recovery

For `risk=read`, an expired claim may be reacquired. Read-only retries are allowed because repeating observation is expected not to create external state.

For `risk=write` or `risk=privileged`, an expired claim becomes **uncertain**. The previous attempt may have changed state immediately before the process/gateway died, so AIGW blocks automatic replay.

`uncertain` is an operational state requiring reconciliation, not an invitation to retry.
## Result durability

`DelegationResult.durability` communicates journal status:

| Value | Meaning |
| --- | --- |
| `volatile` | Result exists in process memory but has not been durably committed. |
| `committed` | Result is stored in the durable journal under the current execution claim. |
| `uncertain` | A durable side-effecting operation cannot be safely classified after interruption. |

A successful executor return is not enough to claim durable success. The journal commit must also succeed while ownership is still valid.

## Retry layers

There are two retry layers and both are conservative.

**Within one executor:** `max_attempts`, `retry_exit_codes`, and `transient_stderr_patterns` apply only to read-only work. Backoff is exponential from `retry_backoff_seconds`.

**Across executors:** `allow_fallback` permits routing to another candidate when an executor is unavailable. `fallback_on_failure` permits fallback after an actual executor failure and is accepted only for `risk=read`.

A write is never sent to a second executor merely because the first executor returned an ambiguous failure.

## Process containment

Subprocesses receive bounded stdin/output handling, explicit timeouts, and their own process group/session. Timeout or cancellation attempts to terminate the entire process tree rather than leaving grandchildren running in the background.

Output is streamed into bounded buffers so a noisy subprocess cannot grow gateway memory without limit.

## Backpressure

`max_concurrent_delegations` controls actively executing gateway work. `max_queued_delegations` bounds admitted in-memory work waiting for execution. When capacity is exhausted, the HTTP API returns `503` with `Retry-After` rather than allowing unbounded queue growth.
## Failure matrix

| Failure point | Read request | Write/privileged request |
| --- | --- | --- |
| Bridge loses HTTP response after submit | Resubmit same ID; replay result or in-progress state. | Resubmit same ID; never invent a new ID. |
| Gateway process dies before execution | Expired claim can be reacquired. | Expired claim becomes uncertain if execution may have started. |
| Executor unavailable before start | May fall back to another candidate. | May fall back only while no side effect was attempted. |
| Executor transient failure | Configured retry/fallback allowed. | No automatic retry-on-failure. |
| Executor times out | May retry if policy says transient. | Treat as potentially side-effecting; no blind replay. |
| Journal commit fails after executor success | Result remains non-committed; caller must not treat it as durable. | Claim eventually becomes uncertain; manual reconciliation required. |
| Mobile result injection cannot be confirmed | Keep execution result, mark injection uncertain; do not auto-send twice. | Same; UI delivery ambiguity must not cause task re-execution. |

## Async HTTP behavior

Clients can send `Prefer: respond-async`. AIGW returns `202 Accepted` once the delegation is admitted and supplies `Location: /v1/delegations/{id}`. Polling that location is transport retry, not task retry.

The mobile relay uses this pattern because conversational tasks can exceed a normal mobile HTTP response window.

## Operational reconciliation

When a state-changing request is `uncertain`, verify the external state before choosing a new action. Examples: inspect the Git working tree before reapplying an edit, query the remote API before recreating an object, or check whether a deployment already exists.

If the operation was not applied, submit a **new intentional delegation** with a new ID. Do not erase the uncertain record merely to force automatic replay.

## Scope and limits

The journal provides strong single-host idempotency for gateway-managed execution. It cannot make arbitrary external systems transactional. An executor that needs end-to-end exactly-once semantics should use an external idempotency key or transactional API at the target system as well.

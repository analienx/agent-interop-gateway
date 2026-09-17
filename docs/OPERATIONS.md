# Operations guide

## Install

AIGW requires Python 3.11+. Create a virtual environment and install the project with `pip install -e ".[dev]"` for development or `pip install .` for runtime use.

The default configuration path is `~/.agent-interop-gateway/gateway.toml`. The default durable journal is `~/.agent-interop-gateway/state.sqlite3`.

## Production-ish baseline

1. Generate a token with `aigw token` and inject it using `AIGW_TOKEN`.
2. Keep `host = "127.0.0.1"` unless a remote bridge is genuinely required.
3. Keep `allow_write = false` until read-only operation is proven.
4. Enable only executors you need and give each the minimum capabilities/risk set.
5. Prefer absolute executable paths and explicit cwd roots for structured processes.
6. Place the state database on reliable local storage and back it up if delegation history matters operationally.

## Start and verify

Run the execution-plane preflight before exposing a bridge:

```bash
aigw doctor --config ~/.agent-interop-gateway/gateway.toml
aigw serve
aigw health
```

`aigw doctor` validates configuration, initializes the durable journal, and runs each enabled executor's real readiness probe. `GET /health` checks process liveness. `GET /ready` repeats executor/journal readiness through the running service; use readiness rather than liveness for a supervisor/load balancer decision.

## Environment overrides

Supported gateway overrides include `AIGW_CONFIG`, `AIGW_HOST`, `AIGW_PORT`, `AIGW_TOKEN`, `AIGW_ALLOW_WRITE`, `AIGW_ALLOW_PRIVILEGED`, and `AIGW_STATE_DB`.

Do not store secrets in repository-tracked configuration.

## State database

SQLite uses WAL and `synchronous=FULL`. Keep the database and its WAL/SHM companions on a local filesystem. Do not place one SQLite journal on a generic multi-host network filesystem and assume it provides distributed consensus.
Read-only completed rows are pruned after `result_ttl_seconds`. Write/privileged safety records intentionally have long retention so a crash does not cause the gateway to forget that a side effect may already have happened.

## Capacity

`max_concurrent_delegations` bounds actively executing work. `max_queued_delegations` bounds admitted work waiting behind that semaphore. Executor `max_parallel` adds a per-executor limit.

If submission returns `503` with `Retry-After`, retry transport later with the **same delegation ID**.

## Android development

Install Android Platform Tools, authorize the phone, then use `aigw-chatgpt-android devices` and `probe`. For the native companion, the recommended first topology is ADB reverse to gateway loopback.

Never enable write mode just to test whether the bridge can see transcript text. Prove observation and result delivery with read-only work first.

## Upgrade procedure

1. Stop new delegation admission or wait for current work to finish.
2. Back up the configuration and state database.
3. Upgrade the package/repository.
4. Run `aigw doctor`, then `aigw health` and authenticated `/ready`.
5. Submit a deterministic read-only smoke delegation with a fresh ID and verify expected execution provenance.
6. Only then re-enable external bridges or write policy.

## Incident handling

If a state-changing delegation reports an indeterminate/uncertain outcome, inspect target state before taking further action. Do not delete the journal row as a shortcut.

If Android result injection is uncertain, inspect the conversation before attempting manual delivery. The machine task and UI result delivery are separate operations; do not rerun the machine task merely because the chat message was not confirmed.

If an executor becomes unhealthy, `/ready` reports its probe reason. Disable it in configuration or repair its executable/cwd rather than relying on repeated failures.

## Observability

Current v0.2.0-alpha.1 observability is intentionally compact: structured HTTP status, request IDs, readiness detail, normalized attempt history, executor stdout/stderr bounds, and durable result state. Metrics/tracing exporters are a later extension point; they must not leak conversation content or secrets by default.

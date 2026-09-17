# Architecture

```text
Conversation / voice / IDE / automation
              |
              | AIGW/1
              v
       +----------------+
       | Bridge / relay |
       +----------------+
              |
              v
    +---------------------+
    | Agent Interop       |
    | Gateway             |
    | - auth              |
    | - policy            |
    | - capability router |
    +---------------------+
       |       |       |
       v       v       v
     local   session   remote/custom
     agent   agent     executor
       |
       v
  user-controlled machine
```

The **conversation surface** talks with the user. It should not need to stream the desktop for ordinary machine work.

The **bridge** converts whatever the surface exposes into `aigw/1`. A native tool API is ideal; where none exists, a platform-specific bridge can inspect semantic UI state or use explicit OS automation.

The **gateway** authenticates, applies machine-owner policy, and forwards
typed Foundry operations or capability-routed delegations, then normalizes
results. It is not tied to a model provider. It is not a scheduler and not
a model router: it never selects models, accounts, or cost tiers (Cline
Model Optimizer owns routing) and never plans work (Pi/Codex own goals).
New integrations use the typed `foundry/v3` boundary (`POST /v3/jobs:*`,
`GET /v3/jobs/{id}/status|result`, `GET /v3/{bulk-resource}`); `aigw/1`
remains only as a deprecated compatibility surface for the Android
read/relay experiment.

An **executor** does the work. It can be an LLM CLI, deterministic script, headless browser, SSH target, Home Assistant client, or another agent gateway.

## Cost routing

Executor configuration has independent `priority`, `cost_tier`, and `quality_tier` values. This can express policies such as local/free first, then session-pass, then metered only as final fallback without hard-coding vendor names.

## GUI efficiency hierarchy

For inspection and control, bridges should prefer:

1. First-party API / IPC / CLI.
2. Structured application state or logs.
3. OS semantic accessibility/UI automation tree.
4. One-off screenshot for visual-only state.
5. Interactive screen-driving loop only when no lower-cost representation exists.

The Android reference bridge uses step 3 via `uiautomator dump` and exposes screenshots only as a diagnostic fallback.

## Current boundary

v0.1 executes synchronously and keeps result state in memory. Durable job queues, streaming events and distributed gateways are planned after the mobile interoperability path is empirically proven.

The `foundry/v3` adapter ships with a synthetic in-memory Foundry client
for tests and offline use. Binding a live Foundry deployment (shared
SQLite/Postgres state, real native launch/liveness evidence, artifact
verification, approval workflow) is explicit remaining integration work:
the gateway forwards typed operations but does not itself provide native
execution, liveness probes, or artifact stores.

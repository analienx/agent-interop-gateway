# Architecture

This document describes the runtime architecture of Agent Interop Gateway (AIGW), the trust boundaries it introduces, and why the project separates conversation, bridging, policy, and execution.

## System context

```text
┌───────────────────────┐
│ Human                 │
│ voice / chat / IDE    │
└───────────┬───────────┘
            │
            v
┌───────────────────────┐
│ Conversation surface  │  ChatGPT, another assistant, IDE, automation
└───────────┬───────────┘
            │ platform-supported bridge / relay
            v
┌───────────────────────┐
│ AIGW bridge           │  normalize intent -> aigw/1
└───────────┬───────────┘
            │ authenticated HTTP
            v
┌────────────────────────────────────────────┐
│ Agent Interop Gateway                     │
│ auth -> validation -> policy -> journal   │
│            -> router -> executor          │
└───────┬──────────────┬──────────────┬──────┘
        │              │              │
        v              v              v
 local/free agent   session agent   deterministic tool
        │              │              │
        └──────────────┴──────────────┘
                       v
              user-controlled machine
```

The key architectural property is that the **conversation surface is not the execution plane**. It may request work and consume normalized results without maintaining a live graphical control loop.
## Components

### Conversation surface

Owns the human interaction. AIGW does not require a specific provider or model. A surface can expose a first-party tool API, an IPC hook, an OS accessibility representation, or no integration at all.

### Bridge

Adapts a surface into `aigw/1`. Bridges should be thin: detect an explicit delegation, attach origin/capability/risk metadata, choose a stable delegation ID, submit it, and return the normalized result.

Bridges are **not trusted to grant themselves machine privileges**. Gateway and executor policy remain authoritative.

### Gateway

The gateway owns machine-side policy and reliability semantics. The request path is:

```text
authentication
  -> body/schema limits
  -> request fingerprint/idempotency
  -> durable claim
  -> risk/capability policy
  -> route candidates
  -> execute with bounded resources
  -> commit normalized result
```

`/health` answers process liveness. `/ready` additionally checks configured executor probes and the durable journal.

### Durable journal

SQLite in WAL mode stores delegation fingerprints, risk, state, result, owner, lease, and retention metadata. The journal coordinates multiple gateway processes and prevents the same stable delegation from executing twice after reconnect/restart.

Read-only completed entries expire according to `result_ttl_seconds`. State-changing records are retained for a long safety window because forgetting an old write is more dangerous than retaining a small result record.

### Router and executors

The router filters by declared capabilities and executor `allowed_risks`, then orders candidates by the requested policy. Executors perform the actual work and report normalized attempts/results.
## Execution and durability flow

```text
bridge             gateway                 journal                 executor
  | POST id=42        |                       |                        |
  |------------------>| fingerprint           |                        |
  |                   |------ claim --------->|                        |
  |                   |<----- acquired -------|                        |
  |                   | policy + route        |                        |
  |                   |------------------------------ execute -------->|
  |                   |<----------------------------- result ----------|
  |                   |------ complete ------>|                        |
  |                   |<----- committed ------|                        |
  |<-- result + durability=committed ----------|                        |
```

If another gateway already owns the claim, the caller receives a queued/in-progress representation instead of launching a duplicate. If a **read** lease expires, another gateway may reacquire it. If a **write/privileged** lease expires before a committed result exists, the record becomes `uncertain` and automatic replay is blocked.

## Trust boundaries

```text
UNTRUSTED / VARIABLE                 MACHINE-OWNER TRUST DOMAIN
conversation text                    gateway configuration
third-party app UI        ->         bearer token
bridge detection logic               durable journal
network transport                     executor allowlists
                                      OS account permissions
```

A natural-language task is data, not shell syntax. `agent_process` writes it to stdin. `structured_process` accepts argv only under command, cwd, environment, capability, and risk allowlists.

The bearer token authenticates the bridge to the gateway; it does not elevate an executor beyond its configured policy or OS permissions.

## GUI efficiency hierarchy

Bridges should use the cheapest stable representation that can answer the question:

1. First-party API, IPC, CLI, or structured local protocol.
2. Application state, logs, files, DOM, or semantic data model.
3. OS accessibility / UI automation tree.
4. One-off screenshot for genuinely visual state.
5. Interactive screen-driving loop only as the final fallback.

This ordering is architectural, not merely an optimization. Lower layers are usually easier to audit, cheaper to transmit, more deterministic, and less sensitive to pixel/layout changes.
## Android bridge architecture

The repository contains two Android implementations with different purposes.

**ADB diagnostic bridge:** developer-controlled, explicitly paired ADB connection; uses `uiautomator dump` for semantic snapshots and can capture a screenshot only as a diagnostic fallback. It is useful for probing a changing third-party UI but is not the preferred long-running transport.

**Native companion:** an Android `AccessibilityService` scoped to the ChatGPT package. It reacts to accessibility events instead of polling video frames, requires an explicit delegation phrase by default, persists a duplicate-protection ledger, and uses `ACTION_SET_TEXT`/semantic send controls to return a result when exposed.

The companion cannot make a third-party app expose controls that Android does not expose. Therefore ChatGPT Live compatibility is a **runtime compatibility property**, not a gateway guarantee.

## Reference data path: Android + Foundry MCP

The reference local-read implementation deliberately composes existing boundaries instead of making AIGW a machine-inspection framework:

```text
ChatGPT Android text/voice
  -> AccessibilityService bridge
  -> AIGW /v1/delegations
  -> read-risk/capability policy
  -> agent_process: foundry-pi-read
  -> Pi with built-in machine tools disabled
  -> explicit Foundry read-tool allowlist
  -> MCP stdio
  -> project-scoped Foundry runner
```

The Android bridge owns **conversation detection and result return**. AIGW owns **authentication, idempotency, policy, routing, and durability**. Pi owns **natural-language planning across the permitted tool surface**. Foundry owns **project identity, bounded reads/search, Git state, snapshots, and runner authority**.

Remote-desktop control is intentionally outside this normal path. A deployment may retain it as a separate GUI/recovery capability, but ordinary repository inspection should not pay the cost or fragility of a screen-driving loop.

The reference adapter has three independent constraints: AIGW only routes `risk=read` to the executor; Pi has no built-in shell/filesystem tools active; and the MCP client refuses tool names outside its explicit Foundry read allowlist. See `FOUNDRY_MCP.md`.

## Deployment shapes

### Single-machine development

`bridge -> http://127.0.0.1:8765 -> gateway -> local executor`. This is the default and smallest trust surface.

### Tethered Android development

`Android companion -> 127.0.0.1:8765 -> adb reverse -> host gateway`. The phone sees loopback and the bearer token never needs to traverse the LAN.

### Untethered mobile

Use HTTPS through a private VPN/overlay network or an authenticated reverse proxy. Do not expose a write-enabled gateway directly to the public internet.

### Multiple gateway processes

Processes may share the same SQLite journal on one host. Cross-process claims prevent duplicate work. SQLite is intentionally not presented as a distributed multi-host consensus database; multi-host deployments should use one authoritative gateway/journal or a future distributed backend.

## Extension points

New executors implement the executor interface and declare capabilities, risk allowlist, cost/quality tiers, concurrency, and retry policy. New bridges implement `aigw/1` and should keep provider-specific logic outside the gateway core.

See `docs/adr/` for the decisions behind headless-first integration, durable idempotency, and the Android companion design.

# Agent Interop Gateway

**A model-agnostic interoperability layer between conversational AI surfaces and user-controlled executors.**

Agent Interop Gateway (AIGW) lets a conversation delegate work to a machine you control without turning the conversation itself into a continuous remote-desktop session. The project separates the **human interface** from the **execution plane** so voice/chat can stay lightweight while local agents, scripts, CLIs, browsers, or other tools do the work.

> **Project status:** `0.1.1-alpha`. The gateway core, durable delegation semantics, Android companion/emulator coverage, and the Foundry MCP read reference path are implemented. The complete machine-side AIGW -> constrained Pi -> Foundry MCP chain has been exercised; typed and Live ChatGPT Android return behavior still requires acceptance against the current production app on a real device.

## The simple idea

Keep using the normal ChatGPT Android app. When you explicitly delegate something to your computer, AIGW moves only that task onto your machine and brings the result back into the same conversation.

```text
You type or speak in ChatGPT Android
        |
        |  "delegate locally: check the repo"
        v
Android companion
        v
AIGW
        v
constrained local agent
        v
MCP / structured local tools
        v
your machine
        |
        +------ result ------> same ChatGPT conversation
```

**ChatGPT remains the conversation. Your computer becomes an execution capability.** AIGW is not another chatbot and it does not require continuous remote-desktop streaming.

## Best features

- **Normal ChatGPT stays the UI.** Typed ChatGPT works directly; voice is an additional input surface, not a requirement.
- **Explicit local delegation.** Phrases such as `delegate locally: inspect my repo` make the machine boundary obvious and reduce accidental execution.
- **No continuous remote desktop.** Routine inspection uses structured local interfaces; GUI control is a fallback, not the architecture.
- **MCP-first local reads.** The reference Foundry integration exposes project context, file reads, search, Git status, snapshots, and deltas through a read-only MCP surface.
- **Constrained local reasoning.** The reference Pi launcher disables built-in filesystem/shell tools and exposes only the allowlisted Foundry read tools to the model.
- **Same-conversation return.** The Android companion injects a marked local result into the existing ChatGPT conversation and verifies delivery when the semantic UI exposes the needed controls.
- **Durable duplicate protection.** Stable IDs and local/gateway journals prevent a reconnect or UI event from silently executing the same request twice.
- **Model and surface agnostic.** ChatGPT/Android is the reference bridge, but the gateway protocol and executor layer are not tied to one assistant or model vendor.

## Reference implementation: Android -> Foundry MCP

The repository includes a concrete read-only adapter under `scripts/foundry/`:

```text
ChatGPT Android -> Accessibility companion -> AIGW agent_process
    -> constrained Pi -> Foundry MCP stdio -> project-scoped runner/files
```

`pi-read-extension.mjs` registers only the Foundry read allowlist. `run-read-agent.ps1` launches Pi with its built-in machine tools disabled and an explicit tool allowlist. `mcp-client.mjs` rejects any MCP tool outside that read allowlist before it reaches the Foundry server.

This keeps the interoperability layer generic while demonstrating the intended architecture on a real local agent stack. See [`docs/FOUNDRY_MCP.md`](docs/FOUNDRY_MCP.md).

## Why this exists

Modern AI products often have excellent conversational and voice interfaces, while users already have cheaper, faster, or more capable execution paths on their own machines. Repeatedly capturing and driving a GUI is expensive, fragile, and wasteful when a structured path exists.

AIGW defines a small boundary between those worlds:

```text
human <-> conversation surface <-> bridge <-> AIGW <-> executor <-> machine
```

The conversation surface does not need to know which model, vendor, or local runtime eventually performs the task.

## Core design rules

1. **Headless first.** Prefer APIs, CLIs, IPC, logs, structured application state, and semantic UI trees before screenshots or interactive screen control.
2. **Model agnostic.** Executors can be local models, session/pass-based agents, deterministic tools, or remote services.
3. **Fail closed for side effects.** Read work may be retried; state-changing work is never automatically replayed after an indeterminate interruption.
4. **Durable idempotency.** Stable delegation IDs prevent duplicate execution across reconnects and gateway restarts.
5. **Least privilege.** Loopback bind, read-only policy, constrained executors, and explicit capabilities are the defaults.
6. **No private-API dependency.** Provider-specific bridges must use public/OS-supported mechanisms or clearly marked experiments.
7. **Cost-aware routing.** Priority, cost tier, and quality tier are independent so operators can prefer free/local execution before metered fallbacks.

## What is implemented

- `aigw/1` request/result protocol with strict schema validation.
- FastAPI gateway with bearer authentication, request limits, security headers, health/readiness endpoints, and synchronous or asynchronous submission.
- SQLite WAL journal for durable results, execution claims, replay protection, and cross-process coordination.
- Explicit result durability: `volatile`, `committed`, or `uncertain`.
- Capability/risk-aware routing with `local_first`, `lowest_cost`, `quality_first`, and explicit executor selection.
- `agent_process`, `structured_process`, and deterministic `echo` executors.
- Bounded subprocess output, concurrency limits, timeout cleanup, process-tree termination, executor probes, and read-only retries.
- Android ADB diagnostic bridge using semantic UI state before screenshots.
- Native Android AccessibilityService companion using event-driven semantic inspection, encrypted token storage, a durable local relay ledger, and fail-closed result injection.
- Python CI on Linux and Windows across Python 3.11-3.13, Ruff, mypy, pytest, and coverage reporting.
- Android lint/JVM build CI plus hardware-accelerated emulator instrumentation tests.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

# Generate a strong bearer token
aigw token
```

Copy `examples/gateway.toml` to `~/.agent-interop-gateway/gateway.toml`, configure an executor, and set the token through `AIGW_TOKEN` rather than committing it to the file.
Start the gateway:

```bash
export AIGW_TOKEN="..."          # PowerShell: $env:AIGW_TOKEN="..."
aigw serve
```

Then submit a delegation:

```bash
aigw submit "Inspect the repository and explain the failing tests" \
  --capability fs.read --capability git
```

For longer work a bridge can request asynchronous acceptance with `Prefer: respond-async`; the gateway returns `202 Accepted` and the stable delegation ID can be polled at `GET /v1/delegations/{id}`.

## Reliability semantics in one minute

A caller chooses a stable delegation `id`. AIGW fingerprints the rest of the request. Reusing the same ID with the same payload returns the existing result; reusing it with a different payload is rejected.

For **read-only** work, an expired execution claim may be safely reacquired and configured transient failures may be retried. For **write/privileged** work, an expired claim becomes `uncertain`: AIGW refuses to silently run it again because the previous attempt may already have produced a side effect.

A successful response with `durability=committed` means the normalized result was written to the durable journal while the gateway instance still owned the execution claim. See [`docs/RELIABILITY.md`](docs/RELIABILITY.md) for the full state model and failure matrix.

## Android interoperability

There are two Android paths:

- **ADB bridge** — developer/diagnostic tool. It can inspect `uiautomator` semantic state and perform controlled experiments without streaming the screen.
- **Native companion** — preferred runtime direction. It uses Android accessibility events scoped to `com.openai.chatgpt`, requires an explicit delegation phrase by default, and stores its bearer token using Android Keystore-backed AES-GCM encryption.

For a tethered development test, keep the companion URL at `http://127.0.0.1:8765` and run `adb reverse tcp:8765 tcp:8765`; this avoids exposing the token on the LAN.
The Android emulator CI can validate **our companion app**—build, Android Keystore, local ledger, configuration defaults, and instrumentation behavior. It cannot prove the third-party ChatGPT app exposes the same accessibility/composer behavior as a signed-in real phone. That final boundary is deliberately tracked as a real-device compatibility test, not hidden behind a green CI badge.

## Repository map

- `src/agent_interop_gateway/` — protocol models, policy, durable store, router, executors, HTTP API, CLI, and ADB relay.
- `android-companion/` — native Android event-driven bridge.
- `examples/gateway.toml` — hardened configuration reference.
- `docs/ARCHITECTURE.md` — components, data flow, deployment shapes, and trust boundaries.
- `docs/PROTOCOL.md` — `aigw/1` wire contract and asynchronous behavior.
- `docs/RELIABILITY.md` — idempotency, claims, durability, retries, and crash semantics.
- `docs/SECURITY.md` — threat model, authentication, executor isolation, and mobile security.
- `docs/ANDROID_BRIDGE.md` — ADB and AccessibilityService bridge design.
- `docs/FOUNDRY_MCP.md` — concrete Android -> AIGW -> constrained Pi -> Foundry MCP read path.
- `docs/OPERATIONS.md` — installation, deployment, health checks, backups, and incident handling.
- `docs/TESTING.md` — local/CI test strategy, Android emulator scope, and real-device validation.
- `docs/STATUS.md` — capability and interoperability status matrix.
- `docs/adr/` — architecture decision records.

## Non-goals

AIGW does **not** reverse-engineer private ChatGPT APIs, bypass provider subscriptions/quotas, silently grant an AI unrestricted machine access, or treat continuous screen recording as the normal integration path.

## Contributing

The project is Apache-2.0 licensed and intentionally provider-neutral. New bridge or executor adapters should preserve the protocol boundary, declare their capability/risk model, include failure-mode tests, and document any external platform assumptions.

Start with [`CONTRIBUTING.md`](CONTRIBUTING.md), then read the architecture and reliability docs before adding a state-changing executor.

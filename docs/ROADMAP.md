# Roadmap

The roadmap tracks interoperability boundaries, not feature count. AIGW should only advance a milestone when the relevant authority and compatibility boundary is demonstrated end to end.

## v0.1 — gateway foundation (implemented)

- `aigw/1` request/result models and strict validation.
- FastAPI gateway, bearer authentication, limits, and security headers.
- Capability/risk routing and deterministic executor selection.
- Durable SQLite journal, idempotency keys, execution claims, and crash semantics.
- Async submission and polling.
- Generic agent/process executors and bounded subprocess lifecycle.
- Native Android companion plus ADB diagnostic bridge.
- Linux/Windows Python CI and Android emulator CI.

## v0.2 — constrained local execution + real Android acceptance (current)

- First-class `constrained_agent` executor contract.
- Transport/profile provenance and downstream readiness probes.
- `aigw doctor` execution-plane preflight.
- Foundry MCP read profile with Pi built-in machine tools disabled.
- Explicit Foundry MCP read allowlist and real `foundry_status` probe.
- Android explicit-trigger delegation and durable duplicate protection.
- Physical signed-in ChatGPT Android typed round trip.
- Verify exactly-once execution and same-conversation return on a real phone.

### v0.2 exit criteria

The machine-side Foundry path must stay green under `aigw doctor` and CI. A physical Android device must prove that a fresh explicit delegation is detected once, executed once, and injected back once using the current production ChatGPT app. Ordinary conversation must not delegate.

Live voice is not required to call typed interoperability complete; it is a separate runtime compatibility surface.

## v0.3 — typed state-changing authority

- Keep read reasoning and mutation authority as separate executor profiles.
- Add typed Foundry operations without exposing a generic shell.
- Add explicit approval/confirmation semantics at the bridge/gateway boundary.
- Preserve indeterminate-outcome handling for interrupted writes.
- Add structured audit events suitable for operator review.
- Define capability grants per bridge identity rather than one shared mobile identity.

A write-capable profile must not be implemented by broadening `foundry-read-v1`.

## v0.4 — untethered private mobile transport

- Private-overlay/VPN reference deployment.
- Device-specific credentials and revocation.
- Reconnect-safe result delivery without weakening delegation idempotency.
- Operational diagnostics for phone, gateway, executor, and authority-plane failures.
- Physical Android tests across tethered and untethered transports.

## v0.5 — executor and bridge ecosystem

- Additional constrained-agent profiles for other MCP/API authority planes.
- Headless browser and Home Assistant examples with explicit capability models.
- Windows semantic UI bridge for applications without structured interfaces.
- Browser/native-messaging bridge where platform APIs are stable.
- Multi-host gateway design only after an explicit distributed journal/claim model exists.

## Long-term success criterion

A user can converse naturally on the surface they prefer, delegate machine work to an executor selected by explicit policy, and receive a durable normalized result without continuous desktop capture or dependence on a single model vendor.

The system should make three facts inspectable at all times: **what was requested, which authority boundary handled it, and whether the outcome is durably known.**

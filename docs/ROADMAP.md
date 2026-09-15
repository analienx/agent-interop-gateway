# Roadmap

## v0.1 — foundation

- `aigw/1` request/response models.
- FastAPI gateway and bearer auth.
- Risk gates.
- Capability/cost/quality router.
- Agent-process and structured-process executors.
- CLI.
- ADB semantic UI probe/watch/inject tooling.
- Dry-run-first Android relay.
- CI and tests.

## v0.2 — real-device interoperability

- Record ChatGPT Android UI snapshots across text and Live voice states.
- Implement robust transcript delta extraction with role discrimination if exposed.
- Add pluggable trigger interface and local classifier adapter.
- Validate same-conversation result injection.
- Add event-driven polling/backoff to reduce ADB calls.
- Add Unicode-safe Android text channel.

## v0.3 — durable gateway

- SQLite job/result store.
- Async submit + event stream.
- Executor health checks and circuit breakers.
- Per-bridge identities and capability grants.
- Structured audit log with secret redaction.

## v0.4 — executor ecosystem

- Reference adapters for common local/pass-based agent CLIs.
- SSH executor.
- Headless browser executor.
- Home Assistant executor example.
- Remote gateway federation.

## v0.5 — platform-native bridges

- Android companion where justified by stable public APIs.
- Windows semantic UI bridge (UI Automation) for apps without CLI/API surfaces.
- Browser extension/native messaging bridge for web conversation surfaces.

## Success criterion

A user can speak naturally to a conversational model, delegate machine work to whichever executor best matches cost/capability policy, and receive the result back with no continuous desktop capture and no dependency on a single model vendor.

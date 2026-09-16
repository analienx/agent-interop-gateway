# ChatGPT Android Live voice interoperability experiment

## Objective

Keep the ordinary ChatGPT Live conversation as the human interface while delegating selected machine work to AIGW, without converting the session into continuous screen takeover.

This document describes an **experiment**, not an official ChatGPT extension API. AIGW does not use or reverse-engineer private ChatGPT network endpoints.

## What must be proven on a real device

1. **Transcript observability** — a newly spoken user turn becomes visible in Android semantic accessibility state.
2. **Freshness** — the bridge can avoid interpreting old visible transcript history as a new request.
3. **Composer availability** — a supported editable control is available when a local result is ready.
4. **Concurrent result delivery** — injected result text reaches the same active conversation without restarting Live voice.
5. **Confirmation** — the bridge can verify one delivery and avoid duplicate injection.
6. **Latency/cost** — semantic event processing remains materially cheaper than continuous screenshot/video control.

## Safe test sequence

Start read-only and tethered. Configure the companion for `fs.read`, keep write mode disabled, use a fresh bearer token, and connect through `adb reverse tcp:8765 tcp:8765`.

First validate `aigw-chatgpt-android probe` and `watch`. Then enable the native companion and use an explicit harmless phrase such as `delegate locally report the current repository branch`.
Record Android version, ChatGPT app version, whether Live voice is active, semantic probe output with private content removed, delegation detection latency, gateway execution latency, and whether the result marker becomes visible after one send.

## Acceptance criteria

The experiment is successful only if:

- normal detection uses semantic events/tree state, not continuous screenshots;
- service startup does not execute old transcript content;
- an explicit fresh delegation executes exactly once across reconnect/restart tests;
- the returned result reaches the same conversation with no more than one deliberate UI transition;
- an unconfirmed send becomes `injection_uncertain` and is not automatically repeated;
- killing/restarting gateway or companion does not duplicate a state-changing delegation;
- disabling the companion immediately stops delegation behavior.

## GitHub CI versus real-device validation

GitHub Actions can compile/lint the Android app and run instrumentation tests on a hardware-accelerated emulator. That exercises Android Keystore, the companion ledger, parsing, configuration, and framework integration.

It cannot reproduce a production signed-in ChatGPT Live session. A green emulator job therefore means **the bridge implementation runs on Android**, not **ChatGPT Live compatibility is proven**.

## Failure policy

If transcript or composer semantics are unavailable, do not compensate with continuous screen recording. Candidate alternatives are a first-party provider tool interface when available, a synchronized web/session transport, an OS-level assistant interoperability API, or an explicit user-triggered one-action transition.

The gateway/executor architecture remains valid even if the ChatGPT-specific bridge changes; provider-specific behavior is intentionally isolated at the edge.

# Testing strategy

AIGW uses a test pyramid because different layers have different authority and failure modes.

## CI

Python CI covers protocol validation, policy/routing, executor failure handling, durable claims, idempotency, retries, API behavior, and security limits on Linux and Windows.

Android CI covers lint, JVM tests, APK assembly, and emulator instrumentation. Emulator tests can validate our Activity, AccessibilityService configuration, Android Keystore use, SQLite ledger behavior, and Android framework interactions.

## Local integration

Run `aigw doctor --config <profile>` first. It must prove configuration validity, durable journal health, and the real downstream executor probe. A local gateway smoke test should then submit a real `risk=read` request through the configured executor and verify a committed successful result plus expected execution provenance.

The Foundry reference probe calls `foundry_status` through MCP before the reasoning agent is involved. This cleanly separates authority-plane failures from model/provider failures and from Android compatibility failures.

## Physical Android acceptance

A signed-in physical phone is required to validate the boundary AIGW does not control: the current production ChatGPT Android semantic accessibility tree and composer/send behavior.

Typed ChatGPT is the baseline. Test one new explicit `delegate locally: ...` message, verify exactly one local execution and exactly one marked result return, then verify ordinary conversation does not delegate.

Live voice is tested only after typed mode passes because Live can expose a different Android UI/accessibility surface.

See `FOUNDRY_MCP.md` for the concrete reference acceptance procedure.

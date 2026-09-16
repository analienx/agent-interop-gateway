# Capability status

This matrix separates implemented repository behavior from external runtime compatibility.

| Capability | Status | Evidence boundary |
| --- | --- | --- |
| `aigw/1` gateway | Implemented | Python unit/integration CI |
| Durable idempotency / SQLite journal | Implemented | Python reliability tests |
| Read/write risk policy | Implemented | Router/service tests |
| Android companion APK | Implemented | Android lint, JVM and emulator CI |
| Android Keystore + relay ledger | Implemented | Emulator instrumentation tests |
| Typed ChatGPT explicit-trigger detection | Implemented in companion | Requires current ChatGPT app for production acceptance |
| Result injection into ChatGPT composer | Implemented in companion | Requires current ChatGPT app for production acceptance |
| Foundry MCP read adapter | Implemented | Local MCP + AIGW smoke test |
| Constrained Pi read executor | Implemented | Built-in tools disabled; Foundry allowlist only |
| Physical ChatGPT Android typed round trip | Acceptance pending | Real signed-in phone |
| ChatGPT Live voice round trip | Experimental | Real signed-in phone / current Live UI |

Green CI proves the code and Android companion environment under our control. It does not turn a third-party app's current accessibility behavior into a guaranteed API contract.

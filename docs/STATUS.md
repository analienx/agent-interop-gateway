# Capability status

This matrix separates repository behavior, machine-side integration, and third-party runtime compatibility.

| Capability | Status | Evidence boundary |
| --- | --- | --- |
| `aigw/1` gateway | Implemented | Python unit/integration CI |
| Durable idempotency / SQLite journal | Implemented | Python reliability tests |
| Risk/capability router | Implemented | Router/service tests |
| `constrained_agent` executor contract | Implemented | Config/executor tests |
| Transport/profile execution provenance | Implemented | Constrained-agent result tests |
| Downstream readiness probes | Implemented | `aigw doctor` + readiness tests |
| Android companion APK | Implemented | Android lint/JVM/emulator CI |
| Android Keystore + relay ledger | Implemented | Emulator instrumentation tests |
| Typed ChatGPT explicit-trigger detection | Implemented in companion | Current production app still requires physical acceptance |
| Result injection into ChatGPT composer | Implemented in companion | Current production app still requires physical acceptance |
| Foundry MCP read profile | Implemented | Real `foundry_status` probe + AIGW delegation smoke |
| Constrained Pi read reasoning | Implemented | Pi built-ins disabled; Foundry read allowlist only |
| Physical ChatGPT Android typed round trip | Acceptance pending | Signed-in physical phone |
| ChatGPT Live voice round trip | Experimental | Signed-in phone / current Live UI |

Green CI proves code under our control. `aigw doctor` additionally proves the configured machine authority plane is reachable. Neither can guarantee the current semantic accessibility behavior of a third-party Android app; that boundary is deliberately tested on a real device.

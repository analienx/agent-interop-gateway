# ADR 0001: Constrained agents are a first-class read authority boundary

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

AIGW originally represented the Foundry read path as a generic `agent_process` that happened to launch a carefully constrained Pi process. That worked operationally, but the important authority properties lived in scripts and TOML conventions rather than in the gateway's type system and readiness model.

This made two undesirable states possible: a future configuration could accidentally broaden the executor's allowed risk, and `/ready` could report the launcher as available without proving that the downstream MCP authority plane was usable.

## Decision

Introduce `constrained_agent` as a first-class executor type for natural-language reasoning over an explicitly bounded read tool plane.

The gateway validates that every `constrained_agent` is read-only and has an explicit `probe_argv`. The executor reports operator-defined `transport` and `profile` metadata and includes non-authoritative execution provenance in successful results.

The readiness probe must test the real downstream authority plane, not merely the existence of the launcher binary. The Foundry reference profile therefore verifies Pi/profile prerequisites and performs `foundry_status` over MCP.

Write or privileged authority must use a separate executor/profile rather than expanding a constrained read profile.

## Consequences

- The core remains provider- and MCP-server-agnostic; `transport` and `profile` are descriptive strings, not hard-coded Foundry concepts.
- Operators can distinguish process liveness, reasoning-runtime health, and authority-plane health.
- Read-only authority is enforceable at configuration validation rather than relying solely on wrapper scripts.
- Future state-changing integrations require an explicit architectural decision and cannot silently inherit the read profile.
- `agent_process` remains available for generic integrations where these stronger semantics are unnecessary.

## Reference implementation

The first profile is `foundry-read-v1`: AIGW `constrained_agent` -> constrained Pi -> allowlisted Foundry MCP read tools -> project-scoped runner. See `../EXECUTION_MODEL.md` and `../FOUNDRY_MCP.md`.

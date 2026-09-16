# Architecture decisions

The current architecture follows three foundational decisions:

1. **Conversation and execution are separate planes.** A conversational surface delegates bounded work instead of becoming a continuous machine-control runtime.
2. **Headless/semantic interfaces precede pixels.** APIs, MCP, CLI, structured state, and accessibility semantics are preferred before screenshots or interactive remote desktop.
3. **Side effects fail closed.** Durable identity and execution claims allow safe read recovery while ambiguous write execution becomes `uncertain` instead of being replayed automatically.

The detailed rationale and operational consequences are maintained in `ARCHITECTURE.md`, `RELIABILITY.md`, `SECURITY.md`, and `FOUNDRY_MCP.md`. Future decisions that introduce incompatible protocol or trust-boundary changes should receive a numbered ADR here.

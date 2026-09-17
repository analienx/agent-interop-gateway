# Execution model

AIGW separates **conversation intent** from **machine authority**. A bridge may carry a natural-language request, but the gateway decides which declared executor can satisfy that request and which machine boundary that executor is allowed to cross.

The execution model has five independent dimensions:

- **risk** — `read`, `write`, or `privileged`;
- **capabilities** — opaque requirements such as `fs.read`, `git`, or `foundry.read`;
- **routing** — local/cost/quality preference and fallback policy;
- **transport** — how the executor reaches its authority plane, for example `local_mcp`;
- **profile** — an operator-defined contract such as `foundry-read-v1`.

Keeping these dimensions separate prevents a model name, CLI command, or mobile bridge from accidentally becoming an authority boundary.

## Executor families

AIGW currently defines four executor families:

| Type | Input | Intended authority |
| --- | --- | --- |
| `echo` | task text | deterministic demo/test |
| `agent_process` | task on stdin | generic agent process |
| `constrained_agent` | task on stdin | read-only reasoning over an explicit tool plane |
| `structured_process` | explicit argv action | state-capable operator-approved command |

`constrained_agent` is the preferred architecture for local repository and machine inspection. It is not merely an `agent_process` with a different name: configuration validation enforces `allowed_risks = ["read"]` and requires a real readiness probe.

## Constrained-agent contract

A constrained agent must satisfy all of these properties:

1. The gateway admits only `risk=read` requests to it.
2. Its configured capabilities describe the maximum authority it can satisfy.
3. Its runtime disables generic machine tools that sit outside the declared tool plane.
4. Its tool adapter independently allowlists callable operations.
5. Its `probe_argv` validates the actual downstream authority plane, not only the launcher executable.
6. Successful results expose execution metadata so operators can see which transport/profile handled the request.

For the Foundry reference profile this becomes:

```text
natural-language task
  -> constrained Pi process
  -> no Pi built-in filesystem/shell tools
  -> Foundry read-tool allowlist
  -> MCP stdio
  -> project-scoped Foundry runner
```

This is defense in depth. A routing mistake, prompt mistake, or model hallucination does not by itself create write authority.

## Readiness is part of execution

A process existing on disk is not sufficient evidence that an executor is usable. `constrained_agent` therefore requires an explicit probe command.

`GET /ready` returns each executor's type, capabilities, allowed risks, transport, profile, routing tiers, readiness state, and failure reason. `aigw doctor` runs the same checks without starting the HTTP service.

A healthy Foundry profile should therefore report something structurally similar to:

```json
{
  "type": "constrained_agent",
  "transport": "local_mcp",
  "profile": "foundry-read-v1",
  "allowed_risks": ["read"],
  "ready": true
}
```

The probe belongs to the executor profile because only that adapter knows what "ready" means. The Foundry probe verifies Pi/runtime prerequisites and performs a real `foundry_status` call over MCP.

## Routing and fallback

Routing is capability-first. An executor is considered only when the request risk is allowed and every requested capability is declared by that executor. Preference ordering happens after this filter.

Read requests may use fallback because retrying or changing executors does not intentionally create side effects. Write and privileged work use stricter semantics and cannot use failure fallback.

A future write-capable Foundry path should therefore **not** be implemented by broadening `foundry-read-v1`. It should use a separate executor/profile with typed operations, explicit approval semantics, and write-specific idempotency behavior.

## Result observability

A successful constrained-agent result adds an `execution` object to the result payload. It records the executor kind, transport, profile, and declared capabilities. This is operational provenance rather than a security grant: policy was already enforced before execution.

## Android does not own machine authority

The Android companion is a bridge, not an executor. It detects an explicit delegation, packages `aigw/1`, submits it, and returns a normalized result to the conversation. It cannot grant itself filesystem, Git, MCP, shell, or write access.

That distinction is central to the mobile architecture:

```text
ChatGPT Android
  -> Accessibility bridge
  -> authenticated AIGW request
  -> machine-side policy
  -> constrained executor
  -> declared transport/tool plane
```

A compromise or parsing bug in the bridge is therefore limited by gateway policy and executor constraints.

## Reference profiles

`foundry-read-v1` is the first concrete constrained-agent profile. Its deployment is described in `FOUNDRY_MCP.md` and configured by `examples/foundry-read-gateway.toml`.

Additional profiles should document their authority plane, transport, readiness contract, tool allowlist, failure semantics, and whether their results are safe to retry. Provider/model selection is intentionally not part of the profile identity unless it changes those authority semantics.

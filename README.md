# Agent Interop Gateway

**A model-agnostic bridge between conversational AI surfaces and user-controlled local executors.**

The goal is simple: a conversation should be able to hand work to a computer you control **without forcing the conversation itself to become the computer-control runtime**. The gateway keeps expensive, screen-driven interaction out of the critical path and routes delegated work to the best available local or pass-based executor.

> Status: **v0.1 alpha / working gateway core + experimental Android/ADB bridge.** The Android-to-ChatGPT voice path is deliberately marked experimental because ChatGPT does not expose a public mobile tool-invocation interface to third-party software.

## What is implemented

- Neutral `aigw/1` delegation protocol (deprecated; returns `Deprecation: true`).
- Typed `foundry/v3` adapter: forwards explicit Foundry
  prepare/attach/execute/status/cancel/read-result/quarantine with
  idempotency keys, request hashes, policy results, attempt ids, and states,
  plus cursor/paginated bulk reads for projects, jobs, attempts, activity,
  artifacts, approvals, health, and evidence. Prepare carries the optional
  job-bound `artifact_policy` with policy-hash digest semantics; attach
  carries the flat `foundry.artifact/v1` manifest plus a typed
  staged-payload reference/verification handoff (payload bytes never travel
  in JSON, and the gateway runs no fetch/network/package logic). Events and
  results bind `source_digest`, `artifact_digests`, `policy_hash`, and the
  frozen attachment set; execute mirrors the launch-reservation/native-
  identity semantics with stable error mapping. The gateway never selects
  models, accounts, or cost tiers (Cline Model Optimizer owns routing) and
exposes no general shell/filesystem mutation as a v3 tool.
- FastAPI local gateway with bearer-token authentication.
- Capability-based routing with `local_first`, `lowest_cost`, `quality_first`, or explicit executor selection.
- Pluggable executor adapters:
  - `agent_process`: sends natural-language work to any CLI agent over stdin.
  - `structured_process`: executes an explicit argv action without `shell=True`.
  - `echo`: deterministic test/demo executor.
- Failover when an executor is unavailable.
- Risk gates: writes and privileged operations are disabled by default.
- Cross-platform CLI (`aigw`).
- Experimental Android ADB bridge that prefers **semantic UI hierarchy dumps** over screenshots.
- ChatGPT Android probe/injection tooling and a **dry-run-by-default** experimental semantic relay for empirical interoperability testing.
- Tests and GitHub Actions CI on public standard runners.

## Design principles

1. **Headless first.** Prefer APIs, CLI, logs, semantic UI trees and structured automation. Use a screenshot only when semantic state is insufficient; avoid continuous screen takeover.
2. **Model agnostic.** The gateway does not care whether the executor is Pi, Codex, Claude, a local model, a custom script, or something not invented yet.
3. **Cost-aware.** Routing metadata separates operator priority, cost tier and quality tier.
4. **User-controlled machine.** Default bind is `127.0.0.1`; write/privileged execution is opt-in.
5. **No fake integration claims.** ChatGPT mobile voice currently has no public third-party tool API. The Android bridge is an interoperability experiment, not an official ChatGPT plugin.

## Quick start

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"

# Generate a token
set AIGW_TOKEN=$(aigw token)        # cmd syntax differs by shell

# Start the gateway
aigw serve
```

In another terminal:

```bash
aigw health

aigw submit "hello from the conversation" --capability demo
```

For a real executor, copy `examples/gateway.toml` to `~/.agent-interop-gateway/gateway.toml`, enable an `agent_process`, and point its `argv` at a CLI that accepts a task on stdin.

## Delegation example

```json
{
  "protocol": "aigw/1",
  "task": "Inspect the repository and explain why CI is failing.",
  "origin": {"surface": "voice", "conversation_id": "optional"},
  "capabilities": ["fs.read", "git"],
  "risk": "read",
  "routing": {
    "preference": "lowest_cost",
    "allow_fallback": true
  }
}
```

## Android: semantic inspection before screenshots

With Android Platform Tools installed and a device explicitly paired through USB or Wireless debugging:

```bash
aigw-chatgpt-android probe

aigw-chatgpt-android snapshot

aigw-chatgpt-android watch --interval 2
```

`probe` reports whether ChatGPT is foreground, whether an editable message control is exposed, and the semantic text currently visible. `watch` **does not execute anything**; it only proves whether conversation text can be observed without screen capture. Once that is proven, `aigw-android-relay` can be run in dry-run mode and explicitly armed with `--arm` for end-to-end experiments.

If the normal text composer is exposed, a test result can be inserted with:

```bash
aigw-chatgpt-android inject "[local delegation result] build passed"
```

See [`docs/CHATGPT_VOICE_EXPERIMENT.md`](docs/CHATGPT_VOICE_EXPERIMENT.md) before using this path. Live voice UI behavior can change between app versions and must be measured rather than assumed.

## Repository map

- `src/agent_interop_gateway/` – protocol models, routing, gateway, executors and Android bridge.
- `docs/PROTOCOL.md` – wire contract.
- `docs/ARCHITECTURE.md` – components and trust boundaries.
- `docs/ANDROID_BRIDGE.md` – headless Android approach.
- `docs/CHATGPT_VOICE_EXPERIMENT.md` – concrete validation plan for ChatGPT voice interoperability.
- `docs/SECURITY.md` – threat model and safe defaults.
- `docs/ROADMAP.md` – implementation phases.

## Why this exists

Conversational AI products increasingly have excellent voice interfaces, while local computers already have cheaper or more capable execution paths. Coupling the two through repeated GUI capture is wasteful and fragile. Agent Interop Gateway defines a small interoperability boundary so the conversation can remain conversational and the machine can remain an execution environment.

## License

Apache-2.0. Contributions and compatible executor/bridge adapters are welcome.

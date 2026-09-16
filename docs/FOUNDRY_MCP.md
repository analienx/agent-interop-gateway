# Foundry MCP reference integration

This adapter demonstrates the intended AIGW architecture for read-only work on a user-controlled development machine.

```text
ChatGPT Android
  -> native AccessibilityService companion
  -> AIGW HTTP gateway
  -> agent_process executor
  -> constrained Pi reasoning process
  -> Foundry MCP stdio gateway
  -> project-scoped runner / files
  -> normalized result back through AIGW
  -> same ChatGPT conversation
```

The integration is deliberately split at stable boundaries. AIGW does not learn Foundry internals, and Foundry does not need to know anything about ChatGPT or Android.

## Files

- `scripts/foundry/mcp-client.mjs` — minimal MCP stdio client plus hard read allowlist.
- `scripts/foundry/pi-read-extension.mjs` — exposes only the allowlisted Foundry tools to Pi.
- `scripts/foundry/run-read-agent.ps1` — reads the delegated task from stdin and launches constrained Pi.
- `examples/foundry-read-gateway.toml` — AIGW executor configuration example.

## Exposed read surface

The adapter currently allows `foundry_status`, `list_projects`, `project_context`, `read_project_files`, `list_project_tree`, `search_project`, `project_repo_status`, `create_project_snapshot`, and `read_project_delta`.

It intentionally does not expose `submit_typed_operation`, `cancel_operation`, a generic shell, PowerShell, Pi's built-in filesystem tools, or unrestricted filesystem paths.

## Pi constraint model

The launcher uses `--no-builtin-tools`, so Pi cannot use its normal `read`, `bash`/`powershell`, `edit`, `write`, `grep`, `find`, or `ls` tools. It then applies an explicit `--tools` allowlist containing only the Foundry extension tools.

Provider/model selection remains deployment configuration. The adapter does not hard-code a commercial provider contract; `AIGW_PI_AGENT_DIR`, `AIGW_PI_PROVIDER`, and `AIGW_PI_MODEL` can override the local defaults.

The delegated natural-language task is passed to Pi only after AIGW policy has classified the request as `risk=read` and selected the read executor.

## MCP boundary

The MCP client starts the local Foundry stdio launcher for each tool call, sends `initialize` and one `tools/call`, reads the bounded JSON-RPC response, then exits. Per-call process startup is intentional in the first reference implementation: it keeps lifecycle and recovery simple and prevents a stale MCP child from becoming hidden long-lived state.

The client performs its own allowlist check before starting MCP. Foundry performs its independent project/scope checks after the call arrives. These are separate policy layers.

## Android development transport

For a tethered phone, keep AIGW loopback-only and reverse a configurable port:

```bash
adb reverse tcp:8785 tcp:8785
```

Then configure the companion with `http://127.0.0.1:8785`, READ-ONLY mode, and capability `fs.read`. Loopback/ADB reverse requires no LAN exposure.

For an untethered phone, use HTTPS through a private VPN/overlay or another authenticated private transport. Plain LAN HTTP exists only as an explicit development opt-in.

## Physical-phone acceptance test

1. Start AIGW with the Foundry read executor and verify `/health` plus one CLI delegation.
2. Connect or pair the Android phone with ADB and verify it appears in `adb devices -l`.
3. Install the debug companion APK and configure the reversed loopback URL.
4. Enable the AIGW AccessibilityService in Android settings.
5. Open the normal ChatGPT Android text conversation and establish the companion baseline.
6. Send a new message such as `delegate locally: list the readable Foundry projects`.
7. Confirm exactly one AIGW delegation is recorded and the result is injected once into the same conversation.
8. Repeat with a project-specific read such as repository status or a bounded search.
9. Confirm ordinary text without an explicit delegation trigger does not execute locally.

Voice/Live mode is a separate compatibility test. Typed ChatGPT is the baseline interoperability path because submitted text is already represented in the normal semantic UI.

## Current reference milestone

The machine-side path has been exercised as a real delegation: AIGW selected `foundry-pi-read`, constrained Pi called the local Foundry MCP read plane, and AIGW returned a committed successful result. The remaining acceptance boundary is the current production ChatGPT Android UI on a physical device.

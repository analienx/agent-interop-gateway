# AIGW/1 protocol

`aigw/1` is a small JSON contract between a conversational surface/bridge and a user-controlled gateway.

## Request

`POST /v1/delegations`

```json
{
  "protocol": "aigw/1",
  "id": "uuid-or-caller-id",
  "task": "Natural-language work description",
  "origin": {"surface": "chatgpt-android-voice"},
  "capabilities": ["fs.read", "git"],
  "risk": "read",
  "routing": {
    "preference": "local_first",
    "executor": null,
    "allow_fallback": true
  },
  "timeout_seconds": 120,
  "metadata": {}
}
```

Risk values are `read`, `write`, and `privileged`. The reference gateway rejects `write` and `privileged` requests unless the machine owner enables those categories server-side.

Capabilities are opaque strings matched as a required subset of executor capabilities. Recommended conventions include `fs.read`, `fs.write`, `git`, `github`, `shell`, `browser.read`, `browser.write`, `android.ui.read`, and `android.ui.write`.

A bridge may submit a structured `process` action with `argv`, `cwd`, and explicitly allowlisted environment keys. The reference executor uses direct argv execution rather than a shell string, and the configured executor must allowlist the command.

## Response

```json
{
  "protocol": "aigw/1",
  "delegation_id": "...",
  "state": "succeeded",
  "executor": "local-agent",
  "exit_code": 0,
  "stdout": "...",
  "stderr": "",
  "payload": {},
  "error": null
}
```

States are `queued`, `running`, `succeeded`, `failed`, and `rejected`.

## Routing

`local_first` uses operator priority first, then cost and quality. `lowest_cost` ranks by cost tier, then quality. `quality_first` ranks by quality tier, then cost. `specific` is intended for explicit executor selection.

Cost and quality are relative integers rather than vendor/model names, keeping the protocol independent of model pricing and product changes.

## Authentication

If `AIGW_TOKEN` or `[gateway].token` is configured, protected routes require `Authorization: Bearer <token>`. The health endpoint remains unauthenticated so local supervisors can check liveness without receiving execution privileges.

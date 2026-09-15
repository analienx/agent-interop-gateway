# Security model

Agent Interop Gateway gives conversational systems access to a machine you control. The safe default is deliberately restrictive.

## Defaults

- Binds to `127.0.0.1`.
- Write delegations are disabled.
- Privileged delegations are disabled.
- Executor commands use direct argv execution, not `shell=True`.
- Structured commands require an executor-side allowlist; caller-provided environment keys are denied unless allowlisted.
- Android control requires an already-authorized ADB device.
- No reverse-engineered private service APIs are included.

## Authentication

Generate a bearer token with `aigw token`, then set `AIGW_TOKEN` in the gateway environment and provide it only to trusted bridges.

The CLI refuses a non-loopback bind without a configured token. For remote use, place the gateway behind a private VPN/overlay network or mutually authenticated reverse proxy rather than exposing it directly to the public internet.

## Risk gates

`risk=write` requires `allow_write=true` server-side. `risk=privileged` requires `allow_privileged=true` server-side. These are machine-owner policy controls, not caller assertions.

Future versions should add per-capability grants, signed bridge identities and interactive approvals for high-risk actions.

## Agent process adapter

`agent_process` feeds the natural-language task to a configured executable through stdin. It does not interpolate the task into argv and does not invoke a shell. The configured agent itself may still possess broad machine permissions; run it under the least-privileged OS account that can do the required work.

## Structured process adapter

`structured_process` executes caller-provided argv directly only when the executable is allowlisted. Environment overrides are separately allowlisted. Do not enable it for untrusted bridges.

## Android

ADB is a high-trust debugging channel. Pair only machines you trust and revoke Wireless debugging pairings you no longer use.

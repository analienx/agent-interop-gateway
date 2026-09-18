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

The same bearer token guards `/v1/*` and `/v3/*`. There is no separate
v3 credential, scope split, or per-route token: authentication stays
server-side and uniform. Approval workflows (`approve_action` /
`reject_action` semantics) belong to the Foundry deployment behind the
adapter boundary, not to the gateway.

The CLI refuses a non-loopback bind without a configured token. For remote use, place the gateway behind a private VPN/overlay network or mutually authenticated reverse proxy rather than exposing it directly to the public internet.

## Risk gates

`risk=write` requires `allow_write=true` server-side. `risk=privileged` requires `allow_privileged=true` server-side. These are machine-owner policy controls, not caller assertions.

Future versions should add per-capability grants, signed bridge identities and interactive approvals for high-risk actions.

## Agent process adapter

`agent_process` feeds the natural-language task to a configured executable through stdin. It does not interpolate the task into argv and does not invoke a shell. The configured agent itself may still possess broad machine permissions; run it under the least-privileged OS account that can do the required work.

## Structured process adapter

`structured_process` executes caller-provided argv directly only when the executable is allowlisted. Environment overrides are separately allowlisted. Do not enable it for untrusted bridges.

## Foundry v3 adapter policy

- The v3 write set is closed: prepare, attach, execute, cancel,
  quarantine. General shell or filesystem mutation is not a v3 tool and
  must not be added as one.
- The v3 path carries no model, cost-tier, or account fields. Submissions
  containing them are rejected so routing ownership cannot drift back
  into the gateway.
- Every v3 write echoes the request hash, the adapter policy decision,
  the attempt id, and the resulting state, so callers can audit what was
  forwarded versus decided elsewhere.
- Attach accepts only an **authenticated** typed deployment attachment
  receipt. Plan hashes and per-step commitments hash only public manifest
  inputs, so they are never accepted as proof on their own: an injected
  `ReceiptVerifier` must authenticate the receipt either by verifying an
  issuer MAC against an operator-configured trusted issuer store, or by
  resolving an opaque server-side deployment receipt whose statement must
  equal the claim exactly. The authenticated statement binds job id,
  generation, artifact digest, staged ref, a separate immutable read-only
  mount handle (never derived from the CAS/staging ref), verifier
  identity, the canonical hash of the complete normalized
  `verify_commands` plan, ordered per-step verified-step commitments
  covering every named profile and structured argv step, and the
  issuance/expiry/replay-domain triple (24-hour maximum receipt lifetime).
  Partial, reordered, mismatched, tampered, expired, cross-job replayed,
  or unknown-issuer receipts quarantine the job; arbitrary evidence text
  and CAS refs are never accepted as proof. The gateway owns no signing
  secrets and never fabricates mount points, verification outcomes, or
  receipts; with no trusted verifier configured, attach fails closed
  (`503`) until a trust store is injected.
- Attachment evidence is immutable: exactly one receipt row exists per
  (`job_id`, `generation`, `artifact_digest`). Replaying the identical
  signed receipt under a new idempotency key returns the original
  response without mutation; a conflicting signed receipt is rejected
  (`409`, `receipt_conflict`) without appending or replacing evidence and
  quarantines the job under the explicit conflict policy.
- `aigw/1` responses carry `Deprecation: true`. The Android ADB bridge
  and relay tooling are untouched by the v3 path.

## Android

ADB is a high-trust debugging channel. Pair only machines you trust and revoke Wireless debugging pairings you no longer use.

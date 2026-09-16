# Security model

AIGW gives a conversational system a path to a machine you control. The primary security objective is to make that path **explicit, authenticated, capability-limited, and recoverable without silently duplicating side effects**.

## Threat model

Assume conversation text may contain untrusted instructions, a third-party UI may change unexpectedly, bridges may misclassify text, network traffic may be observable outside loopback, executors may have bugs, and a gateway process may crash mid-task.

AIGW does not assume the caller is entitled to every capability it requests. Machine-owner configuration is authoritative.

## Safe defaults

- Bind to `127.0.0.1`.
- Read-only gateway policy.
- Privileged work disabled.
- Durable SQLite journal enabled by default.
- Direct argv execution; never `shell=True`.
- Minimal inherited environment unless explicitly enabled.
- Executor risk/capability filters.
- Bare PATH lookup disabled for structured commands unless explicitly allowed.
- Request-supplied cwd restricted to configured roots.
- Bounded request/task/metadata/output sizes and bounded concurrency.
- Explicit mobile delegation phrase; natural-language broad heuristics are opt-in.

## Authentication

Generate a token with `aigw token`. Tokens shorter than 24 characters are rejected when configured. A token is mandatory for non-loopback binds and whenever write or privileged gateway policy is enabled.

Prefer `AIGW_TOKEN` or another secret-injection mechanism rather than committing a token to `gateway.toml`.
## Network exposure

Loopback is the recommended topology. For remote/mobile use, prefer one of:

1. ADB reverse for tethered development.
2. A private VPN/overlay network plus bearer authentication.
3. HTTPS behind a hardened authenticated reverse proxy.

Do not publish a write-enabled AIGW listener directly to the public internet.

## Executor isolation

`agent_process` passes natural-language task text through stdin. It does not interpolate task text into argv. The configured agent can still be powerful; run it as the least-privileged OS account that can do the required work.

`structured_process` is stricter: the executable must be allowlisted, absolute paths are preferred, PATH lookup is opt-in, caller environment variables require allowlisting, and caller cwd must remain under configured roots.

An executor's `allowed_risks` is independent of the gateway-wide risk gate. Enabling gateway writes does not make a read-only executor writable.

## Retry safety

Automatic retries and failure fallback are limited to `risk=read`. If a state-changing execution becomes indeterminate, the durable record blocks blind replay. This is a security property as well as a reliability property because duplicate writes can be destructive.

## Android companion

The AccessibilityService is package-scoped to `com.openai.chatgpt` and does not request screen-capture permission. It consumes semantic accessibility events, not video.

The bearer token is encrypted with an AES-GCM key held in Android Keystore. The configuration activity sets `FLAG_SECURE` so screenshots/screen recording of the token UI are blocked by Android where supported.

The companion refuses plain HTTP to non-loopback hosts unless the user explicitly opts into insecure LAN transport. Write mode requires an explicit confirmation and a token.
## Third-party UI ambiguity

AIGW never treats a changing third-party UI as a stable security boundary. The bridge must verify foreground package and semantic controls before injection, refuse to overwrite a user's existing draft, and mark delivery as uncertain if a send action cannot be confirmed.

The ADB path restricts text injection to conservative ASCII because `adb shell input text` crosses a remote shell boundary. Arbitrary Unicode result injection is delegated to the native Android companion using `ACTION_SET_TEXT`.

## Secrets and logs

Do not place bearer tokens, provider API keys, passwords, or private conversation content into repository examples, CI logs, issue bodies, or debug snapshots. Readiness output intentionally reports executor availability without dumping executor environment.

Durable results may contain executor stdout/stderr. Operators should place the state database on storage appropriate for the sensitivity of delegated work and apply OS filesystem permissions accordingly.

## Out of scope

The project does not bypass Android sandboxing, reverse-engineer private provider APIs, circumvent subscription or quota controls, or promise isolation from a malicious executor already running with the user's OS permissions.

Security issues should be reported according to the root `SECURITY.md` disclosure instructions rather than opened with exploit details in a public issue.

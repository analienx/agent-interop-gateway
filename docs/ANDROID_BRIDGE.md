# Android bridge

AIGW has two Android bridge implementations: an ADB-based diagnostic path and a native AccessibilityService companion. Both are designed around semantic UI state rather than continuous screen capture.

## Why semantic UI first

A video/screenshot control loop repeatedly transmits pixels, asks a model to rediscover UI structure, and is sensitive to animation/layout changes. Android already exposes structured information such as text, editable state, clickability, resource IDs, focus, and bounds. AIGW uses that representation when available.

## ADB diagnostic bridge

The Python `aigw-chatgpt-android` command is intended for development and compatibility probing on an explicitly authorized device.

```bash
aigw-chatgpt-android devices
aigw-chatgpt-android probe
aigw-chatgpt-android snapshot
aigw-chatgpt-android watch --interval 1.5
```

`probe` checks the foreground package, semantic editor/send exposure, and visible text. `watch` is observation-only. Screenshots are available only as an explicit diagnostic fallback.

ADB injection validates the foreground package, chooses enabled semantic controls, refuses unsupported arbitrary text, sends once, then tries to confirm the result. If delivery cannot be confirmed it raises an uncertain-delivery condition rather than sending again.

## Native companion

`android-companion/` contains an Android app with an AccessibilityService scoped to `com.openai.chatgpt`. The service is event-driven: content/window changes schedule a throttled semantic scan instead of polling frames.

By default only explicit phrases such as `delegate locally ...` or `local delegate ...` are executable. This dramatically reduces accidental delegation from assistant-generated text or ordinary conversation.
## Companion execution flow

```text
ChatGPT accessibility event
  -> verify ChatGPT foreground/root
  -> establish startup baseline
  -> scan visible semantic text
  -> parse explicit delegation
  -> durable local fingerprint/ledger claim
  -> POST stable ID with Prefer: respond-async
  -> poll gateway result
  -> wait for exposed empty composer
  -> ACTION_SET_TEXT
  -> semantic send/IME action
  -> verify unique result marker
  -> mark injected OR injection_uncertain
```

The startup baseline is important: enabling/restarting the service must not execute an old delegation phrase already visible in transcript history.

Delegation detection is transcript-only: text inside an editable ChatGPT composer is ignored. Typing `delegate locally ...` is therefore inert until the message has actually been submitted and appears in the conversation transcript.

The configuration activity deliberately separates **execution-plane diagnostics** from **conversation-surface diagnostics**. A failed gateway test means transport/auth/executor readiness must be fixed first; a passing gateway test followed by a failed ChatGPT round trip isolates the problem to the current accessibility/composer compatibility boundary.

Before result injection the companion checks that the editor is empty so it cannot overwrite a draft the user is typing. Once a send action has been issued, failure to observe the unique result marker becomes `injection_uncertain`; automatic resend is blocked.

## Networking

For development, reverse the same port configured in the companion. The generic gateway default is `8765`; the Foundry reference deployment uses `8785`:

```bash
adb reverse tcp:8785 tcp:8785
```

Set the companion gateway URL to `http://127.0.0.1:8785`. Before enabling Accessibility, tap **Test gateway + executor**. The companion calls `/health` and authenticated `/ready`; a passing diagnostic proves phone-to-gateway transport, authentication, durable-journal readiness, and at least one usable executor without involving ChatGPT UI automation.

For untethered use, prefer HTTPS through a VPN/overlay or reverse proxy. Plain LAN HTTP is an explicit unsafe opt-in.

## Secrets

The companion encrypts the bearer token with AES-GCM using an Android Keystore key. Configuration screenshots are blocked with `FLAG_SECURE`. The app intentionally has no broad storage permission.
## What GitHub Android CI can test

GitHub-hosted Linux runners support Android SDK hardware acceleration, so this repository runs a real Android emulator in CI. The emulator suite validates the companion APK, Android framework integration, Keystore token round-tripping, SQLite relay persistence, and instrumentation-level behavior.

It **cannot** establish production compatibility with ChatGPT Live because CI does not have a signed-in production ChatGPT app/session. That boundary needs a real-device compatibility run against the installed app version.

The workflow intentionally boots the emulator directly instead of depending on a UI-testing SaaS. Emulator diagnostics (`logcat`, activity dump, emulator log) are uploaded on failure.

## Compatibility contract

A successful real-device ChatGPT bridge requires all of the following:

1. spoken/user transcript text becomes visible to Android semantic accessibility state;
2. the bridge can distinguish a new explicit delegation from startup/history content;
3. an editable composer or equivalent supported text-entry path is available when returning the result;
4. the send operation can be confirmed without repeatedly driving the screen;
5. ChatGPT Live continues the same conversation after injected text.

If any item fails, the bridge must fail closed. The correct response is to change transport—not to add an uncontrolled pixel-driving loop.

See `CHATGPT_VOICE_EXPERIMENT.md` for the real-device validation procedure and `TESTING.md` for the CI/physical-device test pyramid.

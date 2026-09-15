# Android bridge

The reference Android bridge is deliberately **ADB-first** rather than an AccessibilityService application.

Why: ADB is an explicit device-owner/developer authorization path; `uiautomator dump` exposes semantic UI state as XML at much lower data volume than repeated screenshots; and it works from Windows, Linux and macOS with standard Android Platform Tools.

## Pairing

Enable Developer options and USB or Wireless debugging on a device you control, then pair it using normal Android tooling. The gateway does not attempt to bypass Android authorization.

```bash
adb devices
```

For multiple devices, pass `--serial <serial>` to the Android commands.

## Commands

`aigw-chatgpt-android probe` reports the foreground package, whether ChatGPT is foreground, whether an editable text control/send control is exposed, and human-readable text nodes.

`aigw-chatgpt-android snapshot` emits a compact semantic JSON view. It does not capture video.

`aigw-chatgpt-android watch` polls semantic hierarchy and prints newly visible text. It does not execute delegated work. A conservative natural-language hint detector marks likely delegation phrases only to assist experimentation.

`aigw-chatgpt-android inject "[local delegation result] ..."` inserts text only if the current UI exposes a normal editable composer.

`adb shell input text` has imperfect Unicode support. A future Android companion can add a robust text channel after the end-to-end experiment is proven.

## Experimental relay

After `watch` proves that relevant text is visible, run:

```bash
aigw-android-relay --capability demo
```

This is dry-run mode. To submit matching text to the gateway, explicitly add `--arm`. The relay ignores its own `[local delegation result]` messages to reduce feedback-loop risk.

Role detection is not guaranteed because the ChatGPT Android accessibility schema is not a public contract. Automatic use remains experimental until real-device snapshots establish a reliable discriminator.

## No second always-on microphone

The preferred experiment observes the text/transcript the conversation UI already exposes rather than duplicating audio capture. Modern Android also constrains background microphone foreground services, making a second microphone path a poor default.

## Screenshot fallback

`AndroidAdb.screenshot(path)` exists for states semantic nodes cannot represent. Bridges should use it for a specific diagnostic need rather than a continuous interaction loop.

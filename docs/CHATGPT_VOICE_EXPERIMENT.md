# ChatGPT Android voice interoperability experiment

## Objective

Keep the normal ChatGPT voice conversation as the human interface while delegating selected machine work to a local gateway, without turning the entire session into a screenshot-driven computer-use loop.

## What must be measured

1. **Transcript observability** — while ChatGPT is active, does Android's UI hierarchy expose enough conversation/transcript text to identify a newly spoken request?
2. **Composer availability** — while voice is active, is an editable message field present in the same UI hierarchy?
3. **Concurrent result injection** — if the bridge inserts a text result into that conversation, does the active voice session consume/respond to it without being restarted?
4. **State cost** — can semantic polling be slowed or event-driven enough to remain substantially cheaper than screen capture?

The repository includes `probe`, `snapshot`, `watch` and `inject` specifically to answer these questions on real devices instead of assuming a changing application UI.

## Proposed flow if validation succeeds

```text
User speaks naturally to ChatGPT
        |
        v
ChatGPT UI exposes transcript text
        |
        v
ADB bridge observes NEW semantic text
        |
        +--> local classifier: machine delegation needed?
                  |
                  v yes
             POST AIGW/1
                  |
                  v
            gateway routes executor
                  |
                  v
              normalized result
                  |
                  v
bridge injects [local delegation result] into same conversation
                  |
                  v
ChatGPT incorporates result and continues voice conversation
```

No fixed wake word is architecturally required. A local classifier or an explicit machine-action phrase can trigger delegation. v0.1 uses only conservative heuristics and requires `--arm` for execution.

## Fallbacks

If Live voice does not expose a composer, the bridge must not blindly navigate the UI. Record the probe and test another transport: a synchronized web session, an Android companion/overlay, a future platform-native assistant API, or an explicit one-action UI transition chosen by the user.

If concurrent insertion is ignored by Live voice, the model-agnostic gateway remains useful; only the ChatGPT-specific bridge needs replacement.

## Non-goals

- Reverse-engineering private ChatGPT network APIs.
- Circumventing authentication, subscriptions, quotas or platform controls.
- Continuous screen recording.
- Misrepresenting a general automation service as an accessibility aid.

## First real-device milestone

- `probe` works on the target Android build and current ChatGPT app.
- A spoken user turn appears in semantic UI text soon after transcript availability.
- No screenshot is needed for normal detection.
- A delegation result can return to the active conversation with at most one UI transition.
- Gateway execution remains independent of whichever LLM handled the conversation.

# Agent instructions

This repository exists to make AI-to-machine delegation more interoperable, lower-bandwidth and less vendor-dependent.

When modifying it:

- Prefer APIs, CLI, logs and semantic UI trees over screenshots.
- Never add a continuous screen-capture loop where structured state can solve the problem.
- Keep `aigw/1` model/provider agnostic.
- Keep authentication and execution policy server-side.
- Do not reverse-engineer private ChatGPT service APIs or add quota/subscription bypasses.
- Android automation must require explicit device-owner authorization; ADB is the current reference path.
- New structured executors must have an allowlist or equally strong policy boundary.
- Run `ruff check .` and `pytest` before merging.
- Update docs and tests with behavior changes.

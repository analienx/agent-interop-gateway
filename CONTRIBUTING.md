# Contributing

Contributions are welcome, especially new **standards-compliant, user-authorized** bridges and executor adapters.

## Development

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check .
pytest
```

Please keep provider-specific behavior behind adapters and keep the core `aigw/1` protocol model-agnostic.

For GUI integrations, prefer semantic/structured state over screenshots and avoid continuous screen-driving when a lower-bandwidth interface exists.

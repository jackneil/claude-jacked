# Project Rules

## Environment
- Always run tests with `uv run python -m pytest` — never bare `python -m pytest`. Runtime deps (fastapi, httpx) and dev deps (pytest) are resolved by uv from `[dependency-groups] dev` in pyproject.toml. The system/conda Python won't have them.
- Never use bare `pip install`. Use `uv pip install` for deps or `uv tool install` for CLI tools.

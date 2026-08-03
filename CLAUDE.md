# Project Rules

## Environment
- Always run tests with `uv run python -m pytest` — never bare `python -m pytest`. Runtime deps (fastapi, httpx) are declared in `[project].dependencies`; pytest is in `[dependency-groups] dev` in pyproject.toml. uv resolves both for the project environment. The system/conda Python may not have them.
- Never use bare `pip install`. Use `uv pip install` for deps or `uv tool install` for CLI tools.

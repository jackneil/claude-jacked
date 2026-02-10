## Python-Specific

### Tooling
- Linter: ruff (not flake8/pylint — ruff replaces both, 100x faster).
- Formatter: ruff format (not black — ruff format is drop-in replacement).
- Type checking: mypy or pyright for critical modules.

### Style
- Type hints on all public functions. Internal helpers optional.
- f-strings over .format() and % formatting.
- pathlib over os.path for all file operations.
- Use `from __future__ import annotations` for forward references.

### Patterns
- Pydantic v2 for data models (not v1 — v1 @validator is deprecated).
- Pytest over unittest. Use fixtures, not setUp/tearDown.
- Dataclasses for simple data containers without validation.
- Context managers (with) for resource cleanup.
- List/dict comprehensions over map/filter when readable.

### Avoid
- Mutable default arguments (def f(x=[])). Use None + conditional.
- Global state. Pass dependencies explicitly.
- Star imports (from x import *). Always import specific names.
- Bare except. Catch specific exceptions only.

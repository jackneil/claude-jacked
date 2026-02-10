# Design Guardrails

## Quality Gates
- Run /dc before any commit to verify code quality.
- Run the project linter before pushing. Fix all errors, not just warnings.

## Size Limits
- Files: 300 lines target, 500 lines hard max. Split at that point.
- Functions/methods: 30 lines average, 50 lines max. If longer, extract.
- Classes: 200 lines target, 300 lines max.
- Line length: follow project formatter (ruff default 88, prettier default 80).
- Arguments: 4 max per function. Use a config object/dataclass beyond that.

## Structure
- One concept per file. Don't mix unrelated classes/functions.
- Flat is better than nested. Max 3 levels of indentation in any block.
- No circular imports. If A imports B and B imports A, restructure.
- Keep public API surface small. Prefix internal helpers with _.

## Error Handling
- Never silently swallow exceptions. At minimum, log them.
- Fail fast at boundaries (user input, external APIs). Trust internal code.
- Use specific exception types, not bare except/catch.
- Return early on error conditions — avoid deep nesting.

## Testing
- Every new function gets a test. No exceptions.
- Tests go in tests/ directory, mirroring source structure.
- Use doctest format for simple pure-function tests.
- Mock external dependencies (network, filesystem, databases).
- Test edge cases: empty input, None/null, boundary values.

## Security
- NEVER hardcode secrets, API keys, or credentials.
- Validate all external input. Sanitize before use.
- Use parameterized queries for databases — no string concatenation.
- No eval(), exec(), or dynamic code execution on user input.

## Naming
- Variables/functions: descriptive, lowercase_snake (Python/Rust) or camelCase (JS/Go).
- Boolean variables: prefix with is_, has_, can_, should_.
- Constants: UPPER_SNAKE_CASE.
- No single-letter names except loop counters (i, j) and lambdas (x).

## Git
- Commit messages: imperative mood, <72 chars first line.
- One logical change per commit. Don't mix features with refactors.
- Run linter before pushing. Fix all errors, not just warnings.

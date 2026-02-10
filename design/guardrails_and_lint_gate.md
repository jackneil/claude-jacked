# Guardrails Templates + Pre-PR Lint Gate — Design Document

## Status: Design Complete, Ready for Implementation

**Last Updated**: 2026-02-07
**Target Version**: v0.3.12

---

## Problem Statement

Two gaps in the jacked workflow:

1. **No coding guardrails out of the box.** Users' global CLAUDE.md already says "follow DESIGN_GUARDRAILS.md rules if they exist" — but nobody has a DESIGN_GUARDRAILS.md. There's no easy way to generate one with sensible defaults for a project's language/stack.

2. **No lint enforcement before PRs.** Claude can create PRs with lint errors, style violations, or formatting issues. The `/pr` command delegates directly to `pr-workflow-checker` without checking code quality. There's nothing stopping Claude from running `gh pr create` directly and skipping any quality gates.

---

## Feature 1: Guardrails Templates

### Overview

Guardrails templates are language-specific coding standards files deployed to `~/.claude/jacked-guardrails/` during `jacked install`. A behavioral rule prompts Claude to offer guardrails setup at the start of each session if the project doesn't have a `DESIGN_GUARDRAILS.md` yet. No CLI command needed — Claude handles it conversationally.

The user's global CLAUDE.md already says "follow DESIGN_GUARDRAILS.md rules if they exist." This feature closes the loop by making it trivially easy to get one.

### How It Works

```
┌─────────────────────────────────────────────────────────────┐
│ jacked install                                               │
│   → copies templates to ~/.claude/jacked-guardrails/         │
│   → adds behavioral rule to CLAUDE.md                        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Session start (any project)                                  │
│                                                              │
│ Claude checks: does DESIGN_GUARDRAILS.md exist?              │
│   ├─ YES → read it, follow it, move on                      │
│   └─ NO  → detect language from config files                 │
│            → ask user: "Want me to set up coding guardrails  │
│              for this Python project?"                       │
│            ├─ YES → read base.md + python.md from            │
│            │        ~/.claude/jacked-guardrails/              │
│            │        → write DESIGN_GUARDRAILS.md to project  │
│            └─ NO  → move on, don't ask again this session    │
└─────────────────────────────────────────────────────────────┘
```

### Key Design Choices

- **Always ask first** — never auto-create. User must say yes.
- **Fire-and-forget rule** — the behavioral rule is designed so Claude executes it once at session start and doesn't need to keep it in working memory. Saves context.
- **Separate files per language** — only the relevant template gets read into context. A Python project never loads the Rust guardrails.
- **No CLI command** — Claude handles it via the behavioral rule. Simpler, more discoverable, zero friction.
- **Templates are editable** — once `DESIGN_GUARDRAILS.md` is in the project, the user can customize it freely. Claude follows whatever's there.

### Language Detection

Claude checks project root for config files (in priority order):

| File Found          | Language Detected |
|---------------------|-------------------|
| `pyproject.toml`    | Python            |
| `setup.py`          | Python            |
| `package.json`      | Node/JS/TS        |
| `Cargo.toml`        | Rust              |
| `go.mod`            | Go                |
| (none)              | Base only         |

### Template Deployment

Templates are bundled in the jacked package at `jacked/data/guardrails/` and copied to `~/.claude/jacked-guardrails/` during `jacked install`:

```
~/.claude/jacked-guardrails/
├── base.md         # Language-agnostic rules (~40 lines, always included)
├── python.md       # Python-specific (~25 lines)
├── node.md         # Node/JS/TS-specific (~25 lines)
├── rust.md         # Rust-specific (~20 lines)
└── go.md           # Go-specific (~20 lines)
```

When Claude creates a project's `DESIGN_GUARDRAILS.md`, it concatenates `base.md` + the detected language file. Total output: ~60-65 lines. Concise enough to not bloat context, concrete enough to be useful.

### Behavioral Rule

Added to `jacked/data/rules/jacked_behaviors.md`:

```
- At session start, if no DESIGN_GUARDRAILS.md exists in the project root, detect the
  project language (pyproject.toml → Python, package.json → Node, Cargo.toml → Rust,
  go.mod → Go) and ask the user if they want coding guardrails set up. If yes, read
  ~/.claude/jacked-guardrails/base.md + the language file, combine them, and write
  DESIGN_GUARDRAILS.md to the project root. If the user declines, move on.
```

### Template Content

Based on research: Rule of 30 (DZone), Clean Code (Robert Martin), PEP 8, CodeScene AI coding guardrails metrics, Addy Osmani's 2026 LLM coding workflow, and Claude Code team best practices.

#### base.md (~40 lines — always included)

```markdown
# Design Guardrails

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
```

#### python.md (~25 lines)

```markdown
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
```

#### node.md (~25 lines)

```markdown
## Node/JavaScript/TypeScript-Specific

### Tooling
- TypeScript over plain JavaScript for any non-trivial project.
- Linter: eslint with typescript-eslint plugin.
- Formatter: prettier (let it handle all formatting, don't fight it).
- Package manager: use whatever lockfile exists (package-lock.json/yarn.lock/pnpm-lock.yaml).

### Style
- ESM (import/export) over CommonJS (require/module.exports).
- Async/await over raw Promises over callbacks. Never mix.
- Strict TypeScript: enable strict mode in tsconfig.json.
- Use const by default. let only when mutation needed. Never var.

### Patterns
- Zod for runtime validation of external data (API responses, form input).
- Functional patterns for data transformation (map, filter, reduce).
- Error boundaries in React. try/catch in async functions.
- Environment variables via process.env with validation at startup.

### Avoid
- any type. Use unknown + type narrowing instead.
- Nested ternaries. Use if/else or early returns.
- Default exports (use named exports for better refactoring).
- console.log in production code. Use a structured logger.
```

#### rust.md (~20 lines)

```markdown
## Rust-Specific

### Tooling
- Linter: cargo clippy (run with --all-targets --all-features).
- Formatter: cargo fmt (rustfmt). No arguments needed.
- Check: cargo check before full builds for faster feedback.

### Style
- Use Result<T, E> for recoverable errors, panic! only for unrecoverable.
- Prefer &str over String in function parameters.
- Use derive macros (Debug, Clone, PartialEq) liberally.
- Document public items with /// doc comments.

### Patterns
- Iterators over manual loops when possible.
- ? operator for error propagation. Avoid unwrap() in library code.
- Builder pattern for complex struct construction.
- Enum + match for state machines. Compiler enforces exhaustiveness.

### Avoid
- unwrap() and expect() in library code. Reserve for tests and examples.
- Clone as a first resort. Understand ownership first.
- Unsafe blocks without a safety comment explaining the invariant.
```

#### go.md (~20 lines)

```markdown
## Go-Specific

### Tooling
- Linter: go vet (always) + golangci-lint (recommended).
- Formatter: gofmt (non-negotiable — Go's standard formatter).
- Test: go test ./... with -race flag for race detection.

### Style
- Accept interfaces, return structs.
- Check every error. if err != nil is correctness, not boilerplate.
- Short variable names in small scopes (i, n, err). Descriptive in larger scopes.
- Package names: short, lowercase, no underscores.

### Patterns
- Table-driven tests for comprehensive coverage.
- Context propagation for cancellation and deadlines.
- Functional options pattern for configurable constructors.
- Channels for communication, mutexes for state protection.

### Avoid
- init() functions. Explicit initialization in main or constructors.
- Goroutine leaks. Always ensure goroutines can exit.
- Interface pollution. Define interfaces where they're used, not implemented.
```

### Installation Changes

Add to `jacked install` flow (in `jacked/cli.py`):

```python
# Deploy guardrails templates
guardrails_src = data_dir / "guardrails"
guardrails_dst = claude_dir / "jacked-guardrails"
guardrails_dst.mkdir(exist_ok=True)
for template in guardrails_src.glob("*.md"):
    dst = guardrails_dst / template.name
    if dst.exists() and not force:
        # Don't overwrite user-customized templates
        continue
    shutil.copy2(template, dst)
```

No changes to `jacked uninstall` needed — the templates are harmless if left behind.

---

## Feature 2: Pre-PR Lint Gate

### Overview

A PreToolUse hook that intercepts PR-related commands (`gh pr create`, `git push`) and enforces linting before they execute. Uses a **signal token protocol** to communicate between Claude and the hook without any persistent state.

### Architecture: Signal Token Protocol

The core insight: the hook is stateless, but Claude can embed a signal in the command itself to prove linting was done.

```
┌──────────────────────────────────────────────────────────────┐
│ FIRST ATTEMPT                                                 │
│                                                               │
│ Claude: gh pr create --title "Add feature X"                  │
│    ↓                                                          │
│ Hook: No JACKED_LINTED=1 prefix detected                      │
│    ↓                                                          │
│ Hook returns:                                                 │
│   permissionDecision: "deny"                                  │
│   permissionDecisionReason: "Pre-PR lint required"            │
│   additionalContext: [smart prompt telling Claude what to do] │
│    ↓                                                          │
│ Claude: Reads the prompt, runs ruff/eslint, fixes issues      │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ RETRY WITH SIGNAL                                             │
│                                                               │
│ Claude: JACKED_LINTED=1 gh pr create --title "Add feature X"  │
│    ↓                                                          │
│ Hook: JACKED_LINTED=1 prefix detected                         │
│    ↓                                                          │
│ Hook returns:                                                 │
│   permissionDecision: "allow"                                 │
│    ↓                                                          │
│ Command executes normally                                     │
└──────────────────────────────────────────────────────────────┘
```

### Why This Works

| Concern | How It's Solved |
|---------|-----------------|
| State tracking | Signal is IN the command — hook is 100% stateless |
| Infinite loops | Second attempt includes token → allowed through |
| Race conditions | No temp files, no shared state |
| Intelligence | Claude decides if linting is needed (has conversation context) |
| False positives | `additionalContext` tells Claude to check its history first |

### Command Matching

The hook intercepts commands matching these patterns:

```python
PR_PATTERNS = [
    re.compile(r'(?:^|\s)gh\s+pr\s+create\b'),   # gh pr create
    re.compile(r'(?:^|\s)git\s+push\b'),           # git push (any variant)
]
```

**Matches**: `gh pr create`, `gh pr create --title "foo" --body "bar"`, `git push`, `git push origin main`, `git push -u origin feature`

**Does NOT match**: `gh pr list`, `gh pr view`, `gh pr merge`, `gh issue create`, `git pull`, `git fetch`

### Signal Token Detection

```python
SIGNAL_TOKEN = "JACKED_LINTED=1"

def has_lint_signal(command: str) -> bool:
    """Check if command includes the lint-completed signal."""
    return SIGNAL_TOKEN in command
```

The signal is a shell environment variable prefix. When Claude writes `JACKED_LINTED=1 gh pr create ...`, the shell sets `JACKED_LINTED=1` for that command only, then runs `gh pr create` normally. The env var has no effect on `gh` — it's purely a signal to our hook.

### The additionalContext Prompt

This is the key piece — the prompt injected into Claude's context when a PR command is denied:

```
PRE-PR LINT GATE: This command was blocked because linting verification is required
before creating a PR or pushing code.

INSTRUCTIONS:
1. Detect the project type:
   - pyproject.toml or setup.py → Python → run: ruff check .
   - package.json → JS/TS → run: npx eslint .
   - Cargo.toml → Rust → run: cargo clippy
   - go.mod → Go → run: go vet ./...

2. Run the appropriate linter command.

3. If the linter reports errors:
   - Fix all auto-fixable issues (ruff check --fix, eslint --fix)
   - Review remaining issues and fix what you can
   - If some issues are intentional (noqa, eslint-disable), that's OK

4. Once linting passes (or all remaining issues are intentional):
   - Retry the EXACT same command, but prefix it with JACKED_LINTED=1
   - Example: JACKED_LINTED=1 gh pr create --title "My PR"

5. If you already ran the linter in this session and it passed:
   - Just retry with JACKED_LINTED=1 immediately
   - No need to re-lint

IMPORTANT: The JACKED_LINTED=1 prefix is a shell env var assignment that tells
the lint hook you've verified code quality. It does not affect the command itself.
```

### Hook Output Format

Following the official Claude Code PreToolUse response spec:

**When blocking (no signal):**
```json
{
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Pre-PR lint gate: run your project linter before creating a PR. See additionalContext for instructions.",
        "additionalContext": "[the prompt above]"
    }
}
```

**When allowing (signal present):**
```json
{
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "Lint signal verified"
    }
}
```

**When not a PR command (passthrough):**
```python
sys.exit(0)  # No output — normal permission check applies
```

### Interaction with Security Gatekeeper

The existing security gatekeeper at `jacked/data/hooks/security_gatekeeper.py` already has `"gh pr "` and `"git push"` in its `SAFE_PREFIXES` (line 95-96). Both hooks receive the raw command string on stdin.

**Hook execution order**: Claude Code runs all matching PreToolUse hooks. If ANY hook returns `deny`, the tool call is blocked regardless of what other hooks return.

So:
- Gatekeeper: sees `gh pr create` → auto-approve (safe prefix)
- Lint hook: sees `gh pr create` without signal → deny
- **Result**: denied (deny wins)

After linting:
- Gatekeeper: sees `JACKED_LINTED=1 gh pr create` → gatekeeper strips env vars internally via `ENV_ASSIGN_RE` → sees `gh pr create` → auto-approve
- Lint hook: sees `JACKED_LINTED=1` in command → allow
- **Result**: allowed (both approve)

No conflicts. The hooks are independent and composable.

### Hook Script: `pre_pr_lint.py`

```
jacked/data/hooks/pre_pr_lint.py
```

Standalone Python script (no jacked imports, same constraint as security_gatekeeper.py). Receives JSON on stdin, returns JSON on stdout.

**Structure:**
```python
#!/usr/bin/env python3
"""Pre-PR lint gate hook for Claude Code PreToolUse events.

Intercepts gh pr create and git push commands.
Uses signal token protocol: JACKED_LINTED=1 prefix = lint verified.
"""
import json
import re
import sys

SIGNAL_TOKEN = "JACKED_LINTED=1"

PR_PATTERNS = [
    re.compile(r'(?:^|\s)gh\s+pr\s+create\b'),
    re.compile(r'(?:^|\s)git\s+push\b'),
]

ADDITIONAL_CONTEXT = """..."""  # The prompt from above

def main():
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)  # Fail-open

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")

    # Check if this is a PR-related command
    is_pr_command = any(p.search(command) for p in PR_PATTERNS)
    if not is_pr_command:
        sys.exit(0)  # Not our problem

    # Check for signal token
    if SIGNAL_TOKEN in command:
        # Lint verified — allow through
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Lint signal verified"
            }
        }
    else:
        # No signal — block and instruct
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Pre-PR lint gate: run linter first",
                "additionalContext": ADDITIONAL_CONTEXT
            }
        }

    print(json.dumps(result))
    sys.exit(0)

if __name__ == "__main__":
    main()
```

### Installation

Add a `--lint` flag to `jacked install`:

```bash
jacked install --lint        # Install lint gate hook only
jacked install --security    # Install security gatekeeper only (existing)
jacked install --all-hooks   # Install both
```

The install command registers the hook in `~/.claude/settings.json`:

```json
{
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "python /path/to/pre_pr_lint.py",
                        "timeout": 5
                    }
                ]
            }
        ]
    }
}
```

Short timeout (5s) since this is just regex matching — no network calls, no LLM.

### Belt and Suspenders: `/pr` Agent Enhancement

In addition to the hook (which catches raw `gh pr create` / `git push` commands), also enhance the `/pr` slash command's `pr-workflow-checker` agent to run linting as part of its workflow.

Add a lint check phase to `jacked/data/agents/pr-workflow-checker.md`:

```
### PHASE 1.5: LINT CHECK (before PR creation)
- Detect project type from config files in project root
- Run the appropriate linter:
  - Python: ruff check .
  - JS/TS: npx eslint .
  - Rust: cargo clippy
  - Go: go vet ./...
- If errors found: fix auto-fixable issues, report remaining
- If linter not installed: note it but don't block
- Proceed to PR creation only after linting passes
```

This means:
- **`/pr` path**: linting enforced by the agent (clean workflow)
- **Raw `gh pr create` path**: linting enforced by the hook (safety net)
- **Both paths covered**, neither blocks indefinitely

---

## Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `jacked/data/hooks/pre_pr_lint.py` | PreToolUse hook script for lint gate |
| `jacked/data/guardrails/base.md` | Language-agnostic guardrails template |
| `jacked/data/guardrails/python.md` | Python guardrails template |
| `jacked/data/guardrails/node.md` | Node/JS/TS guardrails template |
| `jacked/data/guardrails/rust.md` | Rust guardrails template |
| `jacked/data/guardrails/go.md` | Go guardrails template |
| `tests/unit/test_pre_pr_lint.py` | Lint hook tests |

### Modified Files

| File | Change |
|------|--------|
| `jacked/cli.py` | Add `--lint` flag to `install`, deploy guardrails templates |
| `jacked/data/rules/jacked_behaviors.md` | Add guardrails setup behavioral rule |
| `jacked/data/agents/pr-workflow-checker.md` | Add lint check phase |
| `jacked/data/rules/jacked-reference.md` | Document lint hook and guardrails |
| `jacked/__init__.py` | Version bump to 0.3.12 |
| `pyproject.toml` | Version bump to 0.3.12 |

---

## Testing Strategy

### Pre-PR Lint Hook Tests

All tests use mocked stdin (no real hook invocation):

- `test_non_bash_tool_passthrough` — tool_name != "Bash" → exit 0
- `test_non_pr_command_passthrough` — `npm test` → exit 0
- `test_gh_pr_create_no_signal_denied` — `gh pr create` → deny + context
- `test_gh_pr_create_with_signal_allowed` — `JACKED_LINTED=1 gh pr create` → allow
- `test_git_push_no_signal_denied` — `git push origin main` → deny
- `test_git_push_with_signal_allowed` — `JACKED_LINTED=1 git push` → allow
- `test_gh_pr_list_passthrough` — `gh pr list` → exit 0 (not a create)
- `test_gh_pr_view_passthrough` — `gh pr view 123` → exit 0
- `test_git_pull_passthrough` — `git pull` → exit 0
- `test_malformed_json_failopen` — garbage stdin → exit 0
- `test_empty_command_passthrough` — empty command string → exit 0
- `test_piped_command_with_push` — `ruff check . && git push` → deny
- `test_additional_context_present` — verify deny response includes full prompt
- `test_signal_in_env_var_position` — `JACKED_LINTED=1 SOME=other gh pr create` → allow

### Guardrails Install Tests

- `test_guardrails_templates_deployed` — after install, ~/.claude/jacked-guardrails/ exists with all .md files
- `test_guardrails_no_overwrite_existing` — custom user templates not overwritten without --force
- `test_guardrails_force_overwrites` — --force replaces existing templates
- `test_base_template_content` — base.md contains size limits, error handling, etc.
- `test_python_template_content` — python.md contains ruff, pydantic v2, etc.

---

## Design Decisions

### Why behavioral rule instead of CLI command for guardrails?
A CLI command (`jacked guardrails`) requires the user to know it exists and remember to run it. A behavioral rule triggers automatically at session start — zero friction, maximum adoption. The templates are pre-deployed during `jacked install`, so Claude just reads and combines them. No CLI round-trip needed.

### Why deploy templates to ~/.claude/jacked-guardrails/?
Keeps templates out of the project repo (they're scaffolding, not project code). Using `jacked-guardrails` as the folder name avoids collision with other Claude tools. Templates are always available for any project Claude works on.

### Why separate files per language?
Context efficiency. A Python project never loads the Rust guardrails. Each file is 20-40 lines. Only the relevant ones get read into Claude's context when creating a project's DESIGN_GUARDRAILS.md.

### Why ~60 lines for the combined output?
Research (Claude Code team, shanraisshan/claude-code-best-practice) shows CLAUDE.md loses effectiveness beyond ~150 lines. DESIGN_GUARDRAILS.md at ~60 lines is substantial enough to be useful but concise enough that Claude actually follows every rule. Based on Rule of 30 (DZone), Clean Code (Robert Martin), PEP 8, and CodeScene metrics research.

### Why always ask, never auto-create?
Respects user autonomy. Some projects have their own standards. Some users don't want guardrails. Asking takes one sentence; auto-creating can annoy.

### Why signal token over state tracking for lint gate?
State tracking (temp files, databases, env vars) creates race conditions, stale state, and cleanup problems. The signal token is embedded in the command itself — the hook is completely stateless. Claude communicates "I linted" by including the token. No persistence, no cleanup, no races.

### Why `JACKED_LINTED=1` as the signal?
Shell env var prefix is invisible to the target command (`gh`, `git`). It's a standard shell construct, not a hack. The env var is set for that single command invocation only — it doesn't pollute the session environment.

### Why deny + additionalContext, not just deny + reason?
The `permissionDecisionReason` on deny is shown to Claude but is a simple string. The `additionalContext` field lets us inject a rich, structured prompt with step-by-step instructions. Claude gets both: a short reason (what happened) and detailed context (what to do about it).

### Why not an agent-based hook for lint?
Agent hooks spawn a subagent with tool access (Read, Grep, Glob) and could check the transcript for prior lint runs. But they have 60s default timeout, spawn an entire agent process, and add significant latency. The signal token approach achieves the same result with a <10ms regex check.

### Why enhance /pr AND add a hook?
Belt and suspenders. The `/pr` agent catches the controlled workflow. The hook catches when Claude runs `gh pr create` directly (bypassing `/pr`). Together they close both paths.

### Why block all `git push`, not just to main?
Even pushes to feature branches should have clean lint. The signal token protocol means Claude only needs to lint once per push, and if it already linted, it just adds the prefix and retries immediately.

### Why 5s timeout for the lint hook?
The lint hook does zero network calls and zero LLM evaluation. It's pure regex matching on the command string. 5 seconds is generous — typical execution is <50ms.

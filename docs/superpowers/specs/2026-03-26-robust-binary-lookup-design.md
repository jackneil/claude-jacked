# Robust Binary Lookup Design

**Date:** 2026-03-26
**Status:** Draft
**Problem:** Dashboard upgrade fails on Windows because `shutil.which("uv")` can't find binaries that were installed outside the current shell's PATH.

## Problem

`shutil.which()` only searches `$PATH`. On Windows, Claude Code runs in Git Bash, which has a different PATH than PowerShell/CMD. When `uv` is installed via PowerShell but `jacked webux` runs from Git Bash, the dashboard's upgrade button fails with "uv not found on PATH."

The same class of bug affects every `shutil.which()` call in the codebase — 9 total across 4 files, 4 of which are CRITICAL (block the operation on failure).

## Design

### New module: `jacked/findbin.py`

A single exported function:

```python
def find_bin(name: str) -> str | None
```

**Lookup order:**
1. `shutil.which(name)` — fast path, works when PATH is correct
2. Env var overrides — `UV_TOOL_BIN_DIR`, `XDG_BIN_HOME` (uv respects these)
3. Platform-specific known locations:
   - All platforms: `~/.local/bin/{name}` (uv's default tool bin dir — on Windows `~` expands to `%USERPROFILE%` via `os.path.expanduser`, so this covers `%USERPROFILE%\.local\bin\` automatically)
   - All platforms: `~/.cargo/bin/{name}` (cargo-installed uv)
   - Windows only: `%LOCALAPPDATA%\uv\bin\{name}.exe` (uv's Windows-native bin dir)
   - Windows only: `%LOCALAPPDATA%\Programs\claude\{name}.exe` (Claude Code's native Windows install location)
4. Return `None` if not found

On Windows, automatically appends `.exe` to the probe paths when the name doesn't already end with `.exe`. On Unix, checks `os.access(path, os.X_OK)` to confirm executability.

The module is ~30 lines. No classes, no state, no dependencies beyond stdlib.

### Callers to update

Only the CRITICAL callers that block on failure. Fail-safe callers (git, pgrep, security) already handle not-found gracefully and don't need this.

| File | Line | Current | After |
|------|------|---------|-------|
| `jacked/api/routes/system.py` | 732 | `shutil.which("uv")` | `find_bin("uv")` |
| `jacked/api/routes/system.py` | 739 | `shutil.which("jacked")` | `find_bin("jacked")` |
| `jacked/api/routes/system.py` | 1004 | `shutil.which("claude")` | `find_bin("claude")` |
| `jacked/launch.py` | 480 | `shutil.which("claude")` | `find_bin("claude")` |

4 call sites changed. The gatekeeper's `subprocess.run(["claude", ...])` is intentionally left alone — it already catches `FileNotFoundError` and returns `None`, and it runs inside a Claude Code hook where `claude` is always on PATH.

### What this does NOT do

- Does not modify PATH globally or for subprocesses
- Does not cache results (lookups are infrequent — upgrade is a one-shot operation)
- Does not try to run `uv tool dir --bin` (chicken-and-egg: we need to find `uv` first)
- Does not touch fail-safe callers (git, pgrep, security, python3 fallback chains)

## Testing

Unit tests for `find_bin()`:
- Returns `shutil.which()` result when available (mock)
- Falls back to `~/.local/bin/` when `shutil.which()` returns None (mock + tmp file)
- Appends `.exe` on Windows (mock `sys.platform`)
- Respects `UV_TOOL_BIN_DIR` env var override
- Returns `None` when binary genuinely doesn't exist anywhere

## Decisions

| Question | Answer |
|----------|--------|
| New module or inline? | New module `jacked/findbin.py` — reusable, testable, single responsibility |
| Which callers to update? | Only CRITICAL ones (4 sites). Fail-safe callers are fine as-is. |
| Cache results? | No — lookups are rare and caching adds complexity for no benefit |
| Modify PATH? | No — probing is safer than mutating global state |
| Windows `.exe` handling? | Auto-append `.exe` suffix when `sys.platform == "win32"` |

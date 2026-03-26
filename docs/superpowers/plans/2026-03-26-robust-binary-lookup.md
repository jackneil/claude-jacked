# Robust Binary Lookup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fragile `shutil.which()` calls with a `find_bin()` function that probes known install locations when PATH lookup fails, fixing the Windows dashboard upgrade bug.

**Architecture:** New `jacked/findbin.py` module (~30 lines) with a single `find_bin(name)` function. Four call sites in `system.py` and `launch.py` switch from `shutil.which()` to `find_bin()`.

**Tech Stack:** Python stdlib only (os, sys, shutil, pathlib)

**Spec:** `docs/superpowers/specs/2026-03-26-robust-binary-lookup-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `jacked/findbin.py` | `find_bin()` function — probe PATH then known locations |
| Create | `tests/test_findbin.py` | Unit tests for find_bin |
| Modify | `jacked/api/routes/system.py` | Replace 3 `shutil.which()` calls with `find_bin()` |
| Modify | `jacked/launch.py` | Replace 1 `shutil.which()` call with `find_bin()` |

---

### Task 1: Create `jacked/findbin.py` with tests

**Files:**
- Create: `jacked/findbin.py`
- Create: `tests/test_findbin.py`

- [ ] **Step 1: Write the test file**

```python
"""Tests for jacked.findbin."""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jacked.findbin import find_bin


@pytest.fixture
def fake_bin(tmp_path):
    """Create a fake executable in a temp directory."""
    def _make(name: str) -> Path:
        suffix = ".exe" if sys.platform == "win32" else ""
        p = tmp_path / f"{name}{suffix}"
        p.write_text("fake")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return p
    return _make


def test_finds_via_shutil_which():
    """If shutil.which succeeds, return its result immediately."""
    with patch("jacked.findbin.shutil.which", return_value="/usr/bin/uv"):
        assert find_bin("uv") == "/usr/bin/uv"


def test_falls_back_to_local_bin(fake_bin, tmp_path):
    """When PATH lookup fails, probe ~/.local/bin/."""
    fake_bin("uv")
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(tmp_path)):
        result = find_bin("uv")
        assert result is not None
        assert "uv" in result


def test_respects_uv_tool_bin_dir(fake_bin, tmp_path):
    """UV_TOOL_BIN_DIR env var takes priority over default paths."""
    fake_bin("jacked")
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch.dict(os.environ, {"UV_TOOL_BIN_DIR": str(tmp_path)}), \
         patch("jacked.findbin._home_dir", return_value="/nonexistent"):
        result = find_bin("jacked")
        assert result is not None
        assert "jacked" in result


def test_returns_none_when_not_found():
    """When binary doesn't exist anywhere, return None."""
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value="/nonexistent"):
        assert find_bin("nonexistent_binary_xyz") is None


@patch("jacked.findbin.sys")
def test_appends_exe_on_windows(mock_sys, fake_bin, tmp_path):
    """On Windows, probe paths should use .exe suffix."""
    mock_sys.platform = "win32"
    p = tmp_path / "uv.exe"
    p.write_text("fake")
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(tmp_path)):
        result = find_bin("uv")
        # Should find it if the .exe probing logic works
        # (exact assertion depends on platform mock fidelity)
```

- [ ] **Step 2: Run tests to verify they fail (module doesn't exist yet)**

Run: `uv run python -m pytest tests/test_findbin.py -v`
Expected: ImportError — `jacked.findbin` doesn't exist yet

- [ ] **Step 3: Write the implementation**

```python
"""Robust binary lookup with fallback to known install locations.

shutil.which() only searches PATH, which may be incomplete when the
dashboard server runs in a shell (e.g., Git Bash on Windows) that
doesn't inherit paths from other shells (e.g., PowerShell).

Usage:
    from jacked.findbin import find_bin
    uv = find_bin("uv")  # returns full path or None
"""

import os
import shutil
import sys
from pathlib import Path


def _home_dir() -> str:
    """Return the user's home directory. Extracted for testability."""
    return str(Path.home())


def find_bin(name: str) -> str | None:
    """Find a binary by name, searching PATH then known install locations."""
    found = shutil.which(name)
    if found:
        return found

    is_win = sys.platform == "win32"
    suffix = ".exe" if is_win and not name.endswith(".exe") else ""
    target = f"{name}{suffix}"
    home = _home_dir()

    # Build candidate list: env overrides first, then platform defaults.
    candidates: list[str] = []

    uv_tool_bin = os.environ.get("UV_TOOL_BIN_DIR")
    if uv_tool_bin:
        candidates.append(os.path.join(uv_tool_bin, target))

    xdg_bin = os.environ.get("XDG_BIN_HOME")
    if xdg_bin:
        candidates.append(os.path.join(xdg_bin, target))

    # All platforms: uv's default tool bin dir
    candidates.append(os.path.join(home, ".local", "bin", target))

    # All platforms: cargo bin (uv can be installed via cargo)
    candidates.append(os.path.join(home, ".cargo", "bin", target))

    if is_win:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            # uv's Windows-native bin dir
            candidates.append(os.path.join(local_app, "uv", "bin", target))
            # Claude Code's native Windows install
            candidates.append(os.path.join(local_app, "Programs", "claude", target))

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_findbin.py -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
git add jacked/findbin.py tests/test_findbin.py
git commit -m "feat: add find_bin() for robust binary lookup across platforms

Probes PATH first, then known install locations (~/.local/bin,
~/.cargo/bin, %LOCALAPPDATA%\uv\bin) to fix dashboard upgrade
failing on Windows when uv is not on Git Bash's PATH."
```

---

### Task 2: Update call sites

**Files:**
- Modify: `jacked/api/routes/system.py`
- Modify: `jacked/launch.py`

- [ ] **Step 1: Update system.py — upgrade flow (line 732, 739)**

Replace:
```python
uv_bin = shutil.which("uv")
```
With:
```python
uv_bin = find_bin("uv")
```

Replace:
```python
jacked_bin = shutil.which("jacked")
```
With:
```python
jacked_bin = find_bin("jacked")
```

Add import at top of file:
```python
from jacked.findbin import find_bin
```

- [ ] **Step 2: Update system.py — claude diagnostic (line 1004)**

Replace:
```python
cli_available = shutil.which("claude") is not None
```
With:
```python
cli_available = find_bin("claude") is not None
```

- [ ] **Step 3: Update launch.py (line 480)**

Replace the `shutil.which("claude")` call with `find_bin("claude")`.

Add import:
```python
from jacked.findbin import find_bin
```

- [ ] **Step 4: Run full test suite**

Run: `uv run python -m pytest tests/ -v --tb=short`
Expected: All tests pass including new findbin tests

- [ ] **Step 5: Commit**

```bash
git add jacked/api/routes/system.py jacked/launch.py
git commit -m "fix: use find_bin() for uv/jacked/claude lookups

Fixes dashboard upgrade failing on Windows when uv is installed
via PowerShell but jacked webux runs from Git Bash."
```

---

### Task 3: Verify end-to-end

- [ ] **Step 1: Verify find_bin resolves uv**

```bash
uv run python -c "from jacked.findbin import find_bin; print(find_bin('uv'))"
```
Expected: prints the full path to uv

- [ ] **Step 2: Verify find_bin resolves jacked**

```bash
uv run python -c "from jacked.findbin import find_bin; print(find_bin('jacked'))"
```
Expected: prints the full path to jacked

- [ ] **Step 3: Verify find_bin resolves claude**

```bash
uv run python -c "from jacked.findbin import find_bin; print(find_bin('claude'))"
```
Expected: prints the full path to claude

- [ ] **Step 4: Reinstall and verify dashboard still works**

```bash
jacked install --force 2>&1 | tail -3
```
Expected: install completes successfully

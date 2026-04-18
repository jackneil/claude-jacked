# Install-method safety + tray-update progress UX — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 0.41.19 — refuse auto-upgrade on editable/pip installs (close the dev-clone "No module named pip" crash), and give users a cross-platform browser-based progress page when they click Update in the tray.

**Architecture:** Install-method detector gains a fourth category (`editable`) and a `can_auto_upgrade()` gate. CLI + tray pre-flight against the gate. The updater writes `~/.claude/jacked-update-status.json` atomically at every phase transition (both POSIX Python updater and Windows cmd.exe batch, the latter via a new hidden `jacked _update_status` CLI shim). A standalone `update.html` page loads from the current live service right before the tray kills itself, then polls `/api/update/status` + `/api/version` to narrate the upgrade and detect completion — works even as the service is torn down and recreated.

**Tech Stack:** Python 3.10+ / Click / FastAPI / pystray / vanilla JS + HTML. No new runtime dependencies.

---

## File Structure

**New files:**
- `jacked/service/update_status.py` — atomic status-file reader/writer helpers
- `jacked/data/web/update.html` — standalone update progress page (inline CSS + JS, no framework, safe DOM construction only)
- `tests/unit/service/test_update_status.py`
- `tests/unit/test_install_method_editable.py`

**Modified files:**
- `jacked/install_method.py` — add `editable` detection + `can_auto_upgrade()`
- `jacked/cli.py` — `upgrade` pre-flight; add hidden `_update_status` + `_update_status_init` commands
- `jacked/service/tray.py` — `_on_update_click` pre-flight + open browser before stop
- `jacked/service/updater.py` — emit status at every phase (POSIX path)
- `jacked/service/updater.py` — emit status via `jacked _update_status` in the Windows batch (inside `_spawn_windows_tray_updater`)
- `jacked/api/routes/system.py` — new `GET /api/update/status` + extend `/api/version` response
- `jacked/__init__.py` — bump to `0.41.19`
- `README.md` — changelog entry
- `tests/unit/service/test_updater.py` — assert status writes + Windows batch shim calls
- `tests/unit/test_upgrade_command.py` — assert CLI refusal for editable/pip

---

## Task 1: Editable-install detection

**Files:**
- Modify: `jacked/install_method.py`
- Test: `tests/unit/test_install_method_editable.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_install_method_editable.py`:

```python
"""Tests for editable-install detection and can_auto_upgrade() gate."""

import sys
from pathlib import Path
from unittest.mock import patch

from jacked.install_method import (
    detect_install_method,
    can_auto_upgrade,
)


class TestDetectEditable:
    def test_detects_pth_editable_marker(self, tmp_path, monkeypatch):
        sp = tmp_path / "site-packages"
        sp.mkdir()
        (sp / "_editable_impl_claude_jacked.pth").write_text("/tmp/repo\n")
        monkeypatch.setattr(sys, "path", [str(sp), *sys.path])
        with patch("sys.executable", str(tmp_path / ".venv" / "bin" / "python3")):
            assert detect_install_method() == "editable"

    def test_detects_setuptools_editable_marker(self, tmp_path, monkeypatch):
        sp = tmp_path / "site-packages"
        sp.mkdir()
        (sp / "__editable__.claude_jacked-0.41.18.pth").write_text("/tmp/repo\n")
        monkeypatch.setattr(sys, "path", [str(sp), *sys.path])
        with patch("sys.executable", str(tmp_path / ".venv" / "bin" / "python3")):
            assert detect_install_method() == "editable"

    def test_uv_tool_still_beats_editable(self, tmp_path, monkeypatch):
        sp = tmp_path / "site-packages"
        sp.mkdir()
        (sp / "_editable_impl_claude_jacked.pth").write_text("/tmp/repo\n")
        monkeypatch.setattr(sys, "path", [str(sp), *sys.path])
        with patch(
            "sys.executable",
            "/home/u/.local/share/uv/tools/claude-jacked/bin/python3",
        ):
            assert detect_install_method() == "uv"


class TestCanAutoUpgrade:
    def test_uv_is_auto_upgradable(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            ok, reason = can_auto_upgrade()
        assert ok is True
        assert reason == ""

    def test_pipx_is_auto_upgradable(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            ok, reason = can_auto_upgrade()
        assert ok is True
        assert reason == ""

    def test_editable_refused_with_git_pull_recovery(self):
        with patch("jacked.install_method.detect_install_method", return_value="editable"):
            ok, reason = can_auto_upgrade()
        assert ok is False
        assert "editable" in reason.lower()
        assert "git pull" in reason
        assert "uv sync" in reason

    def test_pip_refused_recommending_uv(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            ok, reason = can_auto_upgrade()
        assert ok is False
        assert "pip" in reason.lower()
        assert "uv tool install" in reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_install_method_editable.py -v`
Expected: all fail.

- [ ] **Step 3: Implement editable detection + gate**

Edit `jacked/install_method.py`. Replace `detect_install_method()` and append `can_auto_upgrade()`:

```python
def detect_install_method() -> str:
    """Return 'uv', 'pipx', 'editable', or 'pip' based on install markers.

    Detection order: uv -> pipx -> editable -> pip (fallback).
    """
    try:
        exe = Path(sys.executable).resolve()
    except (OSError, RuntimeError):
        return "pip"

    parts_lower = [p.lower() for p in exe.parts]

    for i, part in enumerate(parts_lower):
        if part == "tools" and i > 0 and parts_lower[i - 1] == "uv":
            return "uv"

    for i, part in enumerate(parts_lower):
        if part == "venvs" and i > 0 and parts_lower[i - 1] == "pipx":
            return "pipx"

    # Editable install: look for marker .pth files on sys.path.
    for entry in sys.path:
        if not entry:
            continue
        try:
            d = Path(entry)
            if not d.is_dir():
                continue
            if any(d.glob("_editable_impl_*.pth")):
                return "editable"
            if any(d.glob("__editable__.*.pth")):
                return "editable"
        except (OSError, RuntimeError):
            continue

    return "pip"


def can_auto_upgrade() -> tuple[bool, str]:
    """Return (ok, reason) — is it safe to auto-upgrade this install?

    uv / pipx: True, empty reason.
    editable:  False with a git-pull/uv-sync recovery hint.
    pip:       False with a 'migrate to uv' recovery hint.
    """
    method = detect_install_method()
    if method in ("uv", "pipx"):
        return True, ""
    if method == "editable":
        return (
            False,
            "This is an editable (dev-clone) install — auto-update disabled. "
            "Upgrade manually from the repo: `cd <repo> && git pull && uv sync`.",
        )
    return (
        False,
        "pip install detected — auto-update disabled (uv is the supported "
        "install method). Migrate with: "
        "`uv tool install \"claude-jacked[tray]\"`.",
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_install_method_editable.py tests/unit/test_install_method.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/install_method.py tests/unit/test_install_method_editable.py
git commit -m "feat(install-method): detect editable installs + can_auto_upgrade() gate"
```

---

## Task 2: Update-status file helpers

**Files:**
- Create: `jacked/service/update_status.py`
- Test: `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/service/test_update_status.py`:

```python
"""Tests for the update-status JSON reader/writer."""

import json
import os


def test_init_creates_file_with_metadata(tmp_path):
    from jacked.service.update_status import init_status, read_status
    p = tmp_path / "status.json"
    init_status(p, from_version="0.41.18", to_version="0.41.19", method="uv")
    data = read_status(p)
    assert data["from_version"] == "0.41.18"
    assert data["to_version"] == "0.41.19"
    assert data["method"] == "uv"
    assert data["overall"] == "in_progress"
    assert data["phases"] == []
    assert "started_at" in data


def test_begin_phase_appends_entry(tmp_path):
    from jacked.service.update_status import init_status, begin_phase, read_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    data = read_status(p)
    assert len(data["phases"]) == 1
    assert data["phases"][0]["name"] == "installing_package"
    assert data["phases"][0]["status"] == "in_progress"
    assert data["phases"][0]["finished_at"] is None
    assert data["current_phase"] == "installing_package"


def test_end_phase_ok(tmp_path):
    from jacked.service.update_status import (
        init_status, begin_phase, end_phase, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    end_phase(p, "installing_package", status="ok")
    data = read_status(p)
    assert data["phases"][0]["status"] == "ok"
    assert data["phases"][0]["finished_at"] is not None


def test_end_phase_failure_sets_overall(tmp_path):
    from jacked.service.update_status import (
        init_status, begin_phase, end_phase, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    end_phase(
        p, "installing_package", status="failed",
        error="uv tool install failed", recovery="Re-run: uv tool install ...",
    )
    data = read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "uv tool install failed"
    assert data["recovery"] == "Re-run: uv tool install ..."


def test_mark_succeeded_finalizes_overall(tmp_path):
    from jacked.service.update_status import (
        init_status, mark_succeeded, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    data = read_status(p)
    assert data["overall"] == "succeeded"


def test_read_missing_returns_none(tmp_path):
    from jacked.service.update_status import read_status
    assert read_status(tmp_path / "does-not-exist.json") is None


def test_read_corrupt_returns_none(tmp_path):
    from jacked.service.update_status import read_status
    p = tmp_path / "status.json"
    p.write_text("{not json at all")
    assert read_status(p) is None


def test_write_is_atomic_no_tmp_leftover(tmp_path):
    from jacked.service.update_status import init_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    siblings = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert siblings == []
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: ModuleNotFoundError on `jacked.service.update_status`.

- [ ] **Step 3: Implement the helper module**

Create `jacked/service/update_status.py`:

```python
"""Atomic reader/writer for the update-status JSON file.

Used by the detached POSIX updater, the Windows cmd.exe batch (via the
`jacked _update_status` CLI shim), and the `/api/update/status` endpoint.

Schema: see docs/superpowers/specs/2026-04-18-install-method-and-update-ux-design.md
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jacked.service import CLAUDE_DIR

# The one canonical location. Readers and writers both use this.
UPDATE_STATUS_FILE: Path = CLAUDE_DIR / "jacked-update-status.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_status(path: Path) -> Optional[dict]:
    """Read the status file. Returns None on missing or corrupt."""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def init_status(
    path: Path,
    from_version: str,
    to_version: str,
    method: str,
    log_path: Optional[str] = None,
) -> None:
    data = {
        "started_at": _now_iso(),
        "from_version": from_version,
        "to_version": to_version,
        "method": method,
        "current_phase": None,
        "phases": [],
        "overall": "in_progress",
        "error": None,
        "recovery": None,
        "log_path": log_path,
    }
    _atomic_write(path, data)


def begin_phase(path: Path, phase: str) -> None:
    data = read_status(path) or {}
    phases = data.get("phases", [])
    phases.append({
        "name": phase,
        "started_at": _now_iso(),
        "finished_at": None,
        "status": "in_progress",
    })
    data["phases"] = phases
    data["current_phase"] = phase
    _atomic_write(path, data)


def end_phase(
    path: Path,
    phase: str,
    status: str,
    error: Optional[str] = None,
    recovery: Optional[str] = None,
) -> None:
    data = read_status(path) or {}
    phases = data.get("phases", [])
    for entry in reversed(phases):
        if entry["name"] == phase and entry["status"] == "in_progress":
            entry["status"] = status
            entry["finished_at"] = _now_iso()
            break
    data["phases"] = phases
    if status == "failed":
        data["overall"] = "failed"
        if error:
            data["error"] = error
        if recovery:
            data["recovery"] = recovery
    _atomic_write(path, data)


def mark_succeeded(path: Path) -> None:
    data = read_status(path) or {}
    data["overall"] = "succeeded"
    data["current_phase"] = None
    _atomic_write(path, data)
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/update_status.py tests/unit/service/test_update_status.py
git commit -m "feat(updater): atomic update-status JSON helpers"
```

---

## Task 3: `/api/update/status` endpoint

**Files:**
- Modify: `jacked/api/routes/system.py`
- Test: extend `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/service/test_update_status.py`:

```python
def test_api_endpoint_returns_null_when_no_status_file(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jacked.api.main import create_app
    from jacked.service import update_status as us_mod
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "nope.json")
    app = create_app()
    client = TestClient(app)
    r = client.get("/api/update/status")
    assert r.status_code == 200
    assert r.json() == {"status": None}


def test_api_endpoint_returns_status_content(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from jacked.api.main import create_app
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    app = create_app()
    client = TestClient(app)
    r = client.get("/api/update/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["from_version"] == "a"
    assert body["status"]["current_phase"] == "installing_package"
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v -k api_endpoint`
Expected: 404 or route-not-found.

- [ ] **Step 3: Add the endpoint**

Edit `jacked/api/routes/system.py`, append:

```python
@router.get("/update/status")
async def get_update_status():
    """Return the current update-status JSON, or {status: null} if no update is in flight.

    Used by the /update.html progress page. The page polls this every 1s.
    """
    from jacked.service import update_status as us_mod
    data = us_mod.read_status(us_mod.UPDATE_STATUS_FILE)
    return {"status": data}
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add jacked/api/routes/system.py tests/unit/service/test_update_status.py
git commit -m "feat(api): GET /api/update/status endpoint"
```

---

## Task 4: Hidden `jacked _update_status` + `_update_status_init` CLI shims

**Files:**
- Modify: `jacked/cli.py`
- Test: extend `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/service/test_update_status.py`:

```python
def test_cli_update_status_init(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
    result = CliRunner().invoke(
        main, ["_update_status_init", "0.41.18", "0.41.19", "uv"],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(tmp_path / "status.json")
    assert data["from_version"] == "0.41.18"
    assert data["to_version"] == "0.41.19"
    assert data["method"] == "uv"


def test_cli_update_status_begin(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main, ["_update_status", "installing_package", "in_progress"],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(p)
    assert data["current_phase"] == "installing_package"


def test_cli_update_status_end_ok(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main, ["_update_status", "installing_package", "ok"],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(p)
    assert data["phases"][0]["status"] == "ok"


def test_cli_update_status_failed_with_error(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    us_mod.begin_phase(p, "installing_package")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main,
        ["_update_status", "installing_package", "failed",
         "--error", "uv tool install failed",
         "--recovery", "Retry: uv tool install ..."],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "uv tool install failed"
    assert data["recovery"] == "Retry: uv tool install ..."
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v -k cli_update_status`
Expected: 4 failures (no such commands).

- [ ] **Step 3: Add the hidden commands**

In `jacked/cli.py`, add alongside other hidden commands (near `_hook_shim`):

```python
@main.command(name="_update_status_init", hidden=True)
@click.argument("from_version")
@click.argument("to_version")
@click.argument("method")
def _update_status_init_shim(from_version: str, to_version: str, method: str):
    """Internal: initialize a fresh update-status file."""
    from jacked.service import update_status as us_mod
    us_mod.init_status(
        us_mod.UPDATE_STATUS_FILE,
        from_version=from_version, to_version=to_version, method=method,
    )


@main.command(name="_update_status", hidden=True)
@click.argument("phase")
@click.argument("status")
@click.option("--error", default=None)
@click.option("--recovery", default=None)
def _update_status_shim(phase: str, status: str, error: str | None, recovery: str | None):
    """Internal: write one status transition to the update-status file.

    Used by the Windows cmd.exe batch updater. `status` is one of:
    in_progress, ok, failed.
    """
    from jacked.service import update_status as us_mod
    path = us_mod.UPDATE_STATUS_FILE
    if status == "in_progress":
        us_mod.begin_phase(path, phase)
    else:
        us_mod.end_phase(path, phase, status=status, error=error, recovery=recovery)
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/service/test_update_status.py
git commit -m "feat(cli): hidden _update_status + _update_status_init shims"
```

---

## Task 5: Pre-flight `jacked upgrade`

**Files:**
- Modify: `jacked/cli.py` (in the `upgrade` command)
- Test: extend `tests/unit/test_upgrade_command.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/test_upgrade_command.py`:

```python
class TestUpgradeRefusal:
    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "This is an editable (dev-clone) install — auto-update disabled. Upgrade manually from the repo: `cd <repo> && git pull && uv sync`."),
    )
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_refuses_editable(self, mock_run, mock_popen, mock_gate):
        from jacked.cli import main
        result = CliRunner().invoke(main, ["upgrade"])
        assert result.exit_code == 2
        assert "editable" in result.output.lower()
        assert "git pull" in result.output
        mock_run.assert_not_called()
        mock_popen.assert_not_called()

    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "pip install detected — auto-update disabled. Migrate with: `uv tool install \"claude-jacked[tray]\"`."),
    )
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_refuses_pip(self, mock_run, mock_popen, mock_gate):
        from jacked.cli import main
        result = CliRunner().invoke(main, ["upgrade"])
        assert result.exit_code == 2
        assert "pip" in result.output.lower()
        mock_run.assert_not_called()
        mock_popen.assert_not_called()
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `uv run python -m pytest tests/unit/test_upgrade_command.py::TestUpgradeRefusal -v`
Expected: both fail.

- [ ] **Step 3: Add the pre-flight**

Edit the `upgrade()` function body in `jacked/cli.py`. At the very start (right after the existing imports block inside the function), add:

```python
    from jacked.install_method import can_auto_upgrade as _can_upgrade
    _ok, _reason = _can_upgrade()
    if not _ok:
        console.print(f"[red]Cannot auto-upgrade:[/red] {_reason}")
        sys.exit(2)
```

- [ ] **Step 4: Run all upgrade tests**

Run: `uv run python -m pytest tests/unit/test_upgrade_command.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/test_upgrade_command.py
git commit -m "feat(cli): jacked upgrade refuses editable/pip installs with recovery"
```

---

## Task 6: Pre-flight the tray's Update click

**Files:**
- Modify: `jacked/service/tray.py`
- Test: extend `tests/unit/service/test_tray.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/service/test_tray.py`:

```python
class TestOnUpdateClickRefusal:
    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "This is an editable (dev-clone) install — auto-update disabled. Upgrade manually from the repo: `cd <repo> && git pull && uv sync`."),
    )
    @patch("jacked.service.updater.spawn_updater_from_tray")
    def test_refuses_editable_without_spawning_or_stopping(
        self, mock_spawn, mock_gate,
    ):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        with patch.object(runner, "_on_stop") as mock_stop:
            runner._on_update_click()
        mock_spawn.assert_not_called()
        mock_stop.assert_not_called()
        runner._icon.notify.assert_called_once()
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run python -m pytest tests/unit/service/test_tray.py::TestOnUpdateClickRefusal -v`
Expected: fail.

- [ ] **Step 3: Add the pre-flight**

Edit `jacked/service/tray.py`, the `_on_update_click` method. Right after `if not self._version_is_clickable(): return` and BEFORE `if not self._lifecycle_lock.acquire(blocking=False):`, insert:

```python
        # Pre-flight: refuse editable / pip installs BEFORE we kill the tray.
        from jacked.install_method import can_auto_upgrade as _can_upgrade
        _ok, _reason = _can_upgrade()
        if not _ok:
            if self._icon:
                try:
                    self._icon.notify(_reason, "Jacked auto-update disabled")
                except Exception:
                    logger.exception("Failed to notify on update refusal")
            try:
                from jacked.service.updater import RECOVERY_FILE
                RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
                RECOVERY_FILE.write_text(_reason + "\n")
            except Exception:
                logger.exception("Could not write recovery file on refusal")
            return
```

- [ ] **Step 4: Run all tray tests**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "feat(tray): refuse Update click on editable/pip installs"
```

---

## Task 7: Standalone progress page (`update.html`)

**Files:**
- Create: `jacked/data/web/update.html`
- Modify: `jacked/service/tray.py` — open browser before spawning updater

- [ ] **Step 1: Create `update.html` with safe-DOM JS**

Create `jacked/data/web/update.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8"/>
    <title>Jacked Update</title>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>
        body { font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
               max-width: 640px; margin: 2em auto; padding: 0 1em; color: #e5e7eb;
               background: #0b1020; }
        h1 { font-size: 1.2em; margin: 0 0 0.5em; }
        .meta { color: #9ca3af; font-size: 0.85em; margin-bottom: 1.5em; }
        ol.phases { list-style: none; padding: 0; }
        li.phase { padding: 0.6em 0.8em; margin: 0.3em 0; border-radius: 6px;
                   background: #1f2937; display: flex; align-items: center;
                   gap: 0.7em; }
        li.phase.ok { background: #064e3b; }
        li.phase.in_progress { background: #1e3a8a; }
        li.phase.failed { background: #7f1d1d; }
        li.phase.pending { opacity: 0.55; }
        .dot { width: 0.7em; height: 0.7em; border-radius: 50%; display: inline-block; }
        .dot.ok { background: #10b981; }
        .dot.in_progress { background: #3b82f6; animation: pulse 1s infinite; }
        .dot.failed { background: #ef4444; }
        .dot.pending { background: #6b7280; }
        @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: 0.3 } }
        .banner { padding: 1em; border-radius: 6px; margin: 1.5em 0;
                  background: #1f2937; display: none; }
        .banner.succeeded { background: #064e3b; }
        .banner.failed { background: #7f1d1d; }
        .banner.stuck { background: #78350f; }
        a.button { display: inline-block; margin-top: 0.7em;
                   padding: 0.5em 1em; background: #2563eb; color: #fff;
                   text-decoration: none; border-radius: 4px; }
        code { background: #111827; padding: 0.1em 0.4em; border-radius: 3px; }
        .banner .row { margin-top: 0.4em; }
    </style>
</head>
<body>
    <h1>Jacked is updating…</h1>
    <div class="meta" id="meta">Waiting for first status update…</div>
    <ol class="phases" id="phases"></ol>
    <div id="banner" class="banner"></div>

    <script>
    // Safe DOM construction — never use innerHTML with dynamic values.
    const PHASES = [
        ["waiting_for_parent", "Waiting for old tray to exit"],
        ["installing_package", "Installing package"],
        ["migrating_settings", "Migrating settings"],
        ["waiting_port_free",  "Waiting for port 8321 to free"],
        ["starting_service",   "Starting new service"],
        ["verifying_service",  "Verifying new service"],
    ];

    const state = {
        startedAt: null,
        lastStatusSeenAt: Date.now(),
        targetVersion: null,
        fromVersion: null,
        method: null,
        currentVersion: null,
        overall: "in_progress",
        error: null,
        recovery: null,
        phases: {},
        serviceDownSince: null,
    };

    function clearChildren(el) {
        while (el.firstChild) el.removeChild(el.firstChild);
    }

    function makeEl(tag, className, text) {
        const el = document.createElement(tag);
        if (className) el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }

    function renderPhases() {
        const ol = document.getElementById("phases");
        clearChildren(ol);
        for (const [name, label] of PHASES) {
            const entry = state.phases[name] || { status: "pending" };
            const li = makeEl("li", "phase " + entry.status);
            li.appendChild(makeEl("span", "dot " + entry.status));
            li.appendChild(makeEl("span", null, label));
            ol.appendChild(li);
        }
    }

    function renderMeta() {
        const meta = document.getElementById("meta");
        if (!state.startedAt) {
            meta.textContent = "Waiting for first status update…";
            return;
        }
        const elapsed = Math.max(0, Math.round((Date.now() - state.startedAt) / 1000));
        let txt = `From v${state.fromVersion || "?"} → v${state.targetVersion || "?"} • ${elapsed}s elapsed`;
        if (state.method) txt += ` • method: ${state.method}`;
        meta.textContent = txt;
    }

    function renderBanner() {
        const banner = document.getElementById("banner");
        clearChildren(banner);
        banner.style.display = "none";
        banner.className = "banner";

        const done = state.overall === "succeeded" ||
            (state.targetVersion && state.currentVersion === state.targetVersion);
        const stuck = !done && state.overall !== "failed" &&
            Date.now() - state.lastStatusSeenAt > 120_000 &&
            state.serviceDownSince && Date.now() - state.serviceDownSince > 120_000;

        if (done) {
            banner.classList.add("succeeded");
            banner.appendChild(makeEl("div", null,
                `Update complete — jacked is now v${state.currentVersion || state.targetVersion}.`));
            const a = makeEl("a", "button", "Open dashboard");
            a.href = "/";
            banner.appendChild(a);
            banner.style.display = "block";
        } else if (state.overall === "failed") {
            banner.classList.add("failed");
            banner.appendChild(makeEl("div", null, "Update failed."));
            if (state.error) {
                const row = makeEl("div", "row");
                row.appendChild(makeEl("b", null, "Error: "));
                row.appendChild(document.createTextNode(state.error));
                banner.appendChild(row);
            }
            if (state.recovery) {
                const row = makeEl("div", "row");
                row.appendChild(makeEl("b", null, "Recovery: "));
                row.appendChild(makeEl("code", null, state.recovery));
                banner.appendChild(row);
            }
            banner.style.display = "block";
        } else if (stuck) {
            banner.classList.add("stuck");
            banner.appendChild(makeEl("div", null,
                "Update appears stuck. See ~/.claude/jacked-update.log."));
            const a = makeEl("a", "button", "Try the dashboard anyway");
            a.href = "/";
            banner.appendChild(a);
            banner.style.display = "block";
        }
    }

    function render() {
        renderPhases();
        renderMeta();
        renderBanner();
    }

    async function pollStatus() {
        try {
            const r = await fetch("/api/update/status", {cache: "no-store"});
            if (r.ok) {
                const body = await r.json();
                if (body && body.status) {
                    state.lastStatusSeenAt = Date.now();
                    state.serviceDownSince = null;
                    if (!state.startedAt && body.status.started_at) {
                        state.startedAt = Date.parse(body.status.started_at);
                    }
                    state.targetVersion = body.status.to_version || state.targetVersion;
                    state.fromVersion = body.status.from_version || state.fromVersion;
                    state.method = body.status.method || state.method;
                    state.overall = body.status.overall || state.overall;
                    state.error = body.status.error;
                    state.recovery = body.status.recovery;
                    state.phases = {};
                    for (const p of body.status.phases || []) {
                        state.phases[p.name] = p;
                    }
                }
            }
        } catch (_) {
            state.serviceDownSince = state.serviceDownSince || Date.now();
        }
    }

    async function pollVersion() {
        try {
            const r = await fetch("/api/version", {cache: "no-store"});
            if (r.ok) {
                const body = await r.json();
                if (body && typeof body.current === "string") {
                    state.currentVersion = body.current;
                }
                state.serviceDownSince = null;
            }
        } catch (_) {
            state.serviceDownSince = state.serviceDownSince || Date.now();
        }
    }

    async function tick() {
        await Promise.all([pollStatus(), pollVersion()]);
        render();
    }

    tick();
    setInterval(tick, 1000);
    </script>
</body>
</html>
```

- [ ] **Step 2: Open the progress page from the tray before spawning updater**

Edit `jacked/service/tray.py`, `_on_update_click`. Immediately BEFORE `spawn_updater_from_tray(...)`:

```python
            try:
                import webbrowser as _wb
                _wb.open(f"http://{self.host}:{self.port}/update.html")
            except Exception:
                logger.exception("Failed to open update progress page")
```

- [ ] **Step 3: Smoke test — existing suite still green**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`
Expected: all pass (no new assertion, just making sure the browser-open doesn't break existing flow).

- [ ] **Step 4: Commit**

```bash
git add jacked/data/web/update.html jacked/service/tray.py
git commit -m "feat(web): standalone /update.html progress page + open on tray click"
```

---

## Task 8: POSIX updater writes status at every phase

**Files:**
- Modify: `jacked/service/updater.py` — `run_update()`
- Test: extend `tests/unit/service/test_updater.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/service/test_updater.py`:

```python
class TestUpdaterWritesStatus:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_succeeded_status_on_happy_path(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")
        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "succeeded"
        phase_names = [p["name"] for p in data["phases"]]
        assert "installing_package" in phase_names
        assert "migrating_settings" in phase_names
        assert "verifying_service" in phase_names


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_failed_status_on_install_failure(
        self, mock_popen, mock_run, mock_find, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)
        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")
        data = us_mod.read_status(tmp_path / "status.json")
        assert data["overall"] == "failed"
```

- [ ] **Step 2: Run — verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_updater.py::TestUpdaterWritesStatus -v`
Expected: both fail.

- [ ] **Step 3: Instrument `run_update()` in `jacked/service/updater.py`**

At the top of `run_update()`, after `log_fh = open(...)`, add helper closures:

```python
    from jacked.service import update_status as _us
    from jacked import __version__ as _current_version
    try:
        _us.init_status(
            _us.UPDATE_STATUS_FILE,
            from_version=_current_version,
            to_version="next",
            method="unknown",
            log_path=str(UPDATE_LOG),
        )
    except Exception:
        logger.exception("Could not initialize update status file")

    def _begin(phase: str) -> None:
        try:
            _us.begin_phase(_us.UPDATE_STATUS_FILE, phase)
        except Exception:
            logger.exception("begin_phase failed: %s", phase)

    def _end(phase: str, status: str, error: str | None = None, recovery: str | None = None) -> None:
        try:
            _us.end_phase(_us.UPDATE_STATUS_FILE, phase, status=status, error=error, recovery=recovery)
        except Exception:
            logger.exception("end_phase failed: %s", phase)
```

Then wrap each phase:

1. Before `wait_for_exit(parent_pid, ...)` call → `_begin("waiting_for_parent")`. After the force-kill fallback → `_end("waiting_for_parent", "ok")`.
2. Before the package-install `subprocess.run(cmd)` → `_begin("installing_package")`. On returncode 0 → `_end("installing_package", "ok")`. On failure (before the existing `_write_recovery`/`return`) → `_end("installing_package", "failed", error=..., recovery=...)`.
3. Before the `jacked install --force` run → `_begin("migrating_settings")`. After (regardless of returncode, since non-zero is tolerated) → `_end("migrating_settings", "ok")` on 0 else `_end("migrating_settings", "failed", ...)`.
4. Before the port-free poll loop → `_begin("waiting_port_free")`. After → `_end("waiting_port_free", "ok")`.
5. Before `_spawn_detached([jacked, "service", "start"], ...)` → `_begin("starting_service")`. Immediately after spawn → `_end("starting_service", "ok")`.
6. Before the verify loop → `_begin("verifying_service")`. After: `_end("verifying_service", "ok" if came_up else "failed", error=...)`.

At the end of the success path (where `came_up` is True):

```python
    try:
        _us.mark_succeeded(_us.UPDATE_STATUS_FILE)
    except Exception:
        logger.exception("mark_succeeded failed")
```

- [ ] **Step 4: Run all updater tests**

Run: `uv run python -m pytest tests/unit/service/test_updater.py -v`
Expected: all existing tests still pass + 2 new status-writer tests pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_updater.py
git commit -m "feat(updater): POSIX updater emits update-status at every phase"
```

---

## Task 9: Windows batch calls `jacked _update_status`

**Files:**
- Modify: `jacked/service/updater.py` — `_spawn_windows_tray_updater()`
- Modify: `jacked/cli.py` — `_spawn_windows_upgrade_helper()`
- Test: extend `tests/unit/service/test_updater.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/service/test_updater.py`:

```python
class TestWindowsBatchCallsUpdateStatus:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_embeds_update_status_calls(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        updater._spawn_windows_tray_updater(parent_pid=12345, extras="tray")
        batch_path = mock_popen.call_args[0][0][2]
        body = open(batch_path).read()
        try:
            assert "_update_status_init" in body
            assert "_update_status installing_package in_progress" in body
            assert "_update_status installing_package ok" in body
            assert "_update_status migrating_settings in_progress" in body
            assert "_update_status starting_service in_progress" in body
        finally:
            import os as _os
            try: _os.unlink(batch_path)
            except OSError: pass
```

- [ ] **Step 2: Run — verify it fails**

Run: `uv run python -m pytest tests/unit/service/test_updater.py::TestWindowsBatchCallsUpdateStatus -v`
Expected: fails.

- [ ] **Step 3: Update the Windows batch body**

In `_spawn_windows_tray_updater()` in `jacked/service/updater.py`, rewrite the batch string:

```python
    batch_body = (
        '@echo off\r\n'
        'set LOGFILE=' + log_path + '\r\n'
        'echo [%date% %time%] tray update helper starting (parent PID ' + str(parent_pid) + ', method ' + method + ') >> "%LOGFILE%"\r\n'
        'echo [%date% %time%] upgrade command: ' + label + ' >> "%LOGFILE%"\r\n'
        ':wait\r\n'
        'tasklist /FI "PID eq ' + str(parent_pid) + '" 2>NUL | find "' + str(parent_pid) + '" >NUL\r\n'
        'if not errorlevel 1 (\r\n'
        '    timeout /t 1 /nobreak >NUL\r\n'
        '    goto wait\r\n'
        ')\r\n'
        'echo [%date% %time%] parent exited >> "%LOGFILE%"\r\n'
        'jacked _update_status_init 0.0.0 next ' + method + '\r\n'
        'jacked _update_status installing_package in_progress\r\n'
        + upgrade_line + ' >> "%LOGFILE%" 2>&1\r\n'
        'if errorlevel 1 (\r\n'
        '    jacked _update_status installing_package failed --error "upgrade command failed" --recovery "' + label + '"\r\n'
        '    echo Jacked tray update failed. See %LOGFILE%. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        'jacked _update_status installing_package ok\r\n'
        'jacked _update_status migrating_settings in_progress\r\n'
        'jacked install --force >> "%LOGFILE%" 2>&1\r\n'
        'jacked _update_status migrating_settings ok\r\n'
        'jacked _update_status starting_service in_progress\r\n'
        'start "" /B jacked service start >> "%LOGFILE%" 2>&1\r\n'
        'jacked _update_status starting_service ok\r\n'
        'echo [%date% %time%] tray update complete >> "%LOGFILE%"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )
```

Apply the same pattern in `_spawn_windows_upgrade_helper()` in `jacked/cli.py`.

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/service/test_updater.py tests/unit/test_upgrade_command.py -v`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py jacked/cli.py tests/unit/service/test_updater.py
git commit -m "feat(win): batch updater emits status via jacked _update_status"
```

---

## Task 10: Surface `update_status_file` in `/api/version`

**Files:**
- Modify: `jacked/api/routes/system.py`

- [ ] **Step 1: Locate `VersionResponse` model**

Run: `grep -n "class VersionResponse\|@router.get.*version" jacked/api/routes/system.py | head`

- [ ] **Step 2: Add optional field + populate**

In `VersionResponse`:

```python
update_status_file: str | None = None
```

In the `/version` handler build step (where the response is constructed):

```python
    from jacked.service import update_status as _us
    ...
    return VersionResponse(
        ...existing fields...,
        update_status_file=str(_us.UPDATE_STATUS_FILE),
    )
```

- [ ] **Step 3: Quick smoke**

Run: `uv run python -m pytest tests/unit/ --ignore=tests/unit/test_analytics_anomalies.py -q 2>&1 | tail -3`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add jacked/api/routes/system.py
git commit -m "feat(api): surface update_status_file path in /api/version"
```

---

## Task 11: Version bump + changelog

**Files:**
- Modify: `jacked/__init__.py`
- Modify: `README.md`

- [ ] **Step 1: Bump**

`jacked/__init__.py`:

```python
__version__ = "0.41.19"
```

- [ ] **Step 2: Changelog row above 0.41.18**

Add to `README.md`:

```markdown
| **0.41.19** | **Install-method safety + tray-update progress UI.** `jacked upgrade` and the tray "Update" button now refuse editable (dev-clone) installs and pip installs, with a clear recovery message (`git pull && uv sync` or `uv tool install "claude-jacked[tray]"`) — fixes the silent `No module named pip` crash on dev machines. Tray "Update" click now opens a browser progress page at `/update.html` that tracks each phase (waiting for parent, installing, migrating settings, restarting service, verifying) and detects completion via `/api/version`. Cross-platform. Windows batch updater emits phase updates via a new hidden `jacked _update_status` CLI shim. |
```

- [ ] **Step 3: Full suite**

Run: `uv run python -m pytest tests/unit/ --ignore=tests/unit/test_analytics_anomalies.py 2>&1 | tail -3`

- [ ] **Step 4: Commit**

```bash
git add jacked/__init__.py README.md
git commit -m "chore: bump to 0.41.19 with changelog"
```

---

## Task 12: Push + tag + GitHub release

- [ ] **Step 1: Push**

```bash
git push origin master
```

- [ ] **Step 2: Tag**

```bash
git tag -a v0.41.19 -m "v0.41.19 — install-method safety + tray update progress UI"
git push origin v0.41.19
```

- [ ] **Step 3: Release**

```bash
gh release create v0.41.19 \
  --title "v0.41.19 — install-method safety + tray update progress UI" \
  --notes "$(cat <<'EOF'
Fixes the silent dev-clone upgrade crash + adds a cross-platform browser progress page when clicking Update in the tray.

**Fixed:**
- Editable (dev-clone) installs no longer crash mid-upgrade with `No module named pip`. `jacked upgrade` + tray Update click now refuse with a clear recovery message: `cd <repo> && git pull && uv sync`.
- Pip installs also refused, recommending `uv tool install "claude-jacked[tray]"`.

**Added:**
- Tray "Update" click opens `/update.html` in your browser — shows each phase (waiting for parent, installing, migrating settings, restarting, verifying) with live status. Detects completion via `/api/version`. Works on macOS / Linux / Windows.
- New hidden `jacked _update_status` + `_update_status_init` CLI shims used by the Windows cmd.exe batch updater.
- New `GET /api/update/status` endpoint backing the progress page.
EOF
)"
```

- [ ] **Step 4: Watch publish**

```bash
gh run list --workflow=publish.yml --limit=1
```

---

## Self-Review

Spec coverage:
- Component 1 (editable detection + `can_auto_upgrade()`) → Task 1
- Component 2 (CLI + tray pre-flight) → Tasks 5, 6
- Component 3 (status JSON) → Task 2
- Component 4 (`update.html`) → Task 7
- Component 5 (`/api/update/status` + `/api/version` field) → Tasks 3, 10
- Component 6 (tray opens browser) → Task 7
- Component 7 (updater + Windows batch) → Tasks 8, 9
- `_update_status` + `_update_status_init` shims → Task 4
- Version + changelog → Task 11
- Ship → Task 12

No placeholders, all code blocks complete. Type/name consistency: `UPDATE_STATUS_FILE`, `can_auto_upgrade()`, phase names all match across tasks. `update.html` uses safe DOM construction (no `innerHTML`), addressing the security-reminder hook.

## Execution

User has explicitly asked for auto-mode / ship on main. Use `superpowers:executing-plans` inline to run through tasks sequentially.

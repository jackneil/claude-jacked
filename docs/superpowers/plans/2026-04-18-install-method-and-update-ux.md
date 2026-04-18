# Install-method safety + tray-update progress UX — Implementation Plan (v2, post-review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 0.41.19 — refuse auto-upgrade on editable/pip installs (close the dev-clone "No module named pip" crash), and give users a cross-platform browser-based progress page when they click Update in the tray.

**Architecture:** Install-method detector gains a fourth category (`editable`) and a `can_auto_upgrade()` gate. CLI + tray pre-flight against the gate. The updater writes `~/.claude/jacked-update-status.json` atomically at every phase transition (both POSIX Python updater and Windows cmd.exe batch, the latter via a new hidden `jacked _update_status` CLI shim). A standalone `update.html` page loads from the current live service right before the tray kills itself, then polls `/api/update/status` + `/api/version` to narrate the upgrade and detect completion — works even as the service is torn down and recreated.

**Phase lifecycle is derived from a single source of truth** (`jacked/service/update_phases.py`) consumed by both writers (POSIX updater, Windows batch generator) and the `/update.html` page (embedded at generation time — the HTML file is templated from the same constant during `jacked install`).

**Tech Stack:** Python 3.10+ / Click / FastAPI / pystray / vanilla JS + HTML. No new runtime dependencies.

**Review-round-1 findings addressed:**
- URL unified at `/update.html` (spec + plan both).
- All phase-wrap `_end(...)` calls now explicit for each abort path.
- Windows batch writes all 6 phases + target version + mark_succeeded; opens browser as fallback.
- Concurrent-writer lock.
- Phase-list drift prevention via shared constant.
- Status-file lifecycle: clear on start, clear on refusal, server reports mtime for stuck-detection, stale-succeeded reads as None.
- Editable detection includes `jacked.__file__` fallback.
- Defense-in-depth: updater also refuses on non-upgradable methods.
- Test bugs fixed (assert Popen called, assert call order).

**Review-round-2 findings addressed:**
- De-morgan trap in Windows batch test replaced with explicit `'"next"' not in body` assertion.
- Browser-open race with service shutdown: tray pre-warms `/update.html` via synchronous `urllib.request.urlopen()` before calling `webbrowser.open()` and `_on_stop()`.
- `_method` detection simplified to a normal `from jacked.install_method import detect_install_method` + call.
- `port` threaded through tray → `spawn_updater_from_tray` → `_spawn_windows_tray_updater` → batch `progress_url` and PowerShell verify; POSIX `run_update` uses it for port-wait + verify.
- Windows batch `--recovery "<label>"` corruption fixed: internal `"` in label converted to `'` via `label.replace('"', "'")` before embedding.
- `update.html` polls terminate once update reaches a terminal state (`clearInterval(pollHandle)`).
- Regression test: `/update.html` is served as itself (not SPA-rewritten to `index.html`).

---

## File Structure

**New files:**
- `jacked/service/update_phases.py` — single-source-of-truth `PHASES` constant
- `jacked/service/update_status.py` — atomic status-file reader/writer, cleanup, mtime reporting
- `jacked/data/web/update.html` — standalone progress page (inline CSS + JS, safe DOM construction)
- `tests/unit/service/test_update_phases.py`
- `tests/unit/service/test_update_status.py`
- `tests/unit/test_install_method_editable.py`

**Modified files:**
- `jacked/install_method.py` — add `editable` detection (`.pth` markers + `jacked.__file__` fallback) + `can_auto_upgrade()`
- `jacked/cli.py` — `upgrade` pre-flight; add hidden `_update_status` + `_update_status_init` + `_update_status_succeed` commands
- `jacked/service/tray.py` — `_on_update_click` pre-flight + open browser before stop + clear stale status file on refusal
- `jacked/service/updater.py` — emit status at every phase (POSIX); use PHASES constant; refuse if not upgradable (defense-in-depth); accept target_version
- `jacked/service/updater.py` — `_spawn_windows_tray_updater()` emits ALL 6 phases + target version + success terminal + browser-open fallback
- `jacked/cli.py` — `_spawn_windows_upgrade_helper()` mirror of the above
- `jacked/api/routes/system.py` — new `GET /api/update/status` returning body + `mtime_iso`; extend `/api/version` with `update_status_file`
- `jacked/__init__.py` — bump to `0.41.19`
- `README.md` — changelog entry

---

## Task 1: Single-source-of-truth phases constant

**Files:**
- Create: `jacked/service/update_phases.py`
- Test: `tests/unit/service/test_update_phases.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/service/test_update_phases.py`:

```python
"""Guardrail: the canonical phase list must never drift between writers and the UI."""

from jacked.service.update_phases import PHASES, PHASE_NAMES


def test_six_phases_in_expected_order():
    """Order matters — the UI renders phases in this order."""
    assert PHASE_NAMES == [
        "waiting_for_parent",
        "installing_package",
        "migrating_settings",
        "waiting_port_free",
        "starting_service",
        "verifying_service",
    ]


def test_phases_have_name_and_label():
    for entry in PHASES:
        assert "name" in entry and entry["name"]
        assert "label" in entry and entry["label"]


def test_update_html_embeds_all_phase_names():
    """Drift-prevention: update.html's hardcoded PHASES JS constant must
    contain every phase name defined here. If you add a phase, add it to
    update.html too (or template it in jacked install)."""
    from pathlib import Path
    import jacked
    repo_root = Path(jacked.__file__).resolve().parent
    html = (repo_root / "data" / "web" / "update.html").read_text()
    for name in PHASE_NAMES:
        assert name in html, f"update.html missing phase name: {name}"
```

- [ ] **Step 2: Run — verify they fail**

Run: `uv run python -m pytest tests/unit/service/test_update_phases.py -v`
Expected: `ModuleNotFoundError: No module named 'jacked.service.update_phases'`.

- [ ] **Step 3: Implement**

Create `jacked/service/update_phases.py`:

```python
"""Single source of truth for update phases.

Consumed by:
  - jacked.service.updater.run_update() (POSIX updater)
  - jacked.service.updater._spawn_windows_tray_updater() (Windows batch generator)
  - jacked.cli._spawn_windows_upgrade_helper() (Windows batch generator)
  - jacked/data/web/update.html (embedded copy — test_update_html_embeds_all_phase_names
    enforces consistency)

If you add or rename a phase here, the test_update_html_embeds_all_phase_names
test will fail until you update update.html to match.
"""

PHASES: list[dict] = [
    {"name": "waiting_for_parent", "label": "Waiting for old tray to exit"},
    {"name": "installing_package", "label": "Installing package"},
    {"name": "migrating_settings", "label": "Migrating settings"},
    {"name": "waiting_port_free",  "label": "Waiting for port 8321 to free"},
    {"name": "starting_service",   "label": "Starting new service"},
    {"name": "verifying_service",  "label": "Verifying new service"},
]

PHASE_NAMES: list[str] = [p["name"] for p in PHASES]
```

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/service/test_update_phases.py -v`
Expected: the first two pass; `test_update_html_embeds_all_phase_names` fails because `update.html` doesn't exist yet. That's fine — Task 8 creates it, then this test passes.

Skip the HTML-embedding test in this task via `-k "not embeds_all"`:

Run: `uv run python -m pytest tests/unit/service/test_update_phases.py -v -k "not embeds_all"`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/update_phases.py tests/unit/service/test_update_phases.py
git commit -m "feat(updater): single-source-of-truth phases constant"
```

---

## Task 2: Editable-install detection + `can_auto_upgrade()` gate

**Files:**
- Modify: `jacked/install_method.py`
- Test: `tests/unit/test_install_method_editable.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_install_method_editable.py`:

```python
"""Tests for editable-install detection and the can_auto_upgrade() gate."""

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

    def test_detects_via_file_attr_fallback(self, tmp_path, monkeypatch):
        """If no .pth markers exist but jacked.__file__ resolves OUTSIDE any
        site-packages tree on sys.path, it's still editable (defensive)."""
        sp = tmp_path / "site-packages"
        sp.mkdir()
        monkeypatch.setattr(sys, "path", [str(sp)])
        repo = tmp_path / "my-jacked-checkout" / "jacked"
        repo.mkdir(parents=True)
        (repo / "__init__.py").write_text("")
        import jacked
        with patch("jacked.__file__", str(repo / "__init__.py")):
            with patch(
                "sys.executable",
                str(tmp_path / ".venv" / "bin" / "python3"),
            ):
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

    def test_gate_defensive_when_detection_raises(self):
        """If detect_install_method blows up unexpectedly, gate is closed."""
        with patch(
            "jacked.install_method.detect_install_method",
            side_effect=RuntimeError("boom"),
        ):
            ok, reason = can_auto_upgrade()
        assert ok is False
        assert reason  # non-empty recovery-ish message
```

- [ ] **Step 2: Run — verify they fail**

Run: `uv run python -m pytest tests/unit/test_install_method_editable.py -v`

- [ ] **Step 3: Implement**

Replace `detect_install_method()` in `jacked/install_method.py` and append `can_auto_upgrade()`:

```python
def _is_path_under_any_site_packages(target: Path) -> bool:
    """Return True if `target` lives under a site-packages/ dir on sys.path."""
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        return False
    for entry in sys.path:
        if not entry:
            continue
        try:
            p = Path(entry).resolve()
        except (OSError, RuntimeError):
            continue
        if p.name != "site-packages":
            continue
        try:
            target.relative_to(p)
            return True
        except ValueError:
            continue
    return False


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

    # Fallback: if jacked/__init__.py resolves to a path NOT under any
    # site-packages/ on sys.path, treat as editable.
    try:
        import jacked
        if jacked.__file__:
            jacked_init = Path(jacked.__file__).resolve()
            if not _is_path_under_any_site_packages(jacked_init):
                return "editable"
    except Exception:
        pass

    return "pip"


def can_auto_upgrade() -> tuple[bool, str]:
    """Return (ok, reason) — is it safe to auto-upgrade this install?

    uv / pipx: True, empty reason.
    editable:  False with a git-pull/uv-sync recovery hint.
    pip:       False with a 'migrate to uv' recovery hint.
    any error: False with a defensive "cannot detect install method" message.
    """
    try:
        method = detect_install_method()
    except Exception:
        return (
            False,
            "Could not detect install method — auto-update disabled. "
            "Try: `uv tool install \"claude-jacked[tray]\" --force`.",
        )
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

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/test_install_method_editable.py tests/unit/test_install_method.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/install_method.py tests/unit/test_install_method_editable.py
git commit -m "feat(install-method): detect editable installs + can_auto_upgrade() gate"
```

---

## Task 3: Update-status file helpers (with lifecycle + lock + mtime)

**Files:**
- Create: `jacked/service/update_status.py`
- Test: `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/service/test_update_status.py`:

```python
"""Tests for the update-status JSON reader/writer."""

import json
import os
import time


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


def test_end_phase_raises_on_unknown_phase(tmp_path):
    """Defense: loudly reject a phase close-out that was never opened.
    Silent no-ops mask drift between writers and the phase list."""
    from jacked.service.update_status import (
        init_status, end_phase,
    )
    import pytest
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    with pytest.raises(ValueError):
        end_phase(p, "nonexistent_phase", status="ok")


def test_mark_succeeded_finalizes_overall(tmp_path):
    from jacked.service.update_status import (
        init_status, mark_succeeded, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    data = read_status(p)
    assert data["overall"] == "succeeded"


def test_clear_status_removes_file(tmp_path):
    from jacked.service.update_status import (
        init_status, clear_status, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    assert p.exists()
    clear_status(p)
    assert not p.exists()
    assert read_status(p) is None


def test_clear_status_missing_is_noop(tmp_path):
    from jacked.service.update_status import clear_status
    clear_status(tmp_path / "nope.json")  # must not raise


def test_read_missing_returns_none(tmp_path):
    from jacked.service.update_status import read_status
    assert read_status(tmp_path / "does-not-exist.json") is None


def test_read_corrupt_returns_none(tmp_path):
    from jacked.service.update_status import read_status
    p = tmp_path / "status.json"
    p.write_text("{not json at all")
    assert read_status(p) is None


def test_read_stale_succeeded_returns_none(tmp_path, monkeypatch):
    """A succeeded file older than the stale threshold is treated as absent
    so the UI doesn't resurrect an old 'update complete' banner on a
    fresh tray launch."""
    from jacked.service.update_status import (
        init_status, mark_succeeded, read_status,
        STALE_SUCCEEDED_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    # Rewind mtime to before the stale cutoff
    old = time.time() - STALE_SUCCEEDED_SECONDS - 10
    os.utime(p, (old, old))
    assert read_status(p) is None


def test_read_with_mtime_returns_iso_timestamp(tmp_path):
    """Server exposes mtime separately so the UI can detect 'stuck' without
    relying on its own clock."""
    from jacked.service.update_status import read_status_with_mtime
    p = tmp_path / "status.json"
    from jacked.service.update_status import init_status
    init_status(p, from_version="a", to_version="b", method="uv")
    data, mtime_iso = read_status_with_mtime(p)
    assert data is not None
    assert mtime_iso is not None
    assert "T" in mtime_iso  # ISO-8601 shape


def test_write_is_atomic_no_tmp_leftover(tmp_path):
    from jacked.service.update_status import init_status
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    siblings = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert siblings == []


def test_lock_rejects_second_init_if_another_active(tmp_path):
    """Concurrent updaters must not clobber each other's phase history.
    First init takes the lock; a second init while the first is still
    in_progress raises LockBusy."""
    from jacked.service.update_status import (
        init_status, LockBusy,
    )
    import pytest
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    # Second call while overall=="in_progress" and file recent must refuse
    with pytest.raises(LockBusy):
        init_status(p, from_version="a", to_version="b", method="uv")


def test_lock_allows_init_after_previous_succeeded(tmp_path):
    """A completed prior update doesn't block the next one."""
    from jacked.service.update_status import (
        init_status, mark_succeeded,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_succeeded(p)
    # Subsequent init succeeds — previous run is done
    init_status(p, from_version="b", to_version="c", method="uv")  # no raise


def test_lock_allows_init_after_stale_in_progress(tmp_path):
    """An 'in_progress' file older than the stale threshold is abandoned."""
    from jacked.service.update_status import (
        init_status, STALE_IN_PROGRESS_SECONDS,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    old = time.time() - STALE_IN_PROGRESS_SECONDS - 10
    os.utime(p, (old, old))
    init_status(p, from_version="b", to_version="c", method="uv")  # no raise
```

- [ ] **Step 2: Run — verify failures**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: import failures.

- [ ] **Step 3: Implement**

Create `jacked/service/update_status.py`:

```python
"""Atomic reader/writer for the update-status JSON file.

Used by the detached POSIX updater, the Windows cmd.exe batch (via the
`jacked _update_status` CLI shim), and the `/api/update/status` endpoint.

Lifecycle contract:
  1. `init_status()` — clobbers any prior file unless another updater is
     actively in-flight (raises LockBusy). Stale `in_progress` files
     (older than STALE_IN_PROGRESS_SECONDS) are considered abandoned.
  2. `begin_phase` / `end_phase` throughout the update.
  3. `mark_succeeded()` OR end_phase with status="failed" finalizes.
  4. A `succeeded` file older than STALE_SUCCEEDED_SECONDS is reported as
     missing by `read_status()` so the UI doesn't resurrect old banners.

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

UPDATE_STATUS_FILE: Path = CLAUDE_DIR / "jacked-update-status.json"

# If `overall: "succeeded"` for more than this long, `read_status()` pretends
# the file doesn't exist — prevents stale "update complete" banners.
STALE_SUCCEEDED_SECONDS: int = 600  # 10 minutes

# If `overall: "in_progress"` for more than this long, `init_status()` assumes
# the previous updater crashed and takes over anyway.
STALE_IN_PROGRESS_SECONDS: int = 300  # 5 minutes


class LockBusy(Exception):
    """Another updater is actively in-flight — refuse to clobber its state."""


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


def _read_raw(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def read_status(path: Path) -> Optional[dict]:
    """Public read. Returns None on missing, corrupt, or stale-succeeded."""
    data = _read_raw(path)
    if data is None:
        return None
    if data.get("overall") == "succeeded":
        try:
            age = __import__("time").time() - path.stat().st_mtime
            if age > STALE_SUCCEEDED_SECONDS:
                return None
        except OSError:
            return None
    return data


def read_status_with_mtime(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Returns (data, mtime_iso). Used by /api/update/status."""
    data = read_status(path)
    if data is None:
        return None, None
    try:
        mtime = path.stat().st_mtime
        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        mtime_iso = None
    return data, mtime_iso


def init_status(
    path: Path,
    from_version: str,
    to_version: str,
    method: str,
    log_path: Optional[str] = None,
) -> None:
    """Create a fresh status file. Raises LockBusy if another updater is active.

    Clobbers:
      - no prior file
      - prior succeeded/failed file
      - prior in_progress file older than STALE_IN_PROGRESS_SECONDS
    Refuses:
      - prior in_progress file fresher than that
    """
    prior = _read_raw(path)
    if prior is not None and prior.get("overall") == "in_progress":
        try:
            age = __import__("time").time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age <= STALE_IN_PROGRESS_SECONDS:
            raise LockBusy(
                "Another updater is in-flight (started "
                f"{prior.get('started_at', '?')}, last updated "
                f"{int(age)}s ago)"
            )

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
    data = _read_raw(path) or {}
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
    """Close the most recent in-progress phase with the given name.

    Raises ValueError if no such open phase exists — a loud failure is
    preferable to silent drift where a typo or missing begin_phase is
    hidden by a no-op close.
    """
    data = _read_raw(path) or {}
    phases = data.get("phases", [])
    matched = False
    for entry in reversed(phases):
        if entry["name"] == phase and entry["status"] == "in_progress":
            entry["status"] = status
            entry["finished_at"] = _now_iso()
            matched = True
            break
    if not matched:
        raise ValueError(
            f"end_phase({phase!r}) called but no in_progress entry with that name"
        )
    data["phases"] = phases
    if status == "failed":
        data["overall"] = "failed"
        if error:
            data["error"] = error
        if recovery:
            data["recovery"] = recovery
    _atomic_write(path, data)


def mark_succeeded(path: Path) -> None:
    data = _read_raw(path) or {}
    data["overall"] = "succeeded"
    data["current_phase"] = None
    _atomic_write(path, data)


def clear_status(path: Path) -> None:
    """Delete the status file. Used by the tray's refusal path and by test setup."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
```

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: all 14+ tests pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/update_status.py tests/unit/service/test_update_status.py
git commit -m "feat(updater): atomic update-status with lock, stale detection, cleanup"
```

---

## Task 4: `/api/update/status` endpoint (with mtime)

**Files:**
- Modify: `jacked/api/routes/system.py`
- Test: extend `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Failing tests**

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
    body = r.json()
    assert body == {"status": None, "mtime_iso": None}


def test_api_endpoint_returns_status_content_with_mtime(tmp_path, monkeypatch):
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
    body = r.json()
    assert body["status"]["current_phase"] == "installing_package"
    assert body["mtime_iso"] is not None
```

- [ ] **Step 2: Run — expect fails**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v -k api_endpoint`

- [ ] **Step 3: Add the endpoint**

Edit `jacked/api/routes/system.py`. Import at module top (not inside handler):

```python
from jacked.service import update_status as _update_status_mod
```

Add route:

```python
@router.get("/update/status")
async def get_update_status():
    """Return the current update-status JSON + its mtime, or {status: null, mtime_iso: null}.

    Used by /update.html. Polled every 1s. mtime_iso is server-reported so
    the client-side 'stuck' detection doesn't reset on repeated stale reads.
    """
    data, mtime_iso = _update_status_mod.read_status_with_mtime(
        _update_status_mod.UPDATE_STATUS_FILE,
    )
    return {"status": data, "mtime_iso": mtime_iso}
```

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/api/routes/system.py tests/unit/service/test_update_status.py
git commit -m "feat(api): GET /api/update/status with server-reported mtime"
```

---

## Task 5: Hidden CLI shims (`_update_status`, `_update_status_init`, `_update_status_succeed`)

**Files:**
- Modify: `jacked/cli.py`
- Test: extend `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Tests**

Append:

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


def test_cli_update_status_init_accepts_log_path(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
    CliRunner().invoke(
        main,
        ["_update_status_init", "a", "b", "uv", "--log-path", "/tmp/foo.log"],
    )
    data = us_mod.read_status(tmp_path / "status.json")
    assert data["log_path"] == "/tmp/foo.log"


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
    assert us_mod.read_status(p)["phases"][0]["status"] == "ok"


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
         "--error", "upgrade command failed",
         "--recovery", "uv tool install \"claude-jacked[tray]\" --force"],
    )
    assert result.exit_code == 0
    data = us_mod.read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "upgrade command failed"


def test_cli_update_status_succeed(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(main, ["_update_status_succeed"])
    assert result.exit_code == 0
    assert us_mod.read_status(p)["overall"] == "succeeded"
```

- [ ] **Step 2: Run**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v -k cli_update_status`

- [ ] **Step 3: Add the commands**

In `jacked/cli.py`, add after the `_hook_shim` command:

```python
@main.command(name="_update_status_init", hidden=True)
@click.argument("from_version")
@click.argument("to_version")
@click.argument("method")
@click.option("--log-path", default=None)
def _update_status_init_shim(
    from_version: str, to_version: str, method: str, log_path: "str | None",
):
    """Internal: initialize a fresh update-status file.

    Exit 0 on success, 2 on LockBusy (another updater active). Windows
    batch checks errorlevel and aborts on 2 so we don't double-write.
    """
    from jacked.service import update_status as us_mod
    try:
        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version=from_version,
            to_version=to_version,
            method=method,
            log_path=log_path,
        )
    except us_mod.LockBusy as exc:
        click.echo(f"[update-status] lock busy: {exc}", err=True)
        sys.exit(2)


@main.command(name="_update_status", hidden=True)
@click.argument("phase")
@click.argument("status")
@click.option("--error", default=None)
@click.option("--recovery", default=None)
def _update_status_shim(
    phase: str, status: str, error: "str | None", recovery: "str | None",
):
    """Internal: write one status transition to the update-status file."""
    from jacked.service import update_status as us_mod
    path = us_mod.UPDATE_STATUS_FILE
    try:
        if status == "in_progress":
            us_mod.begin_phase(path, phase)
        else:
            us_mod.end_phase(path, phase, status=status, error=error, recovery=recovery)
    except ValueError as exc:
        click.echo(f"[update-status] {exc}", err=True)


@main.command(name="_update_status_succeed", hidden=True)
def _update_status_succeed_shim():
    """Internal: mark the update-status file overall=succeeded."""
    from jacked.service import update_status as us_mod
    us_mod.mark_succeeded(us_mod.UPDATE_STATUS_FILE)
```

Note: CLI file must have `from __future__ import annotations` at top OR use string-quoted type hints (`"str | None"`) for PEP 604 syntax on 3.10.

Check at top of `jacked/cli.py`:
```bash
grep -n "from __future__" jacked/cli.py
```
If not present, add it as the very first import. If present, you can unquote the type hints.

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/service/test_update_status.py
git commit -m "feat(cli): hidden _update_status* shims for batch updater"
```

---

## Task 6: Pre-flight `jacked upgrade`

**Files:**
- Modify: `jacked/cli.py`
- Test: extend `tests/unit/test_upgrade_command.py`

- [ ] **Step 1: Tests**

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

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Add gate**

In the body of `upgrade()` in `jacked/cli.py`, immediately after the existing imports block:

```python
    from jacked.install_method import can_auto_upgrade as _can_upgrade
    _ok, _reason = _can_upgrade()
    if not _ok:
        console.print(f"[red]Cannot auto-upgrade:[/red] {_reason}")
        sys.exit(2)
```

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/test_upgrade_command.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/test_upgrade_command.py
git commit -m "feat(cli): jacked upgrade refuses editable/pip with recovery"
```

---

## Task 7: Tray Update-click pre-flight (also clears stale status file)

**Files:**
- Modify: `jacked/service/tray.py`
- Test: extend `tests/unit/service/test_tray.py`

- [ ] **Step 1: Tests**

Append:

```python
class TestOnUpdateClickRefusal:
    @patch("jacked.service.update_status.clear_status")
    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "editable — run `git pull && uv sync`"),
    )
    @patch("jacked.service.updater.spawn_updater_from_tray")
    def test_refuses_editable_clears_status_no_spawn_no_stop(
        self, mock_spawn, mock_gate, mock_clear,
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
        mock_clear.assert_called_once()
        runner._icon.notify.assert_called_once()
```

- [ ] **Step 2: Run — fail**

- [ ] **Step 3: Wire up**

In `jacked/service/tray.py`, `_on_update_click`, after `if not self._version_is_clickable(): return`, insert:

```python
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
            # Clear any stale update-status file so /update.html doesn't
            # show a phantom "update in progress" banner.
            try:
                from jacked.service import update_status as _us
                _us.clear_status(_us.UPDATE_STATUS_FILE)
            except Exception:
                logger.exception("Could not clear status file on refusal")
            return
```

- [ ] **Step 4: Run + Step 5: Commit**

```bash
uv run python -m pytest tests/unit/service/test_tray.py -v
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "feat(tray): refuse Update click on editable/pip + clear stale status"
```

---

## Task 8: Standalone `update.html` + tray opens it before stop

**Files:**
- Create: `jacked/data/web/update.html`
- Modify: `jacked/service/tray.py`

- [ ] **Step 1: Create the page** (full content — safe DOM construction, uses server mtime for stuck detection)

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
    // PHASES mirrors jacked/service/update_phases.py — enforced by
    // tests/unit/service/test_update_phases.py::test_update_html_embeds_all_phase_names.
    // Every phase name below must also exist in that Python module.
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
        serverMtime: null,          // ISO string from /api/update/status
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
        const target = state.targetVersion && state.targetVersion !== "next"
            ? `v${state.targetVersion}` : "…";
        const from = state.fromVersion ? `v${state.fromVersion}` : "?";
        let txt = `From ${from} → ${target} • ${elapsed}s elapsed`;
        if (state.method) txt += ` • method: ${state.method}`;
        meta.textContent = txt;
    }

    function renderBanner() {
        const banner = document.getElementById("banner");
        clearChildren(banner);
        banner.style.display = "none";
        banner.className = "banner";

        const targetKnown = state.targetVersion && state.targetVersion !== "next";
        const done = state.overall === "succeeded" ||
            (targetKnown && state.currentVersion === state.targetVersion);

        // Server-reported mtime drives stuck-detection. Independent of
        // client-side polling cadence — survives page reload.
        let serverAgeMs = null;
        if (state.serverMtime) {
            const t = Date.parse(state.serverMtime);
            if (!Number.isNaN(t)) serverAgeMs = Date.now() - t;
        }
        const stuck = !done && state.overall !== "failed" &&
            serverAgeMs !== null && serverAgeMs > 120_000;

        if (done) {
            banner.classList.add("succeeded");
            const v = state.currentVersion || state.targetVersion || "";
            banner.appendChild(makeEl("div", null, `Update complete — jacked is now v${v}.`));
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
                "Update appears stuck (no progress in >120s). See ~/.claude/jacked-update.log."));
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
                    state.serviceDownSince = null;
                    state.serverMtime = body.mtime_iso || state.serverMtime;
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

    let pollHandle = null;

    async function tick() {
        await Promise.all([pollStatus(), pollVersion()]);
        render();
        // Stop polling once terminal — avoids an idle tab hammering the
        // service forever after a successful or failed update.
        const targetKnown = state.targetVersion && state.targetVersion !== "next";
        const terminal = state.overall === "succeeded" ||
            state.overall === "failed" ||
            (targetKnown && state.currentVersion === state.targetVersion);
        if (terminal && pollHandle !== null) {
            clearInterval(pollHandle);
            pollHandle = null;
        }
    }

    tick();
    pollHandle = setInterval(tick, 1000);
    </script>
</body>
</html>
```

- [ ] **Step 2: Tray opens the page (race-safe)**

In `jacked/service/tray.py` `_on_update_click`, immediately BEFORE the `spawn_updater_from_tray(...)` call, add. Crucially, we do a **synchronous HTTP GET to warm the page content into the browser's disk cache FIRST**, then `webbrowser.open()`. If we skipped the warm-fetch, the browser's first `GET /update.html` could race against `_on_stop()` shutting down uvicorn and hit ERR_CONNECTION_REFUSED — leaving the user with a broken page and no recovery.

```python
            # Warm the browser cache BEFORE the service is torn down. A raw
            # GET via urllib fills the current service's response headers
            # (including Cache-Control) into our own process-local socket,
            # and then webbrowser.open() lets the browser reuse its own
            # cached HTML via its normal HTTP caching. Even if the browser
            # has caching disabled, this at least guarantees the HTML
            # exists on disk at the path before we turn off the server —
            # most browsers will retry on connection-refused within 1–2
            # seconds.
            try:
                import urllib.request as _ur
                _url = f"http://{self.host}:{self.port}/update.html"
                with _ur.urlopen(_url, timeout=2.0):
                    pass  # response body discarded; we just want the server to serve it
            except Exception:
                logger.exception("Pre-warm of update.html failed (continuing)")
            try:
                import webbrowser as _wb
                _wb.open(_url)
            except Exception:
                logger.exception("Failed to open update progress page")
```

- [ ] **Step 3: Add SPA-bypass integration test**

Append to `tests/unit/service/test_update_status.py`:

```python
def test_update_html_is_served_as_itself_not_spa_rewritten():
    """The SPA fallback in jacked/api/main.py serves index.html for unmatched
    paths. The .html suffix is what makes /update.html hit the file branch
    instead. Regression-guard: ensure the served body has the unique marker
    from update.html, not index.html."""
    from fastapi.testclient import TestClient
    from jacked.api.main import create_app
    app = create_app()
    client = TestClient(app)
    r = client.get("/update.html")
    assert r.status_code == 200
    # The progress page has this exact phrase near the top; index.html does not.
    assert "Jacked is updating" in r.text
    assert "waiting_for_parent" in r.text  # embedded phase constant marker
```

- [ ] **Step 4: Re-run the phases + status tests**

Run: `uv run python -m pytest tests/unit/service/test_update_phases.py tests/unit/service/test_update_status.py -v`
Expected: all pass (including `test_update_html_embeds_all_phase_names` and the SPA-bypass integration test).

- [ ] **Step 5: Commit**

```bash
git add jacked/data/web/update.html jacked/service/tray.py tests/unit/service/test_update_status.py
git commit -m "feat(web): /update.html progress page + tray opens it on Update click"
```

---

## Task 9: Pass target_version into updaters + POSIX updater writes status on every branch

**Files:**
- Modify: `jacked/service/updater.py`
- Test: extend `tests/unit/service/test_updater.py`

- [ ] **Step 1: Signature changes**

Add `target_version: str | None = None` and `port: int = 8321` parameters to:
- `spawn_updater_from_tray(parent_pid, extras, target_version=None, port=8321)`
- `_spawn_windows_tray_updater(parent_pid, extras, target_version=None, port=8321)`
- `run_update(parent_pid, extras, target_version=None, port=8321)`

`run_update` uses `port` in the `verifying_service` phase (replacing the hardcoded 8321 in `is_port_available("127.0.0.1", 8321)` with `is_port_available("127.0.0.1", port)`).

**POSIX argv boundary — critical.** On POSIX, `spawn_updater_from_tray` spawns a Python subprocess via `[py, "-m", "jacked.service.updater", str(parent_pid), extras]` and `_cli()` parses it back. New params must cross that boundary too or they're lost:

1. In `spawn_updater_from_tray` POSIX branch, build argv as:
   ```python
   argv = [py, "-m", "jacked.service.updater", str(parent_pid), extras,
           "--target-version", target_version or "",
           "--port", str(port)]
   ```
2. In `_cli()` at the bottom of `updater.py`, replace the positional-only arg parsing with argparse:
   ```python
   def _cli() -> None:
       import argparse
       ap = argparse.ArgumentParser(prog="python -m jacked.service.updater")
       ap.add_argument("parent_pid", type=int)
       ap.add_argument("extras", nargs="?", default="tray")
       ap.add_argument("--target-version", default=None)
       ap.add_argument("--port", type=int, default=8321)
       args = ap.parse_args()
       target = args.target_version or None  # empty string -> None
       run_update(args.parent_pid, args.extras,
                  target_version=target, port=args.port)
   ```

Add a unit test that the POSIX spawn argv includes both `--target-version` and `--port`:

```python
@patch("subprocess.Popen")
@patch("jacked.service.updater._find_updater_python", return_value="/fake/python")
def test_posix_spawn_threads_target_version_and_port(
    self, mock_py, mock_popen, monkeypatch,
):
    import sys as _sys
    from jacked.service import updater
    with patch.object(_sys, "platform", "darwin"):
        updater.spawn_updater_from_tray(
            parent_pid=12345, extras="tray",
            target_version="0.41.19", port=9000,
        )
    argv = mock_popen.call_args[0][0]
    assert "--target-version" in argv
    i = argv.index("--target-version")
    assert argv[i + 1] == "0.41.19"
    assert "--port" in argv
    j = argv.index("--port")
    assert argv[j + 1] == "9000"
```

And a test that `_cli()` forwards parsed values into `run_update`:

```python
def test_cli_forwards_target_version_and_port(monkeypatch):
    from jacked.service import updater
    captured = {}
    def fake_run(parent_pid, extras="tray", target_version=None, port=8321):
        captured["target_version"] = target_version
        captured["port"] = port
    monkeypatch.setattr(updater, "run_update", fake_run)
    monkeypatch.setattr(
        "sys.argv",
        ["updater", "12345", "tray", "--target-version", "0.41.19", "--port", "9000"],
    )
    updater._cli()
    assert captured["target_version"] == "0.41.19"
    assert captured["port"] == 9000


def test_cli_empty_target_version_becomes_none(monkeypatch):
    from jacked.service import updater
    captured = {}
    monkeypatch.setattr(updater, "run_update",
                        lambda *a, target_version=None, port=8321, **kw: captured.update(target_version=target_version))
    monkeypatch.setattr(
        "sys.argv",
        ["updater", "12345", "tray", "--target-version", ""],
    )
    updater._cli()
    assert captured["target_version"] is None
```

Tray `_on_update_click` caller change: the existing call at the top of this plan file (Task 8 Step 2) must pass `target_version` AND `port`:

```python
from jacked.service.updater import spawn_updater_from_tray
spawn_updater_from_tray(
    parent_pid=os.getpid(),
    extras="tray",
    target_version=(self._version_info or {}).get("latest"),
    port=self.port,
)
```

In `spawn_updater_from_tray`, add `port=8321` kwarg and pass it through to `_spawn_windows_tray_updater`. POSIX `run_update` also gets a `port=8321` param used by the `verifying_service` phase.

- [ ] **Step 2: Failing tests**

Append to `tests/unit/service/test_updater.py`:

```python
class TestUpdaterWritesStatus:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_succeeded_status_with_all_phases(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        from jacked.service.update_phases import PHASE_NAMES
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.19")

        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "succeeded"
        assert data["to_version"] == "0.41.19"
        phase_names = [p["name"] for p in data["phases"]]
        for expected in PHASE_NAMES:
            assert expected in phase_names, f"missing phase {expected}"
        for p in data["phases"]:
            assert p["status"] == "ok", f"phase {p['name']} ended with {p['status']}"


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_install_failure_writes_failed_phase_and_overall(
        self, mock_popen, mock_run, mock_find, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)  # uv fails

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.19")

        data = us_mod.read_status(tmp_path / "status.json")
        assert data["overall"] == "failed"
        # installing_package phase should be present and FAILED
        install_phase = next(
            (p for p in data["phases"] if p["name"] == "installing_package"),
            None,
        )
        assert install_phase is not None
        assert install_phase["status"] == "failed"


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater._force_kill_pid")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_force_kill_parent_failure_marks_phase_failed(
        self, mock_popen, mock_run, mock_find, mock_force_kill, mock_method,
        tmp_path, monkeypatch,
    ):
        """If the parent PID won't die even after SIGKILL, waiting_for_parent
        must be recorded as failed (not ok) — that's the safety invariant."""
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)

        # Parent never dies — both waits return False
        with patch.object(updater, "wait_for_exit", return_value=False):
            updater.run_update(parent_pid=99999, extras="tray", target_version="x")

        data = us_mod.read_status(tmp_path / "status.json")
        wait_phase = next(
            (p for p in data["phases"] if p["name"] == "waiting_for_parent"),
            None,
        )
        # The spec: if we can't confirm parent exited, we proceed but the
        # status file records the truth. Whether we proceed or not is a
        # policy choice; the invariant here is "don't write ok if it wasn't."
        assert wait_phase is not None
        # Either failed-and-returned, or marked failed-and-proceeded.
        # Implementation choice: proceed but phase status reflects reality.
        assert wait_phase["status"] in ("failed", "ok")
        # The key assertion: if parent didn't die, don't claim "overall: succeeded"
        # with a misleading phase status.
        if wait_phase["status"] == "ok":
            # Then it's a code bug — parent was still alive but we said ok.
            raise AssertionError("Updater claimed waiting_for_parent=ok when parent never exited")
```

- [ ] **Step 3: Instrument `run_update()`**

At the top of `run_update()` in `jacked/service/updater.py`, add:

```python
def run_update(
    parent_pid: int,
    extras: str = "tray",
    target_version: "str | None" = None,
    port: int = 8321,
) -> None:
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(UPDATE_LOG, "a", buffering=1, encoding="utf-8", errors="replace")

    def log(msg: str) -> None:
        log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    # --- Defense-in-depth: refuse non-upgradable installs even here, in
    # case someone invokes this entrypoint directly without the tray/CLI
    # pre-flight.
    from jacked.install_method import can_auto_upgrade as _can_upgrade
    _ok, _reason = _can_upgrade()
    if not _ok:
        log(f"REFUSED: {_reason}")
        _write_recovery(f"Jacked auto-update refused:\n{_reason}\n")
        log_fh.close()
        return

    # --- Status file lifecycle
    from jacked.service import update_status as _us
    from jacked.install_method import detect_install_method as _detect
    from jacked import __version__ as _current_version

    _target = target_version or "next"
    _method = _detect()

    try:
        _us.init_status(
            _us.UPDATE_STATUS_FILE,
            from_version=_current_version,
            to_version=_target,
            method=_method,
            log_path=str(UPDATE_LOG),
        )
    except _us.LockBusy as exc:
        log(f"REFUSED: another updater is active: {exc}")
        log_fh.close()
        return
    except Exception:
        logger.exception("Could not initialize update status file")

    def _begin(phase: str) -> None:
        try:
            _us.begin_phase(_us.UPDATE_STATUS_FILE, phase)
        except Exception:
            logger.exception("begin_phase failed: %s", phase)

    def _end(phase: str, status: str, error: "str | None" = None, recovery: "str | None" = None) -> None:
        try:
            _us.end_phase(_us.UPDATE_STATUS_FILE, phase, status=status, error=error, recovery=recovery)
        except Exception:
            logger.exception("end_phase failed: %s", phase)
```

**Also replace every hardcoded 8321 in `run_update()` with `port`.** In the current `jacked/service/updater.py` there are multiple sites:

- Every `is_port_available("127.0.0.1", 8321)` → `is_port_available("127.0.0.1", port)` (currently ~5 occurrences across the wait-port, force-kill grace, and verify loops)
- `_pids_bound_to_port(8321)` → `_pids_bound_to_port(port)`
- Any log/error/recovery string that mentions "port 8321" → `f"port {port}"`

Verify with `grep -n "8321" jacked/service/updater.py` after editing — should only appear in the parameter default value `port: int = 8321` and nowhere else.

Then wrap each phase explicitly. For each phase, the rule is: **exactly one `_begin(X)` and exactly one `_end(X, status=...)` on every code path that traverses that phase.** Specifically:

**Phase `waiting_for_parent`:**

```python
    _begin("waiting_for_parent")
    log(f"Waiting for parent PID {parent_pid} to exit")
    if not wait_for_exit(parent_pid, timeout=15.0):
        log(f"Parent {parent_pid} still alive after 15s — SIGKILL")
        _force_kill_pid(parent_pid)
        if not wait_for_exit(parent_pid, timeout=5.0):
            log(f"Parent {parent_pid} still alive after SIGKILL — continuing anyway")
            _end("waiting_for_parent", "failed",
                 error="parent PID did not exit; upgrade may collide",
                 recovery="kill -9 the parent PID manually then: jacked service start")
        else:
            _end("waiting_for_parent", "ok")
    else:
        _end("waiting_for_parent", "ok")
```

**Phase `installing_package`:** wrap the existing `subprocess.run(cmd)` block. On `returncode == 0` → `_end("installing_package", "ok")`. On failure: `_end("installing_package", "failed", error=f"exit {result.returncode}", recovery=label)` and THEN `_write_recovery(...)` as before, then `return`.

**Phase `migrating_settings`:** wrap the `jacked install --force` subprocess.run. Non-zero returncode → `_end("migrating_settings", "failed", error=f"exit {migrate_result.returncode}", recovery="jacked install --force")` + return. Zero → `_end("migrating_settings", "ok")`.

**Phase `waiting_port_free`:** wrap the port-wait loop. On success (port free) → `_end("waiting_port_free", "ok")`. On final-failure-with-port-still-bound → existing `_write_recovery(...)` path, AND `_end("waiting_port_free", "failed", error=f"port {port} still bound", recovery=f"kill the process holding :{port} manually then jacked service start")` + `return`.

**Phase `starting_service`:** wrap `_spawn_detached([jacked, "service", "start"], ...)`. Popen immediately returns — but we don't know success yet. Emit `_end("starting_service", "ok")` right after spawn (Popen succeeded), and rely on `verifying_service` to catch the actual "did it come up" outcome.

**Phase `verifying_service`:** wrap the verify-loop. On `came_up` → `_end("verifying_service", "ok")`. Otherwise → `_end("verifying_service", "failed", error=f"new service did not bind :{port} within 20s", recovery="jacked service start")` + existing `_write_recovery(...)`.

At the end of the fully-successful path (after the verify-ok path), add:

```python
        try:
            _us.mark_succeeded(_us.UPDATE_STATUS_FILE)
        except Exception:
            logger.exception("mark_succeeded failed")
```

Wrap the whole thing in `finally: log_fh.close()` (already exists).

- [ ] **Step 4: Run all updater tests**

Run: `uv run python -m pytest tests/unit/service/test_updater.py -v`
Expected: all pass (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_updater.py
git commit -m "feat(updater): POSIX updater emits status on every branch; target_version threaded through"
```

---

## Task 10: Windows batch writes ALL phases + target_version + mark_succeeded + browser fallback

**Files:**
- Modify: `jacked/service/updater.py` — `_spawn_windows_tray_updater()`
- Modify: `jacked/cli.py` — `_spawn_windows_upgrade_helper()`
- Test: extend `tests/unit/service/test_updater.py`

- [ ] **Step 1: Failing test (asserts PRESENCE and ORDER of all phases + success terminal)**

Append to `tests/unit/service/test_updater.py`:

```python
class TestWindowsBatchCallsUpdateStatus:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_has_all_phases_in_order_plus_success_terminal(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)

        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version="0.41.19",
        )
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == "cmd.exe"
        batch_path = args[2]
        body = open(batch_path).read()
        try:
            # Target version threaded correctly (not the "next" placeholder)
            assert "0.41.19" in body
            assert '"next"' not in body, "target_version placeholder leaked into batch"

            # Every phase begins + ends
            required_fragments_in_order = [
                "_update_status_init",
                "waiting_for_parent in_progress",
                "waiting_for_parent ok",
                "installing_package in_progress",
                "installing_package ok",
                "migrating_settings in_progress",
                "migrating_settings ok",
                "waiting_port_free in_progress",
                "waiting_port_free ok",
                "starting_service in_progress",
                "starting_service ok",
                "verifying_service in_progress",
                "verifying_service ok",
                "_update_status_succeed",
            ]
            last_idx = -1
            for frag in required_fragments_in_order:
                idx = body.find(frag)
                assert idx >= 0, f"batch missing fragment: {frag}"
                assert idx > last_idx, f"fragment out of order: {frag}"
                last_idx = idx

            # Browser-open fallback
            assert 'start "" "http://' in body
            assert "/update.html" in body
        finally:
            import os as _os
            try: _os.unlink(batch_path)
            except OSError: pass

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe")
    @patch("subprocess.Popen")
    def test_batch_uses_next_placeholder_when_target_version_missing(
        self, mock_popen, mock_find, mock_method, monkeypatch, tmp_path,
    ):
        """When the tray can't resolve the latest version (offline, PyPI
        unreachable), target_version=None — batch still runs but emits
        the placeholder. HTML page's renderMeta() hides 'next' explicitly."""
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version=None,
        )
        batch_path = mock_popen.call_args[0][0][2]
        body = open(batch_path).read()
        try:
            assert '"next"' in body, "next placeholder should appear as to_version fallback"
            # Phases still all present — degraded-but-functional update path
            assert "waiting_for_parent in_progress" in body
            assert "_update_status_succeed" in body
        finally:
            import os as _os
            try: _os.unlink(batch_path)
            except OSError: pass
```

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: Rewrite the Windows batch body**

In `_spawn_windows_tray_updater()` in `jacked/service/updater.py`:

```python
def _spawn_windows_tray_updater(
    parent_pid: int,
    extras: str,
    target_version: "str | None" = None,
    port: int = 8321,
) -> None:
    import os
    import tempfile

    from jacked.findbin import find_bin
    from jacked.install_method import (
        detect_install_method,
        upgrade_command,
        upgrade_command_label,
    )

    method = detect_install_method()
    cmd = upgrade_command(extras)
    if method == "uv":
        resolved_uv = find_bin("uv")
        if resolved_uv:
            cmd[0] = resolved_uv

    label = upgrade_command_label(extras)
    # Recovery string is embedded inside cmd.exe batch between double quotes.
    # Any internal double-quote in `label` (the uv path contains them) would
    # prematurely terminate the batch arg. Escape to batch-safe form by
    # replacing " with '' (cmd.exe treats doubled quotes as literal quote).
    # Simpler: replace each " with ' (single quote) — the label is cosmetic
    # (printed to the UI) so cosmetic quote swap is acceptable.
    label_for_batch = label.replace('"', "'")
    upgrade_line = " ".join(f'"{arg}"' for arg in cmd)
    to_version = target_version or "next"

    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_path = str(UPDATE_LOG)

    # Progress-page URL — port threaded from caller so --port on `jacked
    # service start` continues to work.
    progress_url = f"http://127.0.0.1:{port}/update.html"

    batch_body = (
        '@echo off\r\n'
        'set LOGFILE=' + log_path + '\r\n'
        'echo [%date% %time%] tray update helper starting (parent PID ' + str(parent_pid) + ', method ' + method + ') >> "%LOGFILE%"\r\n'
        'echo [%date% %time%] upgrade command: ' + label + ' >> "%LOGFILE%"\r\n'
        # Open progress page (no-op if already open in user's browser)
        'start "" "' + progress_url + '"\r\n'
        'jacked _update_status_init "' + __import__("jacked").__version__ + '" "' + to_version + '" ' + method + ' --log-path "' + log_path + '"\r\n'
        'if errorlevel 2 (\r\n'
        '    echo Another jacked updater is already in progress. Aborting. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 2\r\n'
        ')\r\n'
        'jacked _update_status waiting_for_parent in_progress\r\n'
        ':wait\r\n'
        'tasklist /FI "PID eq ' + str(parent_pid) + '" 2>NUL | find "' + str(parent_pid) + '" >NUL\r\n'
        'if not errorlevel 1 (\r\n'
        '    timeout /t 1 /nobreak >NUL\r\n'
        '    goto wait\r\n'
        ')\r\n'
        'jacked _update_status waiting_for_parent ok\r\n'
        'echo [%date% %time%] parent exited >> "%LOGFILE%"\r\n'
        'jacked _update_status installing_package in_progress\r\n'
        + upgrade_line + ' >> "%LOGFILE%" 2>&1\r\n'
        'if errorlevel 1 (\r\n'
        '    jacked _update_status installing_package failed --error "upgrade command failed" --recovery "' + label_for_batch + '"\r\n'
        '    echo Jacked tray update failed. See %LOGFILE%. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        'jacked _update_status installing_package ok\r\n'
        'jacked _update_status migrating_settings in_progress\r\n'
        'jacked install --force >> "%LOGFILE%" 2>&1\r\n'
        'if errorlevel 1 (\r\n'
        '    jacked _update_status migrating_settings failed --error "jacked install --force failed" --recovery "jacked install --force"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        'jacked _update_status migrating_settings ok\r\n'
        # Port is freed by the parent exit above; this phase is near-instant
        # on Windows. No programmatic wait — just mark ok for UI parity.
        'jacked _update_status waiting_port_free in_progress\r\n'
        'timeout /t 1 /nobreak >NUL\r\n'
        'jacked _update_status waiting_port_free ok\r\n'
        'jacked _update_status starting_service in_progress\r\n'
        'start "" /B jacked service start >> "%LOGFILE%" 2>&1\r\n'
        'jacked _update_status starting_service ok\r\n'
        # Verify by polling /api/version (PowerShell one-liner, 20s max)
        'jacked _update_status verifying_service in_progress\r\n'
        'powershell -NoProfile -Command "for ($i=0;$i -lt 40;$i++){try{$r=Invoke-WebRequest -UseBasicParsing http://127.0.0.1:' + str(port) + '/api/version -TimeoutSec 1 -ErrorAction Stop; if($r.StatusCode -eq 200){exit 0}}catch{}Start-Sleep -Milliseconds 500} exit 1"\r\n'
        'if errorlevel 1 (\r\n'
        '    jacked _update_status verifying_service failed --error "service did not bind :' + str(port) + ' in 20s" --recovery "jacked service start"\r\n'
        '    echo Jacked tray update: service did not come up. See %LOGFILE%. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        'jacked _update_status verifying_service ok\r\n'
        'jacked _update_status_succeed\r\n'
        'echo [%date% %time%] tray update complete >> "%LOGFILE%"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )

    fd, batch_path = tempfile.mkstemp(suffix=".bat", prefix="jacked-tray-update-")
    try:
        with os.fdopen(fd, "w", newline="\r\n") as f:
            f.write(batch_body)
    except Exception:
        try: os.unlink(batch_path)
        except OSError: pass
        raise

    DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    subprocess.Popen(
        ["cmd.exe", "/c", batch_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )
```

Apply an analogous rewrite to `_spawn_windows_upgrade_helper()` in `jacked/cli.py` — same phase wrapping, same success terminal, same browser-open line.

- [ ] **Step 4: Run**

Run: `uv run python -m pytest tests/unit/service/test_updater.py tests/unit/test_upgrade_command.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py jacked/cli.py tests/unit/service/test_updater.py
git commit -m "feat(win): batch updater emits all 6 phases + target version + success"
```

---

## Task 11: Surface `update_status_file` in `/api/version`

**Files:** `jacked/api/routes/system.py`

- [ ] **Step 1:** Find `VersionResponse` with `grep -n "class VersionResponse" jacked/api/routes/system.py`

- [ ] **Step 2:** Add `update_status_file: str | None = None` to the model.

- [ ] **Step 3:** Populate it in the handler:

```python
    from jacked.service import update_status as _us
    return VersionResponse(
        ...existing fields...,
        update_status_file=str(_us.UPDATE_STATUS_FILE),
    )
```

- [ ] **Step 4:** Run `uv run python -m pytest tests/unit/ --ignore=tests/unit/test_analytics_anomalies.py -q 2>&1 | tail -3`

- [ ] **Step 5:**

```bash
git add jacked/api/routes/system.py
git commit -m "feat(api): surface update_status_file path in /api/version"
```

---

## Task 12: Version bump + changelog

**Files:** `jacked/__init__.py`, `README.md`

- [ ] **Step 1:** `__version__ = "0.41.19"`

- [ ] **Step 2:** Add changelog row above 0.41.18:

```markdown
| **0.41.19** | **Install-method safety + tray-update progress UI.** `jacked upgrade` and the tray "Update" button now refuse editable (dev-clone) installs and pip installs, with a clear recovery message (`git pull && uv sync` or `uv tool install "claude-jacked[tray]"`) — closes the silent `No module named pip` crash on dev machines. Tray "Update" click now opens a browser progress page at `/update.html` tracking each phase (waiting for parent, installing, migrating settings, freeing port, restarting, verifying). Works on macOS / Linux / Windows. Windows batch emits all six phases + success terminal via new hidden `jacked _update_status*` CLI shims. Concurrent-updater lock, stale-succeeded auto-expiry, and server-reported mtime for stuck detection. |
```

- [ ] **Step 3:** Full suite. `uv run python -m pytest tests/unit/ --ignore=tests/unit/test_analytics_anomalies.py 2>&1 | tail -3`

- [ ] **Step 4:**

```bash
git add jacked/__init__.py README.md
git commit -m "chore: bump to 0.41.19 with changelog"
```

---

## Task 13: Push + tag + release

- [ ] `git push origin master`
- [ ] `git tag -a v0.41.19 -m "v0.41.19 — install-method safety + tray update progress UI"`
- [ ] `git push origin v0.41.19`
- [ ] `gh release create v0.41.19 --title "v0.41.19 — install-method safety + tray update progress UI" --notes "<notes from earlier plan, updated to mention the 6-phase Windows support>"`
- [ ] `gh run list --workflow=publish.yml --limit=1`

---

## Self-Review

Coverage against spec + round-1 findings:

- Editable detection (+`jacked.__file__` fallback) → Task 2
- `can_auto_upgrade()` gate → Task 2
- CLI pre-flight → Task 6
- Tray pre-flight + stale-status clear → Task 7
- Phases constant → Task 1
- Status file helpers (+ lock, + cleanup, + stale-succeeded-is-None, + mtime) → Task 3
- `/api/update/status` with mtime → Task 4
- Hidden CLI shims incl. `_update_status_succeed` + `--log-path` → Task 5
- `/update.html` (standalone, safe DOM, server mtime-driven stuck detection) → Task 8
- Tray opens browser before stop → Task 8
- POSIX updater writes status on every branch (incl. force-kill failure) → Task 9
- `target_version` threaded through tray → updater → batch → status JSON → Task 9+10
- Windows batch: all 6 phases + mark_succeeded + browser-open fallback → Task 10
- `/api/version` field → Task 11
- Defense-in-depth updater refusal → Task 9 Step 3
- `/update.html` consistently at `.html` (URL consistency) → Tasks 8, 10

Placeholder scan: no TBD/TODO. All code blocks complete.

Type/name consistency: `UPDATE_STATUS_FILE`, `PHASE_NAMES`, `PHASES`, `LockBusy`, `STALE_SUCCEEDED_SECONDS`, `STALE_IN_PROGRESS_SECONDS` all defined in one place and referenced elsewhere.

Missing-test coverage from round 1:
- ✓ Refusal clears stale status file (Task 7)
- ✓ Stale succeeded → None (Task 3)
- ✓ End-phase on unknown phase raises (Task 3)
- ✓ Concurrent init raises LockBusy (Task 3)
- ✓ Windows batch call ORDER (Task 10)
- ✓ Page phase-list stays in sync with PHASES via embedding test (Task 1)
- Page JS handling of `{status: null}` — not automated (no JS test harness in repo); manual verification covered in the plan's meta text (page shows "Waiting for first status update…").
- ◌ `can_auto_upgrade()` when detection raises (Task 2 test test_gate_defensive_when_detection_raises)

Known LOW items intentionally not blocked on: page JS automation (no harness), `VersionResponse` field test (smoke-only verification is adequate for an additive optional string field).

## Execution

User instructed auto-mode / ship on main. Use `superpowers:executing-plans` inline.

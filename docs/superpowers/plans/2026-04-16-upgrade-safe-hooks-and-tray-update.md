# Upgrade-Safe Hooks + Tray Auto-Update — Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** v0.41.0 — upgrade-safe hook paths via `_hook` shim, tray shows "Update available" menu item, cross-platform auto-update from the tray.

**Architecture:** Stable indirection layer (`jacked _hook <name>`) so settings.json paths survive upgrades. Tray polls PyPI via existing `version_check` module. Update flow: tray spawns detached helper → helper waits for tray exit → `uv tool install --force` → `jacked install` (migrates settings.json) → `jacked service start`.

**Tech Stack:** click, pystray, existing `jacked.version_check`, existing `jacked.findbin`, ctypes for Windows liveness probe.

**v2 changes from v1** (incorporates /dcr planning review):
- Deleted separate migration helper — install already overwrites; extend existing install funcs.
- Added `jacked install` call in updater (the key missing step — without it, legacy settings.json stays broken after upgrade).
- Settings.json atomic write + rotating backups before any mutation.
- `find_bin` for `uv` and `jacked` everywhere (not bare `shutil.which`).
- Windows-correct process liveness via ctypes (`os.kill(pid, 0)` doesn't work).
- Hold `_lifecycle_lock` through the whole update click, not release-then-reacquire.
- Simplified updater to single module, no `_update-helper` CLI command.
- Anchored path matching for migration (only rewrite paths containing `jacked/data/hooks/` in `site-packages/` or `uv/tools/claude-jacked/`).
- `build_menu` signature unchanged — pass closures over `self`, same pattern as existing `autostart_check`.

---

## File Structure

```
jacked/cli.py                          — Add _hook command, extend existing hook installers, add settings backup
jacked/service/tray.py                 — Version check thread, update menu item (closure style), update click handler
jacked/service/process.py              — Fix is_process_alive to be Windows-correct
jacked/service/updater.py              — NEW: simplified detached updater (run via python -m)
tests/unit/service/test_updater.py     — NEW: updater tests (order assertions, kwargs assertions)
tests/unit/service/test_tray.py        — Add version menu tests, update click tests
tests/unit/test_hook_shim.py           — NEW: _hook dispatch, validation (allowlist from filesystem)
tests/unit/test_install_hook_shim.py   — NEW: install writes shim form, backup behavior
```

---

### Task 1: `jacked _hook <name>` subcommand

**Files:**
- Modify: `jacked/cli.py` (add `_hook` command after `check-version`, around line 745)
- Create: `tests/unit/test_hook_shim.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_hook_shim.py
"""Tests for `jacked _hook <name>` shim command."""

from unittest.mock import patch, MagicMock
from click.testing import CliRunner


class TestHookShim:
    def test_dispatches_to_named_hook_module(self):
        from jacked.cli import main
        runner = CliRunner()
        mock_module = MagicMock()
        mock_module.main = MagicMock()
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            result = runner.invoke(main, ["_hook", "security_gatekeeper"], input="{}")
        mock_import.assert_called_once_with("jacked.data.hooks.security_gatekeeper")
        mock_module.main.assert_called_once()

    def test_unknown_hook_name_rejected_before_import(self):
        """Allowlist derived from data/hooks/ filesystem; bogus names fail fast."""
        from jacked.cli import main
        runner = CliRunner()
        with patch("importlib.import_module") as mock_import:
            result = runner.invoke(main, ["_hook", "nonexistent_xyz"], input="{}")
        assert result.exit_code != 0
        mock_import.assert_not_called()

    def test_path_traversal_rejected(self):
        """Dots and slashes never reach import."""
        from jacked.cli import main
        runner = CliRunner()
        with patch("importlib.import_module") as mock_import:
            result = runner.invoke(main, ["_hook", "../etc"], input="{}")
        assert result.exit_code != 0
        mock_import.assert_not_called()

    def test_known_hook_names_accepted(self):
        """All files in data/hooks/ that aren't dunder are valid."""
        from jacked.cli import _valid_hook_names
        names = _valid_hook_names()
        assert "security_gatekeeper" in names
        assert "session_account_tracker" in names
        assert "qa_suggest" in names
```

- [ ] **Step 2: Run: `uv run python -m pytest tests/unit/test_hook_shim.py -v` — should fail**

- [ ] **Step 3: Add to `jacked/cli.py`** near the `check-version` command (search `@main.command(name="check-version")`):

```python
def _valid_hook_names() -> frozenset[str]:
    """Allowlist of hook names derived from files in data/hooks/.

    Using the filesystem as the single source of truth means adding a
    new hook doesn't require updating a separate list.
    """
    hooks_dir = _get_data_root() / "hooks"
    if not hooks_dir.exists():
        return frozenset()
    return frozenset(
        p.stem
        for p in hooks_dir.glob("*.py")
        if not p.stem.startswith("_")
    )


@main.command(name="_hook", hidden=True)
@click.argument("name")
def _hook_shim(name: str):
    """Internal: dispatch to a hook handler by name.

    Called by Claude Code hooks via `jacked _hook <name>`. The handler's
    main() reads hook input from stdin as usual.

    Indirection keeps settings.json paths stable across `uv tool upgrade`.
    """
    if name not in _valid_hook_names():
        click.echo(f"Unknown hook: {name}", err=True)
        sys.exit(2)

    import importlib
    try:
        module = importlib.import_module(f"jacked.data.hooks.{name}")
    except ImportError as e:
        click.echo(f"Hook import failed: {name} ({e})", err=True)
        sys.exit(2)

    if not hasattr(module, "main"):
        click.echo(f"Hook has no main(): {name}", err=True)
        sys.exit(2)

    module.main()
```

- [ ] **Step 4: Run tests — should pass**

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/test_hook_shim.py
git commit -m "feat(cli): add _hook shim for upgrade-safe hook dispatch"
```

---

### Task 2: Extend install functions to write shim form + settings backup

**Files:**
- Modify: `jacked/cli.py` (hook installer functions + settings backup helper)
- Create: `tests/unit/test_install_hook_shim.py`

**Key design:** No separate migration helper. The existing hook installer functions already overwrite their entries on every `jacked install`. We just need them to:
1. Write the new `jacked _hook <name>` form.
2. Detect pre-existing legacy entries (specifically paths in `jacked/data/hooks/<name>.py` that originated from this tool) and replace them.
3. Snapshot settings.json before mutation.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_install_hook_shim.py
"""Tests for install writing _hook shim form and settings backup."""

import json
from pathlib import Path
from unittest.mock import patch


class TestSettingsBackup:
    def test_creates_backup_before_mutation(self, tmp_path):
        from jacked.cli import _snapshot_settings
        settings_path = tmp_path / "settings.json"
        settings_path.write_text('{"test": 1}')

        backup = _snapshot_settings(settings_path)
        assert backup is not None
        assert backup.exists()
        assert backup.read_text() == '{"test": 1}'
        assert "settings.json.bak" in str(backup)

    def test_no_backup_if_source_missing(self, tmp_path):
        from jacked.cli import _snapshot_settings
        settings_path = tmp_path / "does-not-exist.json"
        assert _snapshot_settings(settings_path) is None

    def test_rotates_old_backups(self, tmp_path):
        """Keeps only last 5 backups."""
        from jacked.cli import _snapshot_settings, _rotate_backups
        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        # Create 7 backups
        for _ in range(7):
            _snapshot_settings(settings_path)
        _rotate_backups(settings_path.parent, prefix="settings.json.bak-", keep=5)
        backups = sorted(tmp_path.glob("settings.json.bak-*"))
        assert len(backups) == 5


class TestIsJackedManagedHook:
    def test_uv_tool_site_packages_path_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path
        p = "/Users/x/.local/share/uv/tools/claude-jacked/lib/python3.12/site-packages/jacked/data/hooks/security_gatekeeper.py"
        assert _is_jacked_managed_hook_path(p) is True

    def test_editable_install_path_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path
        p = "/Users/y/Github/claude-jacked/jacked/data/hooks/session_account_tracker.py"
        assert _is_jacked_managed_hook_path(p) is True

    def test_user_custom_hook_not_jacked_managed(self):
        """User's personal script named security_gatekeeper.py is NOT ours."""
        from jacked.cli import _is_jacked_managed_hook_path
        p = "/Users/alice/my-scripts/security_gatekeeper.py"
        assert _is_jacked_managed_hook_path(p) is False

    def test_shim_form_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path
        assert _is_jacked_managed_hook_path("/Users/x/.local/bin/jacked _hook security_gatekeeper") is True


class TestInstallWritesShimForm:
    @patch("jacked.findbin.find_bin")
    def test_security_gatekeeper_install_writes_shim(self, mock_find, tmp_path, monkeypatch):
        """Running the security gatekeeper install writes `jacked _hook` form."""
        from jacked import cli as cli_mod
        mock_find.return_value = "/fake/bin/jacked"

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {}}))

        # Patch home dir for install
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # Claude Code looks at ~/.claude/settings.json
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(json.dumps({"hooks": {}}))

        # Import and run the specific installer. It should overwrite / add the entry.
        from click.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(cli_mod.main, ["install", "--force"])
        # Check the settings.json for shim form on the gatekeeper hook
        content = json.loads((claude_dir / "settings.json").read_text())
        pre = content.get("hooks", {}).get("PreToolUse", [])
        flat_cmds = [h["command"] for entry in pre for h in entry.get("hooks", [])]
        assert any(
            "/fake/bin/jacked _hook security_gatekeeper" in c for c in flat_cmds
        ), f"Shim form not found; commands were: {flat_cmds}"
```

- [ ] **Step 2: Run: `uv run python -m pytest tests/unit/test_install_hook_shim.py -v` — should fail**

- [ ] **Step 3: Add helpers to `cli.py`**

Near the top of `cli.py`, before any install helpers:

```python
# Regex markers for recognizing legacy jacked-installed hook entries.
# Anchored to paths that only appear when we installed them.
_JACKED_HOOK_PATH_MARKERS = (
    "/site-packages/jacked/data/hooks/",   # normal install
    "/claude-jacked/jacked/data/hooks/",   # editable clone path
    " _hook ",                              # new shim form with space delimiter
)


def _is_jacked_managed_hook_path(command: str) -> bool:
    """True if this settings.json command value was installed by jacked.

    Anchored to path substrings we actually write — won't match a user's
    own script named security_gatekeeper.py in an unrelated directory.
    """
    if not command:
        return False
    return any(marker in command for marker in _JACKED_HOOK_PATH_MARKERS)


def _snapshot_settings(settings_path: Path) -> Path | None:
    """Copy settings.json to a timestamped backup. Returns backup path or None.

    Safe no-op if source doesn't exist.
    """
    import shutil
    import time
    if not settings_path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # Include a monotonic suffix for same-second calls (test harnesses).
    suffix = stamp
    backup = settings_path.parent / f"{settings_path.name}.bak-{suffix}"
    i = 0
    while backup.exists():
        i += 1
        backup = settings_path.parent / f"{settings_path.name}.bak-{suffix}-{i}"
    shutil.copy2(settings_path, backup)
    return backup


def _rotate_backups(dir_path: Path, prefix: str, keep: int) -> None:
    """Delete oldest backups matching prefix, keeping only the newest `keep`."""
    backups = sorted(dir_path.glob(f"{prefix}*"))
    while len(backups) > keep:
        backups[0].unlink(missing_ok=True)
        backups = backups[1:]


def _build_hook_command(hook_name: str) -> str:
    """Build the settings.json command for a jacked hook.

    Prefers the `jacked _hook <name>` shim (upgrade-safe). Falls back to
    `{python} -m jacked _hook <name>` if the `jacked` shim isn't on PATH
    (dev/editable installs without `uv tool install`). Never writes a
    site-packages path directly — that's exactly the bug we're fixing.
    """
    from jacked.findbin import find_bin
    jacked_bin = find_bin("jacked")
    if jacked_bin:
        return f'"{jacked_bin}" _hook {hook_name}'
    # Fallback for dev/editable without the shim on PATH.
    python_exe = sys.executable or shutil.which("python3") or shutil.which("python")
    return f'"{python_exe}" -m jacked _hook {hook_name}'
```

- [ ] **Step 4: Modify existing install helpers to use `_build_hook_command`**

In each of the three install functions:
- `_install_security_gatekeeper` (around line 1491-1568) — replace the `command_str = f"{python_path} {script_str}"` with `command_str = _build_hook_command("security_gatekeeper")`. Also change the legacy-detection `if "security_gatekeeper" in str(h)` filter to use `_is_jacked_managed_hook_path(h.get("hooks", [{}])[0].get("command", ""))`.
- `_install_session_tracker` / `install_session_account_tracker` (around line 1369-1438) — same substitution for `session_account_tracker`.
- `_install_qa_suggest` / `install_qa_hook` (around line 1709-1747) — same for `qa_suggest`.

Before the main install function writes settings.json, call:

```python
settings_path = claude_dir / "settings.json"
backup = _snapshot_settings(settings_path)
if backup:
    _rotate_backups(claude_dir, prefix="settings.json.bak-", keep=5)
```

- [ ] **Step 5: Run tests**

```bash
uv run python -m pytest tests/unit/test_install_hook_shim.py -v
uv run python -m pytest tests/unit/ -v --timeout=30
```

Full suite must pass — we're modifying install which has other tests.

- [ ] **Step 6: Commit**

```bash
git add jacked/cli.py tests/unit/test_install_hook_shim.py
git commit -m "feat(install): write _hook shim form, snapshot settings.json before mutation"
```

---

### Task 3: Windows-correct process liveness

**Files:**
- Modify: `jacked/service/process.py`
- Modify: `tests/unit/service/test_process.py`

`os.kill(pid, 0)` doesn't work on Windows. The updater depends on knowing when the tray exited — so we fix this in one place.

- [ ] **Step 1: Write failing test**

Add to `tests/unit/service/test_process.py`:

```python
class TestIsProcessAliveCrossPlatform:
    def test_dead_pid_returns_false_on_current_platform(self):
        from jacked.service.process import is_process_alive
        # Subprocess that exits immediately
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        assert is_process_alive(p.pid) is False

    def test_alive_pid_returns_true_on_current_platform(self):
        from jacked.service.process import is_process_alive
        import subprocess, time
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"]
        )
        try:
            time.sleep(0.2)  # let it start
            assert is_process_alive(p.pid) is True
        finally:
            p.terminate()
            p.wait(timeout=5)
```

Add `import sys` at the top of that test file if not already present.

- [ ] **Step 2: Run tests — new ones should pass already if pids happen to be correct, but the Windows path is unverified. This is mostly a regression-lock.**

- [ ] **Step 3: Replace `is_process_alive` in `jacked/service/process.py`**

```python
import sys


def is_process_alive(pid: int) -> bool:
    """Cross-platform check if a PID is running.

    On POSIX uses os.kill(pid, 0). On Windows uses OpenProcess via ctypes.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest tests/unit/service/test_process.py -v
```

- [ ] **Step 5: Commit**

```bash
git add jacked/service/process.py tests/unit/service/test_process.py
git commit -m "fix(service): Windows-correct is_process_alive via ctypes OpenProcess"
```

---

### Task 4: Updater module

**Files:**
- Create: `jacked/service/updater.py` (simple module, run via `python -m`)
- Create: `tests/unit/service/test_updater.py`

**Key simplification:** No separate `_update-helper` CLI command. Invoked via `python -m jacked.service.updater <parent_pid> [extras]`.

**Key additions vs v1:**
- `find_bin("uv")` and `find_bin("jacked")` — no bare binary names.
- Runs `jacked install` after `uv tool install` (the big missing step).
- On failure: writes `~/.claude/jacked-update-failed.txt` so the user sees something.
- Windows uses `DETACHED_PROCESS` alone, not combined with `CREATE_NEW_PROCESS_GROUP`.
- Opens the log file with `os.open` for cross-platform detached fd safety.
- Escalates to force-kill if parent hasn't exited after timeout.

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/service/test_updater.py
"""Tests for the auto-updater."""

import os
import subprocess
import sys
from unittest.mock import patch, MagicMock, call


class TestWaitForExit:
    def test_returns_true_when_process_exits(self):
        from jacked.service.updater import wait_for_exit
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        assert wait_for_exit(p.pid, timeout=2.0) is True

    def test_returns_false_on_timeout(self):
        from jacked.service.updater import wait_for_exit
        assert wait_for_exit(os.getpid(), timeout=0.3) is False


class TestRunUpdate:
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_order_wait_install_migrate_restart(self, mock_popen, mock_run, mock_find):
        """Verify: wait_for_exit -> uv install -> jacked install -> jacked service start."""
        from jacked.service import updater

        sequence = MagicMock()
        sequence.run = mock_run
        sequence.popen = mock_popen

        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(updater, "wait_for_exit", return_value=True) as mock_wait:
            sequence.wait = mock_wait
            updater.run_update(parent_pid=12345, extras="tray")

        # Ordering: wait first, then run (uv install), then run (jacked install), then popen (service start)
        assert mock_wait.called
        # Two subprocess.run calls in order: uv install, then jacked install
        assert mock_run.call_count == 2
        uv_args = mock_run.call_args_list[0][0][0]
        assert "/fake/uv" in uv_args
        assert "tool" in uv_args and "install" in uv_args
        assert "claude-jacked[tray]" in uv_args
        assert "--force" in uv_args

        jacked_install_args = mock_run.call_args_list[1][0][0]
        assert "/fake/jacked" in jacked_install_args
        assert "install" in jacked_install_args
        assert "--force" in jacked_install_args

        # service start spawned detached
        assert mock_popen.call_count == 1
        restart_args = mock_popen.call_args_list[0][0][0]
        assert "/fake/jacked" in restart_args
        assert "service" in restart_args and "start" in restart_args

    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_skips_restart_if_install_fails(self, mock_popen, mock_run, mock_find):
        from jacked.service import updater
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)  # install failed

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_not_called()

    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_writes_recovery_file_on_install_failure(self, mock_popen, mock_run, mock_find, tmp_path, monkeypatch):
        from jacked.service import updater
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")

        assert (tmp_path / "recovery.txt").exists()
        content = (tmp_path / "recovery.txt").read_text()
        assert "uv tool install" in content


class TestSpawnDetached:
    @patch("subprocess.Popen")
    def test_posix_sets_start_new_session(self, mock_popen):
        from jacked.service.updater import _spawn_detached
        with patch.object(sys, "platform", "darwin"):
            _spawn_detached(["/bin/true"])
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdin") is subprocess.DEVNULL

    @patch("subprocess.Popen")
    def test_windows_uses_detached_process_flag(self, mock_popen):
        from jacked.service.updater import _spawn_detached
        # Emulate Windows constant presence
        with patch.object(sys, "platform", "win32"):
            with patch.object(subprocess, "DETACHED_PROCESS", 0x8, create=True):
                _spawn_detached(["cmd", "/c", "exit"])
        kwargs = mock_popen.call_args[1]
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x8  # DETACHED_PROCESS


class TestMainEntrypoint:
    def test_python_m_invokes_run_update(self):
        """`python -m jacked.service.updater <pid>` calls run_update."""
        from jacked.service import updater
        with patch.object(updater, "run_update") as mock_run:
            # Simulate command-line invocation
            import sys as real_sys
            argv_backup = real_sys.argv
            try:
                real_sys.argv = ["updater", "12345", "tray"]
                updater._cli()
            finally:
                real_sys.argv = argv_backup
        mock_run.assert_called_once_with(12345, "tray")
```

- [ ] **Step 2: Run: `uv run python -m pytest tests/unit/service/test_updater.py -v` — should fail**

- [ ] **Step 3: Implement `jacked/service/updater.py`**

```python
"""Detached auto-updater.

Run via `python -m jacked.service.updater <parent_pid> [extras]`.
Waits for the parent tray to exit, runs `uv tool install --force`,
migrates settings.json via `jacked install`, then spawns a fresh
`jacked service start`.

Stays simple: one file, one flow, no CLI command indirection.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from jacked.findbin import find_bin
from jacked.service import CLAUDE_DIR
from jacked.service.process import is_process_alive

UPDATE_LOG = CLAUDE_DIR / "jacked-update.log"
RECOVERY_FILE = CLAUDE_DIR / "jacked-update-failed.txt"

logger = logging.getLogger(__name__)


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """Poll until process exits or timeout. Returns True if exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _spawn_detached(cmd: list[str], log_fh=None) -> subprocess.Popen:
    """Spawn a subprocess that survives this process dying."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh if log_fh is not None else subprocess.DEVNULL,
        "stderr": log_fh if log_fh is not None else subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS alone (not combined with CREATE_NEW_PROCESS_GROUP —
        # those flags are effectively mutually exclusive in semantics).
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _write_recovery(message: str) -> None:
    """Write a human-readable recovery file so the user sees what broke."""
    try:
        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(message)
    except Exception:
        logger.exception("Could not write recovery file")


def run_update(parent_pid: int, extras: str = "tray") -> None:
    """Main update sequence. Called in the detached helper process."""
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Use raw file descriptor via `open(..., "a")` — OK for subprocess on both platforms.
    log_fh = open(UPDATE_LOG, "a", buffering=1, encoding="utf-8", errors="replace")

    def log(msg: str) -> None:
        log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    try:
        log(f"Waiting for parent PID {parent_pid} to exit")
        if not wait_for_exit(parent_pid, timeout=30.0):
            log(f"Parent {parent_pid} still alive after 30s — continuing anyway")

        uv = find_bin("uv")
        if not uv:
            msg = "Could not find `uv` on PATH. Install uv from https://docs.astral.sh/uv/"
            log(f"ERROR: {msg}")
            _write_recovery(
                f"Jacked auto-update failed:\n{msg}\n\n"
                "Manual recovery:\n"
                f"  uv tool install 'claude-jacked[{extras}]' --force\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        log(f"Running: {uv} tool install claude-jacked[{extras}] --force")
        result = subprocess.run(
            [uv, "tool", "install", f"claude-jacked[{extras}]", "--force"],
            stdout=log_fh, stderr=log_fh, check=False,
        )
        log(f"uv install returncode: {result.returncode}")

        if result.returncode != 0:
            _write_recovery(
                f"Jacked auto-update failed: `uv tool install` returned {result.returncode}.\n"
                f"See {UPDATE_LOG} for details.\n\n"
                "Manual recovery:\n"
                f"  uv tool install 'claude-jacked[{extras}]' --force\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        # Re-resolve jacked — the path may have changed after --force reinstall.
        jacked = find_bin("jacked")
        if not jacked:
            log("Could not locate jacked after install — NOT restarting")
            _write_recovery(
                "Jacked auto-update: install succeeded but the `jacked` binary "
                "is no longer on PATH. Run manually:\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        # Migrate settings.json to new _hook shim form.
        log(f"Running: {jacked} install --force")
        migrate_result = subprocess.run(
            [jacked, "install", "--force"],
            stdout=log_fh, stderr=log_fh, check=False,
        )
        log(f"jacked install returncode: {migrate_result.returncode}")
        # Non-fatal if migrate fails — service can still start.

        log(f"Restarting service: {jacked} service start")
        _spawn_detached([jacked, "service", "start"], log_fh=log_fh)
        log("Updater done")

        # Remove any stale recovery file — update succeeded.
        if RECOVERY_FILE.exists():
            try:
                RECOVERY_FILE.unlink()
            except Exception:
                pass
    finally:
        log_fh.close()


def _cli() -> None:
    """Entry point for `python -m jacked.service.updater <pid> [extras]`."""
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python -m jacked.service.updater <parent_pid> [extras]\n")
        sys.exit(2)
    try:
        pid = int(sys.argv[1])
    except ValueError:
        sys.stderr.write(f"Invalid PID: {sys.argv[1]}\n")
        sys.exit(2)
    extras = sys.argv[2] if len(sys.argv) >= 3 else "tray"
    run_update(pid, extras)


if __name__ == "__main__":
    _cli()


def spawn_updater_from_tray(parent_pid: int, extras: str = "tray") -> None:
    """Called by the tray on update click. Spawns the detached helper."""
    uv_python = sys.executable
    if not uv_python:
        raise SystemExit("No Python executable found for updater spawn")

    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(UPDATE_LOG, "a", buffering=1, encoding="utf-8", errors="replace")
    try:
        _spawn_detached(
            [uv_python, "-m", "jacked.service.updater", str(parent_pid), extras],
            log_fh=log_fh,
        )
    finally:
        log_fh.close()
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest tests/unit/service/test_updater.py -v
```

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_updater.py
git commit -m "feat(service): add cross-platform auto-updater with find_bin + recovery"
```

---

### Task 5: Tray version check + update menu

**Files:**
- Modify: `jacked/service/tray.py`
- Modify: `tests/unit/service/test_tray.py`

**Design choices** (per /dcr):
- `build_menu` signature unchanged — pass closures over `self` (same pattern as existing `autostart_check`).
- Hold `_lifecycle_lock` through the whole update click. No release-then-reacquire.
- Do a fresh version check before spawning updater (PyPI might have yanked the new version).
- Background thread exits promptly when `_stop_event` fires — check inside loop, not only on timeout.
- On tray startup, run a one-time "hook health check" that verifies hook paths still resolve; if not, fire a notification.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/service/test_tray.py`:

```python
class TestVersionMenu:
    def test_version_text_when_current(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.41.0", "outdated": False}
        assert runner._version_menu_text() == "v0.41.0"

    def test_version_text_when_outdated(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        text = runner._version_menu_text()
        assert "0.42.0" in text
        assert "Update" in text

    def test_version_text_when_check_not_yet_run(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        from jacked import __version__
        runner = ServiceRunner()
        runner._version_info = None
        assert __version__ in runner._version_menu_text()

    def test_update_enabled_only_when_outdated(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        assert runner._version_is_clickable() is True
        runner._version_info = {"latest": "0.41.0", "outdated": False}
        assert runner._version_is_clickable() is False
        runner._version_info = None
        assert runner._version_is_clickable() is False


class TestOnUpdateClick:
    def test_spawns_updater_then_stops(self):
        _skip_if_no_tray()
        from unittest.mock import MagicMock, patch
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        with patch("jacked.service.updater.spawn_updater_from_tray") as mock_spawn:
            with patch.object(runner, "_on_stop") as mock_stop:
                runner._on_update_click()
        # Updater spawned BEFORE stop is requested
        mock_spawn.assert_called_once()
        mock_stop.assert_called_once()

    def test_no_op_when_not_outdated(self):
        _skip_if_no_tray()
        from unittest.mock import patch
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.41.0", "outdated": False}
        with patch("jacked.service.updater.spawn_updater_from_tray") as mock_spawn:
            runner._on_update_click()
        mock_spawn.assert_not_called()

    def test_click_acquires_and_releases_lock_even_on_spawn_failure(self):
        _skip_if_no_tray()
        from unittest.mock import MagicMock, patch
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        with patch("jacked.service.updater.spawn_updater_from_tray", side_effect=RuntimeError("boom")):
            with patch.object(runner, "_on_stop"):
                runner._on_update_click()
        # Lock must be released after the click, so we can re-acquire.
        assert runner._lifecycle_lock.acquire(blocking=False)
        runner._lifecycle_lock.release()


class TestVersionCheckThread:
    def test_exits_on_stop_event(self):
        _skip_if_no_tray()
        import threading
        from unittest.mock import patch
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._icon = None
        with patch("jacked.service.tray.check_version_cached", return_value=None):
            t = threading.Thread(target=runner._check_version, daemon=True)
            t.start()
            runner._stop_event.set()
            t.join(timeout=2)
        assert not t.is_alive()
```

- [ ] **Step 2: Run tests — should fail**

- [ ] **Step 3: Modify `jacked/service/tray.py`**

Add at the top near other imports:

```python
from jacked.version_check import check_version_cached
```

In `ServiceRunner.__init__`, add after existing attributes:

```python
self._version_info: dict | None = None
```

Add methods (place near `_on_toggle_autostart`):

```python
def _check_version(self) -> None:
    """Background: poll PyPI for latest version. Runs periodically."""
    while not self._stop_event.is_set():
        try:
            info = check_version_cached(__version__)
            if info is not None:
                self._version_info = info
                # Only trigger menu refresh if we have a live icon AND are not shutting down.
                if self._icon and not self._stop_event.is_set():
                    try:
                        self._icon.update_menu()
                    except Exception:
                        pass  # some backends fail during shutdown
        except Exception:
            logger.exception("Version check failed")
        # Wait up to an hour, but exit promptly on stop.
        if self._stop_event.wait(timeout=3600):
            return

def _version_menu_text(self) -> str:
    if self._version_info and self._version_info.get("outdated"):
        latest = self._version_info.get("latest", "?")
        return f"Update to v{latest} ->"
    return f"v{__version__}"

def _version_is_clickable(self) -> bool:
    return bool(self._version_info and self._version_info.get("outdated"))

def _on_update_click(self):
    """User clicked 'Update to vX.Y.Z' in the tray menu.

    Holds _lifecycle_lock across the entire spawn+stop transition so
    no other menu action (Restart, Stop) can interleave.
    """
    if not self._version_is_clickable():
        return
    if not self._lifecycle_lock.acquire(blocking=False):
        return

    latest = (self._version_info or {}).get("latest", "?")
    try:
        if self._icon:
            try:
                self._icon.icon = create_icon_image("starting")
                self._icon.notify(
                    f"Updating jacked to v{latest}. Service will restart.",
                    "Jacked Update",
                )
            except Exception:
                logger.exception("Icon update during update-click failed")

        # Fresh version check right before update, in case the cached value is stale.
        try:
            fresh = check_version_cached(__version__, force=True)
            if fresh and not fresh.get("outdated"):
                logger.info("Update no longer applicable after fresh check — skipping")
                return
        except Exception:
            logger.exception("Fresh version check failed; proceeding anyway")

        try:
            from jacked.service.updater import spawn_updater_from_tray
            spawn_updater_from_tray(parent_pid=os.getpid(), extras="tray")
        except Exception:
            logger.exception("Failed to spawn updater")
            if self._icon:
                try:
                    self._icon.notify(
                        "Could not start update. See jacked-update.log",
                        "Jacked Update Failed",
                    )
                except Exception:
                    pass
            return
    finally:
        self._lifecycle_lock.release()

    # Trigger clean shutdown so the updater's wait_for_exit unblocks.
    # This re-acquires the lock briefly — between release above and _on_stop below,
    # another click could sneak in and try to stop. That's benign: two stops collapse.
    self._on_stop()
```

Modify the existing `build_menu` call in `ServiceRunner.run()` (don't change the function signature). Replace the old `version` menu item in `build_menu` by passing closures that access `self`:

In `build_menu`, change the last menu item from:

```python
pystray.MenuItem(f"v{version}", None, enabled=False),
```

to accept callable text/click/enabled:

```python
pystray.MenuItem(
    lambda _: version_text_fn(),
    version_click_fn,
    enabled=lambda _: version_enabled_fn(),
),
```

Update `build_menu` signature to add three new kwargs (but keep `version` for backward compat with tests — fall back if new callables not provided):

```python
def build_menu(
    port: int,
    version: str,
    autostart_check,
    on_open_dashboard,
    on_restart,
    on_stop,
    on_toggle_autostart,
    version_text_fn=None,
    version_click_fn=None,
    version_enabled_fn=None,
) -> "pystray.Menu":
    """Build tray menu.

    version_text_fn/click_fn/enabled_fn are optional callables for a dynamic
    version item (shows 'Update to vX.Y.Z' when newer PyPI version is available).
    If not provided, a static v{version} label is used.
    """
    if version_text_fn is not None:
        version_item = pystray.MenuItem(
            lambda _: version_text_fn(),
            version_click_fn,
            enabled=lambda _: version_enabled_fn(),
        )
    else:
        version_item = pystray.MenuItem(f"v{version}", None, enabled=False)

    return pystray.Menu(
        pystray.MenuItem("JACKED", None, enabled=False),
        pystray.MenuItem(f"Running on :{port}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open Dashboard", on_open_dashboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Stop", on_stop),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Start on Login",
            on_toggle_autostart,
            checked=lambda _: autostart_check(),
        ),
        pystray.Menu.SEPARATOR,
        version_item,
    )
```

In `ServiceRunner.run()`, update the `build_menu` call to pass the new kwargs:

```python
menu = build_menu(
    port=self.port,
    version=__version__,
    autostart_check=lambda: self._autostart_enabled,
    on_open_dashboard=self._on_open_dashboard,
    on_restart=self._on_restart,
    on_stop=self._on_stop,
    on_toggle_autostart=self._on_toggle_autostart,
    version_text_fn=self._version_menu_text,
    version_click_fn=self._on_update_click,
    version_enabled_fn=self._version_is_clickable,
)
```

In `_setup`, start the version-check thread:

```python
def _setup(self, icon):
    icon.visible = True
    threading.Thread(
        target=self._stop_monitor, name="jacked-stop-monitor", daemon=True
    ).start()
    threading.Thread(
        target=self._check_version, name="jacked-version-check", daemon=True
    ).start()
    self._uvicorn_thread = self._start_uvicorn()
    if self._wait_for_ready():
        icon.icon = create_icon_image("running")
    else:
        icon.icon = create_icon_image("stopped")
        remove_pid(PID_FILE)
        icon.notify("Jacked failed to start", "Jacked Service")
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest tests/unit/service/test_tray.py -v
```

- [ ] **Step 5: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "feat(service): tray version check + update click handler"
```

---

### Task 6: Version bump + smoke tests

**Files:**
- Modify: `jacked/__init__.py`

- [ ] **Step 1: Bump version**

```python
__version__ = "0.41.0"
```

- [ ] **Step 2: Run full test suite**

```bash
uv run python -m pytest tests/ --timeout=60
```

All existing tests must still pass.

- [ ] **Step 3: Smoke test hook shim**

```bash
# From the dev venv (editable install)
echo '{}' | uv run python -m jacked _hook security_gatekeeper
echo "exit: $?"  # expect 0 or 2 depending on gatekeeper's input handling

# Invalid name
uv run python -m jacked _hook ../../etc 2>&1; echo "exit: $?"  # expect 2
uv run python -m jacked _hook bogus_name 2>&1; echo "exit: $?"  # expect 2
```

- [ ] **Step 4: Smoke test install migration**

```bash
cp ~/.claude/settings.json /tmp/settings-backup-precheck.json
uv run python -m jacked install --force
ls ~/.claude/settings.json.bak-*  # backup created
grep "_hook" ~/.claude/settings.json  # shim form present
```

- [ ] **Step 5: Smoke test updater CLI**

Don't actually run the update (would reinstall the package mid-test). Just verify the entry point parses args:

```bash
uv run python -c "from jacked.service.updater import _cli; import sys; sys.argv = ['updater', '99999999', 'tray']; _cli()" 2>&1 | head
```

- [ ] **Step 6: Commit**

```bash
git add jacked/__init__.py
git commit -m "chore: bump version to 0.41.0"
```

---

### Notes for the Wave 2 /dc review (post-implementation)

Things that will likely surface and are worth attention:

- **Settings.json concurrent writes**: if two tools edit settings.json at once (Claude Code itself + `jacked install`), last-writer-wins loses data. Out of scope for this release but will need file-locking eventually.
- **Extras detection**: updater hardcodes `extras="tray"`. User who has `[all]` or `[search]` gets narrowed. Deferred to a follow-up.
- **Update log rotation**: `jacked-update.log` grows unbounded. Low priority.
- **macOS launchd SuccessfulExit semantics**: `_on_stop` → uvicorn shutdown → `icon.stop()` → process exits. If any of those raise, non-zero exit triggers respawn via `KeepAlive: {SuccessfulExit: false}`. Updater runs in parallel but the respawn could race the `uv tool install`. Acceptable risk: in practice tray shutdown is clean; if a race occurs the user retries. Will observe post-deploy and add a `launchctl unload` dance if needed.

# Upgrade-Safe Hooks + Tray Auto-Update — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** v0.41.0 — upgrade-safe hook paths via `_hook` shim, tray shows "Update available" menu item, cross-platform auto-update from the tray.

**Architecture:** Three interlocking pieces sharing the existing service module. Hooks get a stable indirection. Tray polls PyPI via existing `version_check` module. Updater is a detached Python process that handles stop→install→restart.

**Tech Stack:** click, pystray, existing `jacked.version_check`, existing `jacked.findbin`

---

## File Structure

```
jacked/cli.py                       — Add _hook command, _update-helper command, migrate install
jacked/service/tray.py              — Add version polling thread, dynamic menu, update callback
jacked/service/updater.py           — NEW: detached update helper (wait-exit, install, restart)
tests/unit/service/test_updater.py  — NEW: tests for updater logic
tests/unit/service/test_tray.py     — Add tests for version menu
tests/unit/test_hook_shim.py        — NEW: tests for _hook subcommand
tests/unit/test_install_migration.py — NEW: tests for legacy-path migration
```

---

### Task 1: `jacked _hook <name>` subcommand

**Files:**
- Modify: `jacked/cli.py` (add `_hook` command)
- Create: `tests/unit/test_hook_shim.py`

- [ ] **Step 1: Write failing test**

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

    def test_unknown_hook_name_exits_nonzero(self):
        from jacked.cli import main
        runner = CliRunner()
        # Use a name that definitely won't exist
        result = runner.invoke(main, ["_hook", "nonexistent_hook_xyz"], input="{}")
        assert result.exit_code != 0

    def test_hook_name_is_validated(self):
        """Prevent path traversal or import injection."""
        from jacked.cli import main
        runner = CliRunner()
        # Dots in name would allow submodule traversal
        result = runner.invoke(main, ["_hook", "..etc"], input="{}")
        assert result.exit_code != 0
```

- [ ] **Step 2: Run test — should fail with "no such command '_hook'"**

Run: `uv run python -m pytest tests/unit/test_hook_shim.py -v`

- [ ] **Step 3: Add `_hook` command to `cli.py`**

Insert after the `@main.command(name="check-version")` block (around line 745):

```python
@main.command(name="_hook", hidden=True)
@click.argument("name")
def _hook_shim(name: str):
    """Internal: dispatch to a hook handler by name.

    Called by Claude Code hooks via `jacked _hook <name>`. Reads hook
    input JSON from stdin, forwards it to the handler's main() function.

    This indirection keeps settings.json paths stable across upgrades —
    the jacked binary shim path survives `uv tool upgrade`, even when
    the underlying site-packages path changes.
    """
    import importlib
    import re

    # Only allow simple module names — no dots, no path traversal
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        click.echo(f"Invalid hook name: {name}", err=True)
        sys.exit(2)

    try:
        module = importlib.import_module(f"jacked.data.hooks.{name}")
    except ImportError as e:
        click.echo(f"Hook not found: {name} ({e})", err=True)
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

### Task 2: Install-time migration from legacy paths

**Files:**
- Modify: `jacked/cli.py` (install function, hook-writing logic)
- Create: `tests/unit/test_install_migration.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/test_install_migration.py
"""Tests for migrating legacy hook paths to _hook shim form."""

import json
from unittest.mock import patch


class TestLegacyHookMigration:
    def test_legacy_script_path_rewritten_to_hook_shim(self, tmp_path):
        from jacked.cli import _migrate_legacy_hook_commands

        settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": "/Users/x/.local/share/uv/tools/claude-jacked/lib/python3.12/site-packages/jacked/data/hooks/security_gatekeeper.py",
                        "timeout": 30,
                    }],
                }],
            },
        }

        changed = _migrate_legacy_hook_commands(settings, jacked_bin="/Users/x/.local/bin/jacked")

        assert changed is True
        cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert cmd == "/Users/x/.local/bin/jacked _hook security_gatekeeper"

    def test_legacy_python_prefix_rewritten(self, tmp_path):
        from jacked.cli import _migrate_legacy_hook_commands

        settings = {
            "hooks": {
                "Stop": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": "/Users/x/.local/share/uv/tools/claude-jacked/bin/python3 /Users/y/Github/claude-jacked/jacked/data/hooks/session_account_tracker.py",
                        "async": True,
                    }],
                }],
            },
        }

        changed = _migrate_legacy_hook_commands(settings, jacked_bin="/Users/x/.local/bin/jacked")

        assert changed is True
        cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert cmd == "/Users/x/.local/bin/jacked _hook session_account_tracker"

    def test_already_migrated_is_noop(self):
        from jacked.cli import _migrate_legacy_hook_commands

        settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": "/Users/x/.local/bin/jacked _hook security_gatekeeper",
                    }],
                }],
            },
        }

        changed = _migrate_legacy_hook_commands(settings, jacked_bin="/Users/x/.local/bin/jacked")

        assert changed is False

    def test_unrelated_hooks_untouched(self):
        from jacked.cli import _migrate_legacy_hook_commands

        settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "",
                    "hooks": [{
                        "type": "command",
                        "command": "/other/tool/script.sh",
                    }],
                }],
            },
        }

        changed = _migrate_legacy_hook_commands(settings, jacked_bin="/Users/x/.local/bin/jacked")

        assert changed is False
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/other/tool/script.sh"
```

- [ ] **Step 2: Run test — should fail**

- [ ] **Step 3: Implement `_migrate_legacy_hook_commands` and update install functions**

Add to `cli.py` near other hook helpers:

```python
# Names of jacked hooks we manage. Must match filenames in jacked/data/hooks/.
_JACKED_HOOK_NAMES = {
    "security_gatekeeper",
    "session_account_tracker",
    "qa_suggest",
}


def _migrate_legacy_hook_commands(settings: dict, jacked_bin: str) -> bool:
    """Rewrite legacy hook command strings to use `jacked _hook <name>`.

    Returns True if any commands were changed.
    """
    if not jacked_bin:
        return False

    changed = False
    hooks_dict = settings.get("hooks", {})
    for event_name, event_list in hooks_dict.items():
        if not isinstance(event_list, list):
            continue
        for entry in event_list:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                for hook_name in _JACKED_HOOK_NAMES:
                    marker = f"/hooks/{hook_name}.py"
                    if marker in cmd:
                        new_cmd = f"{jacked_bin} _hook {hook_name}"
                        if hook["command"] != new_cmd:
                            hook["command"] = new_cmd
                            changed = True
                        break
    return changed
```

Then modify the hook-writing helpers (`_install_security_gatekeeper`, `_install_session_tracker`, `_install_qa_suggest` or whatever they're named — search for where they build `command_str`) to use the shim form:

Replace the `command_str = f"{python_path} {script_str}"` pattern with:

```python
jacked_bin = shutil.which("jacked") or shutil.which("jacked.exe")
if jacked_bin:
    command_str = f"{jacked_bin} _hook {hook_name}"
else:
    # Fallback for environments where jacked isn't on PATH (dev/test)
    command_str = f"{python_path} {script_str}"
```

Also call `_migrate_legacy_hook_commands(settings, jacked_bin)` at the start of the install flow to clean up pre-existing stale paths.

- [ ] **Step 4: Run tests**

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/test_install_migration.py
git commit -m "feat(install): rewrite hook paths to _hook shim (survives uv upgrade)"
```

---

### Task 3: Updater module (wait-exit, install, restart)

**Files:**
- Create: `jacked/service/updater.py`
- Create: `tests/unit/service/test_updater.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/service/test_updater.py
"""Tests for the auto-updater."""

import os
import time
from unittest.mock import patch, MagicMock


class TestWaitForExit:
    def test_returns_true_when_process_exits(self):
        from jacked.service.updater import wait_for_exit
        # PID 999999999 definitely doesn't exist
        assert wait_for_exit(999999999, timeout=1.0) is True

    def test_returns_false_on_timeout(self):
        from jacked.service.updater import wait_for_exit
        # Our own PID — alive forever during test
        assert wait_for_exit(os.getpid(), timeout=0.5) is False


class TestRunUpdate:
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_runs_uv_install_after_wait(self, mock_popen, mock_run):
        from jacked.service import updater
        mock_run.return_value = MagicMock(returncode=0)

        with patch.object(updater, "wait_for_exit", return_value=True):
            with patch.object(updater, "find_bin", return_value="/fake/jacked"):
                updater.run_update(parent_pid=12345, extras="tray")

        # Verify uv install was called
        uv_call = mock_run.call_args_list[0]
        args = uv_call[0][0]
        assert "uv" in args
        assert "tool" in args and "install" in args
        assert "claude-jacked[tray]" in args
        assert "--force" in args

        # Verify service restart was spawned
        restart_call = mock_popen.call_args_list[0]
        restart_args = restart_call[0][0]
        assert "/fake/jacked" in restart_args
        assert "service" in restart_args and "start" in restart_args

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_skips_restart_if_install_fails(self, mock_popen, mock_run):
        from jacked.service import updater
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(updater, "wait_for_exit", return_value=True):
            with patch.object(updater, "find_bin", return_value="/fake/jacked"):
                updater.run_update(parent_pid=12345, extras="tray")

        mock_popen.assert_not_called()


class TestSpawnDetachedUpdater:
    @patch("subprocess.Popen")
    def test_passes_pid_to_helper(self, mock_popen):
        from jacked.service.updater import spawn_detached_updater
        with patch("jacked.service.updater.find_bin", return_value="/fake/jacked"):
            spawn_detached_updater(parent_pid=12345, extras="tray")

        args = mock_popen.call_args[0][0]
        assert "/fake/jacked" in args
        assert "_update-helper" in args
        assert "12345" in args
        assert "tray" in args
```

- [ ] **Step 2: Run tests — should fail**

- [ ] **Step 3: Implement `jacked/service/updater.py`**

```python
"""Auto-updater: detached helper that handles stop → install → restart."""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from jacked.findbin import find_bin
from jacked.service import CLAUDE_DIR

UPDATE_LOG = CLAUDE_DIR / "jacked-update.log"

logger = logging.getLogger(__name__)


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """Poll until process exits or timeout. Returns True if exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except (OSError, ProcessLookupError):
            return True
    return False


def run_update(parent_pid: int, extras: str = "tray") -> None:
    """Run in the detached helper process.

    1. Wait for parent (the running tray) to exit
    2. Run `uv tool install "claude-jacked[extras]" --force`
    3. If install succeeded, spawn a fresh `jacked service start`
    """
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(UPDATE_LOG, "a", buffering=1)

    log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Waiting for PID {parent_pid} to exit\n")
    wait_for_exit(parent_pid, timeout=30.0)

    log_fh.write("Running uv tool install\n")
    result = subprocess.run(
        ["uv", "tool", "install", f"claude-jacked[{extras}]", "--force"],
        stdout=log_fh, stderr=log_fh, check=False,
    )
    log_fh.write(f"uv install returncode: {result.returncode}\n")

    if result.returncode != 0:
        log_fh.write("Install failed — NOT restarting service\n")
        log_fh.close()
        return

    jacked = find_bin("jacked")
    if not jacked:
        log_fh.write("Could not find updated jacked binary — NOT restarting\n")
        log_fh.close()
        return

    log_fh.write(f"Restarting service via {jacked}\n")
    _spawn_detached([jacked, "service", "start"], log_fh)
    log_fh.write("Updater done\n")
    log_fh.close()


def _spawn_detached(cmd: list, log_fh) -> None:
    """Spawn a fully detached subprocess that survives this helper."""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh,
        "stderr": log_fh,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(cmd, **kwargs)


def spawn_detached_updater(parent_pid: int, extras: str = "tray") -> None:
    """Called by the tray to fire off the updater before exiting."""
    jacked = find_bin("jacked")
    if not jacked:
        raise SystemExit("Could not locate jacked binary to spawn updater")

    log_fh = open(UPDATE_LOG, "a", buffering=1)
    _spawn_detached(
        [jacked, "_update-helper", str(parent_pid), extras],
        log_fh,
    )
    log_fh.close()
```

- [ ] **Step 4: Add `_update-helper` CLI command**

In `cli.py`, near `_hook`:

```python
@main.command(name="_update-helper", hidden=True)
@click.argument("parent_pid", type=int)
@click.argument("extras", default="tray")
def _update_helper(parent_pid: int, extras: str):
    """Internal: run the detached update sequence."""
    from jacked.service.updater import run_update
    run_update(parent_pid, extras)
```

- [ ] **Step 5: Run tests**

- [ ] **Step 6: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_updater.py jacked/cli.py
git commit -m "feat(service): add cross-platform auto-updater"
```

---

### Task 4: Tray version check + dynamic menu

**Files:**
- Modify: `jacked/service/tray.py`
- Modify: `tests/unit/service/test_tray.py`

- [ ] **Step 1: Write failing test**

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

    def test_version_text_when_check_failed(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = None
        # Should show current version without arrow
        from jacked import __version__
        assert __version__ in runner._version_menu_text()

    def test_update_clickable_when_outdated(self):
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
    def test_update_click_spawns_updater_then_stops(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        from unittest.mock import MagicMock, patch
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        # Prevent the actual stop from running
        with patch.object(runner, "_on_stop"):
            with patch("jacked.service.updater.spawn_detached_updater") as mock_spawn:
                runner._on_update_click()
        mock_spawn.assert_called_once()
```

- [ ] **Step 2: Run test — should fail**

- [ ] **Step 3: Update `jacked/service/tray.py`**

Add at the top:

```python
from jacked.version_check import check_version_cached
```

In `ServiceRunner.__init__`, add:

```python
self._version_info: dict | None = None
self._version_check_thread: threading.Thread | None = None
```

Add methods:

```python
def _check_version(self) -> None:
    """Background: poll PyPI for latest version. Runs periodically."""
    while not self._stop_event.is_set():
        try:
            info = check_version_cached(__version__)
            if info is not None:
                self._version_info = info
                if self._icon:
                    self._icon.update_menu()
        except Exception:
            logger.exception("Version check failed")
        # Check once per hour
        if self._stop_event.wait(timeout=3600):
            break

def _version_menu_text(self) -> str:
    if self._version_info and self._version_info.get("outdated"):
        latest = self._version_info.get("latest", "?")
        return f"Update to v{latest} →"
    return f"v{__version__}"

def _version_is_clickable(self) -> bool:
    return bool(self._version_info and self._version_info.get("outdated"))

def _on_update_click(self):
    if not self._version_is_clickable():
        return
    if not self._lifecycle_lock.acquire(blocking=False):
        return
    try:
        latest = self._version_info.get("latest", "?") if self._version_info else "?"
        if self._icon:
            self._icon.icon = create_icon_image("starting")
            self._icon.notify(
                f"Updating to v{latest} — service will restart",
                "Jacked Update",
            )
        # Detect which extras were installed so we restore them
        extras = "tray"  # fixed for now; future: detect from installed extras
        from jacked.service.updater import spawn_detached_updater
        spawn_detached_updater(parent_pid=os.getpid(), extras=extras)
    finally:
        self._lifecycle_lock.release()
    # Kick off clean shutdown (updater will wait for our exit then install+restart)
    self._on_stop()
```

Replace the version menu item in `build_menu`. Change the signature of `build_menu` to accept a `version_text_fn` and `version_click_fn` and `version_enabled_fn`:

```python
def build_menu(
    port: int,
    version_text_fn,
    version_click_fn,
    version_enabled_fn,
    autostart_check,
    on_open_dashboard,
    on_restart,
    on_stop,
    on_toggle_autostart,
) -> "pystray.Menu":
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
        pystray.MenuItem(
            lambda _: version_text_fn(),
            version_click_fn,
            enabled=lambda _: version_enabled_fn(),
        ),
    )
```

Update the `ServiceRunner.run()` call to `build_menu` to pass the new functions:

```python
menu = build_menu(
    port=self.port,
    version_text_fn=self._version_menu_text,
    version_click_fn=self._on_update_click,
    version_enabled_fn=self._version_is_clickable,
    autostart_check=lambda: self._autostart_enabled,
    on_open_dashboard=self._on_open_dashboard,
    on_restart=self._on_restart,
    on_stop=self._on_stop,
    on_toggle_autostart=self._on_toggle_autostart,
)
```

In `_setup`, start the version check thread:

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

Update the existing `test_menu_has_expected_items` test to use the new signature:

```python
def test_menu_has_expected_items(self):
    _skip_if_no_tray()
    from jacked.service.tray import build_menu
    noop = lambda: None
    menu = build_menu(
        port=8321,
        version_text_fn=lambda: "v0.39.0",
        version_click_fn=noop,
        version_enabled_fn=lambda: False,
        autostart_check=lambda: True,
        on_open_dashboard=noop,
        on_restart=noop,
        on_stop=noop,
        on_toggle_autostart=noop,
    )
    items = list(menu)
    texts = [str(item) for item in items]
    assert any("Dashboard" in t for t in texts)
    assert any("Restart" in t for t in texts)
    assert any("Stop" in t for t in texts)
    assert any("Login" in t for t in texts)
    assert any("0.39.0" in t for t in texts)
```

- [ ] **Step 4: Run tests — should pass**

- [ ] **Step 5: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "feat(service): tray version check and update menu item"
```

---

### Task 5: Version bump and smoke test

**Files:**
- Modify: `jacked/__init__.py`

- [ ] **Step 1: Bump version to 0.41.0**

```python
__version__ = "0.41.0"
```

- [ ] **Step 2: Run full test suite**

Run: `uv run python -m pytest tests/ --timeout=30`

- [ ] **Step 3: Smoke test hook shim**

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"ls"}}' | uv run python -m jacked _hook security_gatekeeper
echo "exit: $?"
```

- [ ] **Step 4: Smoke test install migration**

Back up `~/.claude/settings.json`, run `jacked install`, verify hook commands are rewritten:

```bash
cp ~/.claude/settings.json /tmp/settings-backup.json
uv run python -m jacked install
grep "_hook" ~/.claude/settings.json
```

- [ ] **Step 5: Commit version bump**

```bash
git add jacked/__init__.py
git commit -m "chore: bump version to 0.41.0"
```

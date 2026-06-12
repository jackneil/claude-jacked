# 0.41.24 — Upgrade + Restart Hardening (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the whack-a-mole on the upgrade + restart flow. Auto-install the launchd plist when missing (via direct in-process call, not subprocess), delete dead pip-upgrade code, and add `jacked doctor` with real health probes.

**Architecture:** Small composable changes to existing modules. `ensure_native_lifecycle()` calls `install_autostart()` **in-process** — no subprocess, no argv gymnastics, no PATH dep, no non-existent flags. Returns an enum so the caller knows whether to skip kickstart (launchd already started the job fresh) or run it (plist was already installed).

**Tech Stack:** Python 3.10+, Click CLI, launchd (macOS), systemd `--user` (Linux), pytest.

**Spec:** `docs/superpowers/specs/2026-04-19-upgrade-and-restart-redesign-design.md`

---

## File Structure

| File | Change |
| --- | --- |
| `jacked/service/platform.py` | Add `ensure_native_lifecycle()` — calls `install_autostart()` in-process if plist missing |
| `jacked/cli.py` | `service_restart()` uses new helper; add `jacked doctor` command; no `install --tray` flag (doesn't exist) |
| `jacked/service/updater.py` | `starting_service` phase calls `ensure_native_lifecycle()` first |
| `jacked/install_method.py` | Delete dead `pip` branch in `upgrade_command()` + matching branch in `upgrade_command_label()` |
| `jacked/__init__.py` | Version bump to `0.41.24` |
| `README.md` | Changelog |
| `tests/unit/service/test_ensure_native_lifecycle.py` | **NEW** — exercises real `install_autostart` via tmp plist |
| `tests/unit/service/test_cli.py` | Update `TestServiceRestart` |
| `tests/unit/test_doctor.py` | **NEW** — tests PID + HTTP probe, not just port |
| `tests/unit/test_upgrade_command.py` | Remove pip-branch assertions |

---

## Task 1: `ensure_native_lifecycle()` — in-process, not subprocess

**Files:**
- Modify: `jacked/service/platform.py`
- Test: `tests/unit/service/test_ensure_native_lifecycle.py` (new)

The helper returns a 3-tuple so callers can distinguish:
- `(True, "already_installed", ...)` — plist/unit was already there; caller should `launchctl kickstart` to restart the job.
- `(True, "just_installed", ...)` — we just wrote the plist and launchd already started the service via `RunAtLoad`; caller should **skip** kickstart.
- `(False, "unavailable", reason)` — Windows or Linux without a unit; caller falls through to manual path.

- [ ] **Step 1: Write the failing tests — use real `install_autostart` path**

Create `tests/unit/service/test_ensure_native_lifecycle.py`:

```python
"""Tests for ensure_native_lifecycle() — exercises the real install_autostart
path (writes a plist to a tmp location).  No fake_run lambdas that synthesize
the plist — those give false positives (/dc round-1 CRITICAL)."""
import subprocess
import sys
from unittest.mock import MagicMock, patch


class TestMacOS:
    def test_returns_already_installed_when_plist_exists(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        fake_plist.write_text("<plist/>")
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is True
        assert state == "already_installed"

    def test_calls_install_autostart_when_plist_missing(self, tmp_path, monkeypatch):
        """Missing plist → install_autostart writes it + runs launchctl load.
        We mock launchctl so no real launchd interaction, but the plist file
        IS actually written by the real install_autostart code — no
        self-mocking side effects.

        CRITICAL: mock stop_process_graceful so this test can't kill the
        developer's real running jacked service (/dc round-2 v2.1 fix)."""
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)
        # Intercept find_bin so install_autostart finds "jacked"
        monkeypatch.setattr("jacked.findbin.find_bin", lambda name: "/fake/jacked")
        # CRITICAL: stub stop_process_graceful so we don't send SIGTERM to the
        # dev's real ~/.claude/jacked-service.pid process.
        monkeypatch.setattr(
            "jacked.service.process.stop_process_graceful",
            lambda *a, **kw: {"was_running": False, "died": False, "killed": False},
        )
        # Intercept subprocess.run for launchctl load (we don't want real launchd)
        with patch.object(plat.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout=b"", stderr=b"")):
            ok, state, reason = plat.ensure_native_lifecycle()

        assert ok is True
        assert state == "just_installed"
        # The REAL install_autostart wrote the plist via Path.write_text
        assert fake_plist.exists()
        assert "Label" in fake_plist.read_text()
        assert "ai.hank.jacked" in fake_plist.read_text()

    def test_returns_false_when_install_autostart_cannot_find_jacked(
        self, tmp_path, monkeypatch,
    ):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)
        # find_bin returns None → install_autostart returns error string
        monkeypatch.setattr("jacked.findbin.find_bin", lambda name: None)
        # Same stub as above — prevent killing the dev's real jacked service
        monkeypatch.setattr(
            "jacked.service.process.stop_process_graceful",
            lambda *a, **kw: {"was_running": False, "died": False, "killed": False},
        )

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is False
        assert state == "unavailable"
        assert "jacked" in reason.lower() and "binary" in reason.lower()


class TestLinux:
    def test_returns_already_installed_when_unit_exists(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "linux")

        fake_unit = tmp_path / "jacked.service"
        fake_unit.write_text("[Unit]\n")
        monkeypatch.setattr(plat, "_get_systemd_user_unit_path", lambda: fake_unit)

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is True
        assert state == "already_installed"

    def test_returns_unavailable_when_unit_missing(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "linux")

        unit_path = tmp_path / "jacked.service"
        monkeypatch.setattr(plat, "_get_systemd_user_unit_path", lambda: unit_path)

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is False
        assert state == "unavailable"
        assert "systemd" in reason.lower() or "unit" in reason.lower()


class TestWindows:
    def test_windows_returns_unavailable(self, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "win32")
        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is False
        assert state == "unavailable"
        assert "no native lifecycle" in reason.lower()
```

- [ ] **Step 2: Run — expect AttributeError (function missing)**

Run: `uv run python -m pytest tests/unit/service/test_ensure_native_lifecycle.py -v 2>&1 | tail -15`
Expected: FAIL with AttributeError on `ensure_native_lifecycle`.

- [ ] **Step 3: Implement the helper**

Append to `jacked/service/platform.py` after the existing `native_restart()`:

```python
def ensure_native_lifecycle() -> tuple[bool, str, str]:
    """Ensure the platform's native lifecycle manager is configured.

    Returns a 3-tuple: (ok, state, reason).

    - (True, "already_installed", "..."):  plist/unit was present; caller
      should run native_restart() to atomically restart the job.
    - (True, "just_installed", "..."):     we just wrote the plist and
      launchd already started the service via RunAtLoad=true.  Caller
      should SKIP native_restart (the job is already fresh and running).
    - (False, "unavailable", reason):      no plist/unit and we can't
      create one (Linux systemd: user DIYs; Windows: no manager).
      Caller falls through to manual stop+start.

    macOS: auto-creates the plist via in-process call to install_autostart()
    — no subprocess shell-out.  Eliminates the "jacked install --tray
    doesn't exist" class of bugs (/dc round-1 CRITICAL).

    Before creating the plist, if an ad-hoc jacked service is running on
    DEFAULT_PORT (held by some other user-initiated `jacked service start`),
    stop it first so launchctl load's RunAtLoad can bind the port cleanly.
    """
    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        if plist_path.exists():
            return (True, "already_installed", "launchd plist already installed")

        # Ad-hoc service may be holding :DEFAULT_PORT.  Stop it so the
        # RunAtLoad inside install_autostart can bind cleanly.
        from jacked.service import PID_FILE
        from jacked.service.process import stop_process_graceful
        stop_process_graceful(PID_FILE)

        status = install_autostart()
        if plist_path.exists():
            return (True, "just_installed", status)
        return (False, "unavailable", f"install_autostart did not produce plist: {status}")

    if sys.platform.startswith("linux"):
        unit_path = _get_systemd_user_unit_path()
        if unit_path.exists():
            return (True, "already_installed", "systemd user unit already installed")
        return (
            False, "unavailable",
            "no systemd user unit installed — create one manually to enable "
            "native restart.  See docs.",
        )

    return (False, "unavailable", "no native lifecycle manager on this platform")
```

- [ ] **Step 4: Run — expect PASS**

Run: `uv run python -m pytest tests/unit/service/test_ensure_native_lifecycle.py -v`
Expected: 6 tests PASS. Verify the `test_calls_install_autostart_when_plist_missing` test shows the plist was actually written by real code (not a fake_run side effect).

- [ ] **Step 5: Commit**

```bash
git add jacked/service/platform.py tests/unit/service/test_ensure_native_lifecycle.py
git commit -m "feat(service): ensure_native_lifecycle — in-process plist install

- macOS: if ~/Library/LaunchAgents/ai.hank.jacked.plist is missing,
  call install_autostart() directly (in-process, NOT via subprocess)
  to create it and have launchd load it.
- Returns a 3-tuple (ok, state, reason) so callers can distinguish
  'already_installed' (caller should kickstart) from 'just_installed'
  (skip kickstart — launchd already started the service fresh via
  RunAtLoad).
- Before creating the plist, stop any ad-hoc jacked service so
  launchctl load's RunAtLoad can bind :8321 cleanly.
- Linux: detect systemd user unit; do not auto-generate.
- Windows: returns unavailable."
```

---

## Task 2: `service_restart` uses the helper with state-aware kickstart skip

**Files:**
- Modify: `jacked/cli.py` — `service_restart()` at line 3317
- Test: `tests/unit/service/test_cli.py::TestServiceRestart`

- [ ] **Step 1: Enumerate existing TestServiceRestart tests**

Run: `uv run python -m pytest tests/unit/service/test_cli.py -k TestServiceRestart --collect-only 2>&1 | tail -15`

Record the list. We'll update them after the code change so they mock `ensure_native_lifecycle` appropriately.

- [ ] **Step 2: Write new auto-install tests**

Append to `tests/unit/service/test_cli.py` (same TestServiceRestart class or new class):

```python
class TestServiceRestartAutoInstall:
    """0.41.24: service_restart handles missing plist via ensure_native_lifecycle."""

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "already_installed", "plist at ~/Library/..."))
    @patch("jacked.service.platform.native_restart",
           return_value=(True, "launchctl kickstart succeeded"))
    def test_already_installed_runs_kickstart(self, mock_native, mock_ensure):
        from click.testing import CliRunner
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_ensure.assert_called_once()
        mock_native.assert_called_once()
        assert "kickstart" in result.output.lower()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "just_installed", "launchd agent installed and loaded"))
    @patch("jacked.service.platform.native_restart")
    def test_just_installed_skips_kickstart(self, mock_native, mock_ensure):
        """just_installed means launchd already loaded and RunAtLoad started the
        service fresh.  Calling native_restart would race the boot (/dc PM1)."""
        from click.testing import CliRunner
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_ensure.assert_called_once()
        mock_native.assert_not_called()
        assert "installed" in result.output.lower() or "ok" in result.output.lower()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "no native lifecycle manager"))
    @patch("jacked.service.process.stop_process_graceful",
           return_value={"was_running": False, "died": False, "killed": False})
    @patch("subprocess.Popen")
    def test_unavailable_falls_back_to_manual(self, mock_popen, mock_stop, mock_ensure):
        from click.testing import CliRunner
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_popen.assert_called_once()
```

- [ ] **Step 3: Modify `service_restart` at `jacked/cli.py:3317`**

Replace the body of `service_restart` with:

```python
def service_restart(host: str | None, port: int | None, foreground: bool):
    """Restart the jacked service.

    Preferred path: ensure native lifecycle manager is configured, then
    delegate the restart to it.  Skips kickstart when the plist was
    just installed (launchd already started the service fresh via
    RunAtLoad — kickstart would race the boot).

    Fallback: manual stop + detached spawn (Windows / bare POSIX).
    """
    from jacked.service import CLAUDE_DIR, DEFAULT_HOST, DEFAULT_PORT, PID_FILE
    from jacked.service.platform import ensure_native_lifecycle, native_restart
    from jacked.service.process import (
        stop_process_graceful,
        wait_for_port_free,
    )

    the_port = port or DEFAULT_PORT
    the_host = host or DEFAULT_HOST

    # --foreground is explicit debug; skip native handoff entirely.
    if not foreground:
        ok_ens, state, reason_ens = ensure_native_lifecycle()
        if ok_ens:
            if state == "just_installed":
                # launchd loaded the plist and started the service via
                # RunAtLoad.  Do NOT kickstart — it would race the boot.
                console.print(f"[green][OK][/green] {reason_ens}")
                return
            # state == "already_installed": kickstart for atomic restart
            ok, reason = native_restart()
            if ok:
                console.print(f"[green][OK][/green] {reason}")
                return
            console.print(f"[yellow]native_restart failed: {reason}[/yellow]")
        else:
            console.print(f"[dim]native lifecycle unavailable: {reason_ens}[/dim]")

    # Manual stop+start (Windows, or native unavailable, or --foreground).
    result = stop_process_graceful(PID_FILE)
    if result["was_running"]:
        if result["killed"]:
            console.print("[yellow]Tray ignored SIGTERM — force-killed[/yellow]")
        elif result["died"]:
            console.print("[dim]Stopped existing service[/dim]")
        if not result["died"]:
            console.print("[red]Could not stop existing service — aborting restart[/red]")
            sys.exit(1)
        if not wait_for_port_free(the_host, the_port, timeout=10.0):
            console.print(f"[red]Port {the_port} still in use — aborting start[/red]")
            sys.exit(1)

    if foreground:
        from jacked.service.tray import ServiceRunner
        ServiceRunner(host=the_host, port=the_port).run()
        return

    # Detached spawn.
    import subprocess as _subprocess
    from jacked.findbin import find_bin

    jacked_bin = find_bin("jacked") or sys.executable
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CLAUDE_DIR / "jacked-service.log"
    try:
        log_fh = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        log_fh = _subprocess.DEVNULL

    if sys.platform == "win32":
        creationflags = getattr(_subprocess, "DETACHED_PROCESS", 0x00000008)
        _subprocess.Popen(
            [jacked_bin, "service", "start", "--host", the_host, "--port", str(the_port)],
            stdin=_subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            creationflags=creationflags,
            close_fds=True,
        )
    else:
        _subprocess.Popen(
            [jacked_bin, "service", "start", "--host", the_host, "--port", str(the_port)],
            stdin=_subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
            close_fds=True,
        )
    console.print(f"[green][OK][/green] Restarted (detached). Logs: {log_path}")
```

- [ ] **Step 4: Run new tests**

Run: `uv run python -m pytest tests/unit/service/test_cli.py::TestServiceRestartAutoInstall -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Fix existing TestServiceRestart tests**

Run: `uv run python -m pytest tests/unit/service/test_cli.py::TestServiceRestart -v 2>&1 | tail -40`

For each failing existing test, either:
- Add `@patch("jacked.service.platform.ensure_native_lifecycle", return_value=(False, "unavailable", "test"))` to force the manual path. Use this if the test was exercising manual stop+start behavior.
- Add `@patch("jacked.service.platform.ensure_native_lifecycle", return_value=(True, "already_installed", "test"))` + `@patch("jacked.service.platform.native_restart", return_value=(True, "test"))` for tests exercising the native path.

Re-run until green.

- [ ] **Step 6: Commit**

```bash
git add jacked/cli.py tests/unit/service/test_cli.py
git commit -m "feat(cli): service_restart uses ensure_native_lifecycle

- If ensure returns 'just_installed', skip native_restart (launchd
  already booted the service via RunAtLoad — kickstart would race
  the boot, /dc PM1 fix).
- If ensure returns 'already_installed', run native_restart for
  atomic kickstart.
- If ensure returns 'unavailable' (Windows, bare POSIX), fall
  through to manual stop+start path unchanged."
```

---

## Task 3: Updater uses `ensure_native_lifecycle()` with same state awareness

**Files:**
- Modify: `jacked/service/updater.py` — `starting_service` phase
- Test: `tests/unit/service/test_native_restart.py::TestUpdaterUsesNativeRestart`

- [ ] **Step 1: Find the `starting_service` phase**

Run: `grep -n '_begin("starting_service")\|from jacked.service.platform' jacked/service/updater.py`

- [ ] **Step 2: Modify the starting_service block**

```python
# before
from jacked.service.platform import native_restart
...
native_ok, native_reason = native_restart()
if native_ok:
    log(f"Native lifecycle restart: {native_reason}")
else:
    log(f"Restarting service: {jacked} service start (native not used: {native_reason})")
    _spawn_detached([jacked, "service", "start"], log_fh=log_fh)

# after
from jacked.service.platform import ensure_native_lifecycle, native_restart
...
ens_ok, ens_state, ens_reason = ensure_native_lifecycle()
if ens_ok:
    if ens_state == "just_installed":
        log(f"Native lifecycle freshly installed (already running): {ens_reason}")
    else:
        # already_installed — run native_restart for atomic kickstart
        native_ok, native_reason = native_restart()
        if native_ok:
            log(f"Native lifecycle restart: {native_reason}")
        else:
            log(f"Native kickstart failed ({native_reason}); fallback to manual spawn")
            _spawn_detached([jacked, "service", "start"], log_fh=log_fh)
else:
    log(f"Native lifecycle unavailable ({ens_reason}); manual spawn")
    _spawn_detached([jacked, "service", "start"], log_fh=log_fh)
```

- [ ] **Step 3: Update existing updater tests**

Run: `uv run python -m pytest tests/unit/service/test_native_restart.py::TestUpdaterUsesNativeRestart -v 2>&1 | tail -25`

Add `@patch("jacked.service.platform.ensure_native_lifecycle", return_value=(True, "already_installed", "test"))` to the existing tests that mock `native_restart`. For the "no native" case, make it `(False, "unavailable", "test")`.

Re-run until green.

- [ ] **Step 4: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_native_restart.py
git commit -m "feat(updater): auto-install plist before starting_service phase

Tray Update click's starting_service phase calls
ensure_native_lifecycle() before native_restart.  If plist was
just installed, skip kickstart (launchd already booted the
service via RunAtLoad).  Eliminates the post-upgrade race for
users who had never run 'jacked install'."
```

---

## Task 4: Delete dead `pip` branches in install_method.py

**Files:**
- Modify: `jacked/install_method.py`
- Modify: `tests/unit/test_upgrade_command.py`

Two functions have dead pip branches:
- `upgrade_command()` at line 188 — pip branch is unreachable after can_auto_upgrade refuses pip (since 0.41.19).
- `upgrade_command_label()` at line 212 — same branch. Also unreachable.

`is_user_site_install()` is only used by these pip branches; it becomes unreferenced.

- [ ] **Step 1: Verify all callers**

Run: `grep -rn "is_user_site_install\|upgrade_command\|upgrade_command_label" jacked/ tests/ 2>&1 | grep -v __pycache__ | head -30`

Expected: `upgrade_command`/`upgrade_command_label` called from `cli.py::upgrade` and `updater.py::run_update`, both of which gate on `can_auto_upgrade()`. `is_user_site_install` called only inside the pip branches of these two functions.

- [ ] **Step 2: Update test file — replace pip assertions with raise test**

In `tests/unit/test_upgrade_command.py`:

- Remove/rewrite any test that asserts `upgrade_command` or `upgrade_command_label` returns a `python -m pip install` command.
- Add:

```python
def test_upgrade_command_raises_for_pip(monkeypatch):
    """pip installs are refused upstream in can_auto_upgrade.
    upgrade_command must not silently return a broken pip command
    (0.41.17 shipped with the pip command that crashed in uv venvs)."""
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: "pip")
    import pytest
    with pytest.raises(ValueError, match="pip auto-upgrade is not supported"):
        install_method.upgrade_command("tray")


def test_upgrade_command_label_raises_for_pip(monkeypatch):
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: "pip")
    import pytest
    with pytest.raises(ValueError, match="pip auto-upgrade is not supported"):
        install_method.upgrade_command_label("tray")
```

Also remove any test that references `is_user_site_install`.

- [ ] **Step 3: Delete both pip branches + `is_user_site_install`**

In `jacked/install_method.py`:

**3a. `upgrade_command()` at line 188:**

```python
# before
def upgrade_command(extras: str = "tray") -> list[str]:
    method = detect_install_method()
    if method == "uv":
        return ["uv", "tool", "install", f"claude-jacked[{extras}]", "--force"]
    if method == "pipx":
        return ["pipx", "install", f"claude-jacked[{extras}]", "--force"]
    # pip fallback. ...
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if is_user_site_install():
        cmd.append("--user")
    cmd.append(f"claude-jacked[{extras}]")
    return cmd

# after
def upgrade_command(extras: str = "tray") -> list[str]:
    method = detect_install_method()
    if method == "uv":
        return ["uv", "tool", "install", f"claude-jacked[{extras}]", "--force"]
    if method == "pipx":
        return ["pipx", "install", f"claude-jacked[{extras}]", "--force"]
    # Callers must gate on can_auto_upgrade() first — it refuses pip and
    # editable with recovery messages.  Defensive raise so any regression
    # that reintroduces the 0.41.17 "No module named pip" crash fails loudly.
    raise ValueError(
        f"pip auto-upgrade is not supported (detected method: {method}). "
        "Caller must refuse via can_auto_upgrade() with migration guidance."
    )
```

**3b. `upgrade_command_label()` at line 212:**

```python
# before
def upgrade_command_label(extras: str = "tray") -> str:
    method = detect_install_method()
    if method == "uv":
        return f"uv tool install 'claude-jacked[{extras}]' --force"
    if method == "pipx":
        return f"pipx install 'claude-jacked[{extras}]' --force"
    # pip label
    if is_user_site_install():
        return f"{sys.executable} -m pip install --upgrade --user 'claude-jacked[{extras}]'"
    return f"{sys.executable} -m pip install --upgrade 'claude-jacked[{extras}]'"

# after
def upgrade_command_label(extras: str = "tray") -> str:
    method = detect_install_method()
    if method == "uv":
        return f"uv tool install 'claude-jacked[{extras}]' --force"
    if method == "pipx":
        return f"pipx install 'claude-jacked[{extras}]' --force"
    raise ValueError(
        f"pip auto-upgrade is not supported (detected method: {method}). "
        "Caller must refuse via can_auto_upgrade() with migration guidance."
    )
```

**3c. Delete `is_user_site_install()` entirely** (lines ~152-186 in install_method.py).

- [ ] **Step 4: Check other test files for `is_user_site_install` references**

Run: `grep -rn "is_user_site_install" tests/ 2>&1 | grep -v __pycache__`

Update/remove any references (replace mocks with direct-raise assertions or just delete the old pip-label tests).

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/unit/test_upgrade_command.py tests/unit/test_install_method.py -v 2>&1 | tail -20`
Expected: all pass.

- [ ] **Step 6: Make ValueError from `upgrade_command` recoverable in BOTH updater paths**

The new raise in `upgrade_command()` / `upgrade_command_label()` must not crash the detached updater helper. Two call sites need guards:

**6a. `run_update()` in `jacked/service/updater.py`** — `upgrade_command(extras)` call at ~line 208. Wrap in try/except:

```python
# before
cmd = upgrade_command(extras)
label = upgrade_command_label(extras)

# after
try:
    cmd = upgrade_command(extras)
    label = upgrade_command_label(extras)
except ValueError as exc:
    log(f"ERROR: {exc}")
    try:
        _us.mark_failed(
            _us.UPDATE_STATUS_FILE,
            error=str(exc),
            recovery='uv tool install "claude-jacked[tray]" --force',
        )
    except Exception:
        logger.exception("mark_failed after upgrade_command ValueError")
    _write_recovery(
        f"Jacked auto-update failed: {exc}\n\n"
        "Manual recovery:\n"
        '  uv tool install "claude-jacked[tray]" --force\n'
    )
    return
```

**6b. `_spawn_windows_tray_updater()` in `jacked/service/updater.py`** — around line 490-500 this calls `upgrade_command(extras)` / `upgrade_command_label(extras)` BEFORE any `can_auto_upgrade()` gate. On a pip-method Windows install the ValueError would crash at batch-script-generation time.

Add the gate:

```python
# near the top of _spawn_windows_tray_updater, BEFORE upgrade_command is called
from jacked.install_method import can_auto_upgrade
_ok, _reason = can_auto_upgrade()
if not _ok:
    logger.warning("Windows tray updater refused: %s", _reason)
    # Write recovery file the tray's error-display path surfaces
    _write_recovery(
        f"Jacked auto-update refused: {_reason}\n\n"
        "Manual recovery:\n"
        '  uv tool install "claude-jacked[tray]" --force\n'
    )
    return
```

Run: `grep -n "_spawn_windows_tray_updater\|upgrade_command(extras)\|can_auto_upgrade" jacked/service/updater.py` to verify both sites are covered.

- [ ] **Step 7: Add regression tests for both guards**

In `tests/unit/service/test_updater.py`, add:

```python
def test_run_update_handles_upgrade_command_valueerror(tmp_path, monkeypatch):
    """If upgrade_command raises ValueError (pip method reached somehow),
    run_update writes a recovery file instead of crashing."""
    from jacked.service import updater, update_status as us_mod
    from jacked import install_method
    monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(updater, "_write_recovery", lambda msg: (tmp_path / "recovery.txt").write_text(msg))
    monkeypatch.setattr(install_method, "can_auto_upgrade", lambda: (True, ""))
    monkeypatch.setattr(install_method, "upgrade_command",
                        lambda extras: (_ for _ in ()).throw(ValueError("pip not supported")))

    with patch.object(updater, "wait_for_exit", return_value=True):
        updater.run_update(parent_pid=0, extras="tray", target_version="0.41.24")

    recovery = (tmp_path / "recovery.txt").read_text()
    assert "pip not supported" in recovery
    assert "uv tool install" in recovery
```

Only add a Windows-specific test if running cross-platform CI; otherwise skip (Windows path is reviewed via code walk).

- [ ] **Step 8: Commit**

```bash
git add jacked/install_method.py jacked/service/updater.py tests/unit/test_upgrade_command.py tests/unit/test_install_method.py tests/unit/service/test_updater.py
git commit -m "refactor(install_method): delete dead pip branches + is_user_site_install

- can_auto_upgrade() has refused 'pip' since 0.41.19; the pip branches
  in upgrade_command() and upgrade_command_label() were unreachable
  dead code that (pre-0.41.19) crashed with 'No module named pip'
  in uv-managed venvs (the 0.41.17 bug).
- Both functions now raise ValueError if somehow reached with method=='pip'.
- run_update wraps the upgrade_command call in try/except ValueError
  and writes a recovery file instead of crashing the detached helper.
- _spawn_windows_tray_updater gates on can_auto_upgrade() before
  calling upgrade_command (was unreached by the CLI's gate).
- Deletes is_user_site_install() (only caller was the removed pip branches)."
```

---

## Task 5: `jacked doctor` with real health probe

**Files:**
- Modify: `jacked/cli.py` — add `doctor` command
- Test: `tests/unit/test_doctor.py` (new)

Per /dc round-1: doctor must not conflate "port in use" with "service healthy." Real probes:
1. PID file → `is_process_alive(pid)` (detects zombies, stale PIDs)
2. If PID alive: HTTP GET `/api/version` with 2s timeout (detects crashed-mid-init tray)
3. If port held but PID mismatch: foreign process squatting — report owner

- [ ] **Step 1: Write the tests**

Create `tests/unit/test_doctor.py`:

```python
"""Tests for jacked doctor — real health probes, not just port availability."""
import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def test_doctor_reports_version_and_method():
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "Version:" in result.output
    assert "Install method:" in result.output


def test_doctor_plist_missing_recommends_install(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        import pytest
        pytest.skip("macOS-specific")
    from jacked.cli import main
    from jacked.service import platform as plat
    monkeypatch.setattr(plat, "_get_launchd_plist_path",
                        lambda: tmp_path / "nope.plist")
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "plist" in result.output.lower()
    assert "MISSING" in result.output or "missing" in result.output
    assert "service install" in result.output or "jacked install" in result.output


def test_doctor_service_healthy_via_http_probe(monkeypatch):
    """If HTTP probe returns 200, doctor reports HEALTHY."""
    from jacked.cli import main
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"current": "0.41.24"}
    with patch("jacked.service.process.is_port_available", return_value=False), \
         patch("httpx.get", return_value=fake_resp):
        result = CliRunner().invoke(main, ["doctor"])
    assert "healthy" in result.output.lower() or "running" in result.output.lower()


def test_doctor_port_held_but_http_dead(monkeypatch):
    """Port in use but HTTP probe fails → report 'port held but not healthy'."""
    import httpx
    from jacked.cli import main
    with patch("jacked.service.process.is_port_available", return_value=False), \
         patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = CliRunner().invoke(main, ["doctor"])
    assert ("not healthy" in result.output.lower()
            or "unreachable" in result.output.lower()
            or "hung" in result.output.lower())


def test_doctor_service_not_running(monkeypatch):
    """Port free → service not running, recommend jacked service start."""
    from jacked.cli import main
    with patch("jacked.service.process.is_port_available", return_value=True):
        result = CliRunner().invoke(main, ["doctor"])
    assert "not running" in result.output.lower()
    assert "jacked service" in result.output


def test_doctor_editable_install_recovery(monkeypatch):
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: "editable")
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    assert "git pull" in result.output or "uv sync" in result.output


def test_doctor_pip_install_recovery(monkeypatch):
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: "pip")
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    assert "uv tool install" in result.output
```

- [ ] **Step 2: Run — expect NoSuchCommand**

Run: `uv run python -m pytest tests/unit/test_doctor.py -v 2>&1 | tail -15`
Expected: FAIL — `doctor` command doesn't exist.

- [ ] **Step 3: Add the `doctor` command**

In `jacked/cli.py`, after the `@main.command()` for `service_restart` (or at any sibling location), add:

```python
@main.command()
def doctor():
    """Diagnose a broken jacked install and print recovery commands.

    Checks version, install method, launchd/systemd plist/unit, and
    service running state (via PID + HTTP probe, not just port).  Prints
    exact commands to paste for any detected issue.

    Does NOT attempt repair — this is a read-only diagnostic.
    """
    import sys
    import httpx
    from jacked import __version__
    from jacked.install_method import detect_install_method
    from jacked.service import DEFAULT_HOST, DEFAULT_PORT, PID_FILE
    from jacked.service.process import (
        is_port_available, is_process_alive, read_pid,
    )

    console.print(f"[bold]Version:[/bold] {__version__}")
    try:
        method = detect_install_method()
    except Exception as exc:
        method = f"unknown ({exc})"
    console.print(f"[bold]Install method:[/bold] {method}")

    # Plist/unit check
    if sys.platform == "darwin":
        from jacked.service.platform import _get_launchd_plist_path
        plist = _get_launchd_plist_path()
        if plist.exists():
            console.print(f"[bold]Launchd plist:[/bold] [green]OK[/green] ({plist})")
        else:
            console.print(f"[bold]Launchd plist:[/bold] [yellow]MISSING[/yellow]")
            console.print(f"  Recovery: [cyan]jacked service install[/cyan]")
    elif sys.platform.startswith("linux"):
        from jacked.service.platform import _get_systemd_user_unit_path
        unit = _get_systemd_user_unit_path()
        if unit.exists():
            console.print(f"[bold]Systemd user unit:[/bold] [green]OK[/green] ({unit})")
        else:
            console.print(
                f"[bold]Systemd user unit:[/bold] [yellow]NOT INSTALLED[/yellow]"
            )
            console.print("  Linux users configure their own auto-start; see docs.")
    else:
        console.print(f"[bold]Native lifecycle manager:[/bold] [dim]none (Windows)[/dim]")

    # Service health — real probes, not just port availability
    port_free = is_port_available(DEFAULT_HOST, DEFAULT_PORT)
    pid_info = read_pid(PID_FILE)
    pid_alive = (
        pid_info is not None
        and is_process_alive(pid_info.get("pid", 0))
    )

    if port_free:
        console.print(
            f"[bold]Service:[/bold] [yellow]NOT RUNNING[/yellow] "
            f"(port {DEFAULT_PORT} free)"
        )
        console.print(f"  Recovery: [cyan]jacked service start[/cyan]")
        if pid_info and not pid_alive:
            console.print(
                f"  [dim]Stale PID file at {PID_FILE} "
                f"(pid {pid_info.get('pid')} is dead).[/dim]"
            )
    else:
        # Port held — probe HTTP to distinguish healthy vs crashed-mid-init
        try:
            resp = httpx.get(
                f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/api/version",
                timeout=2.0,
            )
            if resp.status_code == 200:
                console.print(
                    f"[bold]Service:[/bold] [green]HEALTHY[/green] "
                    f"(port {DEFAULT_PORT}, HTTP 200)"
                )
            else:
                console.print(
                    f"[bold]Service:[/bold] [yellow]PORT HELD BUT UNHEALTHY[/yellow] "
                    f"(HTTP {resp.status_code})"
                )
                console.print(f"  Recovery: [cyan]jacked service restart[/cyan]")
        except Exception as exc:
            console.print(
                f"[bold]Service:[/bold] [red]PORT HELD BUT UNREACHABLE[/red] "
                f"({type(exc).__name__}: {exc})"
            )
            if pid_alive:
                console.print(
                    f"  PID {pid_info['pid']} is alive but HTTP probe failed — "
                    f"service may have crashed mid-init."
                )
            else:
                console.print(
                    f"  Port held by a process that is NOT the jacked service "
                    f"(our PID file is stale or missing).  "
                    f"Run [cyan]lsof -iTCP:{DEFAULT_PORT} -sTCP:LISTEN[/cyan] "
                    f"to see the owner."
                )
            console.print(f"  Recovery: [cyan]jacked service restart[/cyan]")

    # Install-method-specific recovery
    if method == "editable":
        console.print(
            "\n[bold yellow]Editable (dev-clone) install detected.[/bold yellow]\n"
            "  Auto-upgrade disabled.  Upgrade via:\n"
            "  [cyan]cd <your-repo> && git pull && uv sync[/cyan]"
        )
    elif method == "pip":
        console.print(
            "\n[bold yellow]pip install detected.[/bold yellow]\n"
            "  Auto-upgrade disabled.  Migrate to uv with:\n"
            "  [cyan]uv tool install \"claude-jacked[tray]\" --force[/cyan]"
        )
    elif str(method).startswith("unknown"):
        console.print(
            "\n[bold red]Could not detect install method.[/bold red]\n"
            "  Nuclear-option recovery:\n"
            "  [cyan]uv tool install \"claude-jacked[tray]\" --force[/cyan]"
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_doctor.py -v`
Expected: all 7 tests PASS.

- [ ] **Step 5: Smoke test manually**

Run: `uv run jacked doctor`
Expected: prints version (0.41.23 still), install method (editable for dev clone), plist status, service status, editable recovery message.

- [ ] **Step 6: Commit**

```bash
git add jacked/cli.py tests/unit/test_doctor.py
git commit -m "feat(cli): jacked doctor — diagnose + print recovery

Real health checks, not just port-in-use:
- plist/unit presence (macOS/Linux)
- PID alive + HTTP probe to /api/version (distinguishes healthy,
  crashed-mid-init, and foreign-process-squatting-the-port)
- install-method-specific recovery hint (editable → git pull + uv sync;
  pip → uv tool install --force; unknown → nuclear recovery)

Does not attempt any repair — read-only diagnostic."
```

---

## Task 6: Version bump + changelog + full test suite

**Files:**
- Modify: `jacked/__init__.py`, `README.md`

- [ ] **Step 1: Bump to 0.41.24** — `__version__ = "0.41.24"`

- [ ] **Step 2: README changelog** — insert above 0.41.23:

```markdown
| **0.41.24** | **Upgrade + restart hardening.** (1) New `ensure_native_lifecycle()` calls `install_autostart()` **in-process** (no subprocess) to auto-create the launchd plist when missing.  `jacked service restart` + tray Update's starting_service phase use it to eliminate the "Port 8321 already in use" race for users who set up jacked via raw `uv tool install` without ever running `jacked service install`.  The helper returns a state enum so callers skip `launchctl kickstart` when launchd just loaded the plist fresh via `RunAtLoad` (otherwise kickstart would race the boot).  (2) Deleted dead `pip` branches from `upgrade_command()` and `upgrade_command_label()` — `can_auto_upgrade()` has refused pip since 0.41.19 so they were unreachable, but the old code (pre-0.41.19) crashed with `No module named pip` in uv venvs.  Both now raise ValueError loudly if reached.  Also deletes the now-unreferenced `is_user_site_install()`. (3) New `jacked doctor` command: reports version, install method, plist/unit presence, and service health (PID + HTTP probe — not just port-in-use).  Prints exact recovery commands for every detected issue.  Gives stranded users a clear out without having to remember `uv tool install --force`. |
```

- [ ] **Step 3: Full suite**

Run: `uv run python -m pytest 2>&1 | tail -15`
Expected: all pass except known pre-existing (dual_token, analytics_anomalies, TestServiceRestart tests that existed before this branch and are tracked separately).

- [ ] **Step 4: Commit**

```bash
git add jacked/__init__.py README.md
git commit -m "chore: bump to 0.41.24 with changelog"
```

---

## Task 7: Ship

- [ ] `git push origin master`
- [ ] `git tag -a v0.41.24 -m "v0.41.24 — upgrade + restart hardening" && git push origin v0.41.24`
- [ ] `gh release create v0.41.24` with notes from changelog.
- [ ] Monitor PyPI publish via `gh run list --workflow=publish.yml --limit=1`.
- [ ] Post-ship: `uv tool install --force claude-jacked==0.41.24 && jacked check-version && jacked doctor`.
- [ ] Smoke test: `jacked service restart` on this machine (plist already installed → kickstart). Then `rm ~/Library/LaunchAgents/ai.hank.jacked.plist && jacked service restart` (plist missing → ensure_native_lifecycle creates + loads, no race).

---

## Self-Review (post-round-1 fixes)

- [x] /dc round-1 CRITICAL #0 (jacked install --tray doesn't exist): `ensure_native_lifecycle` now calls `install_autostart()` in-process. No subprocess. No non-existent flags.
- [x] /dc round-1 CRITICAL #1 (test self-mocks plist creation): rewritten to exercise real `install_autostart` via `tmp_path`-scoped plist path; assertion checks actual file content ("Label", "ai.hank.jacked").
- [x] /dc round-1 CRITICAL #2 (jacked install clobbers settings.json): not an issue — we no longer call `jacked install`. We call `install_autostart()` directly, which only writes the plist.
- [x] /dc round-1 PM1 (auto-install + kickstart race after RunAtLoad boot): state enum — "just_installed" → skip kickstart; "already_installed" → run kickstart.
- [x] /dc round-1 PM3 (doctor conflates port-in-use with healthy): doctor now does PID alive check + HTTP probe to `/api/version`, distinguishes healthy / unhealthy / foreign-port-holder.
- [x] /dc round-1 MEDIUM (is_user_site_install still referenced by upgrade_command_label): Task 4 deletes BOTH pip branches (command + label) + is_user_site_install.
- [x] /dc round-1 MEDIUM (plist missing + ad-hoc service running collision): `ensure_native_lifecycle` calls `stop_process_graceful(PID_FILE)` before `install_autostart` on macOS so `launchctl load`'s RunAtLoad can bind the port.
- [x] /dc round-1 MEDIUM (TestServiceRestart enumeration): Task 2 Step 1 requires running `--collect-only` before implementation.
- [x] /dc round-1 LOW (doctor orphan-launchd job / PID cleanup): included via `pid_alive` check and HTTP probe logic.
- [x] /dc round-2 CRITICAL v2.1 (test kills real jacked service): `test_calls_install_autostart_when_plist_missing` and `test_returns_false_when_install_autostart_cannot_find_jacked` now mock `stop_process_graceful` so the real `~/.claude/jacked-service.pid` process is safe.
- [x] /dc round-2 MEDIUM v2.3 (Windows updater bypasses pip gate): Task 4 Step 6 adds (a) try/except around `upgrade_command` in `run_update`, (b) explicit `can_auto_upgrade` gate in `_spawn_windows_tray_updater`. Step 7 adds a regression test.
- [x] No placeholders. Concrete commands, concrete line numbers.
- [x] Cross-task identifier consistency.

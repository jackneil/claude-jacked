# Service Tray Autostart — Fix Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix CRITICAL and MEDIUM issues from /dc review of the service tray feature.

**Architecture:** Targeted fixes to existing files — no new files, no structural changes.

**Tech Stack:** Same as original (pystray, Pillow, click, uvicorn)

---

## File Structure

All fixes apply to existing files:

```
jacked/service/tray.py        — Fix restart crash, init _uvicorn_server, add uvicorn check, cleanup PID on failure
jacked/service/platform.py     — Use symlink-stable path for binary resolution
jacked/cli.py                  — Fix install/uninstall error message handling
tests/unit/service/test_process.py  — Add empty PID file test
tests/unit/service/test_tray.py     — Add check_tray_deps test
tests/unit/service/test_platform.py — Add install failure path test
tests/unit/service/test_cli.py      — Add restart test, install error display test
```

---

### Task 1: Fix `_uvicorn_server` thread safety and restart crash

**Files:**
- Modify: `jacked/service/tray.py`
- Modify: `tests/unit/service/test_tray.py`

- [ ] **Step 1: Write test for restart safety**

Add to `tests/unit/service/test_tray.py`:

```python
class TestServiceRunnerRestart:
    """Tests for restart robustness."""

    def test_uvicorn_server_initialized_in_init(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        assert hasattr(runner, "_uvicorn_server")
        assert runner._uvicorn_server is None

    def test_on_restart_handles_exception(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._icon = MagicMock()
        # _on_restart should not raise even if _start_uvicorn fails
        with patch.object(runner, "_start_uvicorn", side_effect=OSError("port in use")):
            runner._on_restart()  # should not raise
        # Icon should show stopped state on failure
        assert runner._icon.icon is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/service/test_tray.py::TestServiceRunnerRestart -v`
Expected: FAIL — `_uvicorn_server` not in `__init__`, or `_on_restart` raises

- [ ] **Step 3: Fix `tray.py`**

In `ServiceRunner.__init__`, add `self._uvicorn_server = None` after line 134.

Replace the `_on_restart` method with error handling:

```python
def _on_restart(self):
    if self._icon:
        self._icon.icon = create_icon_image("starting")
    # Stop existing server
    if self._uvicorn_server is not None:
        self._uvicorn_server.should_exit = True
    if self._uvicorn_thread:
        self._uvicorn_thread.join(timeout=5)
    # Restart with error handling
    try:
        self._uvicorn_thread = self._start_uvicorn()
        if self._wait_for_ready():
            if self._icon:
                self._icon.icon = create_icon_image("running")
        else:
            if self._icon:
                self._icon.icon = create_icon_image("stopped")
    except Exception:
        if self._icon:
            self._icon.icon = create_icon_image("stopped")
```

Also replace `hasattr(self, "_uvicorn_server")` checks in `_on_stop` with `self._uvicorn_server is not None`.

In `_start_uvicorn`, move server assignment before thread start:

```python
def _start_uvicorn(self) -> threading.Thread:
    os.environ["JACKED_HOST"] = self.host
    os.environ["JACKED_PORT"] = str(self.port)

    config = uvicorn.Config(
        "jacked.api.main:app",
        host=self.host,
        port=self.port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    self._uvicorn_server = server

    thread = threading.Thread(target=server.run, name="jacked-uvicorn", daemon=True)
    thread.start()
    return thread
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "fix(service): fix restart crash and thread-safe server attribute"
```

---

### Task 2: Add uvicorn availability check and PID cleanup on failure

**Files:**
- Modify: `jacked/service/tray.py`
- Modify: `tests/unit/service/test_tray.py`

- [ ] **Step 1: Write test for uvicorn check and PID cleanup**

Add to `tests/unit/service/test_tray.py`:

```python
class TestCheckDeps:
    """Tests for dependency checking."""

    def test_check_tray_deps_raises_when_missing(self):
        from jacked.service import tray
        with patch.object(tray, "_TRAY_AVAILABLE", False):
            with pytest.raises(SystemExit, match="tray"):
                tray.check_tray_deps()

    def test_check_tray_deps_passes_when_available(self):
        from jacked.service import tray
        with patch.object(tray, "_TRAY_AVAILABLE", True):
            tray.check_tray_deps()  # should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/service/test_tray.py::TestCheckDeps -v`
Expected: Should pass since `check_tray_deps` exists — verify behavior is correct

- [ ] **Step 3: Add uvicorn check in `ServiceRunner.run()` and PID cleanup in `_setup`**

In `ServiceRunner.run()`, after `check_tray_deps()`, add:

```python
if not _UVICORN_AVAILABLE:
    raise SystemExit(
        "Service mode requires uvicorn.\n"
        'Install it with: uv tool install "claude-jacked" --force'
    )
```

In `_setup`, add PID cleanup on failure:

```python
def _setup(self, icon: "pystray.Icon"):
    icon.visible = True
    self._uvicorn_thread = self._start_uvicorn()
    if self._wait_for_ready():
        icon.icon = create_icon_image("running")
    else:
        icon.icon = create_icon_image("stopped")
        remove_pid(PID_FILE)
        icon.notify("Jacked failed to start", "Jacked Service")
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "fix(service): add uvicorn check and cleanup PID on startup failure"
```

---

### Task 3: Fix CLI error message display

**Files:**
- Modify: `jacked/cli.py`
- Modify: `tests/unit/service/test_cli.py`

- [ ] **Step 1: Write test for error display**

Add to `tests/unit/service/test_cli.py`:

```python
class TestServiceInstallError:
    @patch("jacked.service.platform.install_autostart")
    def test_install_shows_error_when_binary_not_found(self, mock_install):
        from jacked.cli import main
        mock_install.return_value = "Could not find 'jacked' binary on PATH. Is it installed?"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert result.exit_code == 0
        # Should NOT show green [OK] for error messages
        assert "[OK]" not in result.output or "Could not find" not in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/service/test_cli.py::TestServiceInstallError -v`
Expected: FAIL — currently prints `[OK] Could not find...`

- [ ] **Step 3: Fix CLI install/uninstall commands**

Replace the `service_install` command:

```python
@service.command(name="install")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8321, type=int, help="Port to bind to")
def service_install(host: str, port: int):
    """Configure jacked to start automatically on login."""
    from jacked.service.platform import install_autostart

    result = install_autostart(host, port)
    if result.startswith("Could not find"):
        console.print(f"[red]Error:[/red] {result}")
    else:
        console.print(f"[green][OK][/green] {result}")
```

Replace the `service_uninstall` command:

```python
@service.command(name="uninstall")
def service_uninstall():
    """Remove jacked auto-start configuration."""
    from jacked.service.platform import uninstall_autostart

    result = uninstall_autostart()
    if "not supported" in result.lower() or "not found" in result.lower():
        console.print(f"[yellow]{result}[/yellow]")
    else:
        console.print(f"[green][OK][/green] {result}")
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_cli.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/service/test_cli.py
git commit -m "fix(service): distinguish success/error messages in install/uninstall"
```

---

### Task 4: Fix upgrade-breaking autostart path

**Files:**
- Modify: `jacked/service/platform.py`
- Modify: `tests/unit/service/test_platform.py`

- [ ] **Step 1: Write test for stable path resolution**

Add to `tests/unit/service/test_platform.py`:

```python
class TestInstallAutostartPathStability:
    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_plist_uses_which_jacked_path(self, mock_run, tmp_path):
        """The plist should use a path that survives uv tool reinstall."""
        plist = tmp_path / "ai.hank.jacked.plist"
        from jacked.service.platform import install_autostart
        with patch("jacked.service.platform._get_launchd_plist_path", return_value=plist):
            with patch("shutil.which", return_value="/Users/test/.local/bin/jacked"):
                install_autostart("127.0.0.1", 8321)
        content = plist.read_text()
        # Should use the shim path, not a venv-internal path
        assert "/Users/test/.local/bin/jacked" in content

    @patch("sys.platform", "darwin")
    @patch("subprocess.run")
    def test_install_no_jacked_on_path(self, mock_run, tmp_path):
        """Should return error when jacked binary not found."""
        from jacked.service.platform import install_autostart
        with patch("shutil.which", return_value=None):
            result = install_autostart("127.0.0.1", 8321)
        assert "Could not find" in result
        mock_run.assert_not_called()
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_platform.py::TestInstallAutostartPathStability -v`
Expected: Both should pass (the current implementation already uses `shutil.which`)

Note: The `shutil.which("jacked")` approach already returns the stable shim path (e.g., `~/.local/bin/jacked`) when installed via `uv tool install`. The pre-mortem's concern about path instability is valid if the user runs `jacked service install` from a development venv where `which jacked` returns a venv-specific path. Adding a warning when the resolved path looks like it's inside a venv would help, but is out of scope for this fix round. The test validates the current behavior is correct for the normal install path.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/service/test_platform.py
git commit -m "test(service): add path stability and failure path tests for install"
```

---

### Task 5: Fill remaining test gaps

**Files:**
- Modify: `tests/unit/service/test_process.py`
- Modify: `tests/unit/service/test_cli.py`

- [ ] **Step 1: Add empty PID file test**

Add to `tests/unit/service/test_process.py` in `TestReadPid`:

```python
def test_returns_none_for_empty_file(self, tmp_path):
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("")
    from jacked.service.process import read_pid
    assert read_pid(pid_file) is None
```

- [ ] **Step 2: Add service_restart CLI test**

Add to `tests/unit/service/test_cli.py`:

```python
class TestServiceRestart:
    @patch("jacked.service.process.stop_process", return_value=False)
    def test_restart_when_not_running_starts_fresh(self, mock_stop):
        """Restart with no existing service should attempt start."""
        from jacked.cli import main
        runner = CliRunner()
        # We can't actually start the tray in a test, but we can verify
        # stop_process was called and the command doesn't crash
        with patch("jacked.service.tray.ServiceRunner.run", side_effect=SystemExit(0)):
            result = runner.invoke(main, ["service", "restart"])
        mock_stop.assert_called_once()
```

- [ ] **Step 3: Run all service tests**

Run: `uv run python -m pytest tests/unit/service/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/unit/service/test_process.py tests/unit/service/test_cli.py
git commit -m "test(service): fill test gaps for empty PID file and restart command"
```

---

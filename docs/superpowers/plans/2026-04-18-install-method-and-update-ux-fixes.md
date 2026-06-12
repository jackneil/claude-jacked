# 0.41.19 post-ship /dc fixes — 0.41.20 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address post-implementation findings from the 0.41.19 `/dc` review — eliminate the "tray clicked but no diagnostics" failure class, guarantee every `run_update` exit path leaves a terminal status, prevent silent CLI-shim errors, and add phase-drift enforcement tests.

**Architecture:** Seven small, independent fixes across four files. No new modules. The only semantic addition is a `mark_failed(path, error, recovery)` helper in `update_status.py` that both the updater and tray can call to leave an explicit terminal state from outside any begin/end phase. Phase-drift is enforced at test time via `ast` parsing — catches dev regressions without imposing runtime cost.

**Tech Stack:** Python 3.10+ / Click / pystray / ffmpeg-nothing-here. Tests via `uv run python -m pytest` per project `CLAUDE.md`.

---

## File Structure

**New files:**
- `tests/unit/service/test_phase_drift.py` — ast-based drift guard

**Modified files:**
- `jacked/service/update_status.py` — add `mark_failed()` helper
- `jacked/service/updater.py` — call `mark_failed()` on early-return branches; wrap `mark_succeeded` fallback
- `jacked/service/tray.py` — log breadcrumb + `init_status` before spawn; add tray FileHandler; fix pre-warm host
- `jacked/cli.py` — `_update_status_shim` exits 1 on `ValueError`
- `jacked/service/updater.py` — Windows batch checks `errorlevel 1` after each `_update_status`; uses literal `127.0.0.1` (already)
- `jacked/__init__.py` — 0.41.20
- `README.md` — changelog

**Out of scope (flagged as future work):**
- Atomic settings.json migration inside `jacked install --force`
- File-size split of `jacked/cli.py` (4174 lines) or `jacked/service/tray.py` (661 lines)
- Runtime-enforced phase iteration (data-driven `run_update`) — test-time ast guard is enough for 0.41.20

---

## Task 0: Updater tolerates tray pre-init (LockBusy → reuse)

**Files:**
- Modify: `jacked/service/updater.py` (the `init_status` block inside `run_update`)
- Test: `tests/unit/service/test_updater.py`

**Why first:** Task 5 adds `init_status` in the tray BEFORE spawning the updater. The updater currently catches `LockBusy` and returns immediately — so a tray pre-init would cause the updater to silently exit. Fix the updater first, then the tray can safely pre-init.

- [ ] **Step 1: Failing test**

Append to `tests/unit/service/test_updater.py`:

```python
class TestRunUpdateReusesTrayPreInit:
    """When the tray pre-inits the status file right before spawning the
    updater, the updater's own init_status raises LockBusy. The updater
    must treat a <30s old tray pre-init (no phases yet) as 'reuse this
    file and continue' — not as 'abort because someone else is running'."""

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_reuses_tray_pre_init_and_completes(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        # Simulate tray pre-init: fresh file, overall=in_progress, no phases
        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version="0.41.19",
            to_version="0.41.20",
            method="uv",
        )

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.20")

        data = us_mod.read_status(us_mod.UPDATE_STATUS_FILE)
        # The updater must have proceeded, not aborted on LockBusy
        assert data["overall"] == "succeeded"
        # Metadata from the tray's pre-init is preserved (to_version)
        assert data["to_version"] == "0.41.20"


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    def test_truly_busy_lock_still_refused(
        self, mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        """A status file with an active phase (current_phase != None) means
        another updater is really running — the second updater must still
        abort."""
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")

        us_mod.init_status(
            us_mod.UPDATE_STATUS_FILE,
            from_version="a", to_version="b", method="uv",
        )
        us_mod.begin_phase(us_mod.UPDATE_STATUS_FILE, "installing_package")

        updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.20")

        data = us_mod.read_status(us_mod.UPDATE_STATUS_FILE)
        # Second updater aborted; first updater's phase is still in_progress
        assert data["current_phase"] == "installing_package"
```

- [ ] **Step 2: Run — expect 1 fail (the reuse test), 1 pass (the truly-busy test already works today)**

Run: `uv run python -m pytest tests/unit/service/test_updater.py::TestRunUpdateReusesTrayPreInit -v`
Expected: `test_reuses_tray_pre_init_and_completes` FAILS (updater aborts on LockBusy); `test_truly_busy_lock_still_refused` PASSES.

- [ ] **Step 3: Patch `run_update` LockBusy handler**

Edit `jacked/service/updater.py`. Find the existing block:

```python
    try:
        _us.init_status(
            _us.UPDATE_STATUS_FILE,
            from_version=_current_version,
            to_version=_target,
            method=_method,
            log_path=str(UPDATE_LOG),
        )
    except _us.LockBusy as exc:
        log(f"REFUSED: another updater active: {exc}")
        log_fh.close()
        return
    except Exception:
        logger.exception("Could not initialize update status file")
```

Replace with:

```python
    try:
        _us.init_status(
            _us.UPDATE_STATUS_FILE,
            from_version=_current_version,
            to_version=_target,
            method=_method,
            log_path=str(UPDATE_LOG),
        )
    except _us.LockBusy as exc:
        # Distinguish tray pre-init (we spawned from the tray, which
        # init_status'd a fraction of a second ago with no phases yet) from
        # a real concurrent updater (has phases in flight).
        prior = _us._read_raw(_us.UPDATE_STATUS_FILE) or {}
        phases = prior.get("phases") or []
        current_phase = prior.get("current_phase")
        # No phases opened AND no current_phase = pre-init placeholder. Reuse.
        if not phases and current_phase is None:
            log(f"REUSING tray pre-init status file: {exc}")
            # Don't re-init — the tray's metadata is already there.
        else:
            log(f"REFUSED: another updater active: {exc}")
            log_fh.close()
            return
    except Exception:
        logger.exception("Could not initialize update status file")
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_updater.py::TestRunUpdateReusesTrayPreInit -v`
Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_updater.py
git commit -m "fix(updater): reuse tray pre-init status file instead of aborting on LockBusy"
```

---

## Task 1: `mark_failed()` helper in update_status

**Files:**
- Modify: `jacked/service/update_status.py`
- Test: `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Failing test**

Append to `tests/unit/service/test_update_status.py`:

```python
def test_mark_failed_sets_overall_with_error_and_recovery(tmp_path):
    from jacked.service.update_status import (
        init_status, mark_failed, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    mark_failed(p, error="uv not on PATH",
                recovery="Install uv from https://docs.astral.sh/uv/")
    data = read_status(p)
    assert data["overall"] == "failed"
    assert data["error"] == "uv not on PATH"
    assert data["recovery"] == "Install uv from https://docs.astral.sh/uv/"


def test_mark_failed_preserves_existing_phases(tmp_path):
    from jacked.service.update_status import (
        init_status, begin_phase, end_phase, mark_failed, read_status,
    )
    p = tmp_path / "status.json"
    init_status(p, from_version="a", to_version="b", method="uv")
    begin_phase(p, "installing_package")
    end_phase(p, "installing_package", status="ok")
    mark_failed(p, error="downstream step errored", recovery="")
    data = read_status(p)
    assert data["overall"] == "failed"
    # Prior completed phase stays ok; we're not overwriting per-phase state
    assert data["phases"][0]["status"] == "ok"


def test_mark_failed_on_missing_file_is_noop(tmp_path):
    from jacked.service.update_status import mark_failed, read_status
    # No init_status — file doesn't exist
    mark_failed(tmp_path / "nope.json", error="x", recovery="y")
    # Expected: does NOT raise, does NOT create a new file from whole cloth
    # (because there's no context to stamp — caller must init_status first)
    assert read_status(tmp_path / "nope.json") is None
```

- [ ] **Step 2: Run — expect failures**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v -k mark_failed`
Expected: 3 fails (import error on `mark_failed`).

- [ ] **Step 3: Implement**

Append to `jacked/service/update_status.py`:

```python
def mark_failed(
    path: Path,
    error: str,
    recovery: Optional[str] = None,
) -> None:
    """Mark overall=failed with an explicit error/recovery.

    Use when a failure happens OUTSIDE any active phase (e.g. pre-flight
    failure, post-phase exception). No-op if the status file doesn't exist —
    the caller is responsible for init_status first when appropriate.

    Unlike end_phase with status='failed', this doesn't require an in-progress
    phase. Existing phase history is preserved.
    """
    data = _read_raw(path)
    if data is None:
        return
    data["overall"] = "failed"
    data["error"] = error
    if recovery:
        data["recovery"] = recovery
    _atomic_write(path, data)
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`
Expected: all pass (prior tests + 3 new).

- [ ] **Step 5: Commit**

```bash
git add jacked/service/update_status.py tests/unit/service/test_update_status.py
git commit -m "feat(updater): mark_failed() helper for out-of-phase terminal state"
```

---

## Task 2: `run_update` early-return branches write terminal status

**Files:**
- Modify: `jacked/service/updater.py`
- Test: `tests/unit/service/test_updater.py`

- [ ] **Step 1: Failing tests**

Append to `tests/unit/service/test_updater.py`:

```python
class TestRunUpdateTerminalStatus:
    """Every early-return path in run_update must leave overall='failed',
    not in_progress. Otherwise the UI sits on 'stuck' for 120s instead of
    surfacing the actual error."""

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.find_bin", return_value=None)  # uv missing
    def test_uv_missing_branch_writes_failed(
        self, mock_find, mock_gate, mock_method,
        tmp_path, monkeypatch,
    ):
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")
        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "failed"
        assert "uv" in (data.get("error") or "").lower()


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available", return_value=True)
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_jacked_missing_after_install_writes_failed(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        """uv install succeeds but find_bin('jacked') returns None afterward."""
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "recovery.txt")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        # uv is found (first call), jacked is not (second call)
        find_calls = iter(["/fake/uv", None])
        mock_find.side_effect = lambda name: next(find_calls)
        mock_run.return_value = MagicMock(returncode=0)
        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray")
        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "failed"
        assert "jacked" in (data.get("error") or "").lower()


    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_mark_succeeded_exception_degrades_to_failed(
        self, mock_popen, mock_run, mock_find, mock_port_avail,
        mock_gate, mock_method, tmp_path, monkeypatch,
    ):
        """If mark_succeeded raises, the status file should end in 'failed'
        (not left as 'in_progress' which would show a 120s stuck banner)."""
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        # Force mark_succeeded to raise
        def boom(*_args, **_kw):
            raise OSError("disk full")
        monkeypatch.setattr(us_mod, "mark_succeeded", boom)

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.20")

        data = us_mod.read_status(tmp_path / "status.json")
        assert data is not None
        assert data["overall"] == "failed"
        assert "mark_succeeded" in (data.get("error") or "")
```

- [ ] **Step 2: Run — expect fails**

Run: `uv run python -m pytest tests/unit/service/test_updater.py::TestRunUpdateTerminalStatus -v`
Expected: 3 fails (overall is in_progress, not failed).

- [ ] **Step 3: Patch the early-return branches**

Edit `jacked/service/updater.py`. Find the three return sites:

**Site A — `if method == "uv" and not uv:` branch (uv missing)**. Currently writes `_write_recovery(...)` then `return`. Add before return:

```python
        if method == "uv":
            uv = find_bin("uv")
            if not uv:
                msg = "Could not find `uv` on PATH. Install uv from https://docs.astral.sh/uv/"
                log(f"ERROR: {msg}")
                try:
                    _us.mark_failed(
                        _us.UPDATE_STATUS_FILE,
                        error="uv not found on PATH",
                        recovery="Install uv from https://docs.astral.sh/uv/ and re-run",
                    )
                except Exception:
                    logger.exception("mark_failed after uv-missing failed")
                _write_recovery(
                    f"Jacked auto-update failed:\n{msg}\n\n"
                    "Manual recovery:\n"
                    f"  {label}\n"
                    "  jacked install --force\n"
                    "  jacked service start\n"
                )
                return
            cmd[0] = uv
```

**Site B — `if not jacked:` branch (jacked missing after install)**. Find the block after `installing_package` `_end("installing_package", "ok")`:

```python
        jacked = find_bin("jacked")
        if not jacked:
            log("Could not locate jacked after install - NOT restarting")
            try:
                _us.mark_failed(
                    _us.UPDATE_STATUS_FILE,
                    error="jacked binary missing after install",
                    recovery="jacked install --force && jacked service start",
                )
            except Exception:
                logger.exception("mark_failed after jacked-missing failed")
            _write_recovery(
                "Jacked auto-update: install succeeded but the `jacked` binary "
                "is no longer on PATH. Run manually:\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return
```

**Site C — mark_succeeded fallback**. Replace the existing try/except around `_us.mark_succeeded`:

```python
        if came_up:
            _end("verifying_service", "ok")
            log(f"Updater done — new service is listening on :{port}")
            if RECOVERY_FILE.exists():
                try:
                    RECOVERY_FILE.unlink()
                except Exception:
                    pass
            try:
                _us.mark_succeeded(_us.UPDATE_STATUS_FILE)
            except Exception:
                logger.exception("mark_succeeded failed — writing mark_failed fallback")
                log("WARNING: mark_succeeded raised — attempting mark_failed fallback")
                try:
                    _us.mark_failed(
                        _us.UPDATE_STATUS_FILE,
                        error="mark_succeeded raised — service came up but final status write failed",
                        recovery="Reload the dashboard: http://127.0.0.1:8321/",
                    )
                except Exception:
                    logger.exception("mark_failed fallback also raised")
                    # Disk-full is the likely culprit — log_fh is already
                    # open so this line still hits disk if any bytes free.
                    log("ERROR: mark_failed fallback also raised — disk likely full")
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_updater.py -v`
Expected: all pass (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py tests/unit/service/test_updater.py
git commit -m "fix(updater): early-return branches mark_failed so UI shows specific error"
```

---

## Task 3: `_update_status_shim` exits 1 on `ValueError`

**Files:**
- Modify: `jacked/cli.py`
- Test: `tests/unit/service/test_update_status.py`

- [ ] **Step 1: Failing test**

Append to `tests/unit/service/test_update_status.py`:

```python
def test_cli_update_status_exits_1_on_unknown_phase(tmp_path, monkeypatch):
    """Drift-catcher: calling end_phase with a name that never had begin_phase
    must surface as a non-zero exit so the Windows batch can detect drift."""
    from click.testing import CliRunner
    from jacked.cli import main
    from jacked.service import update_status as us_mod
    p = tmp_path / "status.json"
    us_mod.init_status(p, from_version="a", to_version="b", method="uv")
    monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", p)
    result = CliRunner().invoke(
        main, ["_update_status", "nonexistent_phase", "ok"],
    )
    assert result.exit_code == 1
```

- [ ] **Step 2: Run — expect failure (current code exits 0)**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py::test_cli_update_status_exits_1_on_unknown_phase -v`
Expected: fails (exit_code is 0).

- [ ] **Step 3: Fix the shim**

Edit `jacked/cli.py`. Find `_update_status_shim` and change the `ValueError` handler:

```python
@main.command(name="_update_status", hidden=True)
@click.argument("phase")
@click.argument("status")
@click.option("--error", default=None)
@click.option("--recovery", default=None)
def _update_status_shim(phase, status, error, recovery):
    """Internal: write one status transition. `status` is in_progress|ok|failed."""
    from jacked.service import update_status as us_mod
    path = us_mod.UPDATE_STATUS_FILE
    try:
        if status == "in_progress":
            us_mod.begin_phase(path, phase)
        else:
            us_mod.end_phase(path, phase, status=status, error=error, recovery=recovery)
    except ValueError as exc:
        # Exit non-zero so the Windows batch's `if errorlevel 1` check fires
        # when the batch drifted out of sync with the phase constant.
        click.echo(f"[update-status] {exc}", err=True)
        sys.exit(1)
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_update_status.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/cli.py tests/unit/service/test_update_status.py
git commit -m "fix(cli): _update_status exits 1 on phase-name drift"
```

---

## Task 4: Windows batch checks `errorlevel` after each status call

**Files:**
- Modify: `jacked/service/updater.py` (`_spawn_windows_tray_updater`)
- Modify: `jacked/cli.py` (`_spawn_windows_upgrade_helper` — same pattern)
- Test: `tests/unit/service/test_updater.py`

- [ ] **Step 1: Failing test**

Append to `tests/unit/service/test_updater.py`:

```python
def test_windows_batch_checks_errorlevel_after_status_writes(
    tmp_path, monkeypatch,
):
    """If any _update_status in_progress call exits 1 (phase-name drift),
    the batch must abort rather than marching on with corrupted state.

    Specific assertion: the line IMMEDIATELY after each
    `_update_status <phase> in_progress` must be an errorlevel guard, not
    the work step. Otherwise the test would pass trivially because the
    pre-existing errorlevel guards AFTER the uv install are within 300
    chars of the installing_package begin_phase.
    """
    from unittest.mock import patch as _patch
    from jacked.service import updater
    monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    with _patch("jacked.install_method.detect_install_method", return_value="uv"), \
         _patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe"), \
         _patch("subprocess.Popen") as mock_popen:
        updater._spawn_windows_tray_updater(
            parent_pid=12345, extras="tray", target_version="0.41.20",
        )
    body_path = mock_popen.call_args[0][0][2]
    body = open(body_path).read()
    try:
        lines = body.splitlines()
        for phase in ["waiting_for_parent", "installing_package",
                      "migrating_settings", "waiting_port_free",
                      "starting_service", "verifying_service"]:
            # Find the in_progress line for this phase
            in_prog_idx = None
            for i, ln in enumerate(lines):
                if f"_update_status {phase} in_progress" in ln:
                    in_prog_idx = i
                    break
            assert in_prog_idx is not None, f"missing begin for {phase}"
            # The NEXT non-empty line must be an errorlevel guard.
            for j in range(in_prog_idx + 1, len(lines)):
                candidate = lines[j].strip()
                if not candidate:
                    continue
                assert candidate.startswith("if errorlevel"), (
                    f"first line after `_update_status {phase} in_progress` "
                    f"is not an errorlevel guard — got: {candidate!r}"
                )
                break
    finally:
        import os as _os
        try: _os.unlink(body_path)
        except OSError: pass
```

- [ ] **Step 2: Run — expect fail**

Run: `uv run python -m pytest tests/unit/service/test_updater.py::test_windows_batch_checks_errorlevel_after_status_writes -v`
Expected: fail (current batch has no errorlevel check after begin_phase).

- [ ] **Step 3: Patch the batch**

Edit `_spawn_windows_tray_updater` in `jacked/service/updater.py`. After each `jacked _update_status <phase> in_progress\r\n` line, insert an errorlevel check. Replace the whole `batch_body` construction — do NOT split into many edits since the template is fragile. Use the pattern:

```python
        '# --- phase: installing_package ---\r\n'
        'jacked _update_status installing_package in_progress\r\n'
        'if errorlevel 1 (\r\n'
        '    echo [%date% %time%] ERROR: _update_status installing_package in_progress drifted >> "%LOGFILE%"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
```

For the full body, walk through each of the six phases and emit `in_progress` + errorlevel guard, then the work step, then `ok` (inside the happy path) or `failed` (inside a failure block). The existing `if errorlevel 1` guards after the `uv tool install` / `jacked install` / verify calls stay as-is.

Concretely, the new batch layout for each phase is:

```
jacked _update_status PHASE in_progress
if errorlevel 1 (
    echo [%date% %time%] status shim drift >> "%LOGFILE%"
    exit /b 1
)
<work step>
if errorlevel 1 (
    jacked _update_status PHASE failed --error "..." --recovery "..."
    exit /b 1
)
jacked _update_status PHASE ok
```

For `waiting_for_parent` the work step is the `:wait` / `tasklist` polling block.
For `waiting_port_free` the work step is `timeout /t 1 /nobreak >NUL`.
For `starting_service` the work step is `start "" /B jacked service start >> "%LOGFILE%" 2>&1`.
For `verifying_service` the work step is the PowerShell `Invoke-WebRequest` loop.

Apply the same pattern to `_spawn_windows_upgrade_helper` in `jacked/cli.py`.

- [ ] **Step 4: Run test**

Run: `uv run python -m pytest tests/unit/service/test_updater.py -v`

- [ ] **Step 5: Commit**

```bash
git add jacked/service/updater.py jacked/cli.py tests/unit/service/test_updater.py
git commit -m "fix(win): batch checks errorlevel after every _update_status call"
```

---

## Task 5: Tray breadcrumb + early init_status + pre-warm host fix

**Files:**
- Modify: `jacked/service/tray.py`
- Test: `tests/unit/service/test_tray.py`

- [ ] **Step 1: Failing tests**

Append to `tests/unit/service/test_tray.py`:

```python
class TestOnUpdateClickBreadcrumbs:
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.spawn_updater_from_tray")
    @patch("jacked.service.update_status.init_status")
    def test_init_status_called_before_spawn(
        self, mock_init, mock_spawn, mock_gate,
    ):
        """init_status must be called BEFORE spawn_updater_from_tray so the
        status file exists even if the detached child dies on its first line."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        call_order = []
        mock_init.side_effect = lambda *a, **kw: call_order.append("init")
        mock_spawn.side_effect = lambda *a, **kw: call_order.append("spawn")

        with patch("urllib.request.urlopen"):  # skip real HTTP
            with patch("webbrowser.open"):
                with patch.object(runner, "_on_stop"):
                    runner._on_update_click()

        assert "init" in call_order
        assert "spawn" in call_order
        assert call_order.index("init") < call_order.index("spawn")


    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.spawn_updater_from_tray")
    def test_breadcrumb_appended_to_update_log(
        self, mock_spawn, mock_gate, tmp_path, monkeypatch,
    ):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        from jacked.service import updater as updater_mod
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        monkeypatch.setattr(updater_mod, "UPDATE_LOG", tmp_path / "update.log")

        with patch("urllib.request.urlopen"):
            with patch("webbrowser.open"):
                with patch.object(runner, "_on_stop"):
                    runner._on_update_click()

        log = (tmp_path / "update.log").read_text()
        assert "tray: update clicked" in log
        assert "PID" in log


    def test_pre_warm_uses_127_0_0_1_regardless_of_host(self):
        """Tray.host may be 0.0.0.0 when binding to all interfaces, but
        urlopen/webbrowser must always target loopback."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="0.0.0.0", port=8321)
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        captured_urls = []
        def capture_urlopen(url, *a, **kw):
            captured_urls.append(url)
            class _R:
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return _R()

        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch("urllib.request.urlopen", side_effect=capture_urlopen):
                with patch("webbrowser.open") as mock_wb:
                    with patch("jacked.service.updater.spawn_updater_from_tray"):
                        with patch.object(runner, "_on_stop"):
                            runner._on_update_click()

        assert captured_urls
        assert "127.0.0.1:8321" in captured_urls[0]
        # Also the browser-open URL uses 127.0.0.1
        wb_url = mock_wb.call_args[0][0]
        assert "127.0.0.1:8321" in wb_url
```

- [ ] **Step 2: Run — expect failures**

Run: `uv run python -m pytest tests/unit/service/test_tray.py::TestOnUpdateClickBreadcrumbs -v`
Expected: 3 fails.

- [ ] **Step 3: Update tray `_on_update_click`**

Edit `jacked/service/tray.py`. Replace the block right after the pre-flight refusal path and before `_lifecycle_lock.acquire` with:

```python
        if not self._lifecycle_lock.acquire(blocking=False):
            return  # already updating/stopping

        # Breadcrumb FIRST — proves the click reached us even if everything
        # downstream fails. Uses the same log file the detached updater
        # appends to.
        try:
            from jacked.service.updater import UPDATE_LOG
            UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(UPDATE_LOG, "a", encoding="utf-8") as _lf:
                _lf.write(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] tray: update clicked "
                    f"(tray PID {os.getpid()}, target v{latest})\n"
                )
        except Exception:
            logger.exception("Could not write tray-click breadcrumb")
```

(requires `import time` and `import os` at module top — verify they're present, add if missing.)

Then update the pre-warm + webbrowser.open block:

```python
            # Pre-warm /update.html against loopback regardless of self.host —
            # self.host may be 0.0.0.0 (bind-all), which clients can't route to
            # on Linux. The service is always reachable on 127.0.0.1 after bind.
            _url = f"http://127.0.0.1:{self.port}/update.html"
            try:
                import urllib.request as _ur
                with _ur.urlopen(_url, timeout=2.0):
                    pass
            except Exception:
                logger.exception("Pre-warm of update.html failed (continuing)")
            try:
                import webbrowser as _wb
                _wb.open(_url)
            except Exception:
                logger.exception("Failed to open update progress page")
```

Then init_status BEFORE spawn (this is new):

```python
            # init_status BEFORE the spawn so the status file exists from t=0.
            # If the detached child dies before its own init_status, the UI
            # still has something to show (waiting_for_parent pending with
            # our breadcrumb metadata).
            try:
                from jacked.service import update_status as _us
                from jacked.service.updater import UPDATE_LOG as _upd_log
                from jacked.install_method import detect_install_method as _det
                from jacked import __version__ as _cv
                _us.init_status(
                    _us.UPDATE_STATUS_FILE,
                    from_version=_cv,
                    to_version=latest or "next",
                    method=_det(),
                    log_path=str(_upd_log),
                )
            except _us.LockBusy:
                logger.info("init_status: another updater already in flight")
            except Exception:
                logger.exception("Pre-spawn init_status failed (continuing)")

            try:
                from jacked.service.updater import spawn_updater_from_tray
                spawn_updater_from_tray(
                    parent_pid=os.getpid(),
                    extras="tray",
                    target_version=(self._version_info or {}).get("latest"),
                    port=self.port,
                )
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
                # Tray stays alive — don't _on_stop if the updater didn't spawn
                return
```

**Important:** keep the final `self._on_stop()` call but only reach it after spawn succeeded (use an explicit flag or return early on spawn failure as shown). This prevents the "tray dead, no child" failure mode.

- [ ] **Step 4: Add tray file logger**

Near `ServiceRunner.__init__`, add:

```python
    def _install_tray_file_logger(self) -> None:
        """Write jacked logger output to ~/.claude/jacked-tray.log.

        Detached launchd/systemd services pipe stderr to /dev/null by default,
        so without a file handler any `logger.exception` during tray click
        handling is lost.
        """
        try:
            import logging as _logging
            from jacked.service import CLAUDE_DIR
            CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
            handler = _logging.FileHandler(
                CLAUDE_DIR / "jacked-tray.log", encoding="utf-8"
            )
            handler.setFormatter(
                _logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
            )
            _logging.getLogger("jacked").addHandler(handler)
            _logging.getLogger("jacked").setLevel(_logging.INFO)
        except Exception:
            # Best-effort; if we can't open the file, fall back to stderr
            pass
```

Call it from `ServiceRunner.run()` as the first line (before `check_tray_deps()`).

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/unit/service/test_tray.py -v`

- [ ] **Step 6: Commit**

```bash
git add jacked/service/tray.py tests/unit/service/test_tray.py
git commit -m "fix(tray): breadcrumb + init_status before spawn + 127.0.0.1 pre-warm + file logger"
```

---

## Task 6: Phase-drift enforcement test

**Files:**
- Create: `tests/unit/service/test_phase_drift.py`

- [ ] **Step 1: Write the test**

Create `tests/unit/service/test_phase_drift.py`:

```python
"""Static guard: every phase name in update_phases.PHASES must appear in
both the POSIX updater (as _begin/_end calls) AND the Windows batch body.
Prevents the 'new dev adds phase N+1, forgets writer X' regression."""

import ast
from pathlib import Path

from jacked.service.update_phases import PHASE_NAMES


def _collect_phase_args(source: str) -> set[str]:
    """Parse `source` and return every string literal passed as the FIRST arg
    to a call whose func name ends with 'begin_phase', 'end_phase', '_begin',
    or '_end'."""
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname not in ("begin_phase", "end_phase", "_begin", "_end"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def test_updater_py_covers_all_phases():
    import jacked.service.updater as _u_mod
    src = Path(_u_mod.__file__).read_text()
    found = _collect_phase_args(src)
    for name in PHASE_NAMES:
        assert name in found, (
            f"updater.py has no _begin/_end call for {name!r} — "
            "every PHASE_NAMES entry must be opened AND closed"
        )


def test_windows_batch_body_contains_every_phase():
    """Generate the Windows batch and assert every phase appears with both
    in_progress and ok or failed transitions."""
    import subprocess as _sp
    from unittest.mock import patch
    from jacked.service import updater
    from pathlib import Path as _P
    import tempfile as _tmp

    with patch("jacked.install_method.detect_install_method", return_value="uv"):
        with patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe"):
            with patch("subprocess.Popen") as mock_popen:
                with patch.object(_sp, "DETACHED_PROCESS", 0x8, create=True):
                    updater._spawn_windows_tray_updater(
                        parent_pid=12345, extras="tray", target_version="0.41.20",
                    )

    batch_path = mock_popen.call_args[0][0][2]
    try:
        body = _P(batch_path).read_text()
        for name in PHASE_NAMES:
            assert f"_update_status {name} in_progress" in body, (
                f"Windows batch missing begin for {name!r}"
            )
            assert (
                f"_update_status {name} ok" in body
                or f"_update_status {name} failed" in body
            ), f"Windows batch missing close for {name!r}"
    finally:
        import os as _os
        try: _os.unlink(batch_path)
        except OSError: pass


def test_update_html_still_embeds_every_phase():
    """Belt-and-suspenders: the browser UI must have every phase too.
    Duplicates test_update_phases.test_update_html_embeds_all_phase_names
    but kept here so the drift suite fails as one unit."""
    import jacked
    repo_root = Path(jacked.__file__).resolve().parent
    html = (repo_root / "data" / "web" / "update.html").read_text()
    for name in PHASE_NAMES:
        assert name in html, f"update.html missing phase {name!r}"
```

- [ ] **Step 2: Run**

Run: `uv run python -m pytest tests/unit/service/test_phase_drift.py -v`
Expected: all 3 pass against the current implementation (no drift today).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/service/test_phase_drift.py
git commit -m "test(updater): ast-based phase-drift guard across POSIX + Windows + HTML"
```

---

## Task 7: Version bump + changelog

**Files:**
- Modify: `jacked/__init__.py`
- Modify: `README.md`

- [ ] **Step 1: Bump**

```python
__version__ = "0.41.20"
```

- [ ] **Step 2: Changelog row above 0.41.19**

Add to `README.md`:

```markdown
| **0.41.20** | **Post-ship fixes for 0.41.19.** (1) Tray Update click writes a breadcrumb to `~/.claude/jacked-update.log` and `init_status` before spawning the updater — eliminates the "tray dead, no diagnostics" failure where the detached child crashed before its first log line. Tray also gains a file logger at `~/.claude/jacked-tray.log` so launchd-detached exceptions don't disappear to `/dev/null`. (2) `run_update` early-return branches (`uv not on PATH`, `jacked binary missing after install`, `mark_succeeded` raises) now write `overall: failed` with explicit error + recovery so the UI shows the actual problem instead of a 120s stuck banner. (3) `_update_status` CLI shim exits 1 on `ValueError` so the Windows batch's `errorlevel` check catches phase-name drift. Windows batch now guards every status write with `if errorlevel 1`. (4) Tray pre-warm and browser-open use literal `127.0.0.1` regardless of `self.host` — fixes the case where host=0.0.0.0 broke Linux clients. (5) New AST-based test enforces that every phase in `update_phases.PHASE_NAMES` has matching writers in POSIX updater, Windows batch, and the HTML — prevents a future dev from forgetting a surface when adding phases. |
```

- [ ] **Step 3: Full test suite**

Run: `uv run python -m pytest tests/unit/ --ignore=tests/unit/test_analytics_anomalies.py 2>&1 | tail -3`
Expected: `1777 passed` (1770 from 0.41.19 + 7 new) or equivalent — no new failures.

- [ ] **Step 4: Commit**

```bash
git add jacked/__init__.py README.md
git commit -m "chore: bump to 0.41.20 with changelog"
```

---

## Task 8: Ship (push + tag + release)

- [ ] **Step 1: Push**

```bash
git push origin master
```

- [ ] **Step 2: Tag**

```bash
git tag -a v0.41.20 -m "v0.41.20 — 0.41.19 post-ship diagnostic + terminal-status fixes"
git push origin v0.41.20
```

- [ ] **Step 3: GitHub release**

```bash
gh release create v0.41.20 \
  --title "v0.41.20 — 0.41.19 post-ship diagnostic + terminal-status fixes" \
  --notes "$(cat <<'EOF'
Polish release addressing `/dc` findings from 0.41.19.

**Fixed:**
- Tray Update click no longer dies silently — writes a breadcrumb to `~/.claude/jacked-update.log` + `init_status` before spawning; tray gains file logger at `~/.claude/jacked-tray.log` (launchd-detached exceptions previously went to `/dev/null`).
- `run_update` early-return branches (uv missing, jacked missing, mark_succeeded raise) now write `overall: failed` with explicit error + recovery so the progress page shows the actual problem.
- `jacked _update_status` CLI shim exits 1 on phase-name drift; Windows batch checks `errorlevel` after every status call.
- Tray pre-warm + browser-open use literal `127.0.0.1` regardless of `self.host` (host=0.0.0.0 was broken on Linux clients).

**Added:**
- AST-based test ensures every phase in `update_phases.PHASE_NAMES` has matching writers in the POSIX updater, Windows batch, and `update.html`. Prevents future drift when adding phases.

**Upgrade:** `jacked upgrade` (uv-tool installs) or click Update in the tray.
EOF
)"
```

- [ ] **Step 4: Verify publish**

```bash
gh run list --workflow=publish.yml --limit=1
```

---

## Self-Review Notes

**Spec coverage:**
- CRITICAL (tray silent death): Task 5 — breadcrumb + init_status-before-spawn + tray file logger + spawn-failure keeps tray alive. ✓
- MEDIUM #1 (early-return branches): Task 2 — `mark_failed` called on uv-missing, jacked-missing, mark_succeeded-raise. ✓
- MEDIUM #2 (mark_succeeded fallback): Task 2 Site C. ✓
- MEDIUM #3 (CLI shim silent): Task 3 + Task 4 (batch errorlevel check). ✓
- MEDIUM #4 (pre-warm host): Task 5 Step 3 (literal 127.0.0.1). ✓
- MEDIUM #5 (phase-drift enforcement): Task 6. ✓
- Version/changelog: Tasks 7, 8. ✓

**No placeholders.** All code blocks complete. No "TBD"/"similar to above".

**Type consistency:** `mark_failed(path, error, recovery=None)` signature consistent across Task 1 definition and Task 2 call sites. `PHASE_NAMES` used consistently as the enforcement source. `UPDATE_STATUS_FILE` / `UPDATE_LOG` module-level constants referenced the same way throughout.

**Explicit out-of-scope flags:**
- Atomic settings.json migration (jacked install --force) — existing backup mechanism covers most cases; real atomicity is a bigger refactor for 0.42.x.
- File-size split of cli.py / tray.py — pre-existing condition, not a regression.

Plan complete and saved to `docs/superpowers/plans/2026-04-18-install-method-and-update-ux-fixes.md`. Two execution options:

1. **Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.
2. **Inline Execution** — use superpowers:executing-plans with checkpoints.

**Per /dc rules**, this plan must now be reviewed (planning-phase /dc) before executing. That's the next step regardless of which execution mode the user picks.

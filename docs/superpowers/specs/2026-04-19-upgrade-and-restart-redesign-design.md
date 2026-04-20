# 0.41.24 — Upgrade + Service Restart: Delegate to Platform Primitives

**Date:** 2026-04-19
**Target version:** 0.41.24
**Status:** Approved for planning

## Problem

The `jacked upgrade` + `jacked service restart` flow has been whack-a-mole
across 0.41.16 through 0.41.23. Each release fixes one failure mode; new
modes appear. The pattern is systemic, not isolated:

- **0.41.16**: added uv detection → caught broken pip fallback.
- **0.41.19**: editable detection + refusal → caught dev-clone crashes.
- **0.41.21**: macOS symlink resolve bug → caught uv users mislabeled as pip.
- **0.41.22**: launchctl kickstart → caught stop+start race.
- **0.41.23**: atomic DB watchdog → caught orphaned `validation_status='checking'`.

The underlying cause is architectural:

1. **Bespoke upgrade logic.** `_run_upgrade_inline()` detects install method
   (uv / pipx / pip / editable) at upgrade time and runs the right command.
   Every detection bug (symlink, pip-missing-in-venv, editable heuristic)
   breaks the upgrade path. And the old binary is the one that executes
   upgrade — a bug in the old binary makes the upgrade unbootstrappable
   except via manual `uv tool install --force`.

2. **Bespoke restart logic.** `service_restart()` has two code paths:
   - Native (launchctl kickstart) — works only if plist is installed.
   - Manual (stop + wait for port + spawn) — races launchd's `KeepAlive` or
     any process that binds :8321 in the wait window.
   When the native path is unavailable (plist missing — common on machines
   that installed via raw `uv tool install` without ever running
   `jacked install`), we drop to the race-prone manual path.

3. **Combinatorial test surface.** The matrix of (install method) × (plist
   present?) × (tray running?) × (port state) has too many cells. Each cell
   has its own race. Unit tests cover individual cells but interactions keep
   biting in prod.

## Non-goals

- **Windows native lifecycle.** Windows has no launchd/systemd equivalent
  we can rely on. We keep the current detached stop+start path for Windows
  (the Startup `.vbs` is login-only and isn't a real supervising manager).
  Windows bugs get their own fix later.
- **Systemd unit auto-generation on Linux.** Users who want auto-start on
  Linux still DIY. We use `systemctl --user restart jacked` if a user unit
  is present; otherwise fall through to manual detached spawn.
- **Changing `jacked install` itself.** It already writes the plist + starts
  the service. We just call it from more places.

## Approach — delegate to platform primitives

### A. `jacked upgrade` — shell over `uv tool install --force`

Replace the entire body of `upgrade()` (`jacked/cli.py:787`) and the current
`_run_upgrade_inline()` helper with this logic:

1. Detect current install method:
   - If `_is_editable_install()` → print recovery (`git pull && uv sync`), exit 0.
   - If uv → `uv tool install --force "claude-jacked[<extras>]"`.
   - If pipx → `pipx upgrade "claude-jacked"`.
   - If pip → print recovery (`uv tool install "claude-jacked[tray]" --force`), exit 0.
     Do NOT attempt `pip install --upgrade` — pip in uv-managed venvs
     doesn't exist (the 0.41.17 failure the user reported today).
2. Detect extras from the currently-installed dist-info:
   - Scan `jacked_bin venv`'s `site-packages/claude_jacked-*.dist-info/METADATA`
     for `Provides-Extra` to learn what's published.
   - Probe running venv: if `import pystray` succeeds → include `tray`;
     if `import qdrant_client` succeeds → include `search`.
   - Default to `tray` if nothing detected (most users want it).
3. Run the package manager command via `subprocess.run` (capture stdout for
   display). Exit on non-zero.
4. After successful install, restart the service:
   - Try `native_restart()` first (launchctl kickstart / systemctl --user).
   - If that fails because plist/unit isn't installed, auto-run
     `jacked install --tray` (idempotent — writes plist, bootstraps).
   - Retry `native_restart()` after install. If still fails (e.g., Windows),
     fall through to the Windows-style detached spawn.

This eliminates: the "old binary has broken detection" chicken-and-egg, the
pip-missing-in-uv-venv crash, the mismatched-method upgrade attempt.

### B. `jacked service restart` — require a lifecycle manager (auto-install)

Replace the body of `service_restart()` (`jacked/cli.py:3317`) with:

1. On `--foreground`: keep existing behavior (interactive debug path).
2. Otherwise:
   - Try `native_restart()`. Success → done.
   - If it fails because plist/unit missing AND platform supports one
     (macOS / Linux), auto-run `jacked install --tray` (idempotent), then
     retry `native_restart()`.
   - If native_restart still fails (e.g., Windows, or install itself
     failed), fall through to the Windows detached spawn path with a 5s
     inter-stop/start delay. Print a clear warning that this path is
     race-prone.

This eliminates: the "plist missing → manual stop+start → race" combo.

### C. `jacked/service/updater.py` (tray Update click) — same delegation

The tray's "Update" button currently runs a spawned helper (`run_update`)
that internally does its own pip/uv install command. Replace that with:

1. Call `uv tool install --force` (or pipx upgrade) via subprocess.
2. After install, call `native_restart()` → fall back to `jacked install`
   + retry → fall back to detached spawn.
3. All phase-status writes stay (the /update.html progress page still works).

### D. Remove dead code

After B and C land, the following are dead:
- `_run_upgrade_inline` (jacked/cli.py:850)
- The pip / pipx direct-upgrade code paths inside `updater.py`'s
  `installing_package` phase
- Test files asserting pip upgrade behavior

Delete them. Don't leave them behind as "just in case" — they're the
surface area that keeps growing bugs.

### E. New `ensure_native_lifecycle()` helper

Add to `jacked/service/platform.py`:

```python
def ensure_native_lifecycle() -> tuple[bool, str]:
    """Return (True, 'already installed') if plist/unit exists, else
    attempt to install it. Returns (False, reason) if install fails or
    platform has no native lifecycle manager (Windows).

    This is the single entry point for 'make sure restart will work'.
    """
```

Called by `service_restart()` and by `upgrade()` before `native_restart()`.

## Files touched

| File | Change |
| --- | --- |
| `jacked/cli.py` | Rewrite `upgrade()` + `service_restart()`; delete `_run_upgrade_inline` |
| `jacked/service/platform.py` | Add `ensure_native_lifecycle()` helper |
| `jacked/service/updater.py` | Replace installing_package branch with `uv tool install --force` delegation |
| `jacked/install_method.py` | No change (still used for editable detection) |
| `jacked/__init__.py` | Bump to 0.41.24 |
| `README.md` | Changelog |

Tests:
- `tests/unit/test_upgrade_command.py` — rewrite to assert delegation (no more pip/pipx branch tests)
- `tests/unit/service/test_cli.py::TestServiceRestart` — remove manual-path tests (that path is gone for macOS/Linux), add `test_auto_installs_plist_when_missing_then_kickstarts`
- `tests/unit/service/test_updater.py` — remove pip-branch tests, add delegation test
- New: `tests/unit/service/test_ensure_native_lifecycle.py`

## Test plan

1. Unit: all 3 supported methods (uv, pipx, editable) + editable-refuse + pip-refuse paths verified via mocked subprocess.
2. Unit: `service_restart` with plist-missing → verifies `jacked install` is called.
3. Unit: `service_restart` with plist-present → verifies only `launchctl kickstart`, no install.
4. Integration (manual, not CI): on macOS, uninstall plist (`launchctl unload` + rm), run `jacked service restart` — verify plist gets installed and service comes up.
5. Manual: run `jacked upgrade` from an older version on a fresh uv-tool install, verify it delegates + restarts cleanly.

## Acceptance criteria

- `jacked upgrade` on any install (uv/pipx/pip/editable) either delegates to the correct package-manager command OR prints a clear recovery message. Never fails with `No module named pip`.
- `jacked service restart` on macOS with missing plist auto-installs it and kickstarts — no "Port 8321 already in use" errors.
- `jacked service restart` on macOS with plist present uses only launchctl kickstart — no manual stop+start race path.
- Tray "Update" click goes through the same delegation pipeline.
- No pip / pipx direct-upgrade code path exists in any file.

## Risks + open questions

- **Risk: `uv tool install --force` on a machine where `uv` isn't on PATH.** The old code handled this by printing a recovery message. We need to preserve that check: before running the subprocess, verify `uv` is findable. If not, print recovery, exit 1.
- **Risk: the user has modified their plist manually.** `jacked install` overwrites it. That's acceptable — we warn during install.
- **Risk: `jacked install --tray` triggers a side effect the user didn't want** (writes agent behavioral rules, installs MCP servers, etc.). We need an `--only-service` flag or similar to do a minimal agent-register install. Clarify in plan.
- **Open: what about `pipx`?** pipx is less common but supported. `pipx upgrade` works and replaces the binary atomically. Just delegate to it. Same shell-out pattern.
- **Open: editable install + click "Update" in tray.** Tray should refuse with message (current 0.41.19 behavior). Keep.

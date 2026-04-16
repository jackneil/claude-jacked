# Upgrade-Safe Hooks + Tray Auto-Update

**Date:** 2026-04-16
**Status:** Draft
**Scope:** 0.41.0

## Problems

1. **Hook paths break on upgrade.** `jacked install` bakes the absolute site-packages path (including Python minor version) into `~/.claude/settings.json`. When `uv tool upgrade` bumps Python from 3.11 → 3.12, the path is stale and hooks fail with "file not found."

2. **No update UX in tray.** Users don't know when a newer version is available. They must manually check and run `uv tool install --force`, then stop/start the service.

3. **Windows upgrade fails.** Can't overwrite a running `jacked.exe`. Users have to manually stop the service before `uv tool install --force`.

## Solution

Three interlocking features:

1. **`jacked _hook <name>` shim command** — stable indirection layer that survives upgrades.
2. **Tray version check** — menu shows "Update to vX.Y.Z →" when outdated.
3. **Cross-platform updater** — detached Python helper handles stop-update-restart cycle.

## Feature 1: Upgrade-Safe Hooks

### The shim

New hidden subcommand `jacked _hook <name>` that:
1. Reads hook input JSON from stdin (as Claude Code provides)
2. Dispatches to the corresponding handler inside `jacked.data.hooks.<name>`
3. Exits with the handler's exit code

### Hook dispatch

Instead of handlers living as standalone scripts in `data/hooks/*.py`, each file exposes a `main()` function. The shim imports and calls it:

```python
# jacked/cli.py
@main.command(name="_hook", hidden=True)
@click.argument("name")
def _hook(name: str):
    import importlib
    module = importlib.import_module(f"jacked.data.hooks.{name}")
    module.main()  # reads stdin, does work, may exit with code
```

### settings.json format

**Before (0.40.x):**
```json
{
  "command": "/Users/jack/.local/share/uv/tools/claude-jacked/lib/python3.12/site-packages/jacked/data/hooks/security_gatekeeper.py"
}
```

**After (0.41.x):**
```json
{
  "command": "/Users/jack/.local/bin/jacked _hook security_gatekeeper"
}
```

The `jacked` binary path is a uv-managed shim. It survives `uv tool upgrade` and Python version bumps.

### Migration

`jacked install` detects legacy paths in `settings.json` (any path containing `data/hooks/*.py`) and rewrites them to the `_hook` form. Idempotent: running twice is safe.

### Backward compatibility

The existing `main()` functions in `security_gatekeeper.py`, `session_account_tracker.py`, `qa_suggest.py` will be called both by:
- **Legacy path** (stale settings.json): Python runs the script directly → `if __name__ == "__main__": main()` still works
- **New path** (upgraded settings.json): `jacked _hook <name>` → imports module → calls `main()`

No change to hook handler code required.

## Feature 2: Tray Version Check

### Reuse existing module

`jacked.version_check.check_version_cached(current, force=False)` already exists:
- Polls PyPI for latest version
- Caches result in `~/.claude/version-cache.json` for 24h
- Returns `{"latest": "0.41.0", "outdated": true, ...}`

### Tray integration

On tray startup, the `ServiceRunner._setup` callback calls `check_version_cached()` in a background thread. Result cached in `self._version_info`.

### Dynamic menu

Replace the static version label with a dynamic item:

```python
pystray.MenuItem(
    lambda _: self._version_menu_text(),
    self._on_update_click,
    enabled=lambda _: self._version_info and self._version_info.get("outdated", False),
)
```

`_version_menu_text()` returns:
- `"v0.41.0"` (disabled) when current
- `"Update to v0.41.1 →"` (clickable) when outdated
- `"v0.41.0 (update check failed)"` (disabled) when PyPI unreachable

Re-poll every hour to catch new releases without restarting.

## Feature 3: Cross-Platform Updater

### The flow

1. User clicks "Update to vX.Y.Z →" in tray menu
2. Tray shows notification: "Updating jacked... service will restart"
3. Tray spawns detached updater subprocess, passing current PID
4. Tray triggers clean stop of uvicorn (SuccessfulExit=false prevents launchd respawn)
5. Updater waits for PID to exit
6. Updater runs `uv tool install "claude-jacked[tray]" --force`
7. Updater spawns `jacked service start` detached
8. Updater exits
9. New tray icon appears when new service binds

### The updater

New file `jacked/service/updater.py` with `run_update(parent_pid, extras)` function.

Invoked via hidden CLI command `jacked _update-helper <parent_pid>` so it survives as a top-level process (not a child of the dying tray).

### Cross-platform detachment

- **macOS/Linux:** `subprocess.Popen([...], start_new_session=True)` + `stdin=DEVNULL, stdout/err=FILE`
- **Windows:** `subprocess.Popen([...], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)`

### Waiting for parent exit

```python
import os, time

def _wait_for_exit(pid, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # probe
            time.sleep(0.5)
        except (OSError, ProcessLookupError):
            return True
    return False  # timeout — force continue anyway
```

### Running the install

```python
subprocess.run(
    ["uv", "tool", "install", "claude-jacked[tray]", "--force"],
    check=False,
)
```

Errors logged to `~/.claude/jacked-update.log`. If install fails, tray does NOT restart — user sees old version still.

### Restarting the service

After successful install:
```python
subprocess.Popen(
    [find_bin("jacked"), "service", "start"],
    start_new_session=True,
    stdin=DEVNULL, stdout=log, stderr=log,
)
```

`find_bin` (from existing `jacked.findbin`) resolves the (potentially new) binary location.

## Edge Cases

- **Update during active session:** Clean uvicorn shutdown closes websockets gracefully. Dashboard users see disconnect + reconnect when new service binds.
- **Install fails (network, PyPI down):** Updater logs error, skips restart. User has to start manually. Next tray click shows old version.
- **Multiple quick clicks on "Update":** `_lifecycle_lock` prevents re-entry of update flow.
- **User runs `jacked webux` concurrently:** Update blocks on port conflict. Log message tells user to stop webux first.
- **Windows .exe lock:** Updater waits for PID exit before `uv tool install`. After exit, Windows releases the lock. uv can overwrite.

## Testing

- **Unit:** Hook dispatch, version check integration, updater wait-for-exit logic, menu text generation
- **Integration:** End-to-end update on a dev install (manual)
- **Migration:** Legacy settings.json → new format on `jacked install --force`

## Out of Scope

- Automatic update without user click (not this release)
- Changelog display in tray (future: "See what's new")
- Downgrade support (PyPI doesn't support this cleanly)
- Update notifications outside the tray (e.g., CLI warning on `jacked` invocation)

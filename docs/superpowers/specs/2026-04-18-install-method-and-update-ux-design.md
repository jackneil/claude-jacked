# Install-method safety + tray-update progress UX

**Target release:** 0.41.19
**Platforms:** macOS, Linux, Windows

## Problem statement

Two failures observed on the user's Mac + implied on other boxes:

1. **Editable / pip-without-pip installs silently fail to upgrade.**
   `detect_install_method()` classifies anything that's not a uv-tool or pipx venv
   as `"pip"` and shells out to `<python> -m pip install --upgrade`. When the
   tray is running from a uv-synced editable dev clone, that Python doesn't
   ship the `pip` module → `ModuleNotFoundError: No module named pip` →
   updater writes a misleading recovery file → tray dies with no new tray
   coming up. Evidence: `~/.claude/jacked-update.log` entry at
   `2026-04-18 12:17`.

2. **Clicking "Update" in the tray has no visible progress.**
   The current flow spawns a detached updater and immediately stops the tray.
   On the user's screen: tray disappears. Then either a new tray appears
   (success) or nothing (failure). The user can't tell which for 30–90s,
   and failures leave only `~/.claude/jacked-update.log` + sometimes
   `~/.claude/jacked-update-failed.txt` to find.

Both bite cross-platform. Fix together because they share the "update click
handler" as the ingress point.

## Goals

1. **Make the install-method detector honest about installs it can't safely
   upgrade** — editable installs and pip-without-pip get refused with a clear
   recovery message instead of a silent pip failure.
2. **Give users live, cross-platform visual feedback** during a tray-initiated
   update. On macOS, Linux, and Windows.
3. **Keep the tray survival story intact** — on POSIX the old tray can live
   through `uv tool install --force`; on Windows the binary lock still forces
   it to die before install. UX must work for both.

## Non-goals

- Auto-starting the service after a fresh `uv tool install` (queued for 0.42.0).
- Installing arbitrary upgrade mechanisms (`apt`, `brew`). uv + pipx only.
- Re-introducing pip as a documented install path.

## Design

### Component 1 — `jacked.install_method`

Add a fourth return value: `"editable"`. Detection order stays: uv → pipx →
editable → pip.

Editable detection fingerprint (first hit wins):

1. Scan every directory on `sys.path` for files matching
   `_editable_impl_*.pth` or `__editable__.*.pth`. These are the canonical
   markers written by `uv sync` / `setuptools` for editable installs.
2. Fall back to: resolve `jacked.__file__`. If the resolved path is NOT
   under any directory on `sys.path` that ends in `site-packages/`,
   treat as editable. This catches editable installs that use a different
   marker (rare).

Add a new public function:

```python
def can_auto_upgrade() -> tuple[bool, str]:
    """Return (ok, reason).

    ok=True: method is uv or pipx — auto-upgrade is safe.
    ok=False: method is editable or pip — refuse with the reason string,
              which includes the manual recovery command the user should run.
    """
```

Reasons returned:
- editable → `"dev-clone editable install detected. Auto-update disabled. Upgrade manually: cd <repo> && git pull && uv sync"`
- pip → `"pip install detected. Auto-update disabled (uv is the supported install method). Migrate with: uv tool install \"claude-jacked[tray]\""`

`upgrade_command()` / `upgrade_command_label()` still return values for pip
and editable (so tests don't break), but callers must consult
`can_auto_upgrade()` first.

### Component 2 — Pre-flight in the upgrade entry points

Two entry points run an upgrade today: the CLI `jacked upgrade` command and
the tray's `_on_update_click` handler. Both get a pre-flight check before
doing anything destructive.

**CLI (`jacked upgrade`)**: check `can_auto_upgrade()`. If `ok=False`, print
the reason to stderr and exit 2. Do not touch the service.

**Tray (`ServiceRunner._on_update_click`)**: check `can_auto_upgrade()`. If
`ok=False`, call `icon.notify(title="Auto-update disabled", message=reason)`
and return without killing the tray or spawning the updater. Also write
`~/.claude/jacked-update-failed.txt` with the reason so the user sees it on
next tray startup (the existing recovery-file mechanism handles that).

### Component 3 — Update status JSON

The detached updater (and the Windows cmd.exe batch) writes
`~/.claude/jacked-update-status.json` at every phase transition. One file,
replaced atomically via temp-file-rename.

Schema:

```json
{
  "started_at": "2026-04-18T12:17:10Z",
  "from_version": "0.41.10",
  "to_version": "0.41.19",
  "method": "uv",
  "current_phase": "verifying_service",
  "phases": [
    {"name": "waiting_for_parent", "started_at": "...", "finished_at": "...", "status": "ok"},
    {"name": "installing_package", "started_at": "...", "finished_at": "...", "status": "ok"},
    {"name": "migrating_settings", "started_at": "...", "finished_at": "...", "status": "ok"},
    {"name": "waiting_port_free", "started_at": "...", "finished_at": "...", "status": "ok"},
    {"name": "starting_service", "started_at": "...", "finished_at": "...", "status": "ok"},
    {"name": "verifying_service", "started_at": "...", "finished_at": null, "status": "in_progress"}
  ],
  "overall": "in_progress",
  "error": null,
  "recovery": null,
  "log_path": "/Users/.../jacked-update.log"
}
```

Phase names are stable identifiers:

| Phase                 | Meaning                                         |
|-----------------------|-------------------------------------------------|
| `waiting_for_parent`  | Waiting for the old tray PID to exit            |
| `installing_package`  | Running the upgrade command (uv / pipx)         |
| `migrating_settings`  | Running `jacked install --force`                |
| `waiting_port_free`   | Waiting for port 8321 to release                |
| `starting_service`    | Spawning the new detached `jacked service start`|
| `verifying_service`   | Polling port 8321 until the new service binds   |

Per-phase `status` values: `in_progress`, `ok`, `failed`. Overall:
`in_progress`, `succeeded`, `failed`.

### Component 4 — `/update.html` progress page

New page in the dashboard at `/update.html`. Static HTML + JS, served by the
current live service **before** the tray kills itself. The `.html` suffix
is intentional — without it the SPA catch-all in `jacked/api/main.py` would
fall back to `index.html` instead of serving our dedicated page.

The page:

1. Starts by fetching `/api/update/status` every second.
2. Renders a phase list with visual state: pending (grey), in_progress
   (blue spinner), ok (green check), failed (red X).
3. Shows current version, target version, elapsed time.
4. When `/api/update/status` starts returning connection errors (service is
   being replaced), switches to a "Waiting for new service…" state and
   keeps polling.
5. Polls `/api/version` as well. When the `current` version in the response
   equals the target version, shows "Update complete!" and a button linking to
   the dashboard home (`http://127.0.0.1:{port}/`).
6. If the status JSON ends with `overall: "failed"`, shows the error and the
   recovery instructions from the JSON.
7. If the status file's **server-reported** `mtime` (exposed by
   `/api/update/status` in a new `mtime_iso` field) is older than 120s AND
   the service is unreachable AND the page hasn't seen a version match,
   shows "Update appears stuck — see log" and exposes the log path plus a
   "reopen dashboard" button. Using server-reported mtime (not client-side
   `lastStatusSeenAt`) survives page reloads and doesn't reset on repeated
   reads of a stale file.

### Component 5 — `/api/update/status` endpoint

New GET in `jacked/api/routes/system.py`. Reads
`~/.claude/jacked-update-status.json`, returns it as JSON or `null` if the
file doesn't exist. Cacheable for 0s (always re-read). No auth needed (local
dashboard is already unauthenticated).

Also exposes the file path in `/api/version` response under an optional
`update_status_file` field so the progress page knows where to look.

### Component 6 — Tray wire-up

New flow in `_on_update_click`:

```
if not can_auto_upgrade(): notify + return
open browser: http://127.0.0.1:{port}/update
spawn detached updater
stop tray
```

The browser tab opens BEFORE the tray dies. Once open, the page's JS is
driving everything — it survives the service being torn down and re-created.

On Windows the cmd.exe batch opens the page itself as a fallback (after
parent PID has exited), using `start "" http://127.0.0.1:{port}/update.html`.
If the user already opened it from the tray's `webbrowser.open()` call, this
just focuses the existing tab.

### Component 7 — Updater + batch status writes

POSIX updater (`jacked/service/updater.py`):

- Import a new helper `write_update_status(phase=..., status=..., error=..., ...)`
  that merges into the status JSON file atomically.
- Call it at every phase transition.
- On entry, initialize the file with empty `phases` array + `started_at`.
- On exit (either success or failure), set `overall` and close out the
  final phase.

Windows cmd.exe batch: delegates status-file merges to a new hidden CLI
command, `jacked _update_status <phase> <status> [--error=STR]`. The batch
calls it at each phase transition. The command is a thin wrapper over the
same Python helper the POSIX updater uses, keeping a single source of truth
for the JSON merge logic. Requires the jacked binary to be on PATH — which
it already is after `uv tool install`.

Batch example:

```batch
jacked _update_status installing_package in_progress
"%UV%" tool install "claude-jacked[tray]" --force >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    jacked _update_status installing_package failed --error="uv tool install failed"
    exit /b 1
)
jacked _update_status installing_package ok
```

On early-boot phases where the old jacked binary is mid-replacement, the
status writes may fail silently — the batch tolerates errors on these
commands. Worst case the progress page shows the prior phase for a few
seconds longer than reality; when the new service comes up the `current` /
`target` version check closes out the page regardless.

## Error handling (error-handling lens)

- Every file write uses temp-file + `os.replace()` to avoid half-written JSON.
- Readers treat missing/corrupt status file as "no update in progress".
- Tray pre-flight refusal writes recovery file immediately (not just OS
  notification) so it survives tray restart.
- Updater refuses to spawn if the current install method is not auto-upgradable
  (defense in depth — tray pre-flight is the primary block, but a misuse of
  `jacked _run_updater` shouldn't bypass).
- Browser page has a final timeout: if no status file updates + no service
  comes back within 120s, show a prominent error with log path.
- On phase failure, updater writes `overall: "failed"`, sets `error` + `recovery`
  strings, then exits. Browser sees it and displays.
- Pip is refused preemptively (we don't try to run it). Removes the
  `No module named pip` failure class entirely.

## Testing

- Unit tests (Python):
  - `install_method` detection of editable `.pth` markers
  - `install_method` fallback to `jacked.__file__` outside site-packages
  - `can_auto_upgrade()` returns correct `(ok, reason)` for each method
  - Update-status reader/writer roundtrips
  - `/api/update/status` endpoint returns null / returns file / returns
    on corrupt file
- Unit tests (CLI):
  - `jacked upgrade` on an editable install exits 2 with recovery message
  - `jacked upgrade` on a uv install proceeds
- Manual smoke:
  - Dev clone: run `jacked upgrade` — see refusal
  - uv-tool install: click tray Update — browser opens, progress page
    tracks phases, final "Update complete" shown
  - Kill the updater process mid-run — browser shows "stuck" state after
    120s

## Out of scope

- Fresh-install auto-start (0.42.0).
- Changing the updater's retry logic (already hardened in 0.41.10–0.41.13).
- Changing the Windows cmd.exe-batch approach.

## Migration

No database / schema changes. No user-facing config changes. The new status
file is managed entirely by the updater.

Users on 0.41.18 or older clicking Update in their tray:
- Uv-tool install: upgrade proceeds via old flow (no status file, no browser
  page) — unchanged behavior.
- Editable / pip: old flow runs, hits the familiar silent pip failure.
  Once they upgrade by hand to 0.41.19 (or any method), the new pre-flight
  takes over.

This means the fix takes effect from the RUNNING tray's version. Any user
stuck on an editable dev clone has to run `git pull` manually once.

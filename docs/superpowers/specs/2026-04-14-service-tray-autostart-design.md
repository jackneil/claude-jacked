# Service Mode: System Tray + Auto-Start

**Date:** 2026-04-14
**Status:** Draft
**Scope:** macOS + Windows auto-start with system tray icon for `jacked webux`

## Problem

`jacked webux` must be started manually each session. Users want it running automatically on boot with a visible indicator that it's alive, and the ability to stop/restart/open the dashboard from the tray.

## Solution

New `jacked service` CLI command group + pystray-based system tray icon + platform-native auto-start configuration.

## Architecture

### New Files

```
jacked/
  service/
    __init__.py
    tray.py          # pystray integration, menu, icon rendering
    platform.py      # OS-specific auto-start install/uninstall
    process.py       # PID file management, process lifecycle
```

### CLI Surface

New `@main.group()` in `cli.py`:

```
jacked service install [--port 8321] [--host 127.0.0.1]
                               # Write platform auto-start config with baked port/host
jacked service uninstall       # Remove platform auto-start config
jacked service start [--port 8321] [--host 127.0.0.1]
                               # Start webux with tray icon (what auto-start calls)
jacked service stop            # Stop running instance via PID file
jacked service restart         # stop + start
jacked service status          # Show running/stopped, port, PID
```

All subcommands lazy-import from `jacked.service.*` to avoid loading pystray/Pillow on every `jacked` invocation.

### Dependencies

New optional extra in `pyproject.toml`:

```toml
[project.optional-dependencies]
tray = [
    "pystray>=0.19",
    "Pillow>=9.0",
]
```

Install via:
```bash
uv tool install "claude-jacked[tray]" --force
```

The `service` commands check for these at runtime and print a clear install message if missing.

## Tray Icon

### Rendering

Icons generated programmatically via Pillow — no external asset files. A 64x64 image with a rounded-rect background and "J" glyph centered.

### States

| State | Background | Meaning |
|-------|-----------|---------|
| Running | Purple gradient (#6366f1 → #8b5cf6) | Server is up and healthy |
| Stopped | Gray (#555 → #666) | Server not running |
| Starting | Amber (#f59e0b → #d97706) | Server is booting |

### Menu Items

Right-click menu (both platforms):

1. **JACKED** — header label (non-clickable)
2. **Running on :8321** — status with green dot (non-clickable)
3. ---
4. **Open Dashboard** — opens `http://localhost:{port}` in default browser
5. ---
6. **Restart** — kills uvicorn thread, restarts it, flashes amber icon
7. **Stop** — clean shutdown: stops uvicorn, removes PID file, exits tray app
8. ---
9. **Start on Login** — toggle, checkmark when active. Calls `service install`/`uninstall` internally
10. ---
11. **v0.39.0** — version (non-clickable)

## Process Model

### Threading

pystray requires the main thread on macOS (AppKit constraint). Architecture:

```
Main thread:  pystray event loop (blocking)
Thread 1:     uvicorn.run() — the webux server
Thread 2:     (uvicorn's own async tasks: token refresh, sweeps, watchers, etc.)
```

The uvicorn thread is a daemon thread — it dies when the main (tray) thread exits.

### PID File

Location: `~/.claude/jacked-service.pid`

Written on `service start`, removed on clean shutdown. Contains the PID of the tray process (not the uvicorn thread).

Used by:
- `service stop` — reads PID, sends signal
- `service status` — reads PID, checks if process alive
- `service start` — checks for stale PID file on startup

### Startup Sequence

```
jacked service start [--port 8321] [--host 127.0.0.1]
  1. Check for existing PID file
     - If PID file exists and process alive → print "already running", exit 0
     - If PID file exists and process dead → remove stale PID file, continue
  2. Check port availability
     - If port in use → "Port {port} in use. Another jacked instance?
       Check with: jacked service status", exit 1
  3. Write PID file (includes port number for status command)
  4. Create pystray icon with amber state (starting)
  5. Enter pystray main loop (blocks main thread)
     - pystray's setup callback runs on entry:
       a. Start uvicorn in daemon thread
       b. Poll uvicorn ready (try connect to port, up to 10s)
       c. Update tray icon to purple (running)
```

Port and host are baked into the auto-start config at `service install` time via `--port`/`--host` flags on that command too.

### Shutdown Sequence

```
Stop triggered (menu click or SIGTERM or service stop):
  1. Set tray icon to gray
  2. Signal uvicorn thread to shut down (set threading.Event)
  3. Wait up to 5s for uvicorn thread to exit
  4. Remove PID file
  5. Exit tray app
```

### Signal Handling

- **macOS:** SIGTERM handler triggers clean shutdown
- **Windows:** No SIGTERM. `service stop` uses `taskkill /PID {pid}`. The tray app uses `atexit` to clean up PID file.

## Platform Auto-Start

### macOS: launchd

`jacked service install` writes `~/Library/LaunchAgents/ai.hank.jacked.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hank.jacked</string>
    <key>ProgramArguments</key>
    <array>
        <string>{jacked_bin_path}</string>
        <string>service</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{home}/.claude/jacked-service.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/.claude/jacked-service.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{current_PATH}</string>
    </dict>
</dict>
</plist>
```

- `{jacked_bin_path}` resolved via `shutil.which('jacked')` at install time
- PATH captured at install time to ensure uv/python are findable
- `KeepAlive: true` means launchd restarts if it crashes
- After writing plist, runs `launchctl load` to activate immediately

`jacked service uninstall`:
- Runs `launchctl unload` then removes the plist file

### Windows: Startup Folder Shortcut

`jacked service install` creates a `.vbs` script in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\jacked.vbs`:

```vbs
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """path\to\jacked.exe"" service start", 0, False
```

The `0` in `WshShell.Run` hides the console window, so no flash on boot. `jacked.exe` path resolved via `shutil.which('jacked')` at install time (same as macOS).

Why VBS over shortcut: `.lnk` files are binary and harder to create programmatically from Python without `pywin32`. A `.vbs` wrapper is a single text file that does the same job.

`jacked service uninstall`:
- Removes the `.vbs` file from the Startup folder

### Detection

`jacked service install` auto-detects the platform via `sys.platform`:
- `darwin` → launchd path
- `win32` → startup folder path
- Other → prints "Auto-start not supported on this platform. Run `jacked service start` manually."

## `jacked service status` Output

```
Jacked Service: running
  PID:     12345
  Port:    8321
  Uptime:  2h 34m
  Autostart: enabled (launchd)
  Dashboard: http://localhost:8321
```

Or when stopped:

```
Jacked Service: stopped
  Autostart: enabled (launchd)
  Last run: 2026-04-14 09:15:00
```

Uptime derived from PID file mtime. Last run from PID file mtime or service log.

## Conflict with `jacked webux`

If a user runs `jacked webux` while the service is already running (or vice versa), the port check catches it. The error message:

```
Port 8321 is already in use.
Is another jacked instance running? Check with: jacked service status
Use --port to run on a different port.
```

## Integration with Existing `jacked install`

`jacked install` remains unchanged. It handles hooks/agents/commands. The service is a separate concern — users run `jacked install` first (sets up Claude Code integration), then `jacked service install` (sets up auto-start + tray).

Future consideration: `jacked install` could prompt "Would you like to start jacked on login?" at the end, but that's out of scope for this spec.

## Testing Strategy

- **Unit tests:** PID file management, platform detection, icon generation, menu construction
- **Integration tests:** Start/stop lifecycle on current platform (skip on CI if no display server)
- **Manual test matrix:**
  - macOS: install → reboot → verify tray appears → stop/restart from menu → uninstall
  - Windows: install → reboot → verify tray appears → stop/restart from menu → uninstall

## Out of Scope

- Linux support (no tray standard — varies by DE)
- Rich tray info (account count, health alerts) — minimal menu only
- Notifications/alerts from tray
- Auto-updating the service after `uv tool install` upgrades

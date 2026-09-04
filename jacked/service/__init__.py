"""Service mode: system tray + auto-start for jacked webux."""

from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PID_FILE = CLAUDE_DIR / "jacked-service.pid"
SERVICE_LOG = CLAUDE_DIR / "jacked-service.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
LAUNCHD_LABEL = "ai.hank.jacked"

# A cold boot on a slow disk needs well over 10 s to import the app and run
# lifespan startup (observed 2026-09-04: >10.5 s). The wait returns as soon
# as the port answers, so a long budget costs nothing on a warm start.
COLD_START_READY_TIMEOUT = 90.0
# A restart handoff waits this long for the old owner to exit, then this long
# for the replacement to report ready. The replacement is a cold start.
HANDOFF_EXIT_TIMEOUT = 30.0
REPLACEMENT_READY_TIMEOUT = COLD_START_READY_TIMEOUT + 15.0
# sysexits.h EX_TEMPFAIL: launchd (SuccessfulExit=false), systemd
# (Restart=on-failure) and Task Scheduler (RestartOnFailure) all relaunch a
# non-zero exit. A clean exit would be treated as final.
EX_TEMPFAIL = 75
# After this many failed starts inside the window the service exits cleanly
# so a permanently broken environment does not relaunch forever.
START_FAILURE_LIMIT = 5
START_FAILURE_WINDOW_SECONDS = 600.0
# The breaker only resets if every writer and reader names the same file, so
# the name lives here rather than as a literal at each site.
START_FAILURE_FILENAME = "start-failures.json"

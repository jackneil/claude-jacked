"""Service mode: system tray + auto-start for jacked webux."""

from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PID_FILE = CLAUDE_DIR / "jacked-service.pid"
SERVICE_LOG = CLAUDE_DIR / "jacked-service.log"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8321
LAUNCHD_LABEL = "ai.hank.jacked"

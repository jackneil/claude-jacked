#!/usr/bin/env python3
"""QA suggestion Stop hook for Claude Code.

Detects UI file changes in the current session and suggests running /qa.
Fires on Stop events only. Uses a temp file for per-session dedup.

Fire-and-forget: runs in a daemon thread so the hook returns quickly.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

DEDUP_DIR = "/tmp"

UI_EXTENSIONS = frozenset(
    {
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".css",
        ".scss",
        ".less",
        ".html",
        ".vue",
        ".svelte",
        ".erb",
        ".jinja",
        ".jinja2",
    }
)

EXCLUDED_DIRS = frozenset(
    {"node_modules", "dist", "build", "__pycache__"}
)


def _has_ui_changes(files: list[str]) -> bool:
    """Check if any files are UI-related and not in excluded dirs.

    >>> _has_ui_changes(["src/app.js"])
    True
    >>> _has_ui_changes(["server.py"])
    False
    >>> _has_ui_changes(["node_modules/react/index.js"])
    False
    >>> _has_ui_changes([])
    False
    """
    for f in files:
        parts = f.split("/")
        # Check excluded directories
        if any(p in EXCLUDED_DIRS for p in parts):
            continue
        # Check test files
        basename = parts[-1] if parts else f
        if ".test." in basename or ".spec." in basename:
            continue
        # Check extension
        _, ext = os.path.splitext(basename)
        if ext in UI_EXTENSIONS:
            return True
    return False


def _get_changed_files(cwd: str) -> list[str]:
    """Get list of changed files (staged + unstaged) via git diff.

    Falls back to --cached when HEAD doesn't exist (zero-commit repos).

    >>> # Returns empty list on error
    >>> _get_changed_files("/nonexistent")
    []
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
        )
        if result.returncode != 0:
            # HEAD may not exist (fresh repo, no commits) — try staged files
            result = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=5,
            )
            if result.returncode != 0:
                return []
        return [f for f in result.stdout.strip().splitlines() if f]
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []


def _should_suggest(session_id: str, cwd: str, dedup_path: Path) -> bool:
    """Determine if we should suggest /qa for this session.

    Returns False if:
    - No .git directory in cwd
    - Running as a subagent
    - Already suggested this session (dedup file exists)
    - No UI file changes detected

    >>> _should_suggest("test", "/nonexistent", Path("/tmp/test-dedup"))
    False
    """
    # Early-exit: no git repo
    git_dir = Path(cwd) / ".git"
    if not git_dir.exists():
        return False

    # Suppress for subagents
    if os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID") or os.environ.get(
        "CLAUDE_CODE_AGENT_TYPE"
    ):
        return False

    # Atomic dedup check — if file already exists, we already suggested
    if dedup_path.exists():
        return False

    # Check for UI changes
    files = _get_changed_files(cwd)
    if not _has_ui_changes(files):
        return False

    return True


def _mark_suggested(dedup_path: Path) -> bool:
    """Atomically create dedup marker file. Returns True if created.

    Uses O_CREAT | O_EXCL for kernel-level atomicity.

    >>> import tempfile; p = Path(tempfile.mktemp())
    >>> _mark_suggested(p)
    True
    >>> _mark_suggested(p)
    False
    """
    try:
        fd = os.open(str(dedup_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except (FileExistsError, FileNotFoundError, OSError):
        return False


def _handle_stop(session_id: str, cwd: str):
    """Handle a Stop event: check for UI changes and emit suggestion."""
    dedup_path = Path(DEDUP_DIR) / f"jacked-qa-{session_id}"

    if not _should_suggest(session_id, cwd, dedup_path):
        return

    # Atomically mark as suggested (race-safe)
    if not _mark_suggested(dedup_path):
        return

    output = {
        "systemMessage": (
            "UI files were modified in this session. "
            "Run /qa to browser-test the changes, "
            "or /qa <url> to specify the app URL."
        )
    }
    print(json.dumps(output), flush=True)


def main():
    """Read hook input from stdin, dispatch in fire-and-forget thread.

    >>> # main() reads stdin — structure is tested via TestMainEndToEnd
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    cwd = data.get("cwd", "")

    if event != "Stop" or not session_id or not cwd:
        return

    t = threading.Thread(
        target=_handle_stop,
        args=(session_id, cwd),
        daemon=True,
    )
    t.start()
    t.join(timeout=2.0)


if __name__ == "__main__":
    main()

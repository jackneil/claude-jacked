"""Find the Claude Code project folder of a working directory.

Claude Code keeps the transcripts of a session folder in
``~/.claude/projects/<name>/``. The name is lossy (``jacked.project_slug``),
so ``resolve_project_dir`` never trusts it alone: it reads the cwd that a
transcript in the folder recorded.

Standard library only (with ``jacked.jsonl_lines`` and
``jacked.project_slug``), so ``jacked.recover`` stays usable on a bare install.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from jacked.jsonl_lines import iter_lines as _iter_lines, parse_line as _parse_line
from jacked.project_slug import project_dir_name

logger = logging.getLogger(__name__)

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"

#: The most lines of one transcript that the folder search reads for its cwd.
#: Claude Code writes the cwd in the first records of a session. Measured on
#: 2026-11-03 over 7,033 real transcripts (one Mac): the first record with a
#: cwd was on line 8 at most (p99 line 7, median line 3), and every file had
#: one. 200 is more than 10 times that, so a damaged or cwd-less transcript
#: costs 200 lines, not the whole file.
CWD_LINES = 200


def _norm_path(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def _read_cwd(path: Path, max_lines: Optional[int] = None) -> Optional[str]:
    """Return the first top-level 'cwd' field in a transcript that is a non-empty
    string, or None.

    When ``max_lines`` is given, at most that many lines are read. Each
    physical line counts, also a blank or malformed one, and the bound is
    checked before the next line is read.
    """
    try:
        with open(path, "rb") as f:
            for number, line in enumerate(_iter_lines(f), 1):
                ok, rec_obj = _parse_line(line, number) if line else (False, None)
                cwd = rec_obj.get("cwd") if ok and isinstance(rec_obj, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
                if max_lines is not None and number >= max_lines:
                    break
    except OSError as exc:
        logger.debug("project dirs: cannot read a transcript (%s)", type(exc).__name__)
    return None


def _newest_jsonls(d: Path, n: int = 3) -> list[Path]:
    files = [f for f in d.glob("*.jsonl") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:n]


def _dir_matches_cwd(d: Path, norm_target: str) -> bool:
    for f in _newest_jsonls(d):
        cwd = _read_cwd(f, max_lines=CWD_LINES)
        if cwd and _norm_path(cwd) == norm_target:
            return True
    return False


def resolve_project_dir(cwd, projects_root=None) -> Optional[Path]:
    """Map a working directory to its ~/.claude/projects/<slug> dir.

    Never trusts the slug alone (the stored encoding is lossy): verifies the
    fast-path slug by reading a transcript's recorded cwd, else enumerates all
    project dirs and matches on the cwd field.
    """
    if projects_root is None:
        env = os.getenv("CLAUDE_PROJECTS_DIR")
        projects_root = Path(env) if env else DEFAULT_PROJECTS_ROOT
    root = Path(projects_root)
    if not root.exists():
        return None
    norm = _norm_path(str(cwd))
    fast = root / project_dir_name(str(cwd))
    if _is_dir(fast) and _dir_matches_cwd(fast, norm):
        return fast
    # Several dirs can hold transcripts recorded with this cwd (an older
    # encoding, or a session launched elsewhere that later reported it).
    # Prefer the one with the most recently written transcript, never the
    # alphabetically first.
    matches = [d for d in root.iterdir() if d.is_dir() and _dir_matches_cwd(d, norm)]
    if not matches:
        return None
    return max(matches, key=_newest_mtime)


def _is_dir(path: Path) -> bool:
    """Return True when ``path`` is a folder. An OSError (a name that is too
    long, no permission) gives False, so the caller goes on to the search."""
    try:
        return path.is_dir()
    except OSError as exc:
        logger.debug("project dirs: cannot stat the fast-path folder (%s)", type(exc).__name__)
        return False


def _newest_mtime(d: Path) -> float:
    newest = _newest_jsonls(d, n=1)
    return newest[0].stat().st_mtime if newest else 0.0

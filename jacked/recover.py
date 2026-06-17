# jacked/recover.py
"""Crash-recovery for Claude Code sessions.

Find the most-recently-active prior session for a working directory from the
raw on-disk transcripts under ~/.claude/projects, and reconstruct a budgeted
working-state digest so a fresh session can pick up where a crashed one died.

Qdrant-free by design: imports only jacked.transcript + stdlib so /recover
works on a bare install (the moment right after a crash). Never import
jacked.retriever / jacked.searcher here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jacked import transcript as _t

DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_BUDGET_CHARS = 12000
_RECENT_USER_ASKS = 3
_MAX_TOOL_ACTIONS = 12
_MAX_FILES = 20
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _norm_path(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def _encode_cwd(cwd: str) -> str:
    """Encode a cwd the way current Claude Code names its projects dir:
    keep the leading separator (becomes a leading dash) and replace both
    '/' and '.' with '-'."""
    s = str(cwd).replace("\\", "/")
    return s.replace("/", "-").replace(".", "-")


def _iter_records(path: Path):
    """Yield parsed JSON objects from a JSONL file, skipping blank/garbled
    lines. Tolerates a crash-truncated final line (it is simply skipped)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except (IOError, OSError):
        return


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_cwd(path: Path) -> Optional[str]:
    """Return the first top-level 'cwd' field found in a transcript."""
    for rec_obj in _iter_records(path):
        cwd = rec_obj.get("cwd") if isinstance(rec_obj, dict) else None
        if cwd:
            return cwd
    return None


def _newest_jsonls(d: Path, n: int = 3) -> list[Path]:
    files = [f for f in d.glob("*.jsonl") if f.is_file()]
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return files[:n]


def _dir_matches_cwd(d: Path, norm_target: str) -> bool:
    for f in _newest_jsonls(d):
        cwd = _read_cwd(f)
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
    fast = root / _encode_cwd(str(cwd))
    if fast.is_dir() and _dir_matches_cwd(fast, norm):
        return fast
    for d in sorted(root.iterdir()):
        if d.is_dir() and _dir_matches_cwd(d, norm):
            return d
    return None

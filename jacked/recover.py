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


@dataclass
class SessionCandidate:
    session_id: str
    path: Path
    ai_title: Optional[str] = None
    last_prompt: Optional[str] = None
    last_ts: Optional[datetime] = None
    git_branch: Optional[str] = None
    msg_count: int = 0
    truncated: bool = False

    def to_dict(self, now: Optional[datetime] = None) -> dict:
        return {
            "session_id": self.session_id,
            "path": str(self.path),
            "ai_title": self.ai_title,
            "last_prompt": self.last_prompt,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "age": _relative_age(self.last_ts, now),
            "git_branch": self.git_branch,
            "msg_count": self.msg_count,
            "truncated": self.truncated,
        }


def _relative_age(ts: Optional[datetime], now: Optional[datetime] = None) -> Optional[str]:
    if not ts:
        return None
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((now - ts).total_seconds()))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _scan_candidate(path: Path) -> SessionCandidate:
    """One raw pass over a transcript collecting ranking + preview metadata.
    Reads raw (not via _iter_records) so it can flag a garbled final line."""
    ai_title = last_prompt = git_branch = None
    last_ts: Optional[datetime] = None
    msg_count = 0
    last_line_ok = True
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec_obj = json.loads(stripped)
                    last_line_ok = True
                except json.JSONDecodeError:
                    last_line_ok = False
                    continue
                t = rec_obj.get("type")
                if t == "ai-title":
                    ai_title = rec_obj.get("aiTitle") or ai_title
                elif t == "last-prompt":
                    last_prompt = rec_obj.get("lastPrompt") or last_prompt
                if t in ("user", "assistant"):
                    msg_count += 1
                    if rec_obj.get("gitBranch"):
                        git_branch = rec_obj["gitBranch"]
                ts = _parse_ts(rec_obj.get("timestamp"))
                if ts and (last_ts is None or ts > last_ts):
                    last_ts = ts
    except (IOError, OSError):
        pass
    return SessionCandidate(
        session_id=path.stem, path=path, ai_title=ai_title,
        last_prompt=last_prompt, last_ts=last_ts, git_branch=git_branch,
        msg_count=msg_count, truncated=not last_line_ok,
    )


def list_candidates(project_dir, exclude_session_id: Optional[str] = None) -> list[SessionCandidate]:
    """Rank prior sessions in a project dir, newest-by-content-timestamp first.
    Excludes only the given session id (the live one) — never time-based."""
    project_dir = Path(project_dir)
    out: list[SessionCandidate] = []
    for f in project_dir.glob("*.jsonl"):
        if not f.is_file() or not _t._is_uuid_format(f.stem):
            continue
        if exclude_session_id and f.stem == exclude_session_id:
            continue
        out.append(_scan_candidate(f))
    out.sort(key=lambda c: c.last_ts or _EPOCH, reverse=True)
    return out

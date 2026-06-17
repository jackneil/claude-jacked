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


@dataclass
class Digest:
    session_id: str
    ai_title: Optional[str] = None
    last_prompt: Optional[str] = None
    git_branch: Optional[str] = None
    recent_user_asks: list[str] = field(default_factory=list)
    last_assistant_text: Optional[str] = None
    todos: list[dict] = field(default_factory=list)
    recent_tool_actions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    agent_summaries: list[str] = field(default_factory=list)
    plan_excerpt: Optional[str] = None
    incomplete_last_turn: bool = False
    truncated_file: bool = False
    resume_cmd: str = ""


def resume_command(session_id: str) -> str:
    return f"claude --resume {session_id}"


def _action_label(name: str, tool_input: dict) -> str:
    if name == "Bash":
        lines = (tool_input.get("command") or "").strip().splitlines()
        first = lines[0] if lines else ""
        if len(first) > 80:
            first = first[:77] + "..."
        return f"Bash: {first}"
    fp = tool_input.get("file_path") or tool_input.get("notebook_path")
    if fp:
        return f"{name}: {fp}"
    return name


def _extract_actions(path: Path):
    """Raw pass: latest TodoWrite todos, trailing tool actions, files touched,
    and whether the final tool_use went unanswered (crashed mid-action)."""
    todos: list[dict] = []
    actions: list[str] = []
    files: list[str] = []
    seen_files: set[str] = set()
    open_ids: set[str] = set()
    last_tool_id: Optional[str] = None
    for rec_obj in _iter_records(path):
        if not isinstance(rec_obj, dict):
            continue
        t = rec_obj.get("type")
        content = (rec_obj.get("message") or {}).get("content")
        if t == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "?")
                tool_input = block.get("input") or {}
                tid = block.get("id")
                if tid:
                    open_ids.add(tid)
                    last_tool_id = tid
                if name == "TodoWrite" and isinstance(tool_input.get("todos"), list):
                    todos = tool_input["todos"]
                actions.append(_action_label(name, tool_input))
                fp = tool_input.get("file_path") or tool_input.get("notebook_path")
                if fp and fp not in seen_files:
                    seen_files.add(fp)
                    files.append(fp)
        elif t == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    open_ids.discard(block.get("tool_use_id"))
        elif t == "file-history-snapshot":
            backups = (rec_obj.get("snapshot") or {}).get("trackedFileBackups") or {}
            for fp in backups:
                if fp not in seen_files:
                    seen_files.add(fp)
                    files.append(fp)
    incomplete = last_tool_id is not None and last_tool_id in open_ids
    return todos, actions[-_MAX_TOOL_ACTIONS:], files[:_MAX_FILES], incomplete


def build_digest(session_path) -> Digest:
    session_path = Path(session_path)
    enriched = _t.parse_jsonl_file_enriched(session_path)
    cand = _scan_candidate(session_path)
    todos, actions, files, incomplete = _extract_actions(session_path)

    recent_user_asks = [m.content for m in enriched.user_messages if m.content][-_RECENT_USER_ASKS:]
    last_assistant_text = None
    for m in reversed(enriched.messages):
        if m.role == "assistant" and m.content:
            last_assistant_text = m.content
            break

    return Digest(
        session_id=enriched.session_id,
        ai_title=cand.ai_title,
        last_prompt=cand.last_prompt,
        git_branch=cand.git_branch,
        recent_user_asks=recent_user_asks,
        last_assistant_text=last_assistant_text,
        todos=todos,
        recent_tool_actions=actions,
        files_touched=files,
        agent_summaries=[a.summary_text for a in enriched.agent_summaries if a.summary_text],
        plan_excerpt=enriched.plan.content if enriched.plan else None,
        incomplete_last_turn=incomplete or cand.truncated,
        truncated_file=cand.truncated,
        resume_cmd=resume_command(enriched.session_id),
    )


def render_digest(digest: Digest, budget_chars: int = DEFAULT_BUDGET_CHARS) -> str:
    """Render the digest in priority order under a char budget. Never drops
    silently: clipped/omitted sections are named, with a pointer to resume."""
    sections: list[tuple[str, str]] = []
    head = [f"# Recovered session {digest.session_id}"]
    if digest.ai_title:
        head.append(f"**About:** {digest.ai_title}")
    if digest.git_branch:
        head.append(f"**Branch:** {digest.git_branch}")
    sections.append(("", "\n".join(head)))
    if digest.incomplete_last_turn:
        sections.append(("", "> WARNING: the last turn may be incomplete — work was in progress when the session ended. Verify before building on it."))
    if digest.last_prompt:
        sections.append(("Last instruction", digest.last_prompt))
    if digest.recent_user_asks:
        sections.append(("Recent requests", "\n".join(f"- {a}" for a in digest.recent_user_asks)))
    if digest.todos:
        marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
        sections.append(("Todo state", "\n".join(
            f"- {marks.get(td.get('status'), '[ ]')} {td.get('content', '')}" for td in digest.todos)))
    if digest.last_assistant_text:
        sections.append(("Last assistant message", digest.last_assistant_text))
    if digest.recent_tool_actions:
        sections.append(("Recent actions", "\n".join(f"- {a}" for a in digest.recent_tool_actions)))
    if digest.files_touched:
        sections.append(("Files touched", "\n".join(f"- {f}" for f in digest.files_touched)))
    if digest.plan_excerpt:
        sections.append(("Plan", digest.plan_excerpt))
    if digest.agent_summaries:
        sections.append(("Sub-agent findings", "\n\n".join(digest.agent_summaries)))

    out: list[str] = []
    used = 0
    dropped: list[str] = []
    for title, body in sections:
        block = f"## {title}\n{body}" if title else body
        remaining = budget_chars - used
        if remaining <= 0:
            dropped.append(title or "section")
            continue
        if len(block) > remaining:
            out.append(block[:remaining].rstrip() + "\n...[truncated to fit budget]")
            used = budget_chars
            dropped.append(title or "section")
            continue
        out.append(block)
        used += len(block)

    footer = [f"\nResume natively (preserves Claude's internal state): {digest.resume_cmd}"]
    if dropped:
        named = ", ".join(d for d in dropped if d) or "low-priority content"
        footer.append(f"[budget note] Output trimmed to ~{budget_chars} chars; clipped/omitted: {named}. Run the resume command for the full thread.")
    out.append("\n".join(footer))
    return "\n\n".join(out)

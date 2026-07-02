# jacked/recover.py
"""Crash-recovery for Claude Code sessions.

Find the most-recently-active prior session for a working directory from the
raw on-disk transcripts under ~/.claude/projects, and reconstruct a budgeted
working-state digest so a fresh session can pick up where a crashed one died.

Self-contained by design: imports only jacked.transcript + stdlib so /recover
works on a bare install (the moment right after a crash). Never import
jacked.retriever / jacked.searcher here.
"""
from __future__ import annotations

import json
import os
import re
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
_MAX_FAILED_ACTIONS = 5
_INFLIGHT_INTENT_CHARS = 400
_FAIL_TAIL_CHARS = 100
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Verbosity presets for the digest. 'standard' reproduces the historical fixed
# behaviour (budget 12000 + the original caps); 'brief'/'full' scale message
# count, tool-sample depth, files, and char budget together so a one-line
# recovery and a heavy multi-file recovery each get a fitting digest.
DEPTH_PROFILES = {
    "brief":    {"budget": 6000,  "asks": 2, "actions": 6,  "files": 10},
    "standard": {"budget": DEFAULT_BUDGET_CHARS, "asks": _RECENT_USER_ASKS,
                 "actions": _MAX_TOOL_ACTIONS, "files": _MAX_FILES},
    "full":     {"budget": 24000, "asks": 6, "actions": 24, "files": 40},
}

# /goal and /loop drive long-running, self-pacing work that Claude Code cannot
# auto-resume on --resume; we surface the verbatim kickoff so it can be re-run.
TAIL_WINDOW = 10
_GOAL_LOOP = ("goal", "loop")
_CN_RE = re.compile(r"<command-name>\s*/?([a-z0-9-]+)\s*</command-name>", re.IGNORECASE)
_CA_RE = re.compile(r"<command-args>(.*?)</command-args>", re.IGNORECASE | re.DOTALL)
_RAW_CMD_RE = re.compile(r"^/(goal|loop)\b", re.IGNORECASE)


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


MIN_SUBSTANCE_MSGS = 4


def recommend_index(candidates, min_msgs: int = MIN_SUBSTANCE_MSGS) -> int:
    """Index of the recommended candidate: the newest with real substance.
    Candidates are newest-first; skip a near-empty newest, else fall back to 0."""
    for i, c in enumerate(candidates):
        if c.msg_count >= min_msgs:
            return i
    return 0


@dataclass
class Digest:
    session_id: str
    ai_title: Optional[str] = None
    last_prompt: Optional[str] = None
    git_branch: Optional[str] = None
    recent_user_asks: list[str] = field(default_factory=list)
    last_assistant_text: Optional[str] = None
    in_flight_intent: Optional[str] = None
    todos: list[dict] = field(default_factory=list)
    recent_tool_actions: list[str] = field(default_factory=list)
    failed_actions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    agent_summaries: list[str] = field(default_factory=list)
    plan_excerpt: Optional[str] = None
    incomplete_last_turn: bool = False
    truncated_file: bool = False
    resume_cmd: str = ""
    resumable_commands: list[dict] = field(default_factory=list)


def resume_command(session_id: str) -> str:
    return f"claude --resume {session_id}"


def _result_text(block: dict, rec_obj: dict) -> str:
    """Best-effort text of a tool_result: the block's content first, then the
    transcript record's top-level toolUseResult (stderr preferred over stdout)."""
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        text = "\n".join(parts)
    else:
        text = ""
    if not text.strip():
        tur = rec_obj.get("toolUseResult")
        if isinstance(tur, str):
            text = tur
        elif isinstance(tur, dict):
            text = tur.get("stderr") or tur.get("stdout") or ""
    return text


def _result_outcome(block: dict, rec_obj: dict) -> dict:
    """Classify a tool_result as ok/failed and keep a short text tail.

    A crash's most load-bearing fact is whether the last action errored, so we
    read the result's is_error flag plus any nonzero exit / interrupted signal
    on the record's toolUseResult, and stash the tail of stderr/stdout."""
    is_error = bool(block.get("is_error"))
    tur = rec_obj.get("toolUseResult")
    if isinstance(tur, dict):
        if tur.get("is_error") or tur.get("interrupted"):
            is_error = True
        code = tur.get("exitCode", tur.get("returnCode"))
        if isinstance(code, int) and code != 0:
            is_error = True
    return {"is_error": is_error, "text": _result_text(block, rec_obj)}


def _fail_tail(text: str, limit: int = _FAIL_TAIL_CHARS) -> str:
    """One-line tail of failing output: the last non-blank line, capped."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-1]
    return tail[: limit - 3] + "..." if len(tail) > limit else tail


def _action_label(name: str, tool_input: dict, outcome: Optional[dict] = None) -> str:
    if name == "Bash":
        lines = (tool_input.get("command") or "").strip().splitlines()
        first = lines[0] if lines else ""
        if len(first) > 80:
            first = first[:77] + "..."
        label = f"Bash: {first}"
    else:
        fp = tool_input.get("file_path") or tool_input.get("notebook_path")
        label = f"{name}: {fp}" if fp else name
    if outcome and outcome.get("is_error"):
        tail = _fail_tail(outcome.get("text", ""))
        label += f" → FAILED: {tail}" if tail else " → FAILED"
    return label


def _extract_actions(path: Path, max_actions: int = _MAX_TOOL_ACTIONS,
                     max_files: int = _MAX_FILES,
                     max_failed: int = _MAX_FAILED_ACTIONS):
    """Raw pass: latest TodoWrite todos, trailing tool actions (each folded
    with its tool_result OUTCOME), files touched, whether the final tool_use
    went unanswered (crashed mid-action), and the recent FAILED actions.

    Knowing the last command/edit ERRORED is the single most load-bearing
    recovery fact, so we pair each tool_use with its tool_result by id and
    annotate failures (e.g. 'Bash: pytest -q → FAILED: 3 errors')."""
    todos: list[dict] = []
    raw_actions: list[dict] = []
    by_tid: dict[str, dict] = {}
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
                act = {"name": name, "input": tool_input, "outcome": None}
                raw_actions.append(act)
                if tid:
                    by_tid[tid] = act
                fp = tool_input.get("file_path") or tool_input.get("notebook_path")
                if fp and fp not in seen_files:
                    seen_files.add(fp)
                    files.append(fp)
        elif t == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tuid = block.get("tool_use_id")
                    open_ids.discard(tuid)
                    act = by_tid.get(tuid)
                    if act is not None:
                        act["outcome"] = _result_outcome(block, rec_obj)
        elif t == "file-history-snapshot":
            backups = (rec_obj.get("snapshot") or {}).get("trackedFileBackups") or {}
            for fp in backups:
                if fp not in seen_files:
                    seen_files.add(fp)
                    files.append(fp)
    incomplete = last_tool_id is not None and last_tool_id in open_ids
    labels = [_action_label(a["name"], a["input"], a["outcome"]) for a in raw_actions]
    failed = [_action_label(a["name"], a["input"], a["outcome"])
              for a in raw_actions if a["outcome"] and a["outcome"].get("is_error")]
    failed_recent = list(reversed(failed))[:max_failed]  # most recent first
    return todos, labels[-max_actions:], files[:max_files], incomplete, failed_recent


def _raw_user_text(rec_obj: dict) -> str:
    content = (rec_obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return ""


def _parse_goal_loop(raw: str):
    """Return (type, verbatim_kickoff) if raw invokes /goal or /loop, else (None, None)."""
    m = _CN_RE.search(raw)
    if m and m.group(1).lower() in _GOAL_LOOP:
        name = m.group(1).lower()
        args_m = _CA_RE.search(raw)
        args = args_m.group(1).strip() if args_m else ""
        return name, (f"/{name} {args}".rstrip())
    stripped = raw.strip()
    rm = _RAW_CMD_RE.match(stripped)
    if rm:
        name = rm.group(1).lower()
        return name, stripped.splitlines()[0].strip()
    return None, None


def _extract_kickoffs(path):
    """Return (kickoffs, total_records). Each kickoff: (index, type, verbatim)."""
    kickoffs = []
    total = 0
    for i, rec_obj in enumerate(_iter_records(path)):
        total = i + 1
        if not isinstance(rec_obj, dict) or rec_obj.get("type") != "user":
            continue
        raw = _raw_user_text(rec_obj)
        if not raw:
            continue
        kind, kickoff = _parse_goal_loop(raw)
        if kind:
            kickoffs.append((i, kind, kickoff))
    return kickoffs, total


def _active_kickoffs(path, tail_window: int = TAIL_WINDOW) -> list[dict]:
    """Surface the latest /goal|/loop kickoff only if it falls in the transcript tail."""
    kickoffs, total = _extract_kickoffs(path)
    if not kickoffs:
        return []
    last_idx, kind, kickoff = kickoffs[-1]
    if last_idx >= total - tail_window:
        return [{"type": kind, "kickoff": kickoff}]
    return []


def _last_thinking(path: Path) -> Optional[str]:
    """Last assistant THINKING block — the in-flight intent that explains why
    the next step was next (exactly the context a fresh session lacks). The
    enriched parser strips thinking, so do a dedicated raw pass. Falls back to
    the last assistant text block when no thinking block is present."""
    last_thought: Optional[str] = None
    last_text: Optional[str] = None
    for rec_obj in _iter_records(path):
        if not isinstance(rec_obj, dict) or rec_obj.get("type") != "assistant":
            continue
        content = (rec_obj.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "thinking":
                txt = block.get("thinking") or block.get("text")
                if txt and txt.strip():
                    last_thought = txt.strip()
            elif bt == "text":
                txt = block.get("text")
                if txt and txt.strip():
                    last_text = txt.strip()
    return last_thought or last_text


def build_digest(session_path, depth: str = "standard") -> Digest:
    session_path = Path(session_path)
    prof = DEPTH_PROFILES.get(depth, DEPTH_PROFILES["standard"])
    enriched = _t.parse_jsonl_file_enriched(session_path)
    cand = _scan_candidate(session_path)
    todos, actions, files, incomplete, failed = _extract_actions(
        session_path, max_actions=prof["actions"], max_files=prof["files"])
    resumable = _active_kickoffs(session_path)

    recent_user_asks = [m.content for m in enriched.user_messages if m.content][-prof["asks"]:]
    last_assistant_text = None
    for m in reversed(enriched.messages):
        if m.role == "assistant" and m.content:
            last_assistant_text = m.content
            break
    intent = _last_thinking(session_path)
    if intent and len(intent) > _INFLIGHT_INTENT_CHARS:
        intent = intent[:_INFLIGHT_INTENT_CHARS].rstrip() + "…"

    return Digest(
        session_id=enriched.session_id,
        ai_title=cand.ai_title,
        last_prompt=cand.last_prompt,
        git_branch=cand.git_branch,
        recent_user_asks=recent_user_asks,
        last_assistant_text=last_assistant_text,
        in_flight_intent=intent,
        todos=todos,
        recent_tool_actions=actions,
        failed_actions=failed,
        files_touched=files,
        agent_summaries=[a.summary_text for a in enriched.agent_summaries if a.summary_text],
        plan_excerpt=enriched.plan.content if enriched.plan else None,
        incomplete_last_turn=incomplete or cand.truncated,
        truncated_file=cand.truncated,
        resume_cmd=resume_command(enriched.session_id),
        resumable_commands=resumable,
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
    # In-flight intent: the crashed agent's final reasoning (why the next step
    # was next). Skip when it merely duplicates the last assistant message
    # (the text-fallback case) — compare with any trailing cap-ellipsis removed.
    if digest.in_flight_intent:
        _probe = digest.in_flight_intent.rstrip("…").rstrip()
        if _probe and _probe not in (digest.last_assistant_text or ""):
            sections.append(("In-flight intent", digest.in_flight_intent))
    # What actually broke is the top fact to re-anchor on after a crash.
    if digest.failed_actions:
        sections.append(("Failed actions (most recent first)",
                         "\n".join(f"- {a}" for a in digest.failed_actions)))
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
        footer.append(f"[budget note] Section bodies trimmed to ~{budget_chars} chars; clipped/omitted: {named}. Run the resume command for the full thread.")
    out.append("\n".join(footer))

    # A still-active /goal|/loop can't be auto-resumed by --resume. Surface the
    # verbatim kickoff as an always-on block right under the header — it is tiny
    # and load-bearing, so it bypasses the char budget and is never trimmed.
    if digest.resumable_commands:
        rc = digest.resumable_commands[0]
        restart = (
            "## ⚠ Manual restart required\n"
            f"This session was driving a /{rc['type']} that can't be auto-resumed.\n"
            "Copy this back into Claude Code to restart it:\n\n"
            f"    {rc['kickoff']}"
        )
        out.insert(min(1, len(out)), restart)
    return "\n\n".join(out)

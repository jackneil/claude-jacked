# /recover — Crash-Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a manually-invoked `/recover` skill (backed by a `jacked recover` CLI command) that rebuilds the most-recently-active prior Claude Code session for the current folder from its raw on-disk transcript, injects a budgeted working-state digest into the live session, and offers native `claude --resume <id>`.

**Architecture:** A Qdrant-free Python module (`jacked/recover.py`) does the work — resolve the project dir for the cwd (by matching the recorded `cwd` field, since the stored slug is lossy), rank candidate sessions, and build/render a budgeted digest. A Click subcommand (`jacked recover`) exposes two phases: `--json` (rank candidates) and `--session <id> --digest` (the injection payload). A thin `SKILL.md` orchestrates: find → confirm → inject → re-anchor.

**Tech Stack:** Python 3.10+, Click (CLI), Rich (console), pytest (run via `uv run python -m pytest`). Reuses `jacked/transcript.py` (stdlib-only JSONL parser). Skill is Markdown.

## Global Constraints

- **Qdrant-free.** `jacked/recover.py` and the `recover` CLI command import **only** `jacked.transcript` + stdlib. Never import `jacked.retriever` / `jacked.searcher`. The `recover` command must **not** call `_require_search`. It must work on a bare `uv tool install claude-jacked` (no `[search]` extra).
- **Run tests with** `uv run python -m pytest` — never bare `python -m pytest` (system Python lacks deps).
- **Live-session exclusion is by session id**, primarily `$CLAUDE_CODE_SESSION_ID` passed at the shell by the skill; CLI falls back to env `CLAUDE_CODE_SESSION_ID` then `CLAUDE_SESSION_ID`. Do **not** add a time-based "recent ⇒ live" filter (a crash-then-reopen-fast would be wrongly skipped).
- **No silent data drops** (project data-integrity rule). The digest budget is generous and, whenever it trims, the rendered output names what was clipped and points to `claude --resume <id>` for the full thread.
- **Path resolution never trusts the slug alone.** `config.get_session_dir_for_repo()` is lossy (strips the leading `/`, keeps dots). Resolve by matching the recorded `cwd` field inside transcripts.
- **Naming:** skill dir name == frontmatter `name` == `recover`, kebab-case. CLI command `recover` registered via `@main.command()`.
- **Skill is LLM guidance, not an algorithm.** No scoring tables/regex inside `SKILL.md`; it drives the CLI and applies judgment (per the jacked-skills-are-instructions convention).

---

## File Structure

| File | Responsibility |
|---|---|
| `jacked/recover.py` | **New.** All recovery logic: `resolve_project_dir`, `list_candidates` (+`SessionCandidate`), `build_digest`/`render_digest` (+`Digest`), `resume_command`, private helpers. Qdrant-free. |
| `jacked/cli.py` | **Modify.** Add the `recover` Click subcommand (phase-1 `--json`, phase-2 `--session --digest`). Not `_require_search`-gated. |
| `jacked/data/skills/recover/SKILL.md` | **New.** Thin orchestrator skill. Auto-installs via the existing `jacked install` skills glob. |
| `tests/test_recover.py` | **New.** Unit tests over `jacked/recover.py` + a `CliRunner` test of the command, using JSONL fixtures under `tmp_path`. |
| `jacked/__init__.py` | **Modify.** Version bump. |
| `README.md` | **Modify.** One-paragraph mention of `/recover`. |

---

## Task 1: `recover.py` — project-dir resolution

**Files:**
- Create: `jacked/recover.py`
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `jacked.transcript._is_uuid_format` (later tasks); stdlib only here.
- Produces: `resolve_project_dir(cwd, projects_root=None) -> Optional[Path]`; helpers `_norm_path`, `_encode_cwd`, `_iter_records`, `_parse_ts`, `_read_cwd`; module constants `DEFAULT_PROJECTS_ROOT`, `DEFAULT_BUDGET_CHARS`, `_EPOCH`.

- [ ] **Step 1.1: Write the failing test**

```python
# tests/test_recover.py
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jacked import recover as rec


def _write_session(project_dir: Path, session_id: str, records: list[dict]) -> Path:
    """Write a JSONL transcript fixture; return its path."""
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for rec_obj in records:
            f.write(json.dumps(rec_obj) + "\n")
    return path


def _user_line(cwd: str, ts: str = "2026-06-15T10:00:00.000Z", branch: str = "master") -> dict:
    return {"type": "user", "cwd": cwd, "gitBranch": branch, "timestamp": ts,
            "message": {"role": "user", "content": "hello"}}


SID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def test_resolve_matches_by_cwd_field_despite_lossy_slug(tmp_path):
    projects = tmp_path / "projects"
    cwd = "/Users/jack.neil/Github/claude-jacked"  # dot in username defeats naive slug
    # Folder name uses the CURRENT encoding (dots -> dash, leading dash kept):
    pdir = projects / "-Users-jack-neil-Github-claude-jacked"
    _write_session(pdir, SID_A, [_user_line(cwd)])
    assert rec.resolve_project_dir(cwd, projects_root=projects) == pdir


def test_resolve_returns_none_when_no_match(tmp_path):
    projects = tmp_path / "projects"
    pdir = projects / "-some-other-repo"
    _write_session(pdir, SID_A, [_user_line("/some/other/repo")])
    assert rec.resolve_project_dir("/Users/jack.neil/Github/claude-jacked",
                                   projects_root=projects) is None


def test_resolve_returns_none_when_root_missing(tmp_path):
    assert rec.resolve_project_dir("/whatever", projects_root=tmp_path / "nope") is None
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jacked.recover'` (or `AttributeError`).

- [ ] **Step 1.3: Write minimal implementation**

```python
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
```

- [ ] **Step 1.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: PASS (3 passed).

- [ ] **Step 1.5: Commit**

```bash
git add jacked/recover.py tests/test_recover.py
git commit -m "feat(recover): cwd-matched project-dir resolution (Qdrant-free)"
```

---

## Task 2: `recover.py` — candidate ranking

**Files:**
- Modify: `jacked/recover.py`
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `resolve_project_dir`, `_parse_ts`, `_EPOCH` (Task 1); `jacked.transcript._is_uuid_format`.
- Produces: `SessionCandidate` dataclass (with `.to_dict(now=None)`); `list_candidates(project_dir, exclude_session_id=None) -> list[SessionCandidate]`; `_scan_candidate(path) -> SessionCandidate`; `_relative_age(ts, now=None) -> Optional[str]`.

- [ ] **Step 2.1: Write the failing test**

```python
# append to tests/test_recover.py
SID_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
SID_LIVE = "cccccccc-3333-4333-8333-cccccccccccc"


def _meta(type_: str, **kw) -> dict:
    d = {"type": type_}
    d.update(kw)
    return d


def test_list_candidates_ranks_by_last_timestamp_and_excludes_live(tmp_path):
    pdir = tmp_path / "p"
    cwd = "/repo"
    # older session
    _write_session(pdir, SID_A, [
        _user_line(cwd, ts="2026-06-10T10:00:00.000Z"),
        _meta("ai-title", aiTitle="Old work"),
        _meta("last-prompt", lastPrompt="do the old thing"),
    ])
    # newer session
    _write_session(pdir, SID_B, [
        _user_line(cwd, ts="2026-06-16T09:50:00.000Z"),
        _meta("ai-title", aiTitle="Recent work"),
        _meta("last-prompt", lastPrompt="do the recent thing"),
    ])
    # the live session (newest) — must be excluded by id
    _write_session(pdir, SID_LIVE, [_user_line(cwd, ts="2026-06-17T12:00:00.000Z")])

    cands = rec.list_candidates(pdir, exclude_session_id=SID_LIVE)
    assert [c.session_id for c in cands] == [SID_B, SID_A]
    assert cands[0].ai_title == "Recent work"
    assert cands[0].last_prompt == "do the recent thing"
    assert cands[0].git_branch == "master"


def test_scan_candidate_flags_truncated_final_line(tmp_path):
    pdir = tmp_path / "p"
    pdir.mkdir(parents=True)
    path = pdir / f"{SID_A}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(_user_line("/repo")) + "\n")
        f.write('{"type":"assistant","message":{"role":"assi')  # crash-truncated
    cand = rec._scan_candidate(path)
    assert cand.truncated is True


def test_list_candidates_ignores_non_uuid_files(tmp_path):
    pdir = tmp_path / "p"
    _write_session(pdir, SID_A, [_user_line("/repo")])
    (pdir / "notes.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
    cands = rec.list_candidates(pdir)
    assert [c.session_id for c in cands] == [SID_A]
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: FAIL — `AttributeError: module 'jacked.recover' has no attribute 'list_candidates'`.

- [ ] **Step 2.3: Write minimal implementation**

Append to `jacked/recover.py`:

```python
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
```

- [ ] **Step 2.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: PASS (6 passed).

- [ ] **Step 2.5: Commit**

```bash
git add jacked/recover.py tests/test_recover.py
git commit -m "feat(recover): rank candidate sessions, exclude live by id, flag truncation"
```

---

## Task 3: `recover.py` — digest build + budgeted render

**Files:**
- Modify: `jacked/recover.py`
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `_scan_candidate`, `_iter_records` (Tasks 1-2); `jacked.transcript.parse_jsonl_file_enriched`.
- Produces: `Digest` dataclass; `build_digest(session_path) -> Digest`; `render_digest(digest, budget_chars=DEFAULT_BUDGET_CHARS) -> str`; `resume_command(session_id) -> str`; helpers `_extract_actions`, `_action_label`.

- [ ] **Step 3.1: Write the failing test**

```python
# append to tests/test_recover.py
def _assistant_tool_use(name: str, tool_input: dict, tool_id: str, ts: str) -> dict:
    return {"type": "assistant", "timestamp": ts, "gitBranch": "master",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def _assistant_text(text: str, ts: str) -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def test_resume_command_string():
    assert rec.resume_command("xyz") == "claude --resume xyz"


def test_build_digest_extracts_todos_actions_files_and_branch(tmp_path):
    pdir = tmp_path / "p"
    path = _write_session(pdir, SID_A, [
        _user_line("/repo", ts="2026-06-16T09:00:00.000Z"),
        {"type": "user", "timestamp": "2026-06-16T09:01:00.000Z",
         "message": {"role": "user", "content": "implement the parser"}},
        _assistant_tool_use("TodoWrite",
                            {"todos": [{"content": "write parser", "status": "in_progress"},
                                       {"content": "write tests", "status": "pending"}]},
                            "t1", "2026-06-16T09:02:00.000Z"),
        _assistant_tool_use("Edit", {"file_path": "/repo/parser.py"}, "t2",
                            "2026-06-16T09:03:00.000Z"),
        _assistant_tool_use("Bash", {"command": "uv run python -m pytest"}, "t3",
                            "2026-06-16T09:04:00.000Z"),
        {"type": "user", "timestamp": "2026-06-16T09:04:30.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t3", "content": "ok"}]}},
        _meta("ai-title", aiTitle="Parser work"),
        _meta("last-prompt", lastPrompt="implement the parser"),
    ])
    d = rec.build_digest(path)
    assert d.ai_title == "Parser work"
    assert d.last_prompt == "implement the parser"
    assert d.git_branch == "master"
    assert [td["content"] for td in d.todos] == ["write parser", "write tests"]
    assert "/repo/parser.py" in d.files_touched
    assert any(a.startswith("Bash: uv run python -m pytest") for a in d.recent_tool_actions)
    # t1/t2 had no tool_result, but the final content-bearing action (t3) did,
    # and we only flag when the LAST tool_use is unmatched:
    assert d.resume_cmd == f"claude --resume {SID_A}"


def test_build_digest_flags_incomplete_last_turn(tmp_path):
    pdir = tmp_path / "p"
    path = _write_session(pdir, SID_B, [
        _user_line("/repo", ts="2026-06-16T09:00:00.000Z"),
        _assistant_tool_use("Bash", {"command": "sleep 1"}, "open1",
                            "2026-06-16T09:05:00.000Z"),  # no matching tool_result -> crashed mid-action
    ])
    d = rec.build_digest(path)
    assert d.incomplete_last_turn is True


def test_render_digest_budget_notes_when_trimmed(tmp_path):
    pdir = tmp_path / "p"
    big = "X" * 5000
    path = _write_session(pdir, SID_A, [
        _user_line("/repo"),
        _assistant_text(big, "2026-06-16T09:10:00.000Z"),
        _meta("last-prompt", lastPrompt="keep going"),
    ])
    d = rec.build_digest(path)
    rendered = rec.render_digest(d, budget_chars=500)
    assert "truncated to fit budget" in rendered or "budget note" in rendered
    assert "claude --resume" in rendered
    assert len(rendered) < 2000  # budget respected (plus small footer)
```

- [ ] **Step 3.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: FAIL — `AttributeError: module 'jacked.recover' has no attribute 'build_digest'`.

- [ ] **Step 3.3: Write minimal implementation**

Append to `jacked/recover.py`:

```python
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
```

- [ ] **Step 3.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: PASS (10 passed).

- [ ] **Step 3.5: Commit**

```bash
git add jacked/recover.py tests/test_recover.py
git commit -m "feat(recover): build + budget-render working-state digest"
```

---

## Task 4: `jacked recover` CLI subcommand

**Files:**
- Modify: `jacked/cli.py` (add a new `@main.command()` near the other commands, e.g. after `retrieve`)
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: all of `jacked.recover` (Tasks 1-3); Click group object `main`, module `console`, `sys`, `os`.
- Produces: CLI command `jacked recover` with options `--cwd`, `--exclude`, `--session`, `--digest`, `--limit/-n`, `--budget`, `--json`. Phase-1 (`--json`) emits `{"project_dir","chosen","candidates","count"}`; phase-2 (`--session ID --digest`) emits the rendered digest.

- [ ] **Step 4.1: Write the failing test**

```python
# append to tests/test_recover.py
import json as _json
from click.testing import CliRunner
from jacked.cli import main


def test_cli_recover_json_lists_candidates(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    cwd = "/work/myrepo"
    pdir = projects / "-work-myrepo"
    _write_session(pdir, SID_A, [
        _user_line(cwd, ts="2026-06-16T09:50:00.000Z"),
        _meta("ai-title", aiTitle="My recent work"),
        _meta("last-prompt", lastPrompt="finish the thing"),
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    runner = CliRunner()
    result = runner.invoke(main, ["recover", "--cwd", cwd, "--json"])
    assert result.exit_code == 0, result.output
    payload = _json.loads(result.output.strip())
    assert payload["count"] == 1
    assert payload["chosen"]["session_id"] == SID_A
    assert payload["chosen"]["ai_title"] == "My recent work"


def test_cli_recover_excludes_live_session(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    cwd = "/work/myrepo"
    pdir = projects / "-work-myrepo"
    _write_session(pdir, SID_A, [_user_line(cwd, ts="2026-06-16T09:00:00.000Z")])
    _write_session(pdir, SID_LIVE, [_user_line(cwd, ts="2026-06-17T12:00:00.000Z")])
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    runner = CliRunner()
    result = runner.invoke(main, ["recover", "--cwd", cwd, "--exclude", SID_LIVE, "--json"])
    payload = _json.loads(result.output.strip())
    assert [c["session_id"] for c in payload["candidates"]] == [SID_A]


def test_cli_recover_digest_for_session(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    cwd = "/work/myrepo"
    pdir = projects / "-work-myrepo"
    _write_session(pdir, SID_A, [
        _user_line(cwd),
        {"type": "user", "timestamp": "2026-06-16T09:01:00.000Z",
         "message": {"role": "user", "content": "build it"}},
        _meta("last-prompt", lastPrompt="build it"),
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    runner = CliRunner()
    result = runner.invoke(main, ["recover", "--cwd", cwd, "--session", SID_A, "--digest"])
    assert result.exit_code == 0, result.output
    assert "Recovered session" in result.output
    assert f"claude --resume {SID_A}" in result.output


def test_cli_recover_no_sessions_json(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    runner = CliRunner()
    result = runner.invoke(main, ["recover", "--cwd", "/work/nope", "--json"])
    payload = _json.loads(result.output.strip())
    assert payload == {"project_dir": None, "chosen": None, "candidates": [], "count": 0}
```

- [ ] **Step 4.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: FAIL — `recover` is not a registered command (`result.exit_code != 0`, usage error).

- [ ] **Step 4.3: Write minimal implementation**

Add this command to `jacked/cli.py` (place it after the `retrieve` command, before `sessions`). Note: it does **not** call `_require_search`.

```python
@main.command()
@click.option("--cwd", default=None, help="Working directory to recover (default: current dir)")
@click.option("--exclude", default=None, help="Session id to exclude (the live one)")
@click.option("--session", "session_id", default=None, help="Recover this specific session id")
@click.option("--digest", "as_digest", is_flag=True, help="Emit the working-state digest for --session")
@click.option("--limit", "-n", default=3, help="How many candidates to list")
@click.option("--budget", default=12000, help="Digest size budget in characters")
@click.option("--json", "as_json", is_flag=True, help="Emit candidates as JSON")
def recover(cwd, exclude, session_id, as_digest, limit, budget, as_json):
    """Recover a crashed session for this folder from its on-disk transcript.

    Works on a bare install — no Qdrant/search extra required.
    Phase 1: 'jacked recover --json' ranks candidate sessions.
    Phase 2: 'jacked recover --session <id> --digest' prints the injection digest.
    """
    import json as _json
    from datetime import datetime, timezone
    from jacked import recover as rec

    target_cwd = cwd or os.getcwd()
    project_dir = rec.resolve_project_dir(target_cwd)

    if project_dir is None:
        if as_json:
            click.echo(_json.dumps({"project_dir": None, "chosen": None, "candidates": [], "count": 0}))
        else:
            console.print(f"[yellow]No recorded Claude sessions found for[/yellow] {target_cwd}")
        return

    # Phase 2 — digest for a specific session
    if session_id and as_digest:
        session_path = project_dir / f"{session_id}.jsonl"
        if not session_path.exists():
            console.print(f"[red]Session {session_id} not found in {project_dir}[/red]")
            sys.exit(1)
        digest = rec.build_digest(session_path)
        click.echo(rec.render_digest(digest, budget_chars=budget))
        return

    # Phase 1 — rank candidates
    exclude_id = exclude or os.getenv("CLAUDE_CODE_SESSION_ID") or os.getenv("CLAUDE_SESSION_ID")
    candidates = rec.list_candidates(project_dir, exclude_session_id=exclude_id)
    now = datetime.now(timezone.utc)
    top = candidates[:limit]

    if as_json:
        payload = {
            "project_dir": str(project_dir),
            "chosen": top[0].to_dict(now) if top else None,
            "candidates": [c.to_dict(now) for c in top],
            "count": len(candidates),
        }
        click.echo(_json.dumps(payload))
        return

    if not top:
        console.print(f"[yellow]No prior session to recover in[/yellow] {project_dir}")
        return
    for i, c in enumerate(top):
        marker = "->" if i == 0 else "  "
        click.echo(f"{marker} {c.session_id}  ({c.ai_title or 'untitled'})  "
                   f"{rec._relative_age(c.last_ts, now)}  [{c.git_branch or '?'}]")
        if c.last_prompt:
            click.echo(f"     last: {c.last_prompt[:120]}")
```

- [ ] **Step 4.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: PASS (14 passed).

- [ ] **Step 4.5: Verify the command is wired and Qdrant-free**

Run: `uv run jacked recover --help`
Expected: help text for `recover` with the options above (no error, no `[search]` nag).

Run: `grep -n "retriever\|searcher\|qdrant\|_require_search" jacked/recover.py`
Expected: no matches (empty output).

- [ ] **Step 4.6: Commit**

```bash
git add jacked/cli.py tests/test_recover.py
git commit -m "feat(recover): add 'jacked recover' CLI (candidates + digest phases)"
```

---

## Task 5: `/recover` SKILL.md (RED baseline → write → GREEN)

**REQUIRED BACKGROUND:** This is a technique skill. Per superpowers:writing-skills, you must watch a fresh agent fail the task WITHOUT the skill before writing it.

**Files:**
- Create: `jacked/data/skills/recover/SKILL.md`

**Interfaces:**
- Consumes: the `jacked recover` CLI (Task 4).
- Produces: a manually-invoked `/recover` skill installed to `~/.claude/skills/recover/SKILL.md`.

- [ ] **Step 5.1: RED — run the baseline WITHOUT the skill**

Dispatch a fresh subagent (general-purpose) with this scenario and **record verbatim** what it does and where it flails:

> "A Claude Code session I was running in this repo (`<repo path>`) crashed before I could save anything, and I don't know its session ID. Get me back to where I was so I can continue."

Document the failure modes (expected, from baseline): doesn't know transcripts live in `~/.claude/projects/<slug>/*.jsonl`; can't handle the lossy dot-slug; doesn't know to exclude the live session via `$CLAUDE_CODE_SESSION_ID`; may reach for `/jacked`/Qdrant; may dump the whole raw transcript (context blowup); doesn't offer `claude --resume`. These gaps are what the skill must close.

- [ ] **Step 5.2: GREEN — write the skill**

Create `jacked/data/skills/recover/SKILL.md`:

```markdown
---
name: recover
description: Use when a Claude Code session in this folder crashed mid-work (computer died, terminal closed, Claude broke) and you reopened Claude without a checkpoint and don't know the session ID — rebuild the last session for THIS folder from its on-disk transcript and continue. Triggers include "recover my session", "my session crashed", "it crashed before I could save", "get me back to where I was", "restore the crashed session", "resume the session that died". NOT for transient API/rate-limit errors mid-turn (use retry), NOT for deliberately saved state (use /checkpoint resume), NOT for finding an old session by topic across machines (use /jacked).
---

# Recover a crashed session

Rebuild the most-recently-active prior Claude Code session **for the current folder** from its raw on-disk transcript, inject a budgeted working-state digest into THIS session, and offer native `claude --resume` for a full thread continuation.

## When to use
- A session crashed or was killed mid-task, you reopened Claude here, no `/checkpoint` was saved, and you don't know the session ID.
- You want to keep working in the session you already have open (digest injection), or get the original thread back natively.

**Not this skill:** transient API/rate-limit blip mid-turn -> `retry`. Deliberately saved checkpoint -> `/checkpoint resume`. Topic search across all past sessions/machines -> `/jacked`.

## Requirements
Needs the `jacked` CLI on PATH. A bare install is enough — recovery never needs the Qdrant/`[search]` extra. If `jacked` is not found, tell the user to install or repair jacked instead of half-recovering by hand.

## Steps

1. **Find candidates.** Run:
   ```bash
   jacked recover --json --exclude "$CLAUDE_CODE_SESSION_ID"
   ```
   Passing `$CLAUDE_CODE_SESSION_ID` at the shell excludes the session you are running in (it would otherwise rank newest).

2. **No candidates** (`count` is 0 or `chosen` is null) -> tell the user no recoverable session was found for this folder (fresh repo, wrong folder, or nothing crashed here) and stop. Do not invent state.

3. **Sanity-check the auto-pick.** If `chosen.last_prompt` is itself a `/recover` invocation, that is the live session leaking through — drop it and use the next candidate (or re-run with that id added to `--exclude`).

4. **Present and confirm — before injecting.** Show the chosen session and the alternates: `ai_title`, `age`, `git_branch`, and `last_prompt`. Ask: "Recover this one, or pick an alternate (<ids>)?" Wait for the user. Do not inject until they confirm.

5. **Inject the digest.** On confirmation:
   ```bash
   jacked recover --session <id> --digest
   ```
   The output IS the recovered working state — read it. It ends with a `claude --resume <id>` line and, if it was trimmed to fit, a budget note.

6. **Offer native resume.** Tell the user: "For a true continuation that preserves Claude's internal state, run `claude --resume <id>` in a fresh terminal. The digest above lets us continue right here instead."

7. **Re-anchor and continue.** Summarize in 1-2 lines: "You were working on X; last step was Y; next was Z." `MEMORY.md` already carries standing project conventions. Then continue the work.

## Wrong pick
If the user says it is the wrong session, re-run step 5 with the alternate's id from the candidate list.

## Incomplete last turn
When the digest flags "the last turn may be incomplete," treat that work as in-progress, not finished — verify it before building on it.
```

- [ ] **Step 5.3: GREEN — verify with the skill**

Re-run the Step 5.1 scenario with a subagent that has the skill available (or walk the skill yourself against this repo). Confirm it: runs `jacked recover --json --exclude "$CLAUDE_CODE_SESSION_ID"`, presents the pick, confirms before injecting, runs the `--digest` phase, and offers `claude --resume`. Capture the transcript as evidence.

- [ ] **Step 5.4: REFACTOR — close any new gaps**

If the GREEN run revealed a loophole (e.g., the agent injected before confirming, or dumped raw transcript instead of using `--digest`), tighten the corresponding step wording and re-run 5.3 until clean.

- [ ] **Step 5.5: Commit**

```bash
git add jacked/data/skills/recover/SKILL.md
git commit -m "feat(recover): add /recover skill (find -> confirm -> inject -> re-anchor)"
```

---

## Task 6: Version bump, README, and end-to-end verification

**Files:**
- Modify: `jacked/__init__.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: everything above.
- Produces: a shippable, installed, self-verified `/recover`.

- [ ] **Step 6.1: Bump the version**

In `jacked/__init__.py`, bump `__version__` (patch bump from the current value, e.g. `0.49.1` -> `0.50.0` for a new feature — confirm the current value first with `grep __version__ jacked/__init__.py`).

```python
__version__ = "0.50.0"
```

- [ ] **Step 6.2: Document in README**

Add a short entry under the skills/commands section of `README.md`:

```markdown
### `/recover` — rebuild a crashed session

Opened Claude in a folder whose last session crashed mid-work and you don't know the
session ID? Run `/recover`. It finds the most-recently-active prior session for the
current folder from its on-disk transcript, shows you the pick to confirm, then injects
a budgeted working-state digest (last instruction, todos, recent actions, files touched)
so you continue right where it died — and prints `claude --resume <id>` for a full native
continuation. Works on a bare install; no Qdrant/search extra required.
```

- [ ] **Step 6.3: Run the full test suite**

Run: `uv run python -m pytest tests/test_recover.py -v`
Expected: all tests pass.

Run: `uv run python -m pytest -q`
Expected: no regressions in the broader suite.

- [ ] **Step 6.4: End-to-end smoke test against real data**

This repo's own transcripts are real recovery fodder. Run from the repo root:

```bash
uv run jacked recover --json --exclude "$CLAUDE_CODE_SESSION_ID" | python -m json.tool
```
Expected: a JSON object with a non-null `chosen` (a prior session in this repo) and a `candidates` list — and the current session is absent.

Then exercise phase 2 with one of the returned ids:

```bash
uv run jacked recover --session <id-from-above> --digest
```
Expected: a readable digest ending in `claude --resume <id>`, comfortably under the budget.

- [ ] **Step 6.5: Verify install wiring**

Confirm the skill is picked up by the installer glob (`jacked install` globs `jacked/data/skills/*/SKILL.md`):

```bash
ls jacked/data/skills/recover/SKILL.md
grep -n "skills" jacked/cli.py | grep -i "glob\|SKILL"
```
Expected: the file exists and the install glob covers `*/SKILL.md` (no per-skill registration needed).

- [ ] **Step 6.6: Commit**

```bash
git add jacked/__init__.py README.md
git commit -m "chore(recover): bump version + document /recover"
```

---

## Self-Review

**Spec coverage** (against `2026-06-17-recover-crashed-session-design.html`):
- `recover.py` Qdrant-free, reuses `transcript.py` — Task 1-3, enforced by Global Constraints + Step 4.5 grep. ✔
- `resolve_project_dir` defeats the dot-slug via cwd-field matching — Task 1. ✔
- `list_candidates` excludes the live session, ranks by last timestamp, tolerates truncation — Task 2. ✔
- `build_digest`/`render_digest`: ai-title, last-prompt, recent asks, last assistant text, TodoWrite, tool actions, files, agent summaries, plan, incomplete-turn flag, budget-with-visible-truncation — Task 3. ✔
- Two-phase CLI (`--json`, `--session --digest`), not `_require_search`-gated, `--exclude` + env fallback — Task 4. ✔
- Thin skill: find -> confirm-before-inject -> inject -> offer `claude --resume` -> re-anchor; trigger-tight description that doesn't poach retry/checkpoint/jacked — Task 5, built RED-first. ✔
- Version bump + README — Task 6. ✔

**Refinements over the spec (intentional, source-driven):**
- Live-exclusion is id-only (no time-based heuristic) — the spec's "within ~60s" belt-and-suspenders would wrongly skip a crash-then-reopen-fast session; replaced by the skill-layer content check in Step 3 (Task 5).
- Digest split into `build_digest` (pure extraction) + `render_digest` (budget) for testability.
- `recover.py` does its own metadata pass for `ai-title`/`last-prompt`/`gitBranch`/TodoWrite (transcript.py only parses user/assistant/summary), and relies on `parse_jsonl_file`'s existing skip-bad-line behavior for crash-truncated tails (no parser hardening needed — Step 2 test proves it).

**Placeholder scan:** none — every code/test step contains complete content.

**Type consistency:** `SessionCandidate`/`Digest` field names and `resolve_project_dir`/`list_candidates`/`build_digest`/`render_digest`/`resume_command` signatures are identical across the module, tests, and CLI command.

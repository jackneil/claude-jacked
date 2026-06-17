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

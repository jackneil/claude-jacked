# tests/test_recover_upgrade.py
"""Field-researched recover upgrades: tool OUTCOMES (failed actions),
in-flight intent (last thinking block), and the --depth verbosity lever."""
import json
from pathlib import Path

from click.testing import CliRunner

from jacked import recover as rec
from jacked.cli import main

SID = "dddddddd-4444-4444-8444-dddddddddddd"


def _write_session(project_dir: Path, session_id: str, records: list[dict]) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for rec_obj in records:
            f.write(json.dumps(rec_obj) + "\n")
    return path


def _user_line(cwd: str = "/repo", ts: str = "2026-06-20T10:00:00.000Z") -> dict:
    return {"type": "user", "cwd": cwd, "gitBranch": "master", "timestamp": ts,
            "message": {"role": "user", "content": "hello"}}


def _tool_use(name: str, tool_input: dict, tid: str, ts: str) -> dict:
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}}


def _tool_result(tid: str, ts: str, content="ok", is_error=None, tool_use_result=None) -> dict:
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    rec_obj = {"type": "user", "timestamp": ts,
               "message": {"role": "user", "content": [block]}}
    if tool_use_result is not None:
        rec_obj["toolUseResult"] = tool_use_result
    return rec_obj


def _thinking(text: str, ts: str, also_text: str = None) -> dict:
    content = [{"type": "thinking", "thinking": text}]
    if also_text:
        content.append({"type": "text", "text": also_text})
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": content}}


# ---- P0: tool OUTCOMES ----

def test_failed_bash_action_annotated_with_tail(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Bash", {"command": "pytest -q"}, "t1", "2026-06-20T10:01:00.000Z"),
        _tool_result("t1", "2026-06-20T10:01:30.000Z", content="3 errors", is_error=True),
    ])
    d = rec.build_digest(path)
    assert d.failed_actions == ["Bash: pytest -q → FAILED: 3 errors"]
    # the same outcome is folded into the recent-actions list, too
    assert any("→ FAILED: 3 errors" in a for a in d.recent_tool_actions)


def test_successful_action_not_marked_failed(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Bash", {"command": "echo hi"}, "t1", "2026-06-20T10:01:00.000Z"),
        _tool_result("t1", "2026-06-20T10:01:30.000Z", content="hi"),
    ])
    d = rec.build_digest(path)
    assert d.failed_actions == []
    assert d.recent_tool_actions == ["Bash: echo hi"]


def test_nonzero_exit_from_tooluseresult_marks_failure(tmp_path):
    # No is_error on the block; failure inferred from a nonzero exitCode and the
    # tail is pulled from the record-level toolUseResult stderr.
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Bash", {"command": "npm run build"}, "t1", "2026-06-20T10:01:00.000Z"),
        _tool_result("t1", "2026-06-20T10:01:30.000Z", content="",
                     tool_use_result={"exitCode": 1, "stderr": "boom"}),
    ])
    d = rec.build_digest(path)
    assert d.failed_actions == ["Bash: npm run build → FAILED: boom"]


def test_edit_failure_marked(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Edit", {"file_path": "/repo/auth.ts"}, "t1", "2026-06-20T10:01:00.000Z"),
        _tool_result("t1", "2026-06-20T10:01:30.000Z",
                     content="String to replace not found", is_error=True),
    ])
    d = rec.build_digest(path)
    assert d.failed_actions == ["Edit: /repo/auth.ts → FAILED: String to replace not found"]


def test_failed_actions_most_recent_first(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Bash", {"command": "first"}, "t1", "2026-06-20T10:01:00.000Z"),
        _tool_result("t1", "2026-06-20T10:01:10.000Z", content="e1", is_error=True),
        _tool_use("Bash", {"command": "second"}, "t2", "2026-06-20T10:02:00.000Z"),
        _tool_result("t2", "2026-06-20T10:02:10.000Z", content="e2", is_error=True),
    ])
    d = rec.build_digest(path)
    assert d.failed_actions == [
        "Bash: second → FAILED: e2",
        "Bash: first → FAILED: e1",
    ]


def test_render_shows_failed_actions_before_last_instruction(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Bash", {"command": "pytest"}, "t1", "2026-06-20T10:01:00.000Z"),
        _tool_result("t1", "2026-06-20T10:01:10.000Z", content="boom", is_error=True),
        {"type": "last-prompt", "lastPrompt": "fix the tests"},
    ])
    d = rec.build_digest(path)
    out = rec.render_digest(d)
    assert "Failed actions (most recent first)" in out
    assert "Bash: pytest → FAILED: boom" in out
    assert out.index("Failed actions") < out.index("Last instruction")


# ---- P1: in-flight intent (last thinking block) ----

def test_in_flight_intent_from_thinking_block(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _thinking("I need to wire the parser into the CLI next",
                  "2026-06-20T10:01:00.000Z", also_text="Working on it"),
    ])
    d = rec.build_digest(path)
    assert d.in_flight_intent == "I need to wire the parser into the CLI next"
    assert d.last_assistant_text == "Working on it"
    out = rec.render_digest(d)
    assert "In-flight intent" in out
    assert "wire the parser into the CLI" in out


def test_in_flight_intent_capped(tmp_path):
    long_thought = "x" * 1000
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _thinking(long_thought, "2026-06-20T10:01:00.000Z"),
    ])
    d = rec.build_digest(path)
    assert d.in_flight_intent.endswith("…")
    assert len(d.in_flight_intent) <= rec._INFLIGHT_INTENT_CHARS + 1


def test_in_flight_intent_falls_back_to_text_and_not_duplicated(tmp_path):
    # No thinking block: intent falls back to the last assistant text, and the
    # renderer suppresses the duplicate 'In-flight intent' header.
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        {"type": "assistant", "timestamp": "2026-06-20T10:01:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "just a plain reply"}]}},
    ])
    d = rec.build_digest(path)
    assert d.in_flight_intent == "just a plain reply"
    out = rec.render_digest(d)
    assert "In-flight intent" not in out


# ---- P2: --depth verbosity lever ----

def test_depth_standard_matches_historical_constants():
    std = rec.DEPTH_PROFILES["standard"]
    assert std["budget"] == rec.DEFAULT_BUDGET_CHARS
    assert std["asks"] == rec._RECENT_USER_ASKS
    assert std["actions"] == rec._MAX_TOOL_ACTIONS
    assert std["files"] == rec._MAX_FILES


def test_depth_scales_action_and_file_caps(tmp_path):
    records = [_user_line()]
    for i in range(12):
        records.append(_tool_use("Edit", {"file_path": f"/repo/f{i}.py"}, f"t{i}",
                                 "2026-06-20T10:%02d:00.000Z" % i))
    path = _write_session(tmp_path / "p", SID, records)

    brief = rec.build_digest(path, depth="brief")
    standard = rec.build_digest(path, depth="standard")
    assert len(brief.recent_tool_actions) == 6      # brief caps actions at 6
    assert len(brief.files_touched) == 10           # brief caps files at 10
    assert len(standard.recent_tool_actions) == 12  # all 12 present at standard
    assert len(standard.files_touched) == 12


def test_default_depth_equals_standard(tmp_path):
    path = _write_session(tmp_path / "p", SID, [
        _user_line(),
        _tool_use("Edit", {"file_path": "/repo/a.py"}, "t1", "2026-06-20T10:01:00.000Z"),
    ])
    assert rec.build_digest(path).files_touched == rec.build_digest(path, depth="standard").files_touched


def test_cli_depth_full_uses_larger_budget(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    cwd = "/work/depthrepo"
    pdir = projects / "-work-depthrepo"
    big = "Y" * 18000
    _write_session(pdir, SID, [
        _user_line(cwd),
        {"type": "assistant", "timestamp": "2026-06-20T10:05:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": big}]}},
        {"type": "last-prompt", "lastPrompt": "keep going"},
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    runner = CliRunner()
    # standard (12k budget) trims the 18k assistant message; full (24k) does not
    std = runner.invoke(main, ["recover", "--cwd", cwd, "--session", SID, "--digest"])
    full = runner.invoke(main, ["recover", "--cwd", cwd, "--session", SID, "--digest", "--depth", "full"])
    assert std.exit_code == 0 and full.exit_code == 0, (std.output, full.output)
    assert "budget note" in std.output
    assert "budget note" not in full.output


def test_cli_budget_overrides_depth(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    cwd = "/work/depthrepo2"
    pdir = projects / "-work-depthrepo2"
    big = "Z" * 9000
    _write_session(pdir, SID, [
        _user_line(cwd),
        {"type": "assistant", "timestamp": "2026-06-20T10:05:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": big}]}},
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    runner = CliRunner()
    # full's 24k budget would not trim 9k, but an explicit --budget 1000 must
    res = runner.invoke(main, ["recover", "--cwd", cwd, "--session", SID,
                               "--digest", "--depth", "full", "--budget", "1000"])
    assert res.exit_code == 0, res.output
    assert "budget note" in res.output

"""Tests for M2 memory-vault capture: capture.py, the memory_capture shim,
hooks_config.py, and the cli.py installer/remover.

Isolation mirrors test_memory_vault.py: JACKED_HOME / JACKED_VAULT_DIR under
tmp_path, git identity via env, and the subagent env vars popped (so the suite
passes even when pytest itself runs inside a Claude Code subagent). The
`claude -p` boundary is always mocked -- a real claude binary is never invoked.
"""
import json
import subprocess
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from rich.console import Console

import jacked.cli as cli
from jacked.data.hooks import memory_capture
from jacked.memory import capture, hooks_config
from jacked.memory import vault as vault_mod


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    vault = tmp_path / "vault"
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setenv("JACKED_VAULT_DIR", str(vault))
    for key, val in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, val)
    # This suite asserts capture WRITES; if pytest runs inside a subagent the
    # capture guard would silently no-op everything. Pop the markers.
    monkeypatch.delenv("CLAUDE_CODE_PARENT_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AGENT_TYPE", raising=False)

    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False))
    return SimpleNamespace(home=home, vault=vault, tmp=tmp_path, buf=buf, monkeypatch=monkeypatch)


def _init_enabled_vault(env):
    """An initialized, enabled empty vault (no repos mapped)."""
    vault_mod.init_vault(env.vault, [], {})
    vault_mod.set_enabled(env.home, True, vault=env.vault)


def _commit_count(vault):
    r = subprocess.run(["git", "-C", str(vault), "rev-list", "--count", "HEAD"],
                       capture_output=True, text=True)
    return int((r.stdout or "0").strip() or 0)


def _user_line(text, ts="2026-07-18T10:00:00Z"):
    return json.dumps({
        "type": "user", "uuid": str(uuid4()), "timestamp": ts,
        "message": {"role": "user", "content": text},
    })


def _asst_line(text, ts="2026-07-18T10:00:01Z"):
    return json.dumps({
        "type": "assistant", "uuid": str(uuid4()), "timestamp": ts,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    })


def _write_transcript(path, n_human=3):
    lines = []
    for i in range(n_human):
        lines.append(_user_line(f"Please implement feature number {i} with tests and docs."))
        lines.append(_asst_line(f"Done implementing feature {i}; all tests pass."))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cwd_dir(env, name="myrepo"):
    """A plain (non-git) working dir -> identity/group/repo_short all == name."""
    d = env.tmp / name
    d.mkdir()
    return d


def _payload(transcript, cwd, session_id="sess-abcdef123456", event="SessionEnd"):
    return {
        "hook_event_name": event,
        "session_id": session_id,
        "transcript_path": str(transcript) if transcript else None,
        "cwd": str(cwd),
    }


def _mock_triage(env, response):
    """Monkeypatch the single claude-call boundary to return `response`."""
    env.monkeypatch.setattr(capture, "_call_claude", lambda prompt, model: response)


def _episodic_files(env, group="myrepo", repo_short="myrepo"):
    d = env.vault / "groups" / group / "episodic" / repo_short
    return sorted(d.glob("today-*.md")) if d.is_dir() else []


def _episodic_text(env, group="myrepo", repo_short="myrepo"):
    return "".join(f.read_text(encoding="utf-8") for f in _episodic_files(env, group, repo_short))


def _decision_notes(env, group="myrepo"):
    d = env.vault / "groups" / group / "decision"
    return sorted(d.glob("*.md")) if d.is_dir() else []


# --------------------------------------------------------------------------- #
# Episodic write (the always-path)
# --------------------------------------------------------------------------- #

def test_session_end_writes_episodic_entry(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, json.dumps({"episodic": "Refactored the parser.", "semantic": None}))

    before = _commit_count(env.vault)
    capture.session_end(_payload(transcript, cwd))

    text = _episodic_text(env)
    assert "## " in text and "| -" in text  # "## HH:MM | <branch>", branch "-" for a non-repo cwd
    assert "Refactored the parser." in text
    # No semantic note.
    assert _decision_notes(env) == []
    # Exactly one vault commit for the whole run.
    assert _commit_count(env.vault) == before + 1


def test_episodic_header_uses_git_branch(env):
    _init_enabled_vault(env)
    # A real git repo so `git branch --show-current` returns a branch name.
    repo = env.tmp / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True,
                   capture_output=True, text=True)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, json.dumps({"episodic": "Did work.", "semantic": None}))

    capture.session_end(_payload(transcript, repo))

    text = _episodic_text(env, group="gitrepo", repo_short="gitrepo")
    assert "| trunk" in text


# --------------------------------------------------------------------------- #
# Semantic note (at most one)
# --------------------------------------------------------------------------- #

def test_decision_fixture_writes_exactly_one_semantic(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 4)
    _mock_triage(env, json.dumps({
        "episodic": "Chose Postgres.",
        "semantic": {
            "type": "decision",
            "title": "Use Postgres for the vault index",
            "body": "We picked Postgres over SQLite for concurrent writes.",
            "tags": ["database"],
        },
    }))

    capture.session_end(_payload(transcript, cwd))

    notes = _decision_notes(env)
    assert len(notes) == 1
    meta, body = vault_mod.parse_frontmatter(notes[0].read_text(encoding="utf-8"))
    assert meta["type"] == "decision"
    assert "candidate" in meta["tags"]
    assert "database" in meta["tags"]
    assert meta["group"] == "myrepo"
    assert meta["repos"] == ["myrepo"]
    assert meta["created"] and meta["updated"]
    assert "Postgres over SQLite" in body
    # Episodic still written alongside.
    assert "Chose Postgres." in _episodic_text(env)


def test_semantic_type_progress_is_rejected(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, json.dumps({
        "episodic": "Made progress.",
        "semantic": {"type": "progress", "title": "x", "body": "y", "tags": []},
    }))

    capture.session_end(_payload(transcript, cwd))

    # progress is never accepted from triage -> no semantic note, episodic kept.
    assert _decision_notes(env) == []
    for t in vault_mod.VALID_TYPES:
        assert not list((env.vault / "groups" / "myrepo").glob(f"{t}/*.md"))
    assert "Made progress." in _episodic_text(env)


def test_semantic_run_preserves_drift_and_clears_queue(env):
    """add_note bumps drift via its own load/save; the trailing queue save must
    read fresh state so the bump survives (regression guard on state clobber)."""
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, json.dumps({
        "episodic": "Set a convention.",
        "semantic": {"type": "convention", "title": "Tabs vs spaces", "body": "Spaces.", "tags": []},
    }))

    capture.session_end(_payload(transcript, cwd))

    st = vault_mod.load_state(env.home)
    assert st["drift_added"] == 1  # not clobbered back to 0 by the queue save
    assert st["retry_queue"] == []


def test_multiple_semantic_notes_fold_into_one_commit(env):
    """A drained-success note + a current-session note in the same run land in a
    single vault commit (the whole reason add_note(commit=False) exists)."""
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    queued_t = _write_transcript(env.tmp / "q.jsonl", 3)
    cur_t = _write_transcript(env.tmp / "c.jsonl", 3)
    st = vault_mod.load_state(env.home)
    st["retry_queue"] = [{
        "kind": "session_end", "transcript_path": str(queued_t), "cwd": str(cwd),
        "session_id": "old", "ts": "2026-07-18T09:00:00Z", "attempts": 1,
    }]
    vault_mod.save_state(env.home, st)
    _mock_triage(env, json.dumps({
        "episodic": "e",
        "semantic": {"type": "decision", "title": "Same title", "body": "Body here.", "tags": []},
    }))

    before = _commit_count(env.vault)
    capture.session_end(_payload(cur_t, cwd, session_id="new"))

    assert len(_decision_notes(env)) == 2  # one from the drain, one from the current run
    assert _commit_count(env.vault) == before + 1  # ...but a single commit
    st = vault_mod.load_state(env.home)
    assert st["retry_queue"] == []
    assert st["drift_added"] == 2


# --------------------------------------------------------------------------- #
# Triage failure -> stub + retry queue
# --------------------------------------------------------------------------- #

def test_triage_garbage_writes_stub_and_enqueues(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, "this is not json at all")

    capture.session_end(_payload(transcript, cwd, session_id="deadbeefcafe"))

    assert "triage unavailable" in _episodic_text(env)
    assert _decision_notes(env) == []
    q = vault_mod.load_state(env.home)["retry_queue"]
    assert len(q) == 1
    assert q[0]["attempts"] == 1
    assert q[0]["kind"] == "session_end"
    assert q[0]["transcript_path"] == str(transcript)


def test_triage_subprocess_failure_enqueues(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, None)  # None == launch/timeout/non-zero exit

    capture.session_end(_payload(transcript, cwd))

    assert "triage unavailable" in _episodic_text(env)
    assert len(vault_mod.load_state(env.home)["retry_queue"]) == 1


def test_retry_drains_on_next_run(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    queued_transcript = _write_transcript(env.tmp / "queued.jsonl", 3)
    # Pre-seed a failed item; the current run's transcript is absent so only the
    # drain does work.
    st = vault_mod.load_state(env.home)
    st["retry_queue"] = [{
        "kind": "session_end", "transcript_path": str(queued_transcript),
        "cwd": str(cwd), "session_id": "old", "ts": "2026-07-18T09:00:00Z", "attempts": 1,
    }]
    vault_mod.save_state(env.home, st)

    _mock_triage(env, json.dumps({"episodic": "Recovered summary.", "semantic": None}))
    capture.session_end(_payload(None, cwd, session_id="new"))

    assert "Recovered summary." in _episodic_text(env)
    assert vault_mod.load_state(env.home)["retry_queue"] == []


def test_third_attempt_writes_stub_and_drops(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    st = vault_mod.load_state(env.home)
    st["retry_queue"] = [{
        "kind": "session_end", "transcript_path": str(env.tmp / "gone.jsonl"),
        "cwd": str(cwd), "session_id": "old", "ts": "2026-07-18T09:00:00Z", "attempts": 2,
    }]
    vault_mod.save_state(env.home, st)

    # Current run does nothing (no transcript); drain retries -> 3rd failure.
    _mock_triage(env, None)
    capture.session_end(_payload(None, cwd, session_id="new"))

    # Not a silent drop: the stub landed AND the item is gone.
    assert "triage unavailable" in _episodic_text(env)
    assert vault_mod.load_state(env.home)["retry_queue"] == []


# --------------------------------------------------------------------------- #
# Guards (silent no-op)
# --------------------------------------------------------------------------- #

def test_fewer_than_three_human_messages_writes_nothing(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 2)
    _mock_triage(env, json.dumps({"episodic": "nope", "semantic": None}))

    before = _commit_count(env.vault)
    capture.session_end(_payload(transcript, cwd))

    assert _episodic_files(env) == []
    assert vault_mod.load_state(env.home)["retry_queue"] == []
    assert _commit_count(env.vault) == before  # no commit


def test_subagent_env_writes_nothing(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 5)
    _mock_triage(env, json.dumps({"episodic": "should not run", "semantic": None}))
    env.monkeypatch.setenv("CLAUDE_CODE_AGENT_TYPE", "reviewer")

    capture.session_end(_payload(transcript, cwd))

    assert _episodic_files(env) == []


def test_disabled_state_writes_nothing(env):
    # Init vault but leave the feature disabled.
    vault_mod.init_vault(env.vault, [], {})
    vault_mod.set_enabled(env.home, False, vault=env.vault)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 5)
    _mock_triage(env, json.dumps({"episodic": "should not run", "semantic": None}))

    capture.session_end(_payload(transcript, cwd))

    assert _episodic_files(env) == []


def test_uninitialized_vault_writes_nothing(env):
    # Enabled flag but no vault on disk.
    vault_mod.set_enabled(env.home, True, vault=env.vault)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 5)
    _mock_triage(env, json.dumps({"episodic": "x", "semantic": None}))

    capture.session_end(_payload(transcript, cwd))
    assert not env.vault.exists() or _episodic_files(env) == []


# --------------------------------------------------------------------------- #
# PreCompact handover
# --------------------------------------------------------------------------- #

def test_pre_compact_writes_handover_entry(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, json.dumps({"episodic": "Mid-refactor of the router.", "semantic": None}))

    capture.pre_compact(_payload(transcript, cwd, event="PreCompact"))

    text = _episodic_text(env)
    assert "compact handover: Mid-refactor of the router." in text


def test_pre_compact_failure_writes_handover_stub(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)
    _mock_triage(env, "garbage")

    capture.pre_compact(_payload(transcript, cwd, event="PreCompact"))

    text = _episodic_text(env)
    assert "compact handover: triage unavailable" in text


# --------------------------------------------------------------------------- #
# Fail-open: no exception ever escapes
# --------------------------------------------------------------------------- #

def test_exception_inside_capture_is_swallowed(env):
    _init_enabled_vault(env)
    cwd = _cwd_dir(env)
    transcript = _write_transcript(env.tmp / "t.jsonl", 3)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    env.monkeypatch.setattr(capture, "_drain_queue", _boom)
    # Must not raise.
    capture.session_end(_payload(transcript, cwd))

    # Nothing corrupt: state still parses, vault untouched by the current run.
    assert vault_mod.load_state(env.home)["enabled"] is True
    assert _episodic_files(env) == []


# --------------------------------------------------------------------------- #
# _call_claude real boundary (subprocess)
# --------------------------------------------------------------------------- #

def test_call_claude_returns_none_on_launch_error(env):
    with patch("subprocess.run", side_effect=OSError("no binary")):
        assert capture._call_claude("prompt", "haiku") is None


def test_call_claude_returns_none_on_timeout(env):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)):
        assert capture._call_claude("prompt", "haiku") is None


def test_call_claude_returns_none_on_nonzero_exit(env):
    class _P:
        returncode = 1
        stdout = ""
    with patch("subprocess.run", return_value=_P()):
        assert capture._call_claude("prompt", "haiku") is None


def test_call_claude_returns_stdout_on_success(env):
    class _P:
        returncode = 0
        stdout = '{"episodic": "ok", "semantic": null}'
    with patch("subprocess.run", return_value=_P()):
        assert capture._call_claude("prompt", "haiku") == '{"episodic": "ok", "semantic": null}'


# --------------------------------------------------------------------------- #
# Triage prompt: transcript framed as data
# --------------------------------------------------------------------------- #

def test_triage_prompt_contains_data_framing():
    prompt = capture._build_triage_prompt("SOME TRANSCRIPT", "session_end")
    assert capture._DATA_FRAMING in prompt
    assert "SOME TRANSCRIPT" in prompt
    assert "```" in prompt  # transcript is fenced


def test_pre_compact_prompt_asks_for_handover():
    prompt = capture._build_triage_prompt("X", "pre_compact")
    assert capture._DATA_FRAMING in prompt
    assert "PRE-COMPACTION" in prompt


def test_parse_triage_tolerates_fenced_json():
    raw = "Here you go:\n```json\n{\"episodic\": \"hi\", \"semantic\": null}\n```\n"
    parsed = capture._parse_triage(raw)
    assert parsed == {"episodic": "hi", "semantic": None}


def test_parse_triage_rejects_non_object():
    assert capture._parse_triage("[1, 2, 3]") is None
    assert capture._parse_triage("") is None
    assert capture._parse_triage(None) is None


# --------------------------------------------------------------------------- #
# hooks_config: pure settings-dict ensure/remove
# --------------------------------------------------------------------------- #

def test_hooks_config_ensure_and_idempotent():
    settings = {"hooks": {}}
    cmd = '"/x/jacked" _hook memory_capture'
    assert hooks_config.ensure_capture_entries(settings, cmd) is True
    # Re-ensure with the same command is a no-op.
    assert hooks_config.ensure_capture_entries(settings, cmd) is False

    se = settings["hooks"]["SessionEnd"]
    pc = settings["hooks"]["PreCompact"]
    assert len(se) == 1 and se[0]["matcher"] == ""
    assert se[0]["hooks"][0]["async"] is True
    assert "memory_capture" in se[0]["hooks"][0]["command"]
    assert len(pc) == 1 and pc[0]["matcher"] == "auto"
    assert pc[0]["hooks"][0]["async"] is True


def test_hooks_config_leaves_foreign_entries_untouched():
    foreign_end = {"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/mine.sh"}]}
    tracker = {"matcher": "", "hooks": [{"type": "command",
               "command": '"/x/jacked" _hook session_account_tracker', "async": True}]}
    settings = {"hooks": {"SessionEnd": [foreign_end, tracker]}}

    hooks_config.ensure_capture_entries(settings, '"/x/jacked" _hook memory_capture')

    end = settings["hooks"]["SessionEnd"]
    assert foreign_end in end
    assert tracker in end
    assert any("memory_capture" in h["hooks"][0]["command"] for h in end)


def test_hooks_config_upgrades_stale_command():
    settings = {"hooks": {}}
    hooks_config.ensure_capture_entries(settings, '"/old/jacked" _hook memory_capture')
    changed = hooks_config.ensure_capture_entries(settings, '"/new/jacked" _hook memory_capture')
    assert changed is True
    cmds = [e["hooks"][0]["command"] for e in settings["hooks"]["SessionEnd"]]
    assert cmds == ['"/new/jacked" _hook memory_capture']  # replaced, not duplicated


def test_hooks_config_remove_strips_only_ours():
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/mine.sh"}]}
    settings = {"hooks": {"SessionEnd": [foreign]}}
    hooks_config.ensure_capture_entries(settings, '"/x/jacked" _hook memory_capture')

    assert hooks_config.remove_capture_entries(settings) is True
    assert settings["hooks"]["SessionEnd"] == [foreign]
    # No jacked-owned entries remain anywhere.
    assert not any("memory_capture" in str(e) for arr in settings["hooks"].values() for e in arr)
    # Removing again is a no-op.
    assert hooks_config.remove_capture_entries(settings) is False


def test_hooks_config_recall_entry_is_synchronous():
    settings = {"hooks": {}}
    assert hooks_config.ensure_recall_entry(settings, '"/x/jacked" _hook memory_recall') is True
    entry = settings["hooks"]["SessionStart"][0]
    assert entry["matcher"] == ""
    assert "async" not in entry["hooks"][0]  # synchronous: stdout is injected
    assert "memory_recall" in entry["hooks"][0]["command"]
    assert hooks_config.remove_recall_entries(settings) is True


# --------------------------------------------------------------------------- #
# cli.py installer / remover
# --------------------------------------------------------------------------- #

def test_cli_installer_writes_async_entries(env):
    settings_path = env.home / ".claude" / "settings.json"
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/mine.sh"}]}
    existing = {"hooks": {"SessionEnd": [foreign]}}

    cli._install_memory_capture_hook(existing, settings_path)

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    end = data["hooks"]["SessionEnd"]
    pc = data["hooks"]["PreCompact"]
    assert foreign in end  # foreign untouched
    ours = [h for h in end if "memory_capture" in str(h)]
    assert len(ours) == 1 and ours[0]["hooks"][0]["async"] is True
    assert len(pc) == 1 and pc[0]["matcher"] == "auto"
    assert pc[0]["hooks"][0]["async"] is True

    # Remover strips only ours.
    assert cli._remove_memory_hooks(settings_path) is True
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["hooks"]["SessionEnd"] == [foreign]
    assert data["hooks"]["PreCompact"] == []


def test_cli_installer_is_idempotent(env):
    settings_path = env.home / ".claude" / "settings.json"
    existing = {"hooks": {}}
    cli._install_memory_capture_hook(existing, settings_path)
    env.buf.truncate(0)
    env.buf.seek(0)
    # Second call with the already-updated dict: no change.
    cli._install_memory_capture_hook(existing, settings_path)
    assert "already configured" in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# memory_capture dispatch shim
# --------------------------------------------------------------------------- #

def test_shim_routes_session_end(env, capsys):
    calls = []
    env.monkeypatch.setattr(capture, "session_end", lambda p: calls.append(p))
    payload = json.dumps({"hook_event_name": "SessionEnd", "session_id": "s", "cwd": "/tmp"})
    with patch("sys.stdin") as stdin:
        stdin.read.return_value = payload
        memory_capture.main()
    assert len(calls) == 1 and calls[0]["session_id"] == "s"
    assert capsys.readouterr().out == ""  # no stdout ever


def test_shim_routes_pre_compact(env, capsys):
    calls = []
    env.monkeypatch.setattr(capture, "pre_compact", lambda p: calls.append(p))
    payload = json.dumps({"hook_event_name": "PreCompact", "session_id": "s", "cwd": "/tmp"})
    with patch("sys.stdin") as stdin:
        stdin.read.return_value = payload
        memory_capture.main()
    assert len(calls) == 1
    assert capsys.readouterr().out == ""


def test_shim_ignores_other_events(env, capsys):
    called = []
    env.monkeypatch.setattr(capture, "session_end", lambda p: called.append(p))
    env.monkeypatch.setattr(capture, "pre_compact", lambda p: called.append(p))
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "s", "cwd": "/tmp"})
    with patch("sys.stdin") as stdin:
        stdin.read.return_value = payload
        memory_capture.main()
    assert called == []
    assert capsys.readouterr().out == ""


def test_shim_handles_empty_and_invalid_stdin(env, capsys):
    for raw in ("", "   ", "not json", "[1,2,3]"):
        with patch("sys.stdin") as stdin:
            stdin.read.return_value = raw
            memory_capture.main()  # must not raise
    assert capsys.readouterr().out == ""


def test_shim_swallows_downstream_exception(env, capsys):
    def _boom(_p):
        raise RuntimeError("kaboom")

    env.monkeypatch.setattr(capture, "session_end", _boom)
    payload = json.dumps({"hook_event_name": "SessionEnd", "session_id": "s", "cwd": "/tmp"})
    with patch("sys.stdin") as stdin:
        stdin.read.return_value = payload
        memory_capture.main()  # must not raise
    assert capsys.readouterr().out == ""

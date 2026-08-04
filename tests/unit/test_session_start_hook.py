"""Tests for the single combined SessionStart hook.

Claude Code runs SessionStart hook ENTRIES concurrently and concatenates their
stdout in COMPLETION order, so jacked's old three-entry layout randomized the
injected preamble (and with it the inference-side prompt prefix cache). These
tests pin the fix: ONE entry, one stdin read, a fixed emission order, and an
installer that migrates every older layout onto that entry without touching a
foreign SessionStart hook.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

CHAIN_SENTINEL = "=== CHAIN OF COMMAND SENTINEL ==="
RECALL_SENTINEL = "=== MEMORY VAULT SENTINEL ==="

PAYLOAD = json.dumps(
    {
        "hook_event_name": "SessionStart",
        "session_id": "sess-abc",
        "cwd": "/repo/somewhere",
    }
)


@pytest.fixture
def steps(monkeypatch):
    """Stub all three steps; record what each was called with.

    The tracker is stubbed at ``_handle_event`` (the function the hook runs in
    its daemon thread) so no test ever touches the real accounts DB.
    """
    from jacked.data.hooks import (
        chain_of_command_context,
        memory_recall,
        session_account_tracker,
    )

    calls: dict = {"tracker": [], "recall": []}

    def _tracker(event, session_id, repo_path):
        calls["tracker"].append((event, session_id, repo_path))

    def _recall(data=None):
        calls["recall"].append(data)
        return RECALL_SENTINEL

    monkeypatch.setattr(session_account_tracker, "_handle_event", _tracker)
    monkeypatch.setattr(chain_of_command_context, "render", lambda: CHAIN_SENTINEL)
    monkeypatch.setattr(memory_recall, "render", _recall)
    return calls


def _run(stdin_text: str):
    """Run the hook against ``stdin_text``; return the mocked stdin object."""
    from jacked.data.hooks import session_start

    mock_stdin = MagicMock()
    mock_stdin.read.return_value = stdin_text
    with patch("sys.stdin", mock_stdin):
        session_start.main()
    return mock_stdin


# --------------------------------------------------------------------------- #
# Hook module: order, single read, isolation
# --------------------------------------------------------------------------- #

class TestHookMain:
    def test_emits_chain_of_command_before_memory_recall(self, steps, capsys):
        _run(PAYLOAD)

        out = capsys.readouterr().out
        assert CHAIN_SENTINEL in out
        assert RECALL_SENTINEL in out
        # THE point of this hook: a fixed order, so the preamble is byte-stable.
        assert out.index(CHAIN_SENTINEL) < out.index(RECALL_SENTINEL)

    def test_order_is_identical_across_runs(self, steps, capsys):
        outs = []
        for _ in range(5):
            _run(PAYLOAD)
            outs.append(capsys.readouterr().out)
        assert len(set(outs)) == 1  # byte-identical every session

    def test_reads_stdin_exactly_once(self, steps):
        mock_stdin = _run(PAYLOAD)
        assert mock_stdin.read.call_count == 1

    def test_runs_the_session_tracker_with_the_payload(self, steps):
        _run(PAYLOAD)
        assert steps["tracker"] == [("SessionStart", "sess-abc", "/repo/somewhere")]

    def test_tracker_emits_nothing(self, steps, capsys):
        """The tracker step is silent: only the two emitters write to stdout."""
        _run(PAYLOAD)
        out = capsys.readouterr().out
        assert out == f"{CHAIN_SENTINEL}\n{RECALL_SENTINEL}\n"

    def test_passes_payload_to_recall_for_cwd(self, steps):
        _run(PAYLOAD)
        assert steps["recall"] == [json.loads(PAYLOAD)]

    def test_skips_tracker_without_session_id(self, steps, capsys):
        _run(json.dumps({"hook_event_name": "SessionStart", "cwd": "/repo/x"}))
        assert steps["tracker"] == []
        # ...and the emitting steps still run.
        out = capsys.readouterr().out
        assert CHAIN_SENTINEL in out and RECALL_SENTINEL in out

    def test_empty_stdin_is_silent(self, steps, capsys):
        _run("")
        assert capsys.readouterr().out == ""
        assert steps["tracker"] == [] and steps["recall"] == []

    def test_whitespace_stdin_is_silent(self, steps, capsys):
        _run("   \n")
        assert capsys.readouterr().out == ""

    def test_invalid_json_stdin_is_silent(self, steps, capsys):
        _run("this is not json at all {{{")
        assert capsys.readouterr().out == ""

    def test_non_dict_payload_is_silent(self, steps, capsys):
        _run("[1, 2, 3]")
        assert capsys.readouterr().out == ""

    def test_failing_chain_step_still_lets_recall_emit(self, steps, capsys):
        from jacked.data.hooks import chain_of_command_context

        def _boom():
            raise RuntimeError("skill read exploded")

        with patch.object(chain_of_command_context, "render", _boom):
            _run(PAYLOAD)

        out = capsys.readouterr().out
        assert CHAIN_SENTINEL not in out
        assert RECALL_SENTINEL in out  # the later step is never skipped

    def test_failing_tracker_step_still_lets_both_emitters_run(self, steps, capsys):
        from jacked.data.hooks import session_start

        # The tracker is started outside STEPS (joined last), so a failure to
        # even START it must not stop the emitters.
        with patch.object(
            session_start,
            "_start_session_tracker",
            MagicMock(side_effect=RuntimeError("db gone")),
        ):
            _run(PAYLOAD)

        out = capsys.readouterr().out
        assert CHAIN_SENTINEL in out and RECALL_SENTINEL in out

    def test_failing_recall_step_keeps_chain_output(self, steps, capsys):
        from jacked.data.hooks import memory_recall

        def _boom(data=None):
            raise RuntimeError("vault exploded")

        with patch.object(memory_recall, "render", _boom):
            _run(PAYLOAD)

        assert CHAIN_SENTINEL in capsys.readouterr().out

    def test_never_raises_when_every_step_fails(self, steps, capsys):
        from jacked.data.hooks import session_start

        with patch.object(session_start, "STEPS", (
            ("boom-1", MagicMock(side_effect=RuntimeError("1"))),
            ("boom-2", MagicMock(side_effect=RuntimeError("2"))),
        )):
            _run(PAYLOAD)  # must not raise

        assert capsys.readouterr().out == ""


class TestRealStepsWiring:
    """The steps run the REAL emitters, not just stubs."""

    def test_real_chain_of_command_render_is_used(self, tmp_path, capsys, monkeypatch):
        from jacked.data.hooks import memory_recall, session_account_tracker

        skill_dir = tmp_path / ".claude" / "skills" / "chain-of-command"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: chain-of-command\n---\nREAL_POLICY_BODY\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            session_account_tracker, "_handle_event", lambda *a, **k: None
        )
        monkeypatch.setattr(memory_recall, "render", lambda data=None: "")

        _run(PAYLOAD)

        out = capsys.readouterr().out
        assert "=== CHAIN OF COMMAND (auto-loaded by jacked) ===" in out
        assert "REAL_POLICY_BODY" in out
        assert "name: chain-of-command" not in out  # frontmatter stripped

    def test_disabled_features_emit_nothing(self, tmp_path, capsys, monkeypatch):
        """No skill file + a disabled vault: the entry stays, output is empty."""
        from jacked.data.hooks import memory_recall, session_account_tracker

        monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no SKILL.md here
        monkeypatch.setattr(
            session_account_tracker, "_handle_event", lambda *a, **k: None
        )
        monkeypatch.setattr(memory_recall, "render", lambda data=None: "")

        _run(PAYLOAD)

        assert capsys.readouterr().out == ""


# --------------------------------------------------------------------------- #
# Installer: exactly one entry, every legacy layout migrated
# --------------------------------------------------------------------------- #

FOREIGN = {
    "matcher": "",
    "hooks": [{"type": "command", "command": "/usr/local/bin/my-own-startup.sh"}],
}


def _install(existing: dict, settings_path: Path):
    from jacked.cli import _install_session_start_hook

    with patch("jacked.findbin.find_bin", return_value="/fake/bin/jacked"):
        _install_session_start_hook(existing, settings_path)


def _entry(cmd: str, *, is_async: bool = False) -> dict:
    hook: dict = {"type": "command", "command": cmd}
    if is_async:
        hook["async"] = True
    return {"matcher": "", "hooks": [hook]}


class TestInstall:
    def test_fresh_install_writes_exactly_one_synchronous_entry(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        existing: dict = {"hooks": {}}

        _install(existing, settings_path)

        entries = existing["hooks"]["SessionStart"]
        assert len(entries) == 1
        inner = entries[0]["hooks"][0]
        assert inner["command"] == '"/fake/bin/jacked" _hook session_start'
        # SYNCHRONOUS: an async entry's stdout is never injected.
        assert "async" not in inner
        assert entries[0]["matcher"] == ""
        # Persisted to disk.
        persisted = json.loads(settings_path.read_text())
        assert persisted["hooks"]["SessionStart"] == entries

    def test_is_idempotent(self, tmp_path, capsys):
        settings_path = tmp_path / "settings.json"
        existing: dict = {"hooks": {}}

        _install(existing, settings_path)
        _install(existing, settings_path)

        assert len(existing["hooks"]["SessionStart"]) == 1
        assert "already configured" in capsys.readouterr().out

    def test_migrates_the_legacy_three_entry_layout(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        existing = {
            "hooks": {
                "SessionStart": [
                    _entry('"/old/jacked" _hook session_account_tracker', is_async=True),
                    _entry('"/old/jacked" _hook chain_of_command_context'),
                    _entry('"/old/jacked" _hook memory_recall'),
                ]
            }
        }

        _install(existing, settings_path)

        entries = existing["hooks"]["SessionStart"]
        assert len(entries) == 1  # all three legacy entries collapsed into ours
        cmd = entries[0]["hooks"][0]["command"]
        assert "_hook session_start" in cmd
        blob = json.dumps(existing)
        for gone in ("chain_of_command_context", "memory_recall"):
            assert gone not in blob
        # The tracker keeps its OTHER events; only its SessionStart leg moved.
        assert "session_account_tracker" not in blob

    def test_replaces_hand_written_bash_consolidation(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        bash_cmd = (
            "bash -c 'IN=$(cat); "
            '(printf %s "$IN" | "/Users/x/.local/bin/jacked" _hook session_account_tracker &); '
            'printf %s "$IN" | "/Users/x/.local/bin/jacked" _hook chain_of_command_context; '
            'printf %s "$IN" | "/Users/x/.local/bin/jacked" _hook memory_recall\''
        )
        existing = {"hooks": {"SessionStart": [_entry(bash_cmd)]}}

        _install(existing, settings_path)

        entries = existing["hooks"]["SessionStart"]
        assert len(entries) == 1
        assert entries[0]["hooks"][0]["command"] == '"/fake/bin/jacked" _hook session_start'
        assert "bash -c" not in json.dumps(existing)

    def test_upgrades_a_stale_combined_command_path(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        stale = "/old/site-packages/jacked/data/hooks/session_start.py"
        existing = {"hooks": {"SessionStart": [_entry(stale)]}}

        _install(existing, settings_path)

        entries = existing["hooks"]["SessionStart"]
        assert len(entries) == 1  # rewritten in place, not duplicated
        assert entries[0]["hooks"][0]["command"] == '"/fake/bin/jacked" _hook session_start'

    def test_foreign_session_start_hook_survives_untouched(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        existing = {
            "hooks": {
                "SessionStart": [
                    FOREIGN,
                    _entry('"/old/jacked" _hook chain_of_command_context'),
                ]
            }
        }

        _install(existing, settings_path)

        entries = existing["hooks"]["SessionStart"]
        assert len(entries) == 2
        assert entries[0] == FOREIGN  # untouched, and still first
        assert "_hook session_start" in entries[1]["hooks"][0]["command"]

    def test_foreign_hook_named_like_ours_is_not_claimed(self, tmp_path):
        """A user's own session_start.py outside our package stays put."""
        settings_path = tmp_path / "settings.json"
        mine = _entry("/home/me/scripts/session_start.py")
        existing = {"hooks": {"SessionStart": [mine]}}

        _install(existing, settings_path)

        entries = existing["hooks"]["SessionStart"]
        assert len(entries) == 2
        assert mine in entries

    def test_other_events_are_untouched(self, tmp_path):
        settings_path = tmp_path / "settings.json"
        stop_entry = _entry('"/x/jacked" _hook qa_suggest')
        existing = {"hooks": {"Stop": [stop_entry]}}

        _install(existing, settings_path)

        assert existing["hooks"]["Stop"] == [stop_entry]

    def test_skips_when_the_hook_script_is_missing(self, tmp_path, capsys):
        from jacked.cli import _install_session_start_hook

        settings_path = tmp_path / "settings.json"
        existing: dict = {"hooks": {}}
        with patch("jacked.cli._get_data_root", return_value=tmp_path / "nothing"):
            _install_session_start_hook(existing, settings_path)

        assert existing["hooks"] == {}
        assert not settings_path.exists()
        assert "not found" in capsys.readouterr().out


class TestSessionTrackerEvents:
    def test_tracker_no_longer_registers_its_own_session_start_entry(self, tmp_path):
        from jacked.cli import SESSION_TRACKER_EVENTS, _install_session_tracker_hook

        assert "SessionStart" not in [e for e, _ in SESSION_TRACKER_EVENTS]

        settings_path = tmp_path / "settings.json"
        existing: dict = {"hooks": {}}
        with patch("jacked.findbin.find_bin", return_value="/fake/bin/jacked"):
            _install_session_tracker_hook(existing, settings_path)

        assert "SessionStart" not in existing["hooks"]
        # The other tracker events are still wired.
        for event in ("Notification", "SessionEnd", "Stop", "UserPromptSubmit"):
            assert existing["hooks"][event]


# --------------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------------- #

class TestRemove:
    def test_removes_our_entry_and_keeps_foreign(self, tmp_path):
        from jacked.cli import _remove_session_start_hook

        settings_path = tmp_path / "settings.json"
        settings = {
            "hooks": {
                "SessionStart": [
                    FOREIGN,
                    _entry('"/fake/bin/jacked" _hook session_start'),
                ]
            }
        }
        settings_path.write_text(json.dumps(settings))

        assert _remove_session_start_hook(settings_path) is True

        remaining = json.loads(settings_path.read_text())["hooks"]["SessionStart"]
        assert remaining == [FOREIGN]

    def test_removes_the_hand_written_bash_entry(self, tmp_path):
        from jacked.cli import _remove_session_start_hook

        settings_path = tmp_path / "settings.json"
        bash_cmd = (
            'bash -c \'printf %s "$IN" | "/x/jacked" _hook chain_of_command_context; '
            'printf %s "$IN" | "/x/jacked" _hook memory_recall\''
        )
        settings_path.write_text(json.dumps({"hooks": {"SessionStart": [_entry(bash_cmd)]}}))

        assert _remove_session_start_hook(settings_path) is True
        assert json.loads(settings_path.read_text())["hooks"]["SessionStart"] == []

    def test_noop_when_no_entry(self, tmp_path):
        from jacked.cli import _remove_session_start_hook

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {"SessionStart": [FOREIGN]}}))
        assert _remove_session_start_hook(settings_path) is False

    def test_noop_when_settings_missing(self, tmp_path):
        from jacked.cli import _remove_session_start_hook

        assert _remove_session_start_hook(tmp_path / "does-not-exist.json") is False

    def test_single_feature_teardown_leaves_the_combined_entry(self, tmp_path):
        """Removing ONE feature must not strip the entry the others share."""
        from jacked.cli import _remove_chain_of_command_hook, _remove_memory_hooks

        settings_path = tmp_path / "settings.json"
        combined = _entry('"/fake/bin/jacked" _hook session_start')
        settings_path.write_text(json.dumps({"hooks": {"SessionStart": [combined]}}))

        _remove_chain_of_command_hook(settings_path)
        _remove_memory_hooks(settings_path)

        remaining = json.loads(settings_path.read_text())["hooks"]["SessionStart"]
        assert remaining == [combined]


# --------------------------------------------------------------------------- #
# Registration + standalone-hook regression
# --------------------------------------------------------------------------- #

class TestHookRegistration:
    def test_hook_is_in_valid_hook_names(self):
        from jacked.cli import _valid_hook_names

        assert "session_start" in _valid_hook_names()

    def test_shim_dispatches_to_the_combined_hook(self):
        from click.testing import CliRunner

        from jacked.cli import main

        runner = CliRunner()
        mock_module = MagicMock()
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            result = runner.invoke(main, ["_hook", "session_start"], input="{}")
        assert result.exit_code == 0, result.output
        mock_import.assert_called_once_with("jacked.data.hooks.session_start")
        mock_module.main.assert_called_once_with()


class TestStandaloneHooksStillWork:
    """Users on the old wiring keep working: both mains still read stdin themselves."""

    def test_standalone_chain_of_command_still_emits(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from jacked.cli import main

        skill_dir = tmp_path / ".claude" / "skills" / "chain-of-command"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: chain-of-command\n---\nSTANDALONE_POLICY_BODY\n", encoding="utf-8"
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = CliRunner().invoke(
            main, ["_hook", "chain_of_command_context"], input=PAYLOAD
        )

        assert result.exit_code == 0, result.output
        assert "=== CHAIN OF COMMAND (auto-loaded by jacked) ===" in result.output
        assert "STANDALONE_POLICY_BODY" in result.output

    def test_standalone_memory_recall_still_emits(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from jacked.cli import main
        from jacked.memory import recall as recall_mod

        monkeypatch.setenv("JACKED_HOME", str(tmp_path))
        monkeypatch.setattr(recall_mod, "build_brief", lambda cwd: f"BRIEF FOR {cwd}")

        result = CliRunner().invoke(main, ["_hook", "memory_recall"], input=PAYLOAD)

        assert result.exit_code == 0, result.output
        assert "BRIEF FOR /repo/somewhere" in result.output

    def test_standalone_memory_recall_falls_back_to_cwd(self, tmp_path, monkeypatch):
        from click.testing import CliRunner

        from jacked.cli import main
        from jacked.memory import recall as recall_mod

        monkeypatch.setenv("JACKED_HOME", str(tmp_path))
        monkeypatch.setattr(recall_mod, "build_brief", lambda cwd: "FALLBACK BRIEF")

        result = CliRunner().invoke(main, ["_hook", "memory_recall"], input="")

        assert result.exit_code == 0, result.output
        assert "FALLBACK BRIEF" in result.output


# --------------------------------------------------------------------------- #
# Memory-vault entry math against the combined entry
# --------------------------------------------------------------------------- #

class TestMemoryRecallEntryMath:
    def test_enable_does_not_add_a_second_recall_entry(self):
        from jacked.memory import hooks_config

        settings = {
            "hooks": {"SessionStart": [_entry('"/fake/bin/jacked" _hook session_start')]}
        }

        assert hooks_config.ensure_recall_entry(settings, "x _hook memory_recall") is False
        assert len(settings["hooks"]["SessionStart"]) == 1

    def test_recall_counts_as_installed_via_the_combined_entry(self):
        from jacked.memory import hooks_config

        settings = {
            "hooks": {"SessionStart": [_entry('"/fake/bin/jacked" _hook session_start')]}
        }

        assert hooks_config.has_session_start_entry(settings) is True
        assert hooks_config.has_recall_entry(settings) is True

    def test_standalone_recall_entry_is_still_installed_without_the_combined_one(self):
        from jacked.memory import hooks_config

        settings: dict = {"hooks": {}}
        assert hooks_config.ensure_recall_entry(settings, "x _hook memory_recall") is True
        assert hooks_config.has_recall_entry(settings) is True

    def test_disable_leaves_the_combined_entry_in_place(self):
        from jacked.memory import hooks_config

        combined = _entry('"/fake/bin/jacked" _hook session_start')
        settings = {"hooks": {"SessionStart": [combined]}}

        assert hooks_config.remove_recall_entries(settings) is False
        assert settings["hooks"]["SessionStart"] == [combined]

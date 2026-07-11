"""Unit tests for the agent-reach rules line (Claude CLAUDE.md + Codex AGENTS.md).

Fully offline: no subprocess, no network. The Claude side edits a tmp
``CLAUDE.md``; the Codex side edits a tmp ``AGENTS.md`` (CODEX_HOME is isolated to
a per-test path by the package conftest, so a developer's real ``~/.codex`` is
never touched). Runner-integration tests reuse the Recorder harness from
``test_runner`` and assert the rules land only after a successful hash verify.
"""

from pathlib import Path
from unittest import mock

import pytest

from jacked.codex._managed import (
    _AGENTS_BEGIN,
    _AGENTS_END,
    _REACH_BEGIN,
    _REACH_END,
    _install_agents_block,
    _strip_agents_block,
    install_reach_agents_block,
    strip_reach_agents_block,
)
from jacked.integrations import rules
from jacked.integrations.rules import (
    REACH_RULES_END,
    REACH_RULES_START,
    ReachRulesError,
    install_reach_rules,
    install_reach_rules_codex,
    reach_rules_body,
    remove_reach_rules,
    remove_reach_rules_codex,
)

# Reuse the runner harness verbatim rather than re-deriving it.
from tests.unit.integrations.test_runner import (
    PIN_SHA,
    Recorder,
    make_runner,
    patched,
)

BODY = reach_rules_body()


def _claude_md(home: Path) -> Path:
    return home / ".claude" / "CLAUDE.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Claude CLAUDE.md: fresh install / idempotency / removal
# --------------------------------------------------------------------------- #

class TestClaudeInstall:
    def test_fresh_install_into_nonexistent_file(self, tmp_path):
        home = tmp_path / "home"
        action = install_reach_rules(home)
        assert action == "installed"
        text = _read(_claude_md(home))
        assert text.startswith(REACH_RULES_START)
        assert REACH_RULES_END in text
        assert BODY in text
        assert text.endswith("\n")

    def test_fresh_install_preserves_user_content(self, tmp_path):
        home = tmp_path / "home"
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        original = "# My rules\n\n- do the thing\n"
        md.write_text(original, encoding="utf-8")

        install_reach_rules(home)
        text = _read(md)
        # user content byte-preserved as the prefix, block appended after a blank line
        assert text.startswith(original)
        assert REACH_RULES_START in text
        assert text.count(REACH_RULES_START) == 1
        assert text.count(REACH_RULES_END) == 1

    def test_double_install_is_byte_identical_noop(self, tmp_path):
        home = tmp_path / "home"
        install_reach_rules(home)
        first = _read(_claude_md(home))
        action = install_reach_rules(home)
        assert action == "unchanged"
        assert _read(_claude_md(home)) == first  # byte-identical, no rewrite

    def test_drifted_block_is_replaced_in_place(self, tmp_path):
        home = tmp_path / "home"
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        # a stale block (old body) surrounded by user content
        stale = f"intro\n\n{REACH_RULES_START}\nOLD BODY\n{REACH_RULES_END}\n\noutro\n"
        md.write_text(stale, encoding="utf-8")

        action = install_reach_rules(home)
        assert action == "updated"
        text = _read(md)
        assert "OLD BODY" not in text
        assert BODY in text
        assert text.startswith("intro")
        assert text.rstrip().endswith("outro")
        assert text.count(REACH_RULES_START) == 1


class TestClaudeRemove:
    def test_removal_restores_original_with_user_content(self, tmp_path):
        home = tmp_path / "home"
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        original = "# My rules\n\n- do the thing\n"
        md.write_text(original, encoding="utf-8")

        install_reach_rules(home)
        removed = remove_reach_rules(home)
        assert removed is True
        assert _read(md) == original  # exact restoration

    def test_removal_from_solo_block_leaves_empty(self, tmp_path):
        home = tmp_path / "home"
        install_reach_rules(home)  # into a fresh file (no surrounding content)
        assert remove_reach_rules(home) is True
        assert _read(_claude_md(home)) == ""

    def test_removal_absent_is_noop(self, tmp_path):
        home = tmp_path / "home"
        assert remove_reach_rules(home) is False  # file never existed
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        md.write_text("just user content\n", encoding="utf-8")
        assert remove_reach_rules(home) is False
        assert _read(md) == "just user content\n"  # untouched


class TestClaudeOrphanRefusal:
    def test_start_without_end_refuses(self, tmp_path):
        home = tmp_path / "home"
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        corrupt = f"{REACH_RULES_START}\nsome text but no end marker\n"
        md.write_text(corrupt, encoding="utf-8")
        with pytest.raises(ReachRulesError, match="corrupted"):
            install_reach_rules(home)
        assert _read(md) == corrupt  # left untouched

    def test_end_without_start_refuses(self, tmp_path):
        home = tmp_path / "home"
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        corrupt = f"some text\n{REACH_RULES_END}\n"
        md.write_text(corrupt, encoding="utf-8")
        with pytest.raises(ReachRulesError, match="corrupted"):
            install_reach_rules(home)
        assert _read(md) == corrupt

    def test_removal_on_orphan_leaves_untouched(self, tmp_path):
        home = tmp_path / "home"
        md = _claude_md(home)
        md.parent.mkdir(parents=True)
        corrupt = f"{REACH_RULES_START}\ndangling\n"
        md.write_text(corrupt, encoding="utf-8")
        assert remove_reach_rules(home) is False
        assert _read(md) == corrupt


# --------------------------------------------------------------------------- #
# Codex AGENTS.md block (via _managed.py)
# --------------------------------------------------------------------------- #

class TestCodexBlock:
    def test_install_and_strip_preserves_user_content(self, tmp_path):
        codex_home = tmp_path / "codex"
        agents = codex_home / "AGENTS.md"
        agents.parent.mkdir(parents=True)
        original = "# Codex notes\n\nkeep me\n"
        agents.write_text(original, encoding="utf-8")

        install_reach_agents_block(BODY, home=codex_home)
        text = _read(agents)
        assert text.startswith(original)
        assert _REACH_BEGIN in text and _REACH_END in text
        assert BODY in text

        assert strip_reach_agents_block(home=codex_home) is True
        assert _read(agents) == original  # exact restoration

    def test_install_into_missing_agents_md(self, tmp_path):
        codex_home = tmp_path / "codex"
        install_reach_agents_block(BODY, home=codex_home)
        text = _read(codex_home / "AGENTS.md")
        assert text.startswith(_REACH_BEGIN)
        assert text.rstrip().endswith(_REACH_END)

    def test_reach_block_coexists_with_behaviors_block(self, tmp_path):
        codex_home = tmp_path / "codex"
        agents = codex_home / "AGENTS.md"
        agents.parent.mkdir(parents=True)
        # main jacked-install behaviors block already present
        _install_agents_block(agents, "be blunt and correct")
        install_reach_agents_block(BODY, home=codex_home)
        text = _read(agents)
        assert _AGENTS_BEGIN in text and _AGENTS_END in text
        assert _REACH_BEGIN in text and _REACH_END in text

        # stripping the reach block leaves the behaviors block intact
        assert strip_reach_agents_block(home=codex_home) is True
        text2 = _read(agents)
        assert _AGENTS_BEGIN in text2 and "be blunt" in text2
        assert _REACH_BEGIN not in text2

        # and stripping the behaviors block still works independently
        assert _strip_agents_block(agents) is True
        assert _AGENTS_BEGIN not in _read(agents)

    def test_double_install_idempotent(self, tmp_path):
        codex_home = tmp_path / "codex"
        install_reach_agents_block(BODY, home=codex_home)
        first = _read(codex_home / "AGENTS.md")
        install_reach_agents_block(BODY, home=codex_home)
        assert _read(codex_home / "AGENTS.md") == first
        assert first.count(_REACH_BEGIN) == 1

    def test_orphaned_markers_left_untouched(self, tmp_path):
        codex_home = tmp_path / "codex"
        agents = codex_home / "AGENTS.md"
        agents.parent.mkdir(parents=True)
        corrupt = f"{_REACH_BEGIN}\nno end\n"
        agents.write_text(corrupt, encoding="utf-8")
        # extract-or-bail: warn + skip, never clobber
        install_reach_agents_block(BODY, home=codex_home)
        assert _read(agents) == corrupt
        assert strip_reach_agents_block(home=codex_home) is False
        assert _read(agents) == corrupt


# --------------------------------------------------------------------------- #
# Codex wrappers (rules.py) — presence gating + best-effort
# --------------------------------------------------------------------------- #

class TestCodexWrappers:
    def test_install_writes_when_codex_present(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()  # dir exists -> codex_present() True regardless of PATH
        env = {"CODEX_HOME": str(codex_home)}
        assert install_reach_rules_codex(env=env) == "installed"
        assert _REACH_BEGIN in _read(codex_home / "AGENTS.md")
        assert remove_reach_rules_codex(env=env) == "removed"
        assert _REACH_BEGIN not in _read(codex_home / "AGENTS.md")

    def test_install_skipped_when_codex_absent(self, tmp_path):
        codex_home = tmp_path / "codex-absent"  # does not exist
        env = {"CODEX_HOME": str(codex_home)}
        # also force the PATH probe to miss so codex_present() is unambiguously False
        with mock.patch("shutil.which", return_value=None):
            assert install_reach_rules_codex(env=env) == "skipped"
            assert remove_reach_rules_codex(env=env) == "skipped"
        assert not (codex_home / "AGENTS.md").exists()

    def test_install_error_is_swallowed(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        env = {"CODEX_HOME": str(codex_home)}
        with mock.patch(
            "jacked.integrations.rules.install_reach_agents_block",
            side_effect=OSError("disk full"),
        ):
            assert install_reach_rules_codex(env=env) == "error"  # never raises


# --------------------------------------------------------------------------- #
# Runner integration — rules land only after a successful verify
# --------------------------------------------------------------------------- #

class TestRunnerHook:
    def _codex_home(self, tmp_path, monkeypatch) -> Path:
        codex_home = tmp_path / "codexhome"
        codex_home.mkdir()  # exists -> Codex "present" for the runner's best-effort pass
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        return codex_home

    def test_install_places_rules_and_remove_strips(self, tmp_path, monkeypatch):
        codex_home = self._codex_home(tmp_path, monkeypatch)
        runner, _fs, home = make_runner(tmp_path)
        rec = Recorder(home)
        with patched(rec):
            runner.install()

        claude_md = _claude_md(home)
        assert REACH_RULES_START in _read(claude_md)
        assert BODY in _read(claude_md)
        # Codex side landed too (best-effort, Codex present)
        assert _REACH_BEGIN in _read(codex_home / "AGENTS.md")

        with patched(rec):  # remove() places no skills; recorder just returns CPs
            runner.remove()
        assert REACH_RULES_START not in _read(claude_md)
        assert _REACH_BEGIN not in _read(codex_home / "AGENTS.md")

    def test_no_rules_when_hash_verify_fails(self, tmp_path, monkeypatch):
        codex_home = self._codex_home(tmp_path, monkeypatch)
        runner, _fs, home = make_runner(tmp_path)
        rec = Recorder(home, tamper=True)  # skill hashes will mismatch
        with patched(rec):
            with pytest.raises(RuntimeError, match="hash mismatch"):
                runner.install()

        # rules install is AFTER verify, so a failed verify leaves no rules anywhere
        assert not _claude_md(home).exists()
        assert not (codex_home / "AGENTS.md").exists()

    def test_install_fails_when_claude_side_corrupt(self, tmp_path, monkeypatch):
        self._codex_home(tmp_path, monkeypatch)
        runner, _fs, home = make_runner(tmp_path)
        # pre-seed a corrupt CLAUDE.md so the (fatal) Claude rules install raises
        md = _claude_md(home)
        md.write_text(f"{REACH_RULES_START}\norphan, no end\n", encoding="utf-8")
        rec = Recorder(home)
        with patched(rec):
            with pytest.raises(ReachRulesError):
                runner.install()
        # state file never written -> a retry (after the user fixes CLAUDE.md) heals it
        assert not (home / ".claude" / "jacked-reach-state.json").exists()

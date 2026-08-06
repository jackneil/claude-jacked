"""Tests for the `jacked dcr engine` CLI group.

``JACKED_HOME`` is redirected to tmp_path in every test, so the real ~/.claude is
never read or written, and ``codex_preflight`` is patched everywhere, so no test
spawns the real codex CLI.

The load-bearing cases: ``--json`` emits the machine contract and nothing else
(the /dcr command parses it), and `set` on a corrupt config REFUSES rather than
overwriting a file it could not parse.
"""
import json
import os

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from jacked import dcr_settings
from jacked.cli import main

RESOLVE_KEYS = {
    "engine", "model", "effort", "keep_on_claude", "usable", "reason",
    "codex_installed", "codex_logged_in", "codex_path", "schema_path",
}

# chmod-based refusals are meaningless as root.
skip_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores chmod"
)

READY = {"codex_installed": True, "codex_logged_in": True, "reason": None}
NOT_SIGNED_IN = {
    "codex_installed": True,
    "codex_logged_in": False,
    "reason": "Codex CLI is not signed in. Run: codex login",
}


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every command at a throwaway home."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    return tmp_path


def _invoke(args):
    return CliRunner().invoke(main, args)


def test_engine_json_emits_only_the_contract(isolated_home):
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        result = _invoke(["dcr", "engine", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)  # parses => nothing else was printed
    assert set(payload) == RESOLVE_KEYS
    assert payload["engine"] == "claude"
    assert payload["schema_path"] == str(dcr_settings.schema_path())


def test_engine_json_reflects_a_saved_codex_config(isolated_home):
    dcr_settings.write_config(isolated_home, {
        **dcr_settings.DEFAULTS, "engine": "codex", "effort": "max",
    })
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        result = _invoke(["dcr", "engine", "--json"])
    payload = json.loads(result.output)
    assert payload["engine"] == "codex"
    assert payload["effort"] == "max"
    assert payload["usable"] is True


def test_engine_human_output_for_claude(isolated_home):
    result = _invoke(["dcr", "engine"])
    assert result.exit_code == 0
    assert "DCR review engine: Claude (default)" in result.output
    assert "jacked dcr engine set codex" in result.output


def test_engine_human_output_for_codex_ready(isolated_home):
    dcr_settings.write_config(isolated_home, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        result = _invoke(["dcr", "engine"])
    assert result.exit_code == 0
    assert "DCR review engine: Codex (OpenAI)" in result.output
    assert "gpt-5.6-luna" in result.output
    assert "xhigh" in result.output
    assert "Security, Frontend Design" in result.output
    assert "ready" in result.output


def test_engine_human_output_warns_when_codex_is_not_usable(isolated_home):
    dcr_settings.write_config(isolated_home, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight", return_value=NOT_SIGNED_IN):
        result = _invoke(["dcr", "engine"])
    assert result.exit_code == 0
    assert "not usable" in result.output
    assert "codex login" in result.output
    assert "fall back to Claude" in result.output


def test_set_and_clear_round_trip(isolated_home):
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        set_result = _invoke([
            "dcr", "engine", "set", "codex",
            "--model", "gpt-5.6-luna", "--effort", "high",
            "--keep-on-claude", "Security, Frontend Design , Data Integrity",
        ])
    assert set_result.exit_code == 0, set_result.output
    on_disk = json.loads(dcr_settings.config_path(isolated_home).read_text(encoding="utf-8"))
    assert on_disk == {
        "version": 1,
        "engine": "codex",
        "model": "gpt-5.6-luna",
        "effort": "high",
        "keep_on_claude": ["Security", "Frontend Design", "Data Integrity"],
    }
    assert "Codex (OpenAI)" in set_result.output

    clear_result = _invoke(["dcr", "engine", "clear"])
    assert clear_result.exit_code == 0
    assert "Reviews use Claude (default)" in clear_result.output
    assert not dcr_settings.config_path(isolated_home).exists()

    # And the engine reads back as Claude.
    payload = json.loads(_invoke(["dcr", "engine", "--json"]).output)
    assert payload["engine"] == "claude"


def test_set_keeps_existing_values_when_flags_are_omitted(isolated_home):
    dcr_settings.write_config(isolated_home, {
        "engine": "codex",
        "model": "gpt-5.6-luna",
        "effort": "low",
        "keep_on_claude": ["Security"],
        "future_key": "kept",
    })
    result = _invoke(["dcr", "engine", "set", "claude"])
    assert result.exit_code == 0, result.output
    on_disk = json.loads(dcr_settings.config_path(isolated_home).read_text(encoding="utf-8"))
    assert on_disk["engine"] == "claude"
    assert on_disk["effort"] == "low"
    assert on_disk["keep_on_claude"] == ["Security"]
    assert on_disk["future_key"] == "kept"


def test_set_saves_codex_even_when_the_preflight_fails(isolated_home):
    """The runtime falls back to Claude, so a failed preflight warns, never blocks."""
    with patch("jacked.dcr_settings.codex_preflight", return_value=NOT_SIGNED_IN):
        result = _invoke(["dcr", "engine", "set", "codex"])
    assert result.exit_code == 0, result.output
    assert dcr_settings.read_config(isolated_home)["engine"] == "codex"
    assert "not usable" in result.output
    assert "fall back to Claude" in result.output


def test_set_on_a_corrupt_config_exits_1_and_leaves_the_file_untouched(isolated_home):
    path = dcr_settings.config_path(isolated_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")

    result = _invoke(["dcr", "engine", "set", "codex"])

    assert result.exit_code == 1
    assert path.read_text(encoding="utf-8") == "{ not valid json"
    assert "Nothing was written" in result.output
    # Rich hard-wraps at the console width, so rejoin before matching the path.
    assert str(path) in result.output.replace("\n", "")


def test_engine_json_on_a_corrupt_config_degrades_to_claude(isolated_home):
    path = dcr_settings.config_path(isolated_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")

    result = _invoke(["dcr", "engine", "--json"])

    assert result.exit_code == 0
    # `result.output` is the MERGED stdout+stderr stream (click >= 8.2), which is
    # the stream the /dcr command actually reads: Claude Code's Bash tool merges
    # stderr into stdout. A log line about the corrupt file would break this
    # parse, so the reason travels inside the JSON instead.
    payload = json.loads(result.output)
    assert result.stderr == "", f"stderr pollutes the --json contract: {result.stderr!r}"
    assert payload["engine"] == "claude"
    assert "is not valid JSON" in payload["reason"]
    assert payload["reason"].endswith("; using Claude until it is fixed")


@skip_as_root
def test_engine_json_on_an_unreadable_file_still_emits_only_json(isolated_home):
    """Same merged-stream contract for a permission failure, and the reason names
    the REAL cause instead of blaming the JSON syntax."""
    dcr_settings.write_config(isolated_home, dcr_settings.DEFAULTS)
    path = dcr_settings.config_path(isolated_home)
    path.chmod(0o000)
    try:
        result = _invoke(["dcr", "engine", "--json"])
    finally:
        path.chmod(0o600)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert result.stderr == ""
    assert "Permission denied" in payload["reason"]
    assert "is not valid JSON" not in payload["reason"]


def test_engine_human_output_surfaces_a_substituted_value(isolated_home):
    """A hand-edited value that jacked cannot run with is reported, not silently
    swapped: the user needs to know why their setting stopped taking effect."""
    path = dcr_settings.config_path(isolated_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**dcr_settings.DEFAULTS, "effort": "turbo"}),
                    encoding="utf-8")

    result = _invoke(["dcr", "engine"])

    assert result.exit_code == 0
    assert "effort" in result.output
    assert "xhigh" in result.output


def test_invalid_effort_is_rejected_by_click(isolated_home):
    result = _invoke(["dcr", "engine", "set", "codex", "--effort", "turbo"])
    assert result.exit_code == 2
    assert "turbo" in result.output
    assert not dcr_settings.config_path(isolated_home).exists()


def test_invalid_engine_is_rejected_by_click(isolated_home):
    result = _invoke(["dcr", "engine", "set", "gemini"])
    assert result.exit_code == 2
    assert not dcr_settings.config_path(isolated_home).exists()


def test_empty_model_is_rejected_before_the_file_is_written(isolated_home):
    result = _invoke(["dcr", "engine", "set", "codex", "--model", "   "])
    assert result.exit_code == 1
    assert not dcr_settings.config_path(isolated_home).exists()


def test_clear_is_safe_when_no_config_exists(isolated_home):
    result = _invoke(["dcr", "engine", "clear"])
    assert result.exit_code == 0
    assert "Reviews use Claude (default)" in result.output


# --------------------------------------------------------------------------- #
# The empty-carve-out warning
# --------------------------------------------------------------------------- #

def test_emptying_the_carve_out_list_on_codex_warns_loudly(isolated_home):
    """Allowed (an explicit override), but it moves the Security lens onto Codex,
    so it must never happen quietly."""
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        result = _invoke(["dcr", "engine", "set", "codex", "--keep-on-claude", ""])

    assert result.exit_code == 0, result.output
    assert dcr_settings.read_config(isolated_home)["keep_on_claude"] == []
    # Rich hard-wraps at the console width, so collapse whitespace before matching.
    flat = " ".join(result.output.split())
    assert "Warning: the keep-on-Claude list is empty." in flat
    assert "EVERY lens, including Security, will run on Codex." in flat


def test_no_carve_out_warning_while_the_engine_is_claude(isolated_home):
    result = _invoke(["dcr", "engine", "set", "claude", "--keep-on-claude", ""])
    assert result.exit_code == 0
    assert "EVERY lens" not in result.output


def test_no_carve_out_warning_when_lenses_are_kept(isolated_home):
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        result = _invoke([
            "dcr", "engine", "set", "codex", "--keep-on-claude", "Security",
        ])
    assert result.exit_code == 0
    assert "EVERY lens" not in result.output


# --------------------------------------------------------------------------- #
# Stale stored values and filesystem refusals
# --------------------------------------------------------------------------- #

def test_set_switches_engine_even_with_a_stale_invalid_stored_effort(isolated_home):
    """The lockout regression: a hand-edited effort typo used to fail validation
    on the way out, so the user could not switch back to Claude — the very escape
    hatch from a broken codex setup."""
    path = dcr_settings.config_path(isolated_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**dcr_settings.DEFAULTS, "engine": "codex", "effort": "turbo"}),
        encoding="utf-8",
    )

    result = _invoke(["dcr", "engine", "set", "claude"])

    assert result.exit_code == 0, result.output
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["engine"] == "claude"
    assert on_disk["effort"] == dcr_settings.DEFAULTS["effort"], "the typo self-heals"


@skip_as_root
def test_set_on_an_unwritable_home_fails_friendly_without_a_traceback(isolated_home):
    isolated_home.chmod(0o500)
    try:
        result = _invoke(["dcr", "engine", "set", "claude"])
    finally:
        isolated_home.chmod(0o700)

    assert result.exit_code == 1
    flat = result.output.replace("\n", "")
    assert str(dcr_settings.config_path(isolated_home)) in flat
    assert "permissions" in flat
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, PermissionError)


@skip_as_root
def test_clear_on_an_unwritable_dir_fails_friendly_and_never_claims_success(
    isolated_home,
):
    dcr_settings.write_config(isolated_home, dcr_settings.DEFAULTS)
    claude_dir = isolated_home / ".claude"
    claude_dir.chmod(0o500)
    try:
        result = _invoke(["dcr", "engine", "clear"])
    finally:
        claude_dir.chmod(0o700)

    assert result.exit_code == 1
    assert "cleared" not in result.output
    assert "Traceback" not in result.output
    assert str(dcr_settings.config_path(isolated_home)) in result.output.replace("\n", "")
    assert dcr_settings.config_path(isolated_home).exists()


def test_set_surfaces_a_post_write_verification_failure_without_a_traceback(
    isolated_home,
):
    """write_config's own verification raises DcrSettingsUnreadableError AFTER the
    read; the CLI must catch that too, not die with a traceback."""
    with patch("jacked.dcr_settings.json.loads",
               side_effect=json.JSONDecodeError("boom", "x", 0)):
        result = _invoke(["dcr", "engine", "set", "claude"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "Nothing was written" in result.output

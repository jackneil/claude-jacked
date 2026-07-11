"""Click-runner tests for the `jacked reach` verb group.

Fully offline: `jacked.cli._reach_runner` is patched to yield a mock runner, so
no DB is opened and no uv/agent-reach/npm/git process is spawned.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from jacked.cli import main

_STATUS_FIXTURE = {
    "installed": True,
    "installed_sha": "e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
    "pin_sha": "e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
    "authoritative_sha": "e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
    "sha_matches": True,
    "drift": [],
    "override": {"active": False, "sha": None, "ack": False, "at": None},
    "doctor": {"channels": [{"channel": "twitter", "active_backend": "twitter-cli", "ok": True}]},
    "doctor_error": None,
    "pin": {"version_label": "1.5.0", "vetted_at": "2026-07-11", "short_sha": "e825f6740d24"},
}


def _patch_runner(runner: MagicMock):
    @contextmanager
    def _cm():
        yield runner

    return patch("jacked.cli._reach_runner", _cm)


def test_override_ref_without_unvetted_ok_exits_nonzero():
    runner = MagicMock()
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "update", "--override-ref", "main"])
    assert result.exit_code != 0
    assert "unvetted" in result.output.lower()
    assert "--unvetted-ok" in result.output
    # Must never resolve/install the unvetted ref without acknowledgement.
    runner.set_override.assert_not_called()
    runner.update.assert_not_called()


def test_status_json_passes_through():
    runner = MagicMock()
    runner.status.return_value = dict(_STATUS_FIXTURE)
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == _STATUS_FIXTURE


def test_status_human_render_is_readable():
    runner = MagicMock()
    runner.status.return_value = dict(_STATUS_FIXTURE)
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "status"])
    assert result.exit_code == 0
    assert "agent-reach integration" in result.output
    assert "twitter" in result.output  # doctor channel table rendered


def test_enable_channel_unknown_surfaces_runner_error():
    runner = MagicMock()
    runner.enable_channel.side_effect = RuntimeError(
        "unknown channel 'bogus'; valid channels: reddit, search, twitter"
    )
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "enable-channel", "bogus"])
    assert result.exit_code != 0
    assert "unknown channel" in result.output


def test_remove_without_yes_prompts_and_aborts():
    runner = MagicMock()
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "remove"], input="n\n")
    assert result.exit_code != 0  # click.confirm(abort=True) -> Abort
    assert "Remove agent-reach" in result.output
    runner.remove.assert_not_called()


def test_remove_with_yes_skips_prompt():
    runner = MagicMock()
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "remove", "--yes"])
    assert result.exit_code == 0
    runner.remove.assert_called_once()


def test_enable_channel_prints_configure_hint():
    runner = MagicMock()
    runner.enable_channel.return_value = "Twitter/X enabled. Authenticate with ..."
    with _patch_runner(runner):
        result = CliRunner().invoke(main, ["reach", "enable-channel", "twitter"])
    assert result.exit_code == 0
    assert "Authenticate with" in result.output
    runner.enable_channel.assert_called_once_with("twitter")

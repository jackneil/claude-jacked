"""Statusline coverage: the stdlib renderer, the enable/disable engine, the CLI
install/uninstall parity, the dashboard Features toggle, and the frontend toasts.

Isolation contract. Every test runs against a tmp home.

* Renderer tests call ``statusline.render(payload, home=..., now=...)`` with an
  EXPLICIT home and an EXPLICIT ``now``, so no test reads the real
  ``~/.claude.json`` and no assertion depends on wall-clock time. The tmp home
  starts without a ``.claude.json`` and without the caveman flag, so the account
  and badge segments are empty unless the test creates them.
* Engine, CLI, and route tests set ``$JACKED_HOME`` to the same tmp home the
  assertions read, so ``statusline_setup``, the CLI installer, and the route all
  converge on one ``settings.json``.
* The full ``jacked install`` / ``jacked uninstall`` runs stub only the steps
  that reach OUTSIDE the tmp home (the ``claude mcp`` shell-out, the Codex
  uninstall pass, the skill-packs uninstall pass). Everything statusline-related
  runs for real.

Nothing here touches the real ``~/.claude``, the network, or npx.
"""

import io
import json
import os
import sys
import time
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from rich.console import Console
from starlette.testclient import TestClient

import jacked.cli as cli
from jacked import guardrails, statusline, statusline_setup
from jacked.api.routes import features
from jacked.api.routes.features import router
from jacked.cli import main
from jacked.memory.settings_io import SettingsUnreadableError, settings_path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "jacked" / "data"

# ANSI literals, spelled out rather than imported: a silent constant rename in
# the renderer must fail here, not follow along.
RESET = "\033[0m"
BOLD_CYAN = "\033[1;36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
GREEN = "\033[32m"
RED = "\033[31m"
CAVE = "\033[38;5;172m"
SEP = " \033[2m|\033[0m "
ARROW = "→"
MIDDOT = "·"
EM_DASH = "—"

# Fixed epoch so every reset-time assertion is deterministic. Expected strings
# are derived with time.strftime over the SAME epoch, so the tests are
# timezone-independent.
NOW = 1750000000.0

FOREIGN = {"type": "command", "command": 'bash "$HOME/.claude/mine.sh"'}

INSTALL_ARGS = [
    "install", "--force", "--no-tray", "--no-codex", "--no-rules", "--no-packs",
]


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture
def home(tmp_path):
    """Tmp home with a .claude dir, no .claude.json, no caveman flag."""
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    return h


def _render(payload, home_dir, now=NOW):
    return statusline.render(payload, home=str(home_dir), now=now)


def _full_payload():
    """A payload with every segment populated (the happy path)."""
    return {
        "model": {"display_name": "Fable 5"},
        "effort": {"level": "xhigh"},
        "fast_mode": True,
        "context_window": {
            "used_percentage": 63.29999999999999,
            "context_window_size": 1000000,
            "current_usage": {
                "input_tokens": 600000,
                "cache_creation_input_tokens": 20000,
                "cache_read_input_tokens": 10000,
                "output_tokens": 3000,
            },
        },
        "rate_limits": {
            "five_hour": {
                "used_percentage": 7.000000000000001,
                "resets_at": NOW + 3600,
            },
            "seven_day": {
                "used_percentage": 88.0,
                "resets_at": NOW + 3 * 86400,
            },
        },
    }


def _write_account(home_dir, **account):
    (home_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": account}), encoding="utf-8"
    )


def _cache_path(home_dir):
    return home_dir / ".claude" / "statusline-account.cache"


def _bump_mtime(path, seconds=300):
    """Push a file's mtime into the future so mtime comparisons are decisive."""
    future = time.time() + seconds
    os.utime(path, (future, future))


def _settings(home_dir) -> dict:
    p = settings_path(home_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write_settings_file(home_dir, data: dict) -> Path:
    p = settings_path(home_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _ours_entry() -> dict:
    return {"type": "command", "command": statusline_setup.build_command()}


# --------------------------------------------------------------------------- #
# A. Renderer
# --------------------------------------------------------------------------- #

def test_full_payload_renders_every_segment_in_order(home):
    line = _render(_full_payload(), home)
    expected = SEP.join([
        f"{BOLD_CYAN}Fable 5{RESET} {YELLOW}[xhigh]{RESET} {MAGENTA}[fast]{RESET}",
        f"ctx {YELLOW}63%{RESET} (633k/1.0M)",
        f"5h {GREEN}7%{RESET}{ARROW}"
        + time.strftime("%H:%M", time.localtime(NOW + 3600)),
        f"7d {RED}88%{RESET}{ARROW}"
        + time.strftime("%a %H:%M", time.localtime(NOW + 3 * 86400)),
    ])
    assert line == expected


def test_full_payload_carries_the_ansi_codes_and_dim_pipe_separator(home):
    line = _render(_full_payload(), home)
    for code in (BOLD_CYAN, YELLOW, MAGENTA, GREEN, RED, RESET):
        assert code in line
    assert line.count(SEP) == 3  # four segments, three separators


def test_fast_mode_false_drops_the_fast_badge(home):
    payload = _full_payload()
    payload["fast_mode"] = False
    line = _render(payload, home)
    assert f"{MAGENTA}[fast]{RESET}" not in line
    assert f"{YELLOW}[xhigh]{RESET}" in line


def test_missing_effort_drops_only_the_effort_badge(home):
    payload = _full_payload()
    del payload["effort"]
    line = _render(payload, home)
    assert "[xhigh]" not in line
    assert f"{BOLD_CYAN}Fable 5{RESET}" in line


@pytest.mark.parametrize("raw,pct,color", [
    (59.4, 59, GREEN),
    (59.5, 60, YELLOW),
    (84.4, 84, YELLOW),
    (84.6, 85, RED),
    (0, 0, GREEN),
])
def test_pressure_color_keys_off_the_rounded_value(home, raw, pct, color):
    line = _render({"context_window": {"used_percentage": raw}}, home)
    assert line == f"ctx {color}{pct}%{RESET}"


@pytest.mark.parametrize("n,text", [
    (0, "0"),
    (512, "512"),
    (999, "999"),
    (1000, "1k"),
    (80000, "80k"),
    (999499, "999k"),
    (999500, "1.0M"),
    (1000000, "1.0M"),
    (1500000, "1.5M"),
])
def test_fmt_tokens_boundaries(n, text):
    assert statusline._fmt_tokens(n) == text


@pytest.mark.parametrize("value,expected", [
    (7.000000000000001, 7),
    (59.5, 60),
    (84.6, 85),
    (0, 0),
    (100.0, 100),
    (None, None),
    ("7", None),
    ("", None),
    (True, None),
    (False, None),
])
def test_round_pct(value, expected):
    assert statusline._round_pct(value) == expected


def test_null_current_usage_renders_the_percentage_alone(home):
    line = _render(
        {"context_window": {
            "used_percentage": 40,
            "context_window_size": 200000,
            "current_usage": None,
        }},
        home,
    )
    assert line == f"ctx {GREEN}40%{RESET}"
    assert "(" not in line


def test_missing_context_window_size_renders_the_percentage_alone(home):
    line = _render(
        {"context_window": {
            "used_percentage": 40,
            "current_usage": {"input_tokens": 10},
        }},
        home,
    )
    assert line == f"ctx {GREEN}40%{RESET}"


def test_missing_rate_limits_drops_both_window_segments(home):
    payload = _full_payload()
    del payload["rate_limits"]
    line = _render(payload, home)
    assert "5h " not in line
    assert "7d " not in line
    assert "ctx " in line


def test_missing_used_percentage_drops_the_ctx_segment(home):
    payload = _full_payload()
    del payload["context_window"]["used_percentage"]
    line = _render(payload, home)
    assert "ctx " not in line
    assert "5h " in line


def test_missing_model_drops_the_model_segment(home):
    payload = _full_payload()
    del payload["model"]
    line = _render(payload, home)
    assert BOLD_CYAN not in line
    assert line.startswith("ctx ")


def test_rate_limit_without_resets_at_renders_percentage_only(home):
    line = _render({"rate_limits": {"five_hour": {"used_percentage": 12}}}, home)
    assert line == f"5h {GREEN}12%{RESET}"
    assert ARROW not in line


def test_empty_payload_renders_an_empty_line(home):
    assert _render({}, home) == ""


@pytest.mark.parametrize("payload", [None, "x", [1, 2, 3], 7, True])
def test_non_dict_payload_never_crashes(home, payload):
    line = _render(payload, home)
    assert isinstance(line, str)
    assert line == ""


def test_fmt_reset_under_24h_uses_the_clock_form():
    epoch = NOW + 3600
    assert statusline._fmt_reset(epoch, NOW) == time.strftime(
        "%H:%M", time.localtime(epoch)
    )


def test_fmt_reset_at_or_over_24h_uses_the_weekday_form():
    epoch = NOW + 86400
    assert statusline._fmt_reset(epoch, NOW) == time.strftime(
        "%a %H:%M", time.localtime(epoch)
    )


@pytest.mark.parametrize("epoch", [None, "later", "", True, False, [1]])
def test_fmt_reset_rejects_non_numeric(epoch):
    assert statusline._fmt_reset(epoch, NOW) == ""


@pytest.mark.parametrize("usage,total", [
    ({"input_tokens": 1, "output_tokens": 2}, 3),
    ({"input_tokens": None, "output_tokens": 5}, 5),
    ({"input_tokens": "big", "output_tokens": 5}, 5),
    ({}, 0),
])
def test_sum_usage_tolerates_partial_counters(usage, total):
    assert statusline._sum_usage(usage) == total


def test_sum_usage_returns_none_for_non_dicts():
    assert statusline._sum_usage(None) is None
    assert statusline._sum_usage("x") is None


@pytest.mark.parametrize("raw,label", [
    ("default_claude_max_5x", "Max 5x"),
    ("default_claude_max_20x", "Max 20x"),
    ("default_claude_pro", "Pro"),
    ("default_claude_free", "Free"),
    ("default_claude_enterprise_beta", "enterprise_beta"),
    ("custom_thing", "custom_thing"),
    (None, ""),
    ("", ""),
])
def test_tier_label(raw, label):
    assert statusline._tier_label(raw) == label


def test_account_segment_falls_back_to_the_org_tier(home):
    _write_account(
        home,
        emailAddress="me@co.com",
        organizationName="MyOrg",
        userRateLimitTier=None,
        organizationRateLimitTier="default_claude_max_20x",
    )
    assert _render({}, home) == f"me@co.com {MIDDOT} MyOrg {MIDDOT} Max 20x"


def test_account_segment_prefers_the_user_tier(home):
    _write_account(
        home,
        emailAddress="me@co.com",
        organizationName="MyOrg",
        userRateLimitTier="default_claude_max_5x",
        organizationRateLimitTier="default_claude_max_20x",
    )
    assert _render({}, home) == f"me@co.com {MIDDOT} MyOrg {MIDDOT} Max 5x"


def test_account_segment_passes_an_unknown_tier_through_stripped(home):
    _write_account(
        home,
        emailAddress="me@co.com",
        userRateLimitTier="default_claude_team_preview",
    )
    assert _render({}, home) == f"me@co.com {MIDDOT} team_preview"


def test_account_segment_is_empty_without_claude_json(home):
    assert not (home / ".claude.json").exists()
    assert _render({}, home) == ""


def test_account_segment_writes_the_mtime_cache(home):
    _write_account(home, emailAddress="me@co.com")
    segment = _render({}, home)
    assert segment == "me@co.com"
    assert _cache_path(home).read_text(encoding="utf-8") == segment + "\n"


def test_account_cache_refreshes_when_claude_json_moves_forward(home):
    _write_account(home, emailAddress="old@co.com")
    assert _render({}, home) == "old@co.com"

    _write_account(home, emailAddress="new@co.com")
    _bump_mtime(home / ".claude.json")

    assert _render({}, home) == "new@co.com"


def test_stale_claude_json_short_circuits_to_the_cache(home):
    """A cache newer than .claude.json is used verbatim: the sentinel proves the
    multi-MB file was never re-parsed."""
    _write_account(home, emailAddress="disk@co.com")
    _cache_path(home).write_text("SENTINEL-FROM-CACHE\n", encoding="utf-8")
    _bump_mtime(_cache_path(home))

    assert _render({}, home) == "SENTINEL-FROM-CACHE"


def test_corrupt_claude_json_yields_no_account_segment(home):
    (home / ".claude.json").write_text("{ not json", encoding="utf-8")
    assert _render({}, home) == ""


@pytest.mark.parametrize("content,badge", [
    ("", "[CAVEMAN]"),
    ("full", "[CAVEMAN]"),
    ("full\n", "[CAVEMAN]"),
    ("lite", "[CAVEMAN:LITE]"),
    ("lite\n", "[CAVEMAN:LITE]"),
])
def test_caveman_badge(home, content, badge):
    (home / ".claude" / ".caveman-active").write_text(content, encoding="utf-8")
    assert _render({}, home) == f"{CAVE}{badge}{RESET}"


def test_no_caveman_badge_when_the_flag_is_absent(home):
    assert not (home / ".claude" / ".caveman-active").exists()
    assert "CAVEMAN" not in _render(_full_payload(), home)


def test_account_and_caveman_segments_come_last_in_that_order(home):
    _write_account(home, emailAddress="me@co.com")
    (home / ".claude" / ".caveman-active").write_text("lite", encoding="utf-8")
    line = _render({"model": {"display_name": "Opus 5"}}, home)
    assert line.split(SEP) == [
        f"{BOLD_CYAN}Opus 5{RESET}",
        "me@co.com",
        f"{CAVE}[CAVEMAN:LITE]{RESET}",
    ]


@pytest.mark.parametrize("raw", [
    "",
    "   \n",
    "not json at all",
    "{ half",
    "[1, 2, 3]",
    '{"model": {"display_name": "Opus 5"}}',
])
def test_main_always_exits_zero_and_prints_exactly_one_line(
    home, monkeypatch, capsys, raw
):
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

    assert statusline.main() == 0

    out = capsys.readouterr().out
    assert out.endswith("\n")
    assert out.count("\n") == 1


def test_main_renders_a_valid_payload_from_stdin(home, monkeypatch, capsys):
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"model": {"display_name": "Opus 5"}}))
    )

    assert statusline.main() == 0
    assert capsys.readouterr().out == f"{BOLD_CYAN}Opus 5{RESET}\n"


# --------------------------------------------------------------------------- #
# B. Engine: statusline_setup
# --------------------------------------------------------------------------- #

def test_build_command_is_an_absolute_quoted_module_invocation():
    command = statusline_setup.build_command()
    assert command.startswith('"')
    assert sys.executable in command
    assert command.endswith('" -m jacked.statusline')
    assert statusline_setup.is_ours(command)


@pytest.mark.parametrize("command", [
    'bash "$HOME/.claude/statusline.sh"',
    "ccusage statusline",
    None,
    {"command": '"/usr/bin/python3" -m jacked.statusline'},
    "",
    123,
])
def test_is_ours_rejects_anything_without_the_marker(command):
    assert statusline_setup.is_ours(command) is False


@pytest.mark.parametrize("build,expected", [
    (lambda: {}, "absent"),
    (lambda: {"statusLine": None}, "absent"),
    (lambda: {"statusLine": _ours_entry()}, "ours"),
    (lambda: {"statusLine": {"command": statusline_setup.build_command()}}, "ours"),
    (lambda: {"statusLine": dict(FOREIGN)}, "foreign"),
    (lambda: {"statusLine": "bash mine.sh"}, "foreign"),   # present but not a dict
    (lambda: {"statusLine": []}, "foreign"),
    (lambda: {"statusLine": {}}, "foreign"),               # dict, no command
])
def test_entry_state(build, expected):
    assert statusline_setup.entry_state(build()) == expected


def test_ensure_entry_is_idempotent():
    settings: dict = {}
    command = statusline_setup.build_command()
    assert statusline_setup.ensure_entry(settings, command) is True
    assert settings["statusLine"] == {"type": "command", "command": command}
    assert statusline_setup.ensure_entry(settings, command) is False


def test_ensure_entry_overwrites_a_stale_jacked_command():
    settings = {"statusLine": {"type": "command", "command": '"/old/python" -m jacked.statusline'}}
    assert statusline_setup.ensure_entry(settings, statusline_setup.build_command()) is True
    assert settings["statusLine"] == _ours_entry()


def test_remove_entry_refuses_a_foreign_entry():
    settings = {"statusLine": dict(FOREIGN)}
    assert statusline_setup.remove_entry(settings) is False
    assert settings["statusLine"] == FOREIGN


def test_remove_entry_deletes_our_own():
    settings = {"statusLine": _ours_entry(), "other": 1}
    assert statusline_setup.remove_entry(settings) is True
    assert "statusLine" not in settings
    assert settings["other"] == 1


def test_remove_entry_on_absent_is_a_noop():
    settings: dict = {}
    assert statusline_setup.remove_entry(settings) is False
    assert settings == {}


def test_enable_registers_on_empty_settings(home):
    result = statusline_setup.enable(home)

    assert result == {"changed": True, "took_over_foreign": False}
    assert _settings(home)["statusLine"] == _ours_entry()
    state = statusline_setup.load_state(home)
    assert state["state"] == "enabled"
    assert state["previous"] is None


def test_enable_takes_over_a_foreign_entry_and_saves_it_verbatim(home):
    _write_settings_file(home, {"statusLine": dict(FOREIGN), "env": {"A": "1"}})

    result = statusline_setup.enable(home)

    assert result == {"changed": True, "took_over_foreign": True}
    assert statusline_setup.load_state(home)["previous"] == FOREIGN
    after = _settings(home)
    assert after["statusLine"] == _ours_entry()
    assert after["env"] == {"A": "1"}  # unrelated settings survive


def test_enable_is_idempotent(home):
    statusline_setup.enable(home)
    before = _settings(home)

    second = statusline_setup.enable(home)

    assert second == {"changed": False, "took_over_foreign": False}
    assert _settings(home) == before


def test_disable_restores_the_saved_foreign_entry(home):
    _write_settings_file(home, {"statusLine": dict(FOREIGN)})
    statusline_setup.enable(home)

    result = statusline_setup.disable(home)

    assert result == {"changed": True, "restored_previous": True}
    assert _settings(home)["statusLine"] == FOREIGN
    state = statusline_setup.load_state(home)
    assert state["state"] == "disabled"
    assert state["previous"] is None


def test_disable_without_a_previous_drops_the_key(home):
    statusline_setup.enable(home)

    result = statusline_setup.disable(home)

    assert result == {"changed": True, "restored_previous": False}
    assert "statusLine" not in _settings(home)
    assert statusline_setup.load_state(home)["state"] == "disabled"


def test_disable_when_nothing_is_registered_only_records_the_preference(home):
    _write_settings_file(home, {"statusLine": dict(FOREIGN)})

    result = statusline_setup.disable(home)

    assert result == {"changed": False, "restored_previous": False}
    assert _settings(home)["statusLine"] == FOREIGN  # foreign entry untouched
    assert statusline_setup.load_state(home)["state"] == "disabled"


@pytest.mark.parametrize("op", ["enable", "disable"])
def test_engine_refuses_corrupt_settings_without_clobbering(home, op):
    path = settings_path(home)
    path.write_text("{ not json", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(SettingsUnreadableError):
        getattr(statusline_setup, op)(home)

    assert path.read_bytes() == before


def test_sync_on_install_registers_into_a_fresh_dict(home):
    settings: dict = {}
    assert statusline_setup.sync_on_install(home, settings) == "installed"
    assert settings["statusLine"] == _ours_entry()


def test_sync_on_install_migrates_a_stale_jacked_command(home):
    settings = {
        "statusLine": {"type": "command", "command": '"/old/python" -m jacked.statusline'}
    }
    assert statusline_setup.sync_on_install(home, settings) == "migrated"
    assert settings["statusLine"] == _ours_entry()


def test_sync_on_install_is_unchanged_when_already_current(home):
    settings = {"statusLine": _ours_entry()}
    assert statusline_setup.sync_on_install(home, settings) == "unchanged"
    assert settings["statusLine"] == _ours_entry()


def test_sync_on_install_skips_a_foreign_entry(home):
    settings = {"statusLine": dict(FOREIGN)}
    snapshot = json.dumps(settings, sort_keys=True)
    assert statusline_setup.sync_on_install(home, settings) == "skipped_foreign"
    assert json.dumps(settings, sort_keys=True) == snapshot


def test_sync_on_install_never_overrides_an_explicit_disable(home):
    statusline_setup.save_state(
        home, {"version": 1, "state": "disabled", "previous": None}
    )
    settings: dict = {}
    assert statusline_setup.sync_on_install(home, settings) == "skipped_disabled"
    assert settings == {}


def test_remove_on_uninstall_restores_a_saved_previous(home):
    statusline_setup.save_state(
        home, {"version": 1, "state": "enabled", "previous": dict(FOREIGN)}
    )
    settings = {"statusLine": _ours_entry()}

    assert statusline_setup.remove_on_uninstall(home, settings) is True

    assert settings["statusLine"] == FOREIGN
    assert not statusline_setup.state_path(home).exists()


def test_remove_on_uninstall_deletes_the_key_without_a_previous(home):
    settings = {"statusLine": _ours_entry()}
    assert statusline_setup.remove_on_uninstall(home, settings) is True
    assert "statusLine" not in settings


def test_remove_on_uninstall_leaves_a_foreign_entry_alone(home):
    statusline_setup.save_state(
        home, {"version": 1, "state": "enabled", "previous": None}
    )
    settings = {"statusLine": dict(FOREIGN)}

    assert statusline_setup.remove_on_uninstall(home, settings) is False

    assert settings["statusLine"] == FOREIGN
    assert not statusline_setup.state_path(home).exists()  # state still cleaned up


def test_disable_restores_previous_even_when_our_entry_was_removed_out_of_band(home):
    """The backup must survive an out-of-band removal of our entry.

    enable() over a foreign entry saves it; if something else then strips
    the statusLine key entirely, disable() still owes the user their old
    statusline back (the docstring promise), not a silent backup wipe.
    """
    _write_settings_file(home, {"statusLine": dict(FOREIGN)})
    statusline_setup.enable(home)
    settings = _settings(home)
    del settings["statusLine"]  # out-of-band removal
    _write_settings_file(home, settings)

    result = statusline_setup.disable(home)

    assert result == {"changed": True, "restored_previous": True}
    assert _settings(home)["statusLine"] == FOREIGN
    assert statusline_setup.load_state(home)["previous"] is None


def test_disable_never_clobbers_a_newer_foreign_entry_with_the_backup(home):
    """A foreign entry the user set AFTER our takeover supersedes the backup."""
    _write_settings_file(home, {"statusLine": dict(FOREIGN)})
    statusline_setup.enable(home)
    newer = {"type": "command", "command": "bash their-new-one.sh"}
    settings = _settings(home)
    settings["statusLine"] = dict(newer)
    _write_settings_file(home, settings)

    result = statusline_setup.disable(home)

    assert result == {"changed": False, "restored_previous": False}
    assert _settings(home)["statusLine"] == newer
    assert statusline_setup.load_state(home)["previous"] is None


def test_remove_on_uninstall_restores_previous_when_the_key_is_absent(home):
    statusline_setup.save_state(
        home, {"version": 1, "state": "enabled", "previous": dict(FOREIGN)}
    )
    settings: dict = {}

    assert statusline_setup.remove_on_uninstall(home, settings) is True

    assert settings["statusLine"] == FOREIGN


def test_remove_on_uninstall_never_clobbers_a_newer_foreign_with_the_backup(home):
    statusline_setup.save_state(
        home, {"version": 1, "state": "enabled", "previous": dict(FOREIGN)}
    )
    newer = {"type": "command", "command": "bash their-new-one.sh"}
    settings = {"statusLine": dict(newer)}

    assert statusline_setup.remove_on_uninstall(home, settings) is False

    assert settings["statusLine"] == newer


def test_account_cache_with_an_equal_mtime_reparses(home):
    """Equal mtimes must NOT count as fresh: a .claude.json rewritten in the
    same coarse-filesystem tick as the cache write would otherwise pin a stale
    account after a switch. Strict-newer fails in the safe direction."""
    _write_account(home, emailAddress="old@x.com")
    _render({}, home)  # populates the cache
    _write_account(home, emailAddress="new@x.com")
    cache = _cache_path(home)
    stamp = os.path.getmtime(home / ".claude.json")
    os.utime(cache, (stamp, stamp))  # cache mtime == .claude.json mtime

    line = _render({}, home)

    assert "new@x.com" in line
    assert "old@x.com" not in line


def test_main_debug_env_prints_the_traceback_to_stderr(monkeypatch, capsys):
    """JACKED_STATUSLINE_DEBUG=1 surfaces renderer bugs on stderr (Claude Code
    only reads stdout) while the exit code stays 0."""
    monkeypatch.setenv("JACKED_STATUSLINE_DEBUG", "1")
    monkeypatch.setattr(statusline, "render", _raise_boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert statusline.main() == 0

    err = capsys.readouterr().err
    assert "Traceback" in err
    assert "boom" in err


def _raise_boom(*args, **kwargs):
    raise RuntimeError("boom")


@pytest.mark.parametrize("raw,expected_state", [
    (None, ""),                                        # no file at all
    ("{ not json", ""),                                # corrupt
    ("[]", ""),                                        # valid JSON, wrong type
    ('"enabled"', ""),                                 # valid JSON, wrong type
    ('{"state": "bogus"}', ""),                        # unknown state value
    ('{"state": "enabled", "previous": "nope"}', "enabled"),  # non-dict previous
    ('{"state": "disabled"}', "disabled"),
])
def test_load_state_normalizes_every_broken_shape(home, raw, expected_state):
    if raw is not None:
        path = statusline_setup.state_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")

    state = statusline_setup.load_state(home)

    assert set(state) == {"version", "state", "previous"}
    assert state["version"] == statusline_setup.STATE_VERSION
    assert state["state"] == expected_state
    assert state["previous"] is None


def test_save_state_round_trips_through_load_state(home):
    statusline_setup.save_state(
        home, {"version": 1, "state": "enabled", "previous": dict(FOREIGN)}
    )
    assert statusline_setup.load_state(home) == {
        "version": 1, "state": "enabled", "previous": FOREIGN,
    }


@pytest.mark.parametrize("state,expected", [
    (None, True),        # unset -> default on
    ("enabled", True),
    ("disabled", False),
])
def test_is_effectively_enabled(home, state, expected):
    if state is not None:
        statusline_setup.save_state(
            home, {"version": 1, "state": state, "previous": None}
        )
    assert statusline_setup.is_effectively_enabled(home) is expected


# --------------------------------------------------------------------------- #
# C. CLI install / uninstall parity + the `jacked statusline` group
# --------------------------------------------------------------------------- #

@pytest.fixture
def clienv(tmp_path, monkeypatch):
    """Isolated home + buffered Rich console + no shell-outs past the tmp home."""
    home_dir = tmp_path / "home"
    (home_dir / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(home_dir))

    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False))
    # The only install/uninstall step that shells out to the real `claude` CLI.
    monkeypatch.setattr(cli, "_install_chrome_devtools_mcp", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_remove_chrome_devtools_mcp", lambda *a, **k: False)
    # guardrails.deploy_templates resolves its destination from Path.home(), NOT
    # from $JACKED_HOME, so an unstubbed install rewrites the real
    # ~/.claude/jacked-guardrails + ~/.claude/jacked-hooks. Unrelated to the
    # statusline; stub it so this suite stays inside tmp_path.
    monkeypatch.setattr(
        guardrails, "deploy_templates",
        lambda force=False: {"guardrails": [], "hooks": []},
    )

    return SimpleNamespace(home=home_dir, buf=buf, monkeypatch=monkeypatch)


def _stub_uninstall_externals(env):
    """Neutralize the two uninstall passes that reach outside the tmp home."""
    import jacked.codex.installer as cdx
    from jacked import packs as packs_mod

    env.monkeypatch.setattr(
        cdx, "uninstall_codex", lambda *a, **k: {"removed": [], "skipped": []}
    )
    env.monkeypatch.setattr(packs_mod, "effective_enabled_pack_names", lambda *a, **k: [])
    env.monkeypatch.setattr(packs_mod, "enabled_pack_names", lambda *a, **k: [])


def _invoke(env, args):
    """Run a CLI command; return (result, click stdout + Rich console output)."""
    env.buf.truncate(0)
    env.buf.seek(0)
    result = CliRunner().invoke(main, args)
    return result, result.output + env.buf.getvalue()


def test_install_registers_the_statusline(clienv):
    result, out = _invoke(clienv, INSTALL_ARGS)

    assert result.exit_code == 0, out
    entry = _settings(clienv.home)["statusLine"]
    assert entry["type"] == "command"
    assert "-m jacked.statusline" in entry["command"]


def test_install_never_replaces_a_foreign_statusline(clienv):
    path = _write_settings_file(clienv.home, {"statusLine": dict(FOREIGN)})

    result, out = _invoke(clienv, INSTALL_ARGS)

    assert result.exit_code == 0, out
    assert json.loads(path.read_text(encoding="utf-8"))["statusLine"] == FOREIGN
    assert "Kept your existing statusline" in out


def test_install_no_statusline_flag_registers_nothing(clienv):
    result, out = _invoke(clienv, INSTALL_ARGS + ["--no-statusline"])

    assert result.exit_code == 0, out
    assert "statusLine" not in _settings(clienv.home)


def test_install_respects_an_explicit_disable(clienv):
    statusline_setup.save_state(
        clienv.home, {"version": 1, "state": "disabled", "previous": None}
    )

    result, out = _invoke(clienv, INSTALL_ARGS)

    assert result.exit_code == 0, out
    assert "statusLine" not in _settings(clienv.home)
    assert "Statusline disabled" in out


def test_install_migrates_a_stale_jacked_command(clienv):
    _write_settings_file(
        clienv.home,
        {"statusLine": {"type": "command", "command": '"/old/python" -m jacked.statusline'}},
    )

    result, out = _invoke(clienv, INSTALL_ARGS)

    assert result.exit_code == 0, out
    assert _settings(clienv.home)["statusLine"] == _ours_entry()


def test_uninstall_removes_the_statusline_registration(clienv):
    _stub_uninstall_externals(clienv)
    r1, o1 = _invoke(clienv, INSTALL_ARGS)
    assert r1.exit_code == 0, o1
    assert "statusLine" in _settings(clienv.home)

    r2, o2 = _invoke(clienv, ["uninstall", "--yes"])

    assert r2.exit_code == 0, o2
    assert "statusLine" not in _settings(clienv.home)


def test_uninstall_restores_a_saved_previous_statusline(clienv):
    _stub_uninstall_externals(clienv)
    _write_settings_file(clienv.home, {"statusLine": dict(FOREIGN)})
    statusline_setup.enable(clienv.home)  # takes over, backs the foreign entry up
    assert statusline_setup.state_path(clienv.home).exists()

    result, out = _invoke(clienv, ["uninstall", "--yes"])

    assert result.exit_code == 0, out
    assert _settings(clienv.home)["statusLine"] == FOREIGN
    assert not statusline_setup.state_path(clienv.home).exists()


def test_statusline_enable_disable_status_round_trip(clienv):
    result, out = _invoke(clienv, ["statusline", "status"])
    assert result.exit_code == 0, out
    assert "Registration: absent" in out
    assert "default (on)" in out

    result, out = _invoke(clienv, ["statusline", "enable"])
    assert result.exit_code == 0, out
    assert "Statusline enabled" in out
    assert statusline_setup.entry_state(_settings(clienv.home)) == "ours"

    result, out = _invoke(clienv, ["statusline", "enable"])
    assert result.exit_code == 0, out
    assert "Statusline already enabled" in out

    result, out = _invoke(clienv, ["statusline", "status"])
    assert result.exit_code == 0, out
    assert "Registration: ours" in out
    assert "enabled" in out
    assert "-m jacked.statusline" in out

    result, out = _invoke(clienv, ["statusline", "disable"])
    assert result.exit_code == 0, out
    assert "Statusline disabled" in out
    assert "statusLine" not in _settings(clienv.home)

    result, out = _invoke(clienv, ["statusline", "status"])
    assert result.exit_code == 0, out
    assert "Registration: absent" in out
    assert "disabled" in out


def test_statusline_enable_over_foreign_reports_the_backup_and_restores_it(clienv):
    _write_settings_file(clienv.home, {"statusLine": dict(FOREIGN)})

    result, out = _invoke(clienv, ["statusline", "enable"])
    assert result.exit_code == 0, out
    assert "previous statusline was saved" in out

    result, out = _invoke(clienv, ["statusline", "disable"])
    assert result.exit_code == 0, out
    assert "previous statusline restored" in out
    assert _settings(clienv.home)["statusLine"] == FOREIGN


def test_statusline_disable_when_not_registered_says_so(clienv):
    result, out = _invoke(clienv, ["statusline", "disable"])
    assert result.exit_code == 0, out
    assert "was not registered" in out


@pytest.mark.parametrize("sub", ["enable", "disable"])
def test_statusline_command_fails_loudly_on_corrupt_settings(clienv, sub):
    path = settings_path(clienv.home)
    path.write_text("{ not json", encoding="utf-8")
    before = path.read_bytes()

    result, out = _invoke(clienv, ["statusline", sub])

    assert result.exit_code == 1, out
    assert path.read_bytes() == before


# --------------------------------------------------------------------------- #
# D. Route: hermetic app + GET/PUT
# --------------------------------------------------------------------------- #

def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _make_guarded_app() -> FastAPI:
    from jacked.api.security import HostValidationMiddleware, build_allowed_origins
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.add_middleware(HostValidationMiddleware)
    app.state.allowed_origins = build_allowed_origins("127.0.0.1", 8321)
    return app


@pytest.fixture
def routeenv(tmp_path, monkeypatch):
    """SETTINGS_JSON and $JACKED_HOME pinned to the SAME tmp home, so the route's
    detection and the toggle engine read/write one file."""
    home_dir = tmp_path / "home"
    (home_dir / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(home_dir))
    monkeypatch.setattr(features, "SETTINGS_JSON", home_dir / ".claude" / "settings.json")
    return SimpleNamespace(home=home_dir, monkeypatch=monkeypatch)


def _statusline_feature(client):
    feats = client.get("/api/features").json()
    return next(h for h in feats["hooks"] if h["name"] == "statusline")


def test_features_manifest_lists_the_statusline_hook(routeenv):
    assert "statusline" in features.VALID_HOOKS

    entry = _statusline_feature(TestClient(_make_app()))

    assert entry["display_name"] == "Statusline"
    assert entry["installed"] is False
    assert entry["source_available"] is True
    assert EM_DASH not in entry["description"]


def test_put_statusline_enable_then_disable_round_trip(routeenv):
    client = TestClient(_make_app())

    resp = client.put("/api/features/hooks/statusline", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "statusline"
    assert body["category"] == "hooks"
    assert body["enabled"] is True
    assert body["took_over_foreign"] is False
    assert body["changed"] is True
    assert _settings(routeenv.home)["statusLine"] == _ours_entry()
    assert _statusline_feature(client)["installed"] is True

    resp = client.put("/api/features/hooks/statusline", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["restored_previous"] is False
    assert "statusLine" not in _settings(routeenv.home)
    assert _statusline_feature(client)["installed"] is False


def test_put_statusline_surfaces_takeover_then_restore(routeenv):
    _write_settings_file(routeenv.home, {"statusLine": dict(FOREIGN)})
    client = TestClient(_make_app())

    body = client.put("/api/features/hooks/statusline", json={"enabled": True}).json()
    assert body["took_over_foreign"] is True

    body = client.put("/api/features/hooks/statusline", json={"enabled": False}).json()
    assert body["restored_previous"] is True
    assert _settings(routeenv.home)["statusLine"] == FOREIGN


def test_put_statusline_503_on_corrupt_settings(routeenv):
    path = routeenv.home / ".claude" / "settings.json"
    path.write_text("{ corrupt settings", encoding="utf-8")
    before = path.read_bytes()

    resp = TestClient(_make_app()).put(
        "/api/features/hooks/statusline", json={"enabled": True}
    )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SETTINGS_UNREADABLE"
    assert path.read_bytes() == before


def test_put_statusline_foreign_origin_is_403(monkeypatch):
    monkeypatch.setattr("jacked.statusline_setup.enable", lambda home: {})
    resp = TestClient(_make_guarded_app()).put(
        "/api/features/hooks/statusline",
        json={"enabled": True},
        headers={"origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_put_statusline_untrusted_host_is_421(monkeypatch):
    monkeypatch.setattr("jacked.statusline_setup.enable", lambda home: {})
    resp = TestClient(_make_guarded_app()).put(
        "/api/features/hooks/statusline",
        json={"enabled": True},
        headers={"host": "evil.example.com"},
    )
    assert resp.status_code == 421
    assert resp.json()["error"]["code"] == "UNTRUSTED_HOST"


def test_get_features_still_200_when_settings_is_corrupt(routeenv):
    (routeenv.home / ".claude" / "settings.json").write_text(
        "{ broken", encoding="utf-8"
    )

    resp = TestClient(_make_app()).get("/api/features")

    assert resp.status_code == 200
    assert resp.json()["settings_unreadable"] is True
    entry = next(h for h in resp.json()["hooks"] if h["name"] == "statusline")
    assert entry["installed"] is False


# --------------------------------------------------------------------------- #
# E. Frontend: the two statusline follow-up toasts
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag,copy", [
    ("took_over_foreign", "Your previous statusline was saved. Disable to restore it."),
    ("restored_previous", "Your previous statusline is back."),
])
def test_settings_js_toasts_the_statusline_takeover_flags(flag, copy):
    """Each engine flag must be guarded by a `name === 'statusline'` check and
    followed by its showToast call, so the toast can't fire for another hook."""
    lines = (DATA / "web" / "js" / "components" / "settings.js").read_text(
        encoding="utf-8"
    ).splitlines()

    guard = next(
        i for i, ln in enumerate(lines)
        if "name === 'statusline'" in ln and f"res.{flag}" in ln
    )
    toast = next(
        ln for ln in lines[guard + 1:guard + 3] if "showToast(" in ln
    )
    assert copy in toast
    # User-facing toast copy stays em-dash free.
    assert EM_DASH not in toast


# --------------------------------------------------------------------------- #
# F. Served-model segment: gateway fallback visibility
# --------------------------------------------------------------------------- #

def _transcript(tmp_path, models, name="t.jsonl", pad_first=False):
    """Write a session JSONL whose assistant messages carry `models` in order.

    pad_first inflates the first entry so the file exceeds the head window,
    forcing the tail seek (and therefore a partial line at the boundary).
    """
    path = tmp_path / name
    lines = []
    for i, model in enumerate(models):
        entry = {"type": "assistant", "message": {"model": model, "content": []}}
        if pad_first and i == 0:
            entry["message"]["content"] = ["x" * 70000]
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_served_segment_is_silent_when_the_model_never_changed(tmp_path, home):
    """The common case renders nothing: the model segment already names it."""
    tp = _transcript(tmp_path, ["claude-fable-5"] * 3)
    assert statusline._served_segment({"transcript_path": tp}) == ""
    line = _render({"model": {"display_name": "Fable 5"}, "transcript_path": tp}, home)
    assert "FALLBACK" not in line


def test_served_segment_flags_a_mid_session_switch(tmp_path, home):
    """A gateway fallback names both models, since the payload cannot."""
    tp = _transcript(
        tmp_path,
        ["deepseek/deepseek-v4-flash-0731"] * 2 + ["openai/gpt-5.6-luna"],
    )
    seg = statusline._served_segment({"transcript_path": tp})
    assert "gpt-5.6-luna (FALLBACK, not deepseek-v4-flash-0731)" in seg
    # Provider prefixes are stripped for display.
    assert "openai/" not in seg and "deepseek/" not in seg
    assert "FALLBACK" in _render({"transcript_path": tp}, home)


def test_served_segment_clears_itself_when_routing_reverts(tmp_path, home):
    """Routing is per-request, so the warning must disappear on revert."""
    models = ["deepseek/v4"] * 2 + ["openai/luna"] + ["deepseek/v4"]
    assert statusline._served_segment({"transcript_path": _transcript(tmp_path, models)}) == ""


def test_served_segment_survives_a_partial_line_at_the_seek_boundary(tmp_path):
    """A tail seek lands mid-line; that fragment must not abort detection."""
    tp = _transcript(
        tmp_path,
        ["deepseek/v4"] + ["deepseek/v4"] * 3 + ["openai/luna"],
        pad_first=True,
    )
    assert os.path.getsize(tp) > 65536      # head window really is exceeded
    seg = statusline._served_segment({"transcript_path": tp})
    assert "luna (FALLBACK, not v4)" in seg


@pytest.mark.parametrize("payload", [
    {},
    {"transcript_path": None},
    {"transcript_path": ""},
    {"transcript_path": "/nonexistent/nope.jsonl"},
    "not-a-dict",
])
def test_served_segment_treats_absence_as_normal(payload):
    """Absence is never an error; the segment simply drops."""
    assert statusline._served_segment(payload) == ""


def test_served_segment_ignores_garbage_and_synthetic_entries(tmp_path):
    """Unparseable lines and <synthetic> messages never name a model."""
    path = tmp_path / "g.jsonl"
    path.write_text(
        "not json at all\n"
        + json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}) + "\n"
        + json.dumps({"type": "user", "message": {"model": "ignored/user"}}) + "\n",
        encoding="utf-8",
    )
    assert statusline._served_segment({"transcript_path": str(path)}) == ""


def test_served_segment_stays_fast_on_a_large_transcript(tmp_path):
    """Bounded reads: cost must not scale with session length."""
    path = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "user", "message": {"content": "y" * 900}})
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {"model": "a/first"}}) + "\n")
        for _ in range(12000):                      # ~11MB of unrelated traffic
            fh.write(filler + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"model": "b/last"}}) + "\n")
    started = time.monotonic()
    seg = statusline._served_segment({"transcript_path": str(path)})
    elapsed = time.monotonic() - started
    assert "last (FALLBACK, not first)" in seg
    assert elapsed < 0.25, f"took {elapsed:.3f}s; bounded reads regressed"

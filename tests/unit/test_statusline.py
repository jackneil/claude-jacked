"""Statusline coverage: the stdlib renderer, the enable/disable engine, the CLI
install/uninstall parity, the dashboard Features toggle, and the frontend toasts.

Isolation contract. Every test runs against a tmp home.

* Renderer tests call ``statusline.render(payload, home=..., now=...)`` with an
  EXPLICIT home and an EXPLICIT ``now``, so no test reads the real
  ``~/.claude.json`` and no assertion depends on wall-clock time. The tmp home
  starts without a ``.claude.json``, without a ``jacked.db`` and without the
  caveman flag, so the account, model-scoped usage and badge segments are empty
  unless the test creates them. Nothing here ever opens the real
  ``~/.claude/jacked.db``.
* Engine, CLI, and route tests set ``$JACKED_HOME`` to the same tmp home the
  assertions read, so ``statusline_setup``, the CLI installer, and the route all
  converge on one ``settings.json``.
* The full ``jacked install`` / ``jacked uninstall`` runs stub only the steps
  that reach OUTSIDE the tmp home (the ``claude mcp`` shell-out, the Codex
  uninstall pass, the skill-packs uninstall pass). Everything statusline-related
  runs for real.

Nothing here touches the real ``~/.claude``, the network, or npx.
"""

import ast
import builtins
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
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


def test_sum_usage_supports_openrouter_total_tokens_without_double_counting():
    assert statusline._sum_usage({
        "prompt_tokens": 1200,
        "completion_tokens": 340,
        "total_tokens": 1540,
        "prompt_tokens_details": {"cached_tokens": 900},
    }) == 1540


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


def test_account_segment_writes_a_versioned_source_cache(home):
    """The cache carries the identity too, so the model-scoped usage lookup
    never re-reads (or re-parses) the multi-MB ~/.claude.json."""
    _write_account(
        home,
        emailAddress="me@co.com",
        organizationUuid="org-uuid-1",
    )
    segment = _render({}, home)
    assert segment == "me@co.com"
    cached = json.loads(_cache_path(home).read_text(encoding="utf-8"))
    source_stat = (home / ".claude.json").stat()

    assert cached == {
        "version": 3,
        "source": {
            "mtime_ns": source_stat.st_mtime_ns,
            "ctime_ns": source_stat.st_ctime_ns,
            "size": source_stat.st_size,
            "device": source_stat.st_dev,
            "inode": source_stat.st_ino,
            "digest": hashlib.blake2b(
                (home / ".claude.json").read_bytes(), digest_size=16
            ).hexdigest(),
        },
        "segment": segment,
        "email": "me@co.com",
        "org_uuid": "org-uuid-1",
    }


def test_account_cache_refreshes_when_claude_json_moves_forward(home):
    _write_account(home, emailAddress="old@co.com")
    assert _render({}, home) == "old@co.com"

    _write_account(home, emailAddress="new@co.com")
    _bump_mtime(home / ".claude.json")

    assert _render({}, home) == "new@co.com"


def test_matching_source_signature_short_circuits_to_the_cache(home):
    """A matching source revision reuses its rendered segment."""
    _write_account(home, emailAddress="disk@co.com")
    assert _render({}, home) == "disk@co.com"

    cached = json.loads(_cache_path(home).read_text(encoding="utf-8"))
    cached["segment"] = "SENTINEL-FROM-CACHE"
    _cache_path(home).write_text(json.dumps(cached), encoding="utf-8")
    _bump_mtime(_cache_path(home))

    assert _render({}, home) == "SENTINEL-FROM-CACHE"


def test_future_dated_cache_cannot_pin_an_old_account(home):
    _write_account(
        home,
        emailAddress="old@co.com",
        userRateLimitTier="default_claude_max_5x",
    )
    assert _render({}, home) == f"old@co.com {MIDDOT} Max 5x"
    _bump_mtime(_cache_path(home), seconds=3600)

    _write_account(
        home,
        emailAddress="new-account@co.com",
        userRateLimitTier="default_claude_max_20x",
    )

    assert _render({}, home) == f"new-account@co.com {MIDDOT} Max 20x"


@pytest.mark.parametrize("legacy_cache", [
    "OLD PLAIN-TEXT SEGMENT\n",
    "{ malformed json",
    json.dumps({"version": 2, "source": {}, "segment": "WRONG"}),
])
def test_legacy_or_malformed_account_cache_is_a_safe_miss(home, legacy_cache):
    _write_account(home, emailAddress="disk@co.com")
    _cache_path(home).write_text(legacy_cache, encoding="utf-8")
    _bump_mtime(_cache_path(home))

    assert _render({}, home) == "disk@co.com"
    assert json.loads(_cache_path(home).read_text(encoding="utf-8"))[
        "segment"
    ] == "disk@co.com"


def test_old_in_flight_writer_cannot_pin_subsequent_renders(home, monkeypatch):
    _write_account(
        home,
        emailAddress="old@co.com",
        userRateLimitTier="default_claude_max_5x",
    )
    original_write = statusline._write_account_cache
    switched = False

    def write_after_switch(cache_path, segment, source):
        nonlocal switched
        if not switched:
            switched = True
            _write_account(
                home,
                emailAddress="new@co.com",
                userRateLimitTier="default_claude_max_20x",
            )
        original_write(cache_path, segment, source)

    monkeypatch.setattr(statusline, "_write_account_cache", write_after_switch)

    assert _render({}, home) == f"old@co.com {MIDDOT} Max 5x"
    assert _render({}, home) == f"new@co.com {MIDDOT} Max 20x"


def test_content_digest_prevents_reused_source_identity_collision(home, monkeypatch):
    reused_identity = SimpleNamespace(
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_size=40,
        st_dev=7,
        st_ino=11,
    )
    original_signature = statusline._account_source_signature
    monkeypatch.setattr(
        statusline,
        "_account_source_signature",
        lambda _stat, content: original_signature(reused_identity, content),
    )

    _write_account(home, emailAddress="old@co.com")
    assert _render({}, home) == "old@co.com"
    old_cache = _cache_path(home).read_text(encoding="utf-8")

    _write_account(home, emailAddress="new@co.com")
    assert _render({}, home) == "new@co.com"
    assert _cache_path(home).read_text(encoding="utf-8") != old_cache


def test_account_cache_write_failure_does_not_hide_segment(home, monkeypatch):
    _write_account(home, emailAddress="disk@co.com")

    def fail_cache_write(*_args, **_kwargs):
        raise OSError("cache unavailable")

    monkeypatch.setattr(statusline.tempfile, "mkstemp", fail_cache_write)

    assert _render({}, home) == "disk@co.com"
    assert not _cache_path(home).exists()


def test_recursive_account_cache_is_a_safe_miss(home, monkeypatch):
    _write_account(home, emailAddress="disk@co.com")
    _cache_path(home).write_text("{}", encoding="utf-8")
    original_load = statusline.json.load

    def recurse_for_cache(fh):
        if fh.name == str(_cache_path(home)):
            raise RecursionError("cache nesting")
        return original_load(fh)

    monkeypatch.setattr(statusline.json, "load", recurse_for_cache)

    assert _render({}, home) == "disk@co.com"


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


def test_account_cache_ignores_equal_mtime_when_source_signature_changes(home):
    """Cache-file timestamps cannot override a changed source signature."""
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
#
# The baseline is the CONFIGURED model from the stdin payload
# (model.id), not the session's first assistant turn. A first-turn baseline is
# frozen for the life of the session, so it stuck on screen forever after one
# deliberate /model switch and it labelled an UPGRADE as a downgrade
# ("claude-fable-5 (FALLBACK, not claude-opus-5)"). Measured over 12 real
# transcripts, the old baseline produced three warnings and all three were
# deliberate user switches -- zero true positives. This segment exists to catch
# a SERVING mismatch (a gateway answering with a model nobody asked for), never
# the user's own model choice.
#
# The first-turn baseline survives only where the payload carries no usable
# model id, and only that path still pays the growing head read.

def _transcript(tmp_path, models, name="t.jsonl", pad_first=False, pad_bytes=70000):
    """Write a session JSONL whose assistant messages carry `models` in order.

    pad_first inflates the first entry by pad_bytes so the file exceeds the
    head window, forcing the tail seek (and therefore a partial line at the
    boundary). Push pad_bytes past the 256KB tail to also force the growing
    head windows and a non-zero tail seek offset.
    """
    path = tmp_path / name
    lines = []
    for i, model in enumerate(models):
        entry = {"type": "assistant", "message": {"model": model, "content": []}}
        if pad_first and i == 0:
            entry["message"]["content"] = ["x" * pad_bytes]
        lines.append(json.dumps(entry))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _served_payload(transcript_path, model_id=None):
    """A stdin-shaped payload: transcript plus the CONFIGURED model id.

    Claude Code writes model: {"id": ..., "display_name": ...} and rewrites
    it on every /model switch, which is why it is the baseline.
    """
    payload = {"transcript_path": transcript_path}
    if model_id is not None:
        payload["model"] = {"id": model_id, "display_name": model_id}
    return payload


class _ReadLog:
    """Every seek/read `_served_segment` performed on the transcript."""

    def __init__(self):
        self.events = []

    @property
    def total_bytes(self):
        return sum(n for kind, n in self.events if kind == "read")

    @property
    def head_seeks(self):
        return self.events.count(("seek", 0))


class _TracedFile:
    """Read-tracing proxy; only the calls `_served_segment` makes exist here."""

    def __init__(self, fh, log):
        self._fh, self._log = fh, log

    def read(self, *args):
        data = self._fh.read(*args)
        self._log.events.append(("read", len(data)))
        return data

    def seek(self, pos, *args):
        self._log.events.append(("seek", pos))
        return self._fh.seek(pos, *args)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *exc):
        return self._fh.__exit__(*exc)


def _trace_served_reads(monkeypatch, payload):
    """Run `_served_segment` with every read of its transcript recorded."""
    log = _ReadLog()
    target = os.path.realpath(payload["transcript_path"])
    real_open = builtins.open

    def traced(file, *args, **kwargs):
        fh = real_open(file, *args, **kwargs)
        if os.path.realpath(str(file)) == target:
            return _TracedFile(fh, log)
        return fh

    monkeypatch.setattr(builtins, "open", traced)
    try:
        segment = statusline._served_segment(payload)
    finally:
        monkeypatch.undo()
    return segment, log


# --- the configured-model baseline ----------------------------------------- #

def test_served_segment_clears_after_a_deliberate_model_upgrade(tmp_path, home):
    """Regression: the exact real transcript that rendered an INVERTED warning.

    The session started on claude-opus-5 and the user UPGRADED to
    claude-fable-5 with /model. Against a first-turn baseline this rendered
    "claude-fable-5 (FALLBACK, not claude-opus-5)" -- calling the more capable,
    deliberately chosen model a downgrade-fallback, and never clearing.
    """
    tp = _transcript(tmp_path, ["claude-opus-5"] * 2 + ["claude-fable-5"] * 2)
    payload = _served_payload(tp, "claude-fable-5")

    assert statusline._served_segment(payload) == ""
    line = _render(dict(payload, model={"id": "claude-fable-5", "display_name": "Fable 5"}), home)
    assert "FALLBACK" not in line


def test_served_segment_clears_when_the_user_switches_back(tmp_path):
    """Switching back is still a user choice, so nothing may linger."""
    tp = _transcript(tmp_path, ["claude-fable-5"] * 3)
    assert statusline._served_segment(_served_payload(tp, "claude-fable-5")) == ""


def test_served_segment_flags_a_gateway_serving_another_model(tmp_path, home):
    """The case this feature exists for: served != configured."""
    tp = _transcript(tmp_path, ["claude-fable-5"] + ["claude-opus-5"] * 2)
    payload = _served_payload(tp, "claude-fable-5")

    seg = statusline._served_segment(payload)
    assert seg == f"{RED}claude-opus-5 (FALLBACK, not claude-fable-5){RESET}"
    assert "FALLBACK" in _render(payload, home)


def test_served_segment_ignores_a_pure_namespace_difference(tmp_path):
    """A gateway namespaces the SAME model; that is not a fallback."""
    tp = _transcript(tmp_path, ["openrouter/anthropic/claude-opus-5"] * 2)
    assert statusline._served_segment(_served_payload(tp, "claude-opus-5")) == ""
    # Case and stray whitespace are not a mismatch either.
    assert statusline._served_segment(_served_payload(tp, " Claude-Opus-5 ")) == ""


def test_served_segment_flags_a_namespaced_model_that_really_differs(tmp_path):
    """Namespace tolerance must not swallow a genuinely different model."""
    tp = _transcript(tmp_path, ["openrouter/anthropic/claude-fable-5"])
    seg = statusline._served_segment(_served_payload(tp, "claude-opus-5"))
    assert "claude-fable-5 (FALLBACK, not claude-opus-5)" in seg
    assert "openrouter/" not in seg


def test_served_segment_uses_the_configured_model_not_the_display_name(tmp_path):
    """display_name ("Fable 5") never matches a model id; only `id` counts."""
    tp = _transcript(tmp_path, ["claude-fable-5"] * 2)
    payload = {
        "transcript_path": tp,
        "model": {"id": "claude-fable-5", "display_name": "Fable 5"},
    }
    assert statusline._served_segment(payload) == ""


# --- the first-turn fallback, for payloads with no model id ----------------- #

@pytest.mark.parametrize("model", [
    None,                                   # no model key at all
    {},                                     # model object, no id
    {"display_name": "Opus"},               # older Claude Code: name only
    {"id": ""},                             # present but empty
    {"id": "   "},                          # whitespace only
    {"id": 5},                              # wrong type
    "not-a-dict",
])
def test_served_segment_falls_back_to_the_first_turn_without_a_model_id(tmp_path, model):
    """No usable payload id: keep the old baseline rather than drop the feature."""
    # tmp_path is unique per parametrized case, so the default name is safe.
    tp = _transcript(tmp_path, ["deepseek/v4"] * 2 + ["openai/luna"])
    payload = {"transcript_path": tp}
    if model is not None:
        payload["model"] = model
    assert "luna (FALLBACK, not v4)" in statusline._served_segment(payload)


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


# --- the head read is real work, and the payload path must not pay it ------ #

def _head_beyond_the_windows(tmp_path, name):
    """A transcript whose first assistant message is bigger than the tail read.

    First turn claude-opus-5, newest claude-fable-5, and >256KB of padding in
    between, so (a) the first model is only reachable through the GROWING head
    windows and (b) the tail seek offset is non-zero -- which is what makes a
    seek to 0 unambiguous evidence that the head was read.
    """
    tp = _transcript(
        tmp_path,
        ["claude-opus-5"] + ["claude-fable-5"] * 3,
        name=name,
        pad_first=True,
        pad_bytes=300000,
    )
    assert os.path.getsize(tp) > 262144
    return tp


def test_served_segment_skips_the_head_read_when_the_payload_has_a_baseline(tmp_path, monkeypatch):
    """Perf: no head read at all on the payload path -- tail only.

    The head of this transcript says claude-opus-5, which would produce a
    (wrong) warning if it were consulted. Both facts are asserted: the answer
    is empty, and the file was never seeked back to 0.
    """
    tp = _head_beyond_the_windows(tmp_path, "skip.jsonl")

    seg, log = _trace_served_reads(monkeypatch, _served_payload(tp, "claude-fable-5"))

    assert seg == ""                                  # head baseline not used
    assert log.head_seeks == 0                        # head never even read
    assert log.total_bytes <= 262144, log.events      # tail window only
    assert log.events[0][0] == "seek" and log.events[0][1] > 0


def test_served_segment_grows_the_head_read_only_on_the_fallback_path(tmp_path, monkeypatch):
    """Without a payload id the old behavior stands, growing windows included."""
    tp = _head_beyond_the_windows(tmp_path, "grow.jsonl")
    size = os.path.getsize(tp)

    seg, log = _trace_served_reads(monkeypatch, {"transcript_path": tp})

    assert "claude-fable-5 (FALLBACK, not claude-opus-5)" in seg
    # 64KB and 256KB both land inside the padded first line; the 1MB window is
    # what finally reaches the first model.
    assert log.head_seeks == 3, log.events
    assert log.total_bytes == 65536 + 262144 + size


# --- robustness ------------------------------------------------------------ #

@pytest.mark.parametrize("payload", [
    {},
    {"transcript_path": None},
    {"transcript_path": ""},
    {"transcript_path": "/nonexistent/nope.jsonl"},
    {"transcript_path": "/nonexistent/nope.jsonl", "model": {"id": "claude-opus-5"}},
    "not-a-dict",
])
def test_served_segment_treats_absence_as_normal(payload):
    """Absence is never an error; the segment simply drops."""
    assert statusline._served_segment(payload) == ""


def test_served_segment_tolerates_an_empty_transcript(tmp_path):
    """A zero-byte transcript has no served model, with or without a baseline."""
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert statusline._served_segment({"transcript_path": str(path)}) == ""
    assert statusline._served_segment(_served_payload(str(path), "claude-opus-5")) == ""


def test_served_segment_tolerates_an_unreadable_transcript(tmp_path):
    """A permission error drops the segment instead of raising."""
    path = tmp_path / "locked.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"model": "claude-opus-5"}}) + "\n",
        encoding="utf-8",
    )
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        assert statusline._served_segment(_served_payload(str(path), "claude-fable-5")) == ""
    finally:
        path.chmod(0o644)


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
    # A configured baseline must not conjure a warning out of no served model.
    assert statusline._served_segment(_served_payload(str(path), "claude-opus-5")) == ""


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


# --------------------------------------------------------------------------- #
# G. Model-scoped weekly usage: the limit the payload cannot see
# --------------------------------------------------------------------------- #
#
# Claude Code's stdin payload carries only five_hour and seven_day, and its
# seven_day number is the AGGREGATE. On every real account measured, the
# model-scoped weekly percentage runs higher (96% vs 76%, 100% vs 90%), so the
# aggregate under-reports the limit that actually stops the session. jacked
# already polls and caches the usage API that carries the scoped number, in
# ~/.claude/jacked.db, which this segment reads read-only.

EMAIL = "me@co.com"
ORG = "d9e88fe1-7971-42b8-900d-70f3be280306"
OTHER_ORG = "f352d25d-06b5-4f62-86b7-c9d1f03447ad"

SESSION_RESET = NOW + 3600            # under 24h -> "%H:%M"
SCOPED_RESET = NOW + 4 * 86400        # over 24h  -> "%a %H:%M"
FRESH_AT = NOW - 60


def _iso(epoch):
    """Epoch -> the ISO-8601-with-offset shape the Anthropic usage API emits."""
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc
    ).isoformat()


def _expect_reset(epoch, now=NOW):
    """The reset text _fmt_reset produces, derived the same way it is."""
    fmt = "%H:%M" if (epoch - now) < 86400 else "%a %H:%M"
    return time.strftime(fmt, time.localtime(epoch))


def _session_limit(percent=27):
    return {
        "kind": "session", "group": "session", "percent": percent,
        "severity": "normal", "resets_at": _iso(SESSION_RESET),
        "scope": None, "is_active": False,
    }


def _weekly_all(percent=76):
    return {
        "kind": "weekly_all", "group": "weekly", "percent": percent,
        "severity": "warning", "resets_at": _iso(SCOPED_RESET),
        "scope": None, "is_active": False,
    }


_DEFAULT_RESET = object()


def _weekly_scoped(model="Fable", percent=96, is_active=True,
                   resets_at=_DEFAULT_RESET):
    """A weekly_scoped entry shaped exactly like the live API's."""
    return {
        "kind": "weekly_scoped", "group": "weekly", "percent": percent,
        "severity": "critical",
        "resets_at": _iso(SCOPED_RESET) if resets_at is _DEFAULT_RESET else resets_at,
        "scope": {"model": {"id": None, "display_name": model}, "surface": None},
        "is_active": is_active,
    }


def _raw(*entries):
    """A cached_usage_raw blob carrying the given limits."""
    return json.dumps({"five_hour": {}, "seven_day": {}, "limits": list(entries)})


def _write_db(home_dir, rows, path=None):
    """Build a jacked.db-shaped accounts table holding `rows`.

    rows: (email, organization_uuid, cached_usage_raw, usage_cached_at).
    """
    db = Path(path) if path is not None else home_dir / ".claude" / "jacked.db"
    con = sqlite3.connect(str(db))
    try:
        con.execute(
            "CREATE TABLE accounts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, "
            "organization_uuid TEXT DEFAULT '', cached_usage_raw TEXT, "
            "usage_cached_at INTEGER)"
        )
        con.executemany(
            "INSERT INTO accounts "
            "(email, organization_uuid, cached_usage_raw, usage_cached_at) "
            "VALUES (?, ?, ?, ?)",
            list(rows),
        )
        con.commit()
    finally:
        con.close()
    return db


def _usage_home(home_dir, limits=None, cached_at=FRESH_AT, email=EMAIL, org=ORG):
    """A tmp home wired up like a signed-in machine with jacked polling."""
    _write_account(
        home_dir,
        emailAddress=email,
        organizationUuid=org,
        userRateLimitTier="default_claude_max_5x",
    )
    _write_db(
        home_dir,
        [(
            email,
            org,
            _raw(*(limits if limits is not None else (
                _session_limit(), _weekly_all(), _weekly_scoped(),
            ))),
            cached_at,
        )],
    )
    return home_dir


def _usage_cache_path(home_dir):
    return home_dir / ".claude" / "statusline-usage.cache"


def _scoped(label="Fable", pct=96, color=RED, reset=SCOPED_RESET, marker=""):
    return (
        f"{marker}{label} {color}{pct}%{RESET}{ARROW}{_expect_reset(reset)}"
    )


# --- placement ------------------------------------------------------------- #

def test_scoped_usage_lands_between_the_seven_day_and_account_segments(home):
    _usage_home(home)
    line = _render(_full_payload(), home)

    assert line.split(SEP) == [
        f"{BOLD_CYAN}Fable 5{RESET} {YELLOW}[xhigh]{RESET} {MAGENTA}[fast]{RESET}",
        f"ctx {YELLOW}63%{RESET} (633k/1.0M)",
        f"5h {GREEN}7%{RESET}{ARROW}"
        + time.strftime("%H:%M", time.localtime(NOW + 3600)),
        f"7d {RED}88%{RESET}{ARROW}"
        + time.strftime("%a %H:%M", time.localtime(NOW + 3 * 86400)),
        _scoped(),
        f"{EMAIL} {MIDDOT} Max 5x",
    ]


def test_scoped_usage_reports_higher_than_the_aggregate_seven_day(home):
    """The whole point: 7d alone hides the binding constraint."""
    _usage_home(home, limits=(_weekly_all(76), _weekly_scoped("Fable", 96)))
    payload = _full_payload()
    payload["rate_limits"]["seven_day"]["used_percentage"] = 76.0

    line = _render(payload, home)

    assert f"7d {YELLOW}76%{RESET}" in line
    assert f"Fable {RED}96%{RESET}" in line


def test_scoped_usage_precedes_a_fallback_warning(home):
    """Order stays limits -> scoped -> FALLBACK -> account."""
    _usage_home(home)
    tp = _transcript(home, ["a/one", "b/two"], name="fb.jsonl")
    line = _render(
        {"model": {"display_name": "Fable 5"}, "transcript_path": tp}, home
    )
    parts = line.split(SEP)
    assert parts[1] == _scoped()
    assert "FALLBACK" in parts[2]


# --- model matching -------------------------------------------------------- #

@pytest.mark.parametrize("payload_model,bucket_model", [
    ("Fable 5", "Fable"),
    ("Fable", "Fable"),
    ("fable 5", "FABLE"),
    ("Opus", "Claude Opus 4.5"),
    ("Claude Opus 4.5", "Opus"),
])
def test_a_fuller_payload_name_still_matches_its_bucket(
    home, payload_model, bucket_model
):
    _usage_home(home, limits=(_weekly_scoped(bucket_model, 96, is_active=False),))
    line = _render({"model": {"display_name": payload_model}}, home)
    assert _scoped(label=bucket_model) in line


def test_a_non_matching_model_falls_back_to_the_active_bucket(home):
    _usage_home(home, limits=(
        _weekly_all(76),
        _weekly_scoped("Fable", 96, is_active=True),
    ))
    line = _render({"model": {"display_name": "Sonnet 5"}}, home)
    assert _scoped(label="Fable", pct=96) in line


def test_the_session_model_beats_the_active_bucket(home):
    """A matching bucket wins even when another one is flagged active."""
    _usage_home(home, limits=(
        _weekly_scoped("Opus", 100, is_active=True),
        _weekly_scoped("Fable", 96, is_active=False),
    ))
    line = _render({"model": {"display_name": "Fable 5"}}, home)
    assert _scoped(label="Fable", pct=96) in line
    assert "Opus" not in line


def test_no_scoped_bucket_at_all_drops_the_segment(home):
    _usage_home(home, limits=(_session_limit(), _weekly_all(76)))
    line = _render(_full_payload(), home)
    assert "%" in line                       # the payload's own segments survive
    assert line.split(SEP)[-1] == f"{EMAIL} {MIDDOT} Max 5x"
    assert "Fable 5" in line                 # only the model segment names it
    assert line.count("Fable") == 1


def test_no_match_and_no_active_bucket_drops_the_segment(home):
    _usage_home(home, limits=(_weekly_scoped("Opus", 100, is_active=False),))
    assert "Opus" not in _render({"model": {"display_name": "Fable 5"}}, home)


def test_a_missing_model_name_still_shows_the_active_bucket(home):
    """No model in the payload is not a reason to hide the binding limit."""
    _usage_home(home, limits=(_weekly_scoped("Fable", 96, is_active=True),))
    assert _scoped() in _render({}, home)


def test_a_bucket_without_a_display_name_is_dropped(home):
    """An unlabeled percentage beside 7d is noise, not information."""
    bucket = _weekly_scoped("Fable", 96, is_active=True)
    bucket["scope"]["model"]["display_name"] = None
    _usage_home(home, limits=(bucket,))
    assert _render({"model": {"display_name": "Fable 5"}}, home) == (
        f"{BOLD_CYAN}Fable 5{RESET}{SEP}{EMAIL} {MIDDOT} Max 5x"
    )


@pytest.mark.parametrize("pct,color", [(10, GREEN), (70, YELLOW), (96, RED)])
def test_scoped_usage_uses_the_shared_pressure_colors(home, pct, color):
    _usage_home(home, limits=(_weekly_scoped("Fable", pct),))
    assert _scoped(pct=pct, color=color) in _render(
        {"model": {"display_name": "Fable 5"}}, home
    )


def test_scoped_percent_is_rounded_like_every_other_percentage(home):
    _usage_home(home, limits=(_weekly_scoped("Fable", 84.6),))
    assert _scoped(pct=85, color=RED) in _render(
        {"model": {"display_name": "Fable 5"}}, home
    )


@pytest.mark.parametrize("percent", [None, "96", True, [96]])
def test_a_non_numeric_percent_drops_the_segment(home, percent):
    _usage_home(home, limits=(_weekly_scoped("Fable", percent),))
    assert "Fable 9" not in _render({"model": {"display_name": "Fable 5"}}, home)


# --- account disambiguation ------------------------------------------------ #

def test_org_uuid_disambiguates_two_accounts_sharing_one_email(home):
    """Real situation on this machine: one address, two organizations.

    Matching on the email alone would show the other org's numbers.
    """
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    _write_db(home, [
        (EMAIL, OTHER_ORG, _raw(_weekly_scoped("Opus", 100)), FRESH_AT),
        (EMAIL, ORG, _raw(_weekly_scoped("Fable", 96)), FRESH_AT),
    ])

    line = _render({"model": {"display_name": "Fable 5"}}, home)

    assert _scoped(label="Fable", pct=96) in line
    assert "100%" not in line


def test_the_other_org_row_is_selected_when_claude_json_points_there(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=OTHER_ORG)
    _write_db(home, [
        (EMAIL, OTHER_ORG, _raw(_weekly_scoped("Opus", 100)), FRESH_AT),
        (EMAIL, ORG, _raw(_weekly_scoped("Fable", 96)), FRESH_AT),
    ])

    line = _render({"model": {"display_name": "Opus 4.5"}}, home)

    assert _scoped(label="Opus", pct=100) in line
    assert "96%" not in line


def test_an_unknown_org_uuid_drops_the_segment(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid="not-in-the-db")
    _write_db(home, [(EMAIL, ORG, _raw(_weekly_scoped()), FRESH_AT)])
    assert "96%" not in _render({"model": {"display_name": "Fable 5"}}, home)


def test_a_claude_json_without_an_org_uuid_drops_the_segment(home):
    """Email alone resolves ambiguously, so it is never enough on its own."""
    _write_account(home, emailAddress=EMAIL)
    _write_db(home, [(EMAIL, ORG, _raw(_weekly_scoped()), FRESH_AT)])
    assert "96%" not in _render({"model": {"display_name": "Fable 5"}}, home)


def test_a_missing_oauth_account_drops_the_segment(home):
    (home / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    _write_db(home, [(EMAIL, ORG, _raw(_weekly_scoped()), FRESH_AT)])
    line = _render(_full_payload(), home)
    assert "96%" not in line
    assert "ctx " in line


# --- staleness ------------------------------------------------------------- #

def test_fresh_usage_renders_without_a_stale_marker(home):
    _usage_home(home, cached_at=NOW - 2 * 3600)     # exactly at the boundary
    line = _render({"model": {"display_name": "Fable 5"}}, home)
    assert _scoped() in line
    assert "~" not in line


def test_usage_older_than_two_hours_renders_behind_a_dim_marker(home):
    _usage_home(home, cached_at=NOW - 2 * 3600 - 1)
    line = _render({"model": {"display_name": "Fable 5"}}, home)
    assert _scoped(marker=f"{statusline.DIM}~{RESET}") in line


def test_usage_older_than_a_day_is_dropped_rather_than_shown_stale(home):
    _usage_home(home, cached_at=NOW - 24 * 3600 - 1)
    line = _render(_full_payload(), home)
    assert "96%" not in line
    assert "~" not in line
    assert "ctx " in line                            # other segments survive


@pytest.mark.parametrize("cached_at", [None, "recently", True])
def test_a_missing_usage_timestamp_drops_the_segment(home, cached_at):
    _usage_home(home, cached_at=cached_at)
    assert "96%" not in _render({"model": {"display_name": "Fable 5"}}, home)


def test_a_future_dated_cache_is_treated_as_fresh(home):
    """Clock skew must not print a stale marker on live data."""
    _usage_home(home, cached_at=NOW + 300)
    assert "~" not in _render({"model": {"display_name": "Fable 5"}}, home)


# --- ISO-8601 reset conversion --------------------------------------------- #

def test_iso_to_epoch_round_trips_the_api_format():
    assert statusline._iso_to_epoch("2026-08-15T14:59:59.089483+00:00") == (
        datetime.datetime(
            2026, 8, 15, 14, 59, 59, 89483, tzinfo=datetime.timezone.utc
        ).timestamp()
    )


def test_iso_to_epoch_accepts_a_z_suffix_and_assumes_utc_when_naive():
    expected = datetime.datetime(
        2026, 8, 15, 14, 59, 59, tzinfo=datetime.timezone.utc
    ).timestamp()
    assert statusline._iso_to_epoch("2026-08-15T14:59:59Z") == expected
    assert statusline._iso_to_epoch("2026-08-15T14:59:59") == expected


@pytest.mark.parametrize("value", [
    None, "", "   ", "not a timestamp", "2026-13-45T99:99:99+00:00",
    1786805999, True, ["2026-08-15T14:59:59+00:00"],
])
def test_iso_to_epoch_rejects_everything_unparseable(value):
    assert statusline._iso_to_epoch(value) is None


def test_a_reset_under_a_day_away_renders_the_clock_form(home):
    soon = NOW + 3600
    _usage_home(home, limits=(_weekly_scoped("Fable", 96, resets_at=_iso(soon)),))
    line = _render({"model": {"display_name": "Fable 5"}}, home)
    assert _scoped(reset=soon) in line
    assert line.endswith(_expect_reset(soon)) is False   # account segment follows
    assert f"{ARROW}{_expect_reset(soon)}" in line


@pytest.mark.parametrize("resets_at", [None, "", "whenever", 1786805999])
def test_a_broken_reset_drops_the_arrow_not_the_segment(home, resets_at):
    _usage_home(home, limits=(_weekly_scoped("Fable", 96, resets_at=resets_at),))
    line = _render({"model": {"display_name": "Fable 5"}}, home)
    assert f"Fable {RED}96%{RESET}" in line
    assert ARROW not in line


# --- robustness: absence is normal, never an error ------------------------- #

def test_no_database_drops_the_segment(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    assert not (home / ".claude" / "jacked.db").exists()
    line = _render(_full_payload(), home)
    assert isinstance(line, str)
    assert "ctx " in line
    assert not (home / ".claude" / "jacked.db").exists()   # never created


def test_a_file_that_is_not_a_database_drops_the_segment(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    (home / ".claude" / "jacked.db").write_bytes(b"this is not a database\x00\xff")
    line = _render(_full_payload(), home)
    assert isinstance(line, str)
    assert "ctx " in line


def test_a_database_without_an_accounts_table_drops_the_segment(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    con = sqlite3.connect(str(home / ".claude" / "jacked.db"))
    con.execute("CREATE TABLE something_else (id INTEGER)")
    con.commit()
    con.close()
    assert "ctx " in _render(_full_payload(), home)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions",
)
def test_an_unreadable_database_drops_the_segment(home):
    _usage_home(home)
    db = home / ".claude" / "jacked.db"
    db.chmod(0o000)
    try:
        line = _render(_full_payload(), home)
    finally:
        db.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert isinstance(line, str)
    assert "96%" not in line
    assert "ctx " in line


def test_a_locked_database_drops_the_segment_without_hanging(home):
    """The jacked service writes this file concurrently."""
    _usage_home(home)
    writer = sqlite3.connect(str(home / ".claude" / "jacked.db"), timeout=0.1)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("UPDATE accounts SET usage_cached_at = 1")
    try:
        started = time.monotonic()
        line = _render(_full_payload(), home)
        elapsed = time.monotonic() - started
    finally:
        writer.rollback()
        writer.close()

    assert isinstance(line, str)
    assert "96%" not in line
    assert "ctx " in line
    assert elapsed < 2.0, f"took {elapsed:.3f}s; the read timeout regressed"


@pytest.mark.parametrize("raw", [
    None,
    "",
    "{ not json",
    "[1, 2, 3]",
    '"a string"',
    json.dumps({"limits": None}),
    json.dumps({"limits": "weekly"}),
    json.dumps({"limits": [None, 7, "x"]}),
    json.dumps({"limits": [{"kind": "weekly_scoped", "scope": "Fable"}]}),
    json.dumps({"limits": [{"kind": "weekly_scoped", "scope": {"model": None}}]}),
    json.dumps({}),
])
def test_malformed_cached_usage_raw_drops_the_segment(home, raw):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    _write_db(home, [(EMAIL, ORG, raw, FRESH_AT)])
    line = _render(_full_payload(), home)
    assert isinstance(line, str)
    assert "ctx " in line
    assert line.count("Fable") == 1                  # the model segment only


def test_no_row_for_this_account_drops_the_segment(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    _write_db(home, [])
    assert "96%" not in _render(_full_payload(), home)


def test_the_scoped_segment_never_raises_on_a_directory_in_place_of_the_db(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    (home / ".claude" / "jacked.db").mkdir()
    assert "ctx " in _render(_full_payload(), home)


# --- caching --------------------------------------------------------------- #

def test_the_usage_segment_is_cached_in_its_own_file(home):
    _usage_home(home)
    assert _scoped() in _render({"model": {"display_name": "Fable 5"}}, home)

    cached = json.loads(_usage_cache_path(home).read_text(encoding="utf-8"))
    assert cached["version"] == 1
    assert cached["segment"] == _scoped()
    # The account cache is a different file with a different version, and the
    # usage cache must never clobber it.
    assert _cache_path(home).exists()
    assert _usage_cache_path(home) != _cache_path(home)
    assert json.loads(_cache_path(home).read_text(encoding="utf-8"))["version"] == 3


def test_a_matching_usage_revision_short_circuits_to_the_cache(home):
    _usage_home(home)
    _render({"model": {"display_name": "Fable 5"}}, home)

    cached = json.loads(_usage_cache_path(home).read_text(encoding="utf-8"))
    cached["segment"] = "SENTINEL-FROM-USAGE-CACHE"
    _usage_cache_path(home).write_text(json.dumps(cached), encoding="utf-8")

    assert "SENTINEL-FROM-USAGE-CACHE" in _render(
        {"model": {"display_name": "Fable 5"}}, home
    )


def test_a_database_write_invalidates_the_usage_cache(home):
    _usage_home(home)
    assert _scoped(pct=96) in _render({"model": {"display_name": "Fable 5"}}, home)

    con = sqlite3.connect(str(home / ".claude" / "jacked.db"))
    con.execute(
        "UPDATE accounts SET cached_usage_raw = ?",
        (_raw(_weekly_scoped("Fable", 100)),),
    )
    con.commit()
    con.close()
    _bump_mtime(home / ".claude" / "jacked.db")

    assert _scoped(pct=100) in _render({"model": {"display_name": "Fable 5"}}, home)


def test_switching_models_invalidates_the_usage_cache(home):
    _usage_home(home, limits=(
        _weekly_scoped("Opus", 100, is_active=False),
        _weekly_scoped("Fable", 96, is_active=False),
    ))
    assert _scoped(label="Fable", pct=96) in _render(
        {"model": {"display_name": "Fable 5"}}, home
    )
    assert _scoped(label="Opus", pct=100) in _render(
        {"model": {"display_name": "Opus 4.5"}}, home
    )


def test_switching_accounts_invalidates_the_usage_cache(home):
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)
    _write_db(home, [
        (EMAIL, ORG, _raw(_weekly_scoped("Fable", 96)), FRESH_AT),
        (EMAIL, OTHER_ORG, _raw(_weekly_scoped("Fable", 12)), FRESH_AT),
    ])
    assert _scoped(pct=96) in _render({"model": {"display_name": "Fable 5"}}, home)

    _write_account(home, emailAddress=EMAIL, organizationUuid=OTHER_ORG)
    _bump_mtime(home / ".claude.json")

    assert _scoped(pct=12, color=GREEN) in _render(
        {"model": {"display_name": "Fable 5"}}, home
    )


def test_a_cached_segment_cannot_stay_fresh_forever(home):
    """With the jacked service stopped the database mtime freezes, so the
    cache key carries a wall-clock bucket and staleness is re-evaluated."""
    cached_at = NOW - 3600
    _usage_home(home, cached_at=cached_at)
    payload = {"model": {"display_name": "Fable 5"}}

    assert "~" not in _render(payload, home, now=NOW)

    later = cached_at + 2 * 3600 + 601        # past 2h, past one clock bucket
    line = _render(payload, home, now=later)
    assert f"{statusline.DIM}~{RESET}Fable " in line


@pytest.mark.parametrize("legacy_cache", [
    "OLD PLAIN TEXT\n",
    "{ malformed json",
    json.dumps({"version": 0, "source": {}, "segment": "WRONG"}),
    json.dumps({"version": 1, "source": {}, "segment": "WRONG"}),
    json.dumps({"version": 1, "segment": "WRONG"}),
    json.dumps(["not", "a", "dict"]),
])
def test_a_malformed_usage_cache_is_a_safe_miss(home, legacy_cache):
    _usage_home(home)
    _usage_cache_path(home).write_text(legacy_cache, encoding="utf-8")

    line = _render({"model": {"display_name": "Fable 5"}}, home)

    assert _scoped() in line
    assert "WRONG" not in line


def test_a_usage_cache_write_failure_does_not_hide_the_segment(home, monkeypatch):
    _usage_home(home)
    monkeypatch.setattr(
        statusline.tempfile, "mkstemp",
        lambda *a, **k: (_ for _ in ()).throw(OSError("cache unavailable")),
    )
    assert _scoped() in _render({"model": {"display_name": "Fable 5"}}, home)
    assert not _usage_cache_path(home).exists()


def _count_config_parses(monkeypatch, home_dir, payload):
    """Render once, returning how often ~/.claude.json itself was parsed."""
    config_text = (home_dir / ".claude.json").read_text(encoding="utf-8")
    real_loads = statusline.json.loads
    seen = []

    def counting_loads(text, *args, **kwargs):
        seen.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(statusline.json, "loads", counting_loads)
    try:
        line = _render(payload, home_dir)
    finally:
        monkeypatch.undo()
    return line, seen.count(config_text)


def test_claude_json_is_parsed_at_most_once_per_render(home, monkeypatch):
    """~/.claude.json is multi-MB. The account segment and the usage lookup
    share ONE read and ONE parse of it, never two."""
    _usage_home(home)
    payload = {"model": {"display_name": "Fable 5"}}

    line, cold_parses = _count_config_parses(monkeypatch, home, payload)
    assert _scoped() in line
    assert cold_parses == 1

    # Only the usage half recomputes now; the account half stays cached, so
    # the big file must not be parsed at all.
    _usage_cache_path(home).unlink()
    line, warm_parses = _count_config_parses(monkeypatch, home, payload)
    assert _scoped() in line
    assert warm_parses == 0


# --- real schema ----------------------------------------------------------- #

def test_the_query_runs_against_the_real_jacked_schema(home, tmp_path):
    """Pin the live column names: a rename in the accounts table must fail
    here, not silently blank the segment on every user's machine."""
    from jacked.web.database import Database

    db_path = home / ".claude" / "jacked.db"
    Database(str(db_path))          # creates the real schema, then closes

    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            "INSERT INTO accounts (email, organization_uuid, access_token, "
            "expires_at, cached_usage_raw, usage_cached_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (EMAIL, ORG, "not-a-real-token", 0,
             _raw(_session_limit(), _weekly_all(76), _weekly_scoped("Fable", 96)),
             FRESH_AT),
        )
        con.commit()
    finally:
        con.close()
    _write_account(home, emailAddress=EMAIL, organizationUuid=ORG)

    assert _scoped() in _render({"model": {"display_name": "Fable 5"}}, home)


# --- safety and security contracts ----------------------------------------- #

def _statusline_source():
    return (REPO_ROOT / "jacked" / "statusline.py").read_text(encoding="utf-8")


def _code_string_literals(source):
    """Every string literal the module EXECUTES, docstrings excluded.

    Comments never reach the AST, so what survives here is exactly the text
    the module can act on.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_statusline_never_reads_the_credentials_file():
    """~/.claude/.credentials.json holds live OAuth tokens. The statusline
    resolves identity from ~/.claude.json instead; that boundary is a
    security contract, not an implementation detail."""
    source = _statusline_source()

    for literal in _code_string_literals(source):
        assert "credential" not in literal.lower(), literal

    # And the boundary stays documented, so it cannot be dropped quietly.
    assert ".credentials.json" in ast.get_docstring(ast.parse(source))


def test_every_database_connection_is_opened_read_only():
    """jacked's service owns jacked.db. The statusline may look, never touch."""
    source = _statusline_source()
    opens = [i for i in range(len(source)) if source.startswith("sqlite3.connect", i)]

    assert opens, "the database open moved; re-point this contract test"
    for start in opens:
        # The URI is built a few lines above the call, so look both ways.
        window = source[max(0, start - 600):start + 200]
        assert "uri=True" in window, window
        assert "?mode=ro" in window, window
    assert "?mode=ro" in "".join(_code_string_literals(source))


def test_the_statusline_issues_exactly_one_read_only_query():
    """It reads a database another process owns. One SELECT, nothing else."""
    keywords = re.compile(
        r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE"
        r"|PRAGMA|ATTACH|VACUUM|BEGIN|COMMIT)\b"
    )
    sql = [
        literal
        for literal in _code_string_literals(_statusline_source())
        if keywords.search(literal)
    ]

    assert len(sql) == 1, sql
    assert sql[0].startswith("SELECT ")
    assert " FROM accounts " in sql[0]
    assert not keywords.search(sql[0][len("SELECT"):])


def test_the_statusline_imports_nothing_heavy_at_module_scope():
    """click/rich cost ~50ms and sqlite3 ~12ms; the budget cannot afford them
    on a render that never reaches the database."""
    tree = ast.parse(_statusline_source())
    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.add(node.module.split(".")[0])

    assert top_level == {"hashlib", "json", "os", "sys", "tempfile", "time"}


def test_a_full_render_stays_well_inside_the_refresh_budget(home):
    """Claude Code re-runs this on every refresh, budget ~300ms including
    interpreter start, so the render itself must be a small fraction."""
    _usage_home(home)
    # A realistically bloated ~/.claude.json: the real one carries per-project
    # history and reaches megabytes.
    config = {
        "oauthAccount": {
            "emailAddress": EMAIL,
            "organizationUuid": ORG,
            "organizationName": "MyOrg",
            "userRateLimitTier": "default_claude_max_5x",
        },
        "projects": {f"/p/{i}": {"history": ["x" * 400]} for i in range(4000)},
    }
    (home / ".claude.json").write_text(json.dumps(config), encoding="utf-8")
    assert (home / ".claude.json").stat().st_size > 1_500_000
    payload = _full_payload()

    started = time.monotonic()
    cold = _render(payload, home)
    cold_elapsed = time.monotonic() - started

    started = time.monotonic()
    warm = _render(payload, home)
    warm_elapsed = time.monotonic() - started

    assert _scoped() in cold and _scoped() in warm
    assert cold_elapsed < 0.15, f"cold render took {cold_elapsed:.3f}s"
    assert warm_elapsed < 0.05, f"warm render took {warm_elapsed:.3f}s"


def test_statusline_doctests_run():
    """CI runs bare pytest with testpaths=["tests"] and no --doctest-modules,
    so the renderer's own doctests would otherwise never execute."""
    import doctest

    results = doctest.testmod(statusline)
    assert results.attempted > 0
    assert results.failed == 0

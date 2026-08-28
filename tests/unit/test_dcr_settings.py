"""Tests for jacked.dcr_settings: the DCR review-engine config.

The load-bearing behaviors:

* an EXISTING-but-unreadable jacked-dcr.json raises on read (rather than reading
  as defaults, which the next write would then clobber), while a genuinely
  absent file is the benign not-configured-yet case,
* ``resolve`` never raises, never writes, and never logs: a corrupt or
  hand-mangled file degrades to Claude with a reason, so a broken config can
  never block a code review, the corrupt bytes survive for the user to repair,
  and the ``--json`` stream the /dcr command parses stays clean, and
* ``update_config`` is the one read-modify-write both surfaces share: it
  self-heals stale invalid stored values and holds a cross-process lock.

Every test that touches the config path uses tmp_path, so the real ~/.claude is
never read or written. ``codex_preflight`` is always driven through mocked
``shutil.which`` / ``subprocess.run`` — no test ever spawns the real codex CLI.
"""
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from jacked import dcr_settings
from jacked.dcr_settings import DcrSettingsAccessError, DcrSettingsUnreadableError
from tests._platform import requires_posix_dir_permissions, requires_posix_file_read_permissions

# The exact contract keys resolve() promises the /dcr command and the dashboard.
RESOLVE_KEYS = {
    "engine", "model", "effort", "keep_on_claude", "usable", "reason",
    "codex_installed", "codex_logged_in", "codex_path", "schema_path",
}

# chmod-based refusals are meaningless as root, and fcntl is POSIX-only.
skip_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores chmod"
)
needs_flock = pytest.mark.skipif(
    dcr_settings.fcntl is None, reason="no fcntl on this platform"
)


def _write_config(home, payload) -> None:
    """Write raw bytes/text to the config path, bypassing validation."""
    path = dcr_settings.config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# Paths + defaults
# --------------------------------------------------------------------------- #

def test_config_path_shape(tmp_path):
    assert dcr_settings.config_path(tmp_path) == tmp_path / ".claude" / "jacked-dcr.json"


def test_read_missing_returns_defaults(tmp_path):
    assert dcr_settings.read_config(tmp_path) == dcr_settings.DEFAULTS


def test_read_missing_returns_a_copy_not_the_defaults_object(tmp_path):
    """A caller mutating the read result must not poison DEFAULTS for the process."""
    got = dcr_settings.read_config(tmp_path)
    got["engine"] = "codex"
    got["keep_on_claude"].append("Mutated")
    assert dcr_settings.DEFAULTS["engine"] == "claude"
    assert dcr_settings.DEFAULTS["keep_on_claude"] == ["Security", "Frontend Design"]


def test_defaults_are_valid_for_write(tmp_path):
    """The defaults must themselves pass validation (a default that cannot be
    written would break `dcr engine set` on a fresh machine)."""
    dcr_settings.write_config(tmp_path, dcr_settings.read_config(tmp_path))
    assert dcr_settings.config_path(tmp_path).exists()


def test_engine_and_effort_choice_tuples_match_the_valid_sets():
    assert set(dcr_settings.ENGINE_CHOICES) == dcr_settings.VALID_ENGINES
    assert set(dcr_settings.EFFORT_CHOICES) == dcr_settings.VALID_EFFORTS
    assert dcr_settings.VALID_EFFORTS == {
        "none", "minimal", "low", "medium", "high", "xhigh", "max",
    }


# --------------------------------------------------------------------------- #
# read_config
# --------------------------------------------------------------------------- #

def test_read_fills_missing_keys_and_preserves_unknown_ones(tmp_path):
    _write_config(tmp_path, {"engine": "codex", "future_key": {"a": 1}})
    got = dcr_settings.read_config(tmp_path)
    assert got["engine"] == "codex"
    assert got["model"] == dcr_settings.DEFAULTS["model"]
    assert got["effort"] == dcr_settings.DEFAULTS["effort"]
    assert got["keep_on_claude"] == dcr_settings.DEFAULTS["keep_on_claude"]
    # A newer jacked may write fields this build doesn't know; dropping them on
    # the next write would be silent data loss.
    assert got["future_key"] == {"a": 1}


def test_read_does_not_validate_values(tmp_path):
    """Tolerant read: a hand-edited typo comes back as-is so the user can see it."""
    _write_config(tmp_path, {"engine": "gemini", "effort": "turbo", "model": ""})
    got = dcr_settings.read_config(tmp_path)
    assert got["engine"] == "gemini"
    assert got["effort"] == "turbo"
    assert got["model"] == ""


def test_read_corrupt_raises(tmp_path):
    _write_config(tmp_path, "{ not valid json")
    with pytest.raises(DcrSettingsUnreadableError):
        dcr_settings.read_config(tmp_path)


def test_read_non_object_raises(tmp_path):
    _write_config(tmp_path, [1, 2, 3])
    with pytest.raises(DcrSettingsUnreadableError):
        dcr_settings.read_config(tmp_path)


# --------------------------------------------------------------------------- #
# write_config
# --------------------------------------------------------------------------- #

def test_write_round_trip_stamps_version_and_leaves_no_tmp(tmp_path):
    dcr_settings.write_config(tmp_path, {
        "engine": "codex",
        "model": "gpt-5.6-luna",
        "effort": "xhigh",
        "keep_on_claude": ["Security"],
    })
    on_disk = json.loads(dcr_settings.config_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk == {
        "version": 1,
        "engine": "codex",
        "model": "gpt-5.6-luna",
        "effort": "xhigh",
        "keep_on_claude": ["Security"],
    }
    # Writer-unique temp is cleaned up.
    assert not list((tmp_path / ".claude").glob(".jacked-dcr.json.*.tmp"))


def test_write_creates_parent_dirs(tmp_path):
    dcr_settings.write_config(tmp_path / "deep" / "nested", dcr_settings.DEFAULTS)
    assert dcr_settings.config_path(tmp_path / "deep" / "nested").exists()


def test_write_does_not_mutate_the_callers_dict(tmp_path):
    config = {"engine": "claude", "model": "m", "effort": "low", "keep_on_claude": []}
    dcr_settings.write_config(tmp_path, config)
    assert "version" not in config


@pytest.mark.parametrize("bad", [
    {"engine": "gemini"},
    {"engine": None},
    {"effort": "turbo"},
    {"effort": None},
    {"model": ""},
    {"model": "   "},
    {"model": None},
    {"model": 5},
    # Shell metacharacters: /dcr interpolates the model into a `codex exec -m`
    # command line, so the write boundary must rule out injection shapes.
    {"model": 'gpt"; rm -rf ~; echo "'},
    {"model": "gpt-5.6-luna; touch /tmp/pwn"},
    {"model": "gpt $(whoami)"},
    {"model": "gpt`id`"},
    {"model": "gpt luna"},
    {"model": "-gpt"},
    {"keep_on_claude": "Security"},
    {"keep_on_claude": ["Security", ""]},
    {"keep_on_claude": ["Security", 7]},
])
def test_write_rejects_invalid_values_and_leaves_the_file_untouched(tmp_path, bad):
    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    before = dcr_settings.config_path(tmp_path).read_text(encoding="utf-8")

    config = dict(dcr_settings.DEFAULTS)
    config.update(bad)
    with pytest.raises(ValueError):
        dcr_settings.write_config(tmp_path, config)

    assert dcr_settings.config_path(tmp_path).read_text(encoding="utf-8") == before
    assert not list((tmp_path / ".claude").glob(".jacked-dcr.json.*.tmp"))


def test_write_is_atomic_replace_not_truncate(tmp_path):
    """os.replace (not an in-place rewrite) is what makes a concurrent reader
    see either the old file or the new one, never a half-written one."""
    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    with patch("jacked.dcr_settings.os.replace") as replace:
        dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    assert replace.called
    # The replace was mocked out, so the tmp survived — prove a tmp really existed.
    assert list((tmp_path / ".claude").glob(".jacked-dcr.json.*.tmp"))


def test_write_post_verification_raises_when_the_file_reads_back_corrupt(tmp_path):
    """A write that lands corrupt must be surfaced loudly, not trusted."""
    with patch("jacked.dcr_settings.json.loads", side_effect=json.JSONDecodeError("x", "y", 0)):
        with pytest.raises(DcrSettingsUnreadableError):
            dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)


# --------------------------------------------------------------------------- #
# clear_config
# --------------------------------------------------------------------------- #

def test_clear_reports_whether_a_file_existed(tmp_path):
    assert dcr_settings.clear_config(tmp_path) is False
    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    assert dcr_settings.clear_config(tmp_path) is True
    assert not dcr_settings.config_path(tmp_path).exists()
    assert dcr_settings.clear_config(tmp_path) is False


@skip_as_root
@requires_posix_dir_permissions
def test_clear_raises_a_named_access_error_when_the_delete_is_refused(tmp_path):
    """Never report "cleared" when the filesystem said no — and never leak a raw
    PermissionError traceback at the user."""
    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    claude_dir = tmp_path / ".claude"
    claude_dir.chmod(0o500)  # r-x: the entry cannot be unlinked
    try:
        with pytest.raises(DcrSettingsAccessError) as excinfo:
            dcr_settings.clear_config(tmp_path)
    finally:
        claude_dir.chmod(0o700)

    assert str(dcr_settings.config_path(tmp_path)) in str(excinfo.value)
    assert "permissions" in str(excinfo.value)
    assert dcr_settings.config_path(tmp_path).exists()


# --------------------------------------------------------------------------- #
# Filesystem refusals on write
# --------------------------------------------------------------------------- #

@skip_as_root
@requires_posix_dir_permissions
def test_write_raises_a_named_access_error_when_the_home_is_unwritable(tmp_path):
    """PermissionError must not escape: both surfaces catch one named error and
    turn it into a friendly failure (CLI [FAIL] + exit 1, API 503)."""
    home = tmp_path / "locked"
    home.mkdir(mode=0o500)
    try:
        with pytest.raises(DcrSettingsAccessError) as excinfo:
            dcr_settings.write_config(home, dcr_settings.DEFAULTS)
    finally:
        home.chmod(0o700)

    message = str(excinfo.value)
    assert str(dcr_settings.config_path(home)) in message
    assert "permissions" in message
    assert "Permission denied" in message


def test_write_access_error_is_not_confused_with_an_unreadable_config(tmp_path):
    """The two failures have different fixes, so they are different types."""
    assert not issubclass(DcrSettingsAccessError, DcrSettingsUnreadableError)
    assert not issubclass(DcrSettingsUnreadableError, DcrSettingsAccessError)


def test_write_converts_a_replace_failure_and_still_cleans_up_its_tmp(tmp_path):
    with patch("jacked.dcr_settings.os.replace", side_effect=OSError("no space left")):
        with pytest.raises(DcrSettingsAccessError) as excinfo:
            dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    assert "no space left" in str(excinfo.value)
    assert not list((tmp_path / ".claude").glob(".jacked-dcr.json.*.tmp"))


# --------------------------------------------------------------------------- #
# update_config — the one read-modify-write both surfaces share
# --------------------------------------------------------------------------- #

def test_update_applies_only_the_fields_given(tmp_path):
    dcr_settings.write_config(tmp_path, {
        "engine": "claude", "model": "o3", "effort": "low",
        "keep_on_claude": ["Security"],
    })
    got = dcr_settings.update_config(tmp_path, engine="codex")
    assert got == {
        "version": 1, "engine": "codex", "model": "o3", "effort": "low",
        "keep_on_claude": ["Security"],
    }
    assert dcr_settings.read_config(tmp_path) == got


def test_update_preserves_unknown_keys(tmp_path):
    """A newer jacked may have written fields this build does not know."""
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, "future_key": {"a": 1}})
    got = dcr_settings.update_config(tmp_path, effort="low")
    assert got["future_key"] == {"a": 1}
    assert got["effort"] == "low"


def test_update_strips_the_model(tmp_path):
    got = dcr_settings.update_config(tmp_path, engine="codex", model="  o3  ")
    assert got["model"] == "o3"


def test_update_on_a_fresh_home_writes_the_defaults_plus_the_change(tmp_path):
    got = dcr_settings.update_config(tmp_path, engine="codex")
    assert got["engine"] == "codex"
    assert got["model"] == dcr_settings.DEFAULTS["model"]
    assert dcr_settings.config_path(tmp_path).exists()


def test_update_rejects_an_unusable_argument_without_touching_the_file(tmp_path):
    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    before = dcr_settings.config_path(tmp_path).read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        dcr_settings.update_config(tmp_path, engine="codex", model="gpt; rm -rf ~")
    assert dcr_settings.config_path(tmp_path).read_text(encoding="utf-8") == before


def test_update_refuses_to_clobber_an_unparseable_file(tmp_path):
    _write_config(tmp_path, "{ not valid json")
    with pytest.raises(DcrSettingsUnreadableError):
        dcr_settings.update_config(tmp_path, engine="claude")
    assert dcr_settings.config_path(tmp_path).read_text(encoding="utf-8") == "{ not valid json"


@pytest.mark.parametrize("stored", [
    {"effort": "turbo"},
    {"effort": None},
    {"model": ""},
    {"model": "gpt luna"},
    {"engine": "gemini"},
    {"keep_on_claude": "Security"},
])
def test_update_self_heals_a_stale_invalid_field_instead_of_locking_the_user_out(
    tmp_path, stored,
):
    """The lockout regression: one stale hand-edited value used to fail
    validation on the way out, so the user could not change ANY other field —
    including switching back to Claude, the escape hatch from a broken setup."""
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, **stored})

    got = dcr_settings.update_config(tmp_path, engine="claude")

    assert got["engine"] == "claude"
    field = next(iter(stored))
    if field != "engine":
        assert got[field] == dcr_settings.DEFAULTS[field]
    assert dcr_settings.read_config(tmp_path)["engine"] == "claude"


def test_update_heals_before_it_applies_so_the_update_still_wins(tmp_path):
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, "effort": "turbo"})
    got = dcr_settings.update_config(tmp_path, engine="codex", effort="low")
    assert got["effort"] == "low"


@skip_as_root
@requires_posix_dir_permissions
def test_update_surfaces_a_filesystem_refusal_as_an_access_error(tmp_path):
    home = tmp_path / "locked"
    home.mkdir(mode=0o500)
    try:
        with pytest.raises(DcrSettingsAccessError):
            dcr_settings.update_config(home, engine="codex")
    finally:
        home.chmod(0o700)


@needs_flock
def test_update_is_serialized_across_concurrent_writers(tmp_path):
    """Two writers merging DIFFERENT fields must both land. Without the lock each
    reads the old config and the last one out drops the other's field; the sleep
    inside the write forces that window wide open."""
    real_write = dcr_settings.write_config

    def slow_write(home, config):
        import time as _time
        _time.sleep(0.3)
        real_write(home, config)

    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    errors: list[BaseException] = []

    def run(**kwargs):
        try:
            dcr_settings.update_config(tmp_path, **kwargs)
        except BaseException as e:  # pragma: no cover - surfaced below
            errors.append(e)

    with patch("jacked.dcr_settings.write_config", side_effect=slow_write):
        threads = [
            threading.Thread(target=run, kwargs={"engine": "codex"}),
            threading.Thread(target=run, kwargs={"effort": "low"}),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    assert errors == []
    final = dcr_settings.read_config(tmp_path)
    assert final["engine"] == "codex", "the engine update was lost"
    assert final["effort"] == "low", "the effort update was lost"


@needs_flock
def test_update_fails_open_rather_than_hanging_on_a_wedged_lock(tmp_path):
    """A stuck holder must never wedge `jacked dcr engine set` forever: the lock
    is advisory and fail-open, so the write proceeds after the timeout."""
    import fcntl

    original = dcr_settings._config_lock

    def quick_lock(home, timeout=5.0):
        return original(home, timeout=0.1)

    lock = dcr_settings.lock_path(tmp_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with patch("jacked.dcr_settings._config_lock", quick_lock):
            got = dcr_settings.update_config(tmp_path, engine="codex")

    assert got["engine"] == "codex"
    assert dcr_settings.read_config(tmp_path)["engine"] == "codex"


def test_lock_path_sits_next_to_the_config(tmp_path):
    assert dcr_settings.lock_path(tmp_path) == (
        tmp_path / ".claude" / "jacked-dcr.json.lock"
    )


# --------------------------------------------------------------------------- #
# codex_preflight (subprocess always mocked)
# --------------------------------------------------------------------------- #

def test_preflight_not_installed():
    with patch("jacked.dcr_settings.shutil.which", return_value=None), \
            patch("jacked.dcr_settings.subprocess.run") as run:
        got = dcr_settings.codex_preflight()
    run.assert_not_called()
    assert got == {
        "codex_installed": False,
        "codex_logged_in": False,
        "codex_path": None,
        "reason": "Codex CLI is not installed. Install it, then run: codex login",
    }


def test_preflight_installed_and_logged_in():
    with patch("jacked.dcr_settings.shutil.which", return_value="/usr/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "Logged in", "")
        got = dcr_settings.codex_preflight()
    assert run.call_args.args[0] == ["/usr/bin/codex", "login", "status"]
    assert got == {
        "codex_installed": True,
        "codex_logged_in": True,
        "codex_path": "/usr/bin/codex",
        "reason": None,
    }


def test_preflight_never_inherits_stdin():
    """A codex build that decided to prompt would hang the /dcr hot path."""
    with patch("jacked.dcr_settings.shutil.which", return_value="/usr/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        dcr_settings.codex_preflight()
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_preflight_reports_the_resolved_binary_when_not_signed_in():
    """Which codex? With several on PATH, the path is the diagnosable part."""
    with patch("jacked.dcr_settings.shutil.which", return_value="/opt/homebrew/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, "", "")
        got = dcr_settings.codex_preflight()
    assert got["codex_path"] == "/opt/homebrew/bin/codex"


def test_preflight_installed_but_not_logged_in():
    with patch("jacked.dcr_settings.shutil.which", return_value="/usr/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, "", "Not logged in")
        got = dcr_settings.codex_preflight()
    assert got == {
        "codex_installed": True,
        "codex_logged_in": False,
        "codex_path": "/usr/bin/codex",
        "reason": "Codex CLI is not signed in. Run: codex login",
    }


def test_preflight_timeout_degrades_instead_of_raising():
    with patch("jacked.dcr_settings.shutil.which", return_value="/usr/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run",
                  side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=10.0)):
        got = dcr_settings.codex_preflight()
    assert got["codex_installed"] is True
    assert got["codex_logged_in"] is False
    assert got["reason"] == "Codex CLI did not respond within 10s"


def test_preflight_spawn_failure_degrades_instead_of_raising():
    with patch("jacked.dcr_settings.shutil.which", return_value="/usr/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run", side_effect=OSError("boom")):
        got = dcr_settings.codex_preflight()
    assert got["codex_installed"] is True
    assert got["codex_logged_in"] is False
    assert "boom" in got["reason"]


def test_preflight_honors_the_timeout_argument():
    with patch("jacked.dcr_settings.shutil.which", return_value="/usr/bin/codex"), \
            patch("jacked.dcr_settings.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        dcr_settings.codex_preflight(timeout=2.5)
    assert run.call_args.kwargs["timeout"] == 2.5


# --------------------------------------------------------------------------- #
# schema_path
# --------------------------------------------------------------------------- #

def test_schema_path_exists_and_parses():
    path = dcr_settings.schema_path()
    assert path.is_absolute()
    assert path.exists(), f"packaged schema missing at {path}"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["required"] == ["lens_reports"]
    assert schema["additionalProperties"] is False
    lens = schema["$defs"]["lens_report"]
    assert set(lens["required"]) == {"lens", "status", "findings", "what_looks_good"}
    assert lens["properties"]["status"]["enum"] == ["PASS", "ISSUES_FOUND"]
    finding = schema["$defs"]["finding"]
    assert set(finding["required"]) == {
        "severity", "title", "file", "line_start", "line_end",
        "trigger", "why", "confidence", "recommendation",
    }
    assert finding["properties"]["severity"]["enum"] == ["CRITICAL", "MEDIUM", "LOW"]
    assert finding["additionalProperties"] is False


# --------------------------------------------------------------------------- #
# resolve
# --------------------------------------------------------------------------- #

def test_resolve_claude_runs_no_preflight(tmp_path):
    with patch("jacked.dcr_settings.codex_preflight") as preflight:
        got = dcr_settings.resolve(tmp_path)
    preflight.assert_not_called()
    assert set(got) == RESOLVE_KEYS
    assert got["engine"] == "claude"
    assert got["usable"] is True
    assert got["reason"] is None
    assert got["codex_installed"] is None
    assert got["codex_logged_in"] is None
    assert got["schema_path"] == str(dcr_settings.schema_path())


def test_resolve_codex_usable(tmp_path):
    dcr_settings.write_config(tmp_path, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight", return_value={
        "codex_installed": True, "codex_logged_in": True, "reason": None,
    }):
        got = dcr_settings.resolve(tmp_path)
    assert set(got) == RESOLVE_KEYS
    assert got["engine"] == "codex"
    assert got["model"] == "gpt-5.6-luna"
    assert got["effort"] == "xhigh"
    assert got["keep_on_claude"] == ["Security", "Frontend Design"]
    assert got["usable"] is True
    assert got["reason"] is None
    assert got["codex_installed"] is True
    assert got["codex_logged_in"] is True


@pytest.mark.parametrize("check", [
    {"codex_installed": False, "codex_logged_in": False, "reason": "Codex CLI is not installed"},
    {"codex_installed": True, "codex_logged_in": False, "reason": "Codex CLI is not signed in. Run: codex login"},
])
def test_resolve_codex_not_usable_carries_the_preflight_reason(tmp_path, check):
    dcr_settings.write_config(tmp_path, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight", return_value=check):
        got = dcr_settings.resolve(tmp_path)
    assert got["usable"] is False
    assert got["reason"] == check["reason"]
    assert got["codex_installed"] is check["codex_installed"]
    assert got["codex_logged_in"] is check["codex_logged_in"]


def test_resolve_preflight_false_skips_the_subprocess(tmp_path):
    dcr_settings.write_config(tmp_path, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight") as preflight:
        got = dcr_settings.resolve(tmp_path, preflight=False)
    preflight.assert_not_called()
    assert got["engine"] == "codex"
    assert got["usable"] is True
    assert got["codex_installed"] is None
    assert got["codex_logged_in"] is None


def test_resolve_corrupt_config_degrades_to_claude_without_writing(tmp_path):
    _write_config(tmp_path, "{ not valid json")
    before = dcr_settings.config_path(tmp_path).read_text(encoding="utf-8")

    got = dcr_settings.resolve(tmp_path)

    assert set(got) == RESOLVE_KEYS
    assert got["engine"] == "claude"
    assert got["usable"] is True, "a corrupt config must never block a review"
    # The REAL cause, not a hardcoded "corrupt JSON" guess (see the
    # permission-denied test below for the other half of that contract).
    assert "is not valid JSON" in got["reason"]
    assert str(dcr_settings.config_path(tmp_path)) in got["reason"]
    assert got["reason"].endswith("; using Claude until it is fixed")
    assert got["codex_installed"] is None
    assert got["codex_logged_in"] is None
    assert got["codex_path"] is None
    # The corrupt bytes must survive for the user to repair.
    assert dcr_settings.config_path(tmp_path).read_text(encoding="utf-8") == before


def test_resolve_on_a_corrupt_config_logs_nothing(tmp_path, caplog):
    """Claude Code's Bash merges stderr into stdout, so ANY log line here would
    break the --json contract the /dcr command parses. `reason` is the channel."""
    _write_config(tmp_path, "{ not valid json")
    with caplog.at_level("DEBUG", logger="jacked.dcr_settings"):
        got = dcr_settings.resolve(tmp_path)
    assert caplog.records == []
    assert got["reason"]


@skip_as_root
@requires_posix_file_read_permissions
def test_resolve_on_an_unreadable_file_reports_the_access_error(tmp_path):
    """A permission problem must not be reported as a JSON syntax problem: the
    user would hunt for a typo that is not there."""
    dcr_settings.write_config(tmp_path, dcr_settings.DEFAULTS)
    path = dcr_settings.config_path(tmp_path)
    path.chmod(0o000)
    try:
        got = dcr_settings.resolve(tmp_path)
    finally:
        path.chmod(0o600)

    assert got["engine"] == "claude"
    assert got["usable"] is True
    assert "Permission denied" in got["reason"]
    assert "is not valid JSON" not in got["reason"]
    assert got["reason"].endswith("; using Claude until it is fixed")


# --------------------------------------------------------------------------- #
# resolve: re-validation of everything jacked did not write
#
# The file is hand-editable and other builds write it, so every field is checked
# again on the way out. A bad value can neither crash a consumer nor reach the
# `codex exec -m "<model>"` command line.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stored, field, expected", [
    # Engine the build cannot run.
    ({"engine": "gemini"}, "engine", "claude"),
    ({"engine": None}, "engine", "claude"),
    ({"engine": 7}, "engine", "claude"),
    # Effort typos and wrong types.
    ({"effort": "turbo"}, "effort", "xhigh"),
    ({"effort": None}, "effort", "xhigh"),
    ({"effort": ["high"]}, "effort", "xhigh"),
    # Model: blank, wrong type, and injection shapes.
    ({"model": ""}, "model", "gpt-5.6-luna"),
    ({"model": "   "}, "model", "gpt-5.6-luna"),
    ({"model": None}, "model", "gpt-5.6-luna"),
    ({"model": 5}, "model", "gpt-5.6-luna"),
    ({"model": 'gpt"; touch /tmp/x; "'}, "model", "gpt-5.6-luna"),
    ({"model": "gpt $(whoami)"}, "model", "gpt-5.6-luna"),
    ({"model": "gpt`id`"}, "model", "gpt-5.6-luna"),
    # keep_on_claude: a bare string must NOT expand per character, and a number
    # must not raise.
    ({"keep_on_claude": "Security, Frontend Design"}, "keep_on_claude",
     ["Security", "Frontend Design"]),
    ({"keep_on_claude": 5}, "keep_on_claude", ["Security", "Frontend Design"]),
    ({"keep_on_claude": None}, "keep_on_claude", ["Security", "Frontend Design"]),
    ({"keep_on_claude": ["Security", ""]}, "keep_on_claude",
     ["Security", "Frontend Design"]),
    ({"keep_on_claude": ["Security", 7]}, "keep_on_claude",
     ["Security", "Frontend Design"]),
    ({"keep_on_claude": {"a": 1}}, "keep_on_claude", ["Security", "Frontend Design"]),
])
def test_resolve_substitutes_the_default_and_explains_it(tmp_path, stored, field, expected):
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, **stored})

    got = dcr_settings.resolve(tmp_path)

    assert set(got) == RESOLVE_KEYS
    assert got[field] == expected
    assert field in got["reason"], got["reason"]
    assert got["usable"] is True


def test_resolve_keeps_a_valid_stored_engine_out_of_the_reason(tmp_path):
    """Only a genuinely wrong value earns a reason line."""
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, "engine": "claude"})
    assert dcr_settings.resolve(tmp_path)["reason"] is None


def test_resolve_echoes_a_bad_value_safely_and_short(tmp_path):
    """The bad value is repr'd (never raw) and truncated, so a hand-pasted blob
    cannot become a megabyte-long reason or dump control characters."""
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, "model": "x " * 500})
    reason = dcr_settings.resolve(tmp_path)["reason"]
    assert len(reason) < 200
    assert reason.endswith(f"is not a valid model id; using {dcr_settings.DEFAULTS['model']}")


def test_resolve_joins_several_reasons(tmp_path):
    _write_config(tmp_path, {"engine": "gemini", "effort": "turbo", "model": "a b"})
    reason = dcr_settings.resolve(tmp_path)["reason"]
    assert "engine" in reason and "effort" in reason and "model" in reason
    assert reason.count("; using") == 3


def test_resolve_survives_every_json_shape_in_every_field(tmp_path):
    """resolve() promises never to raise for ANY json-representable content."""
    junk = [None, True, 0, -1.5, "", "  ", "x" * 300, [], [1, 2], {"a": 1}, [[]]]
    for value in junk:
        for field in ("engine", "model", "effort", "keep_on_claude", "version"):
            _write_config(tmp_path, {field: value})
            got = dcr_settings.resolve(tmp_path)
            assert set(got) == RESOLVE_KEYS
            assert got["engine"] in dcr_settings.VALID_ENGINES
            assert got["effort"] in dcr_settings.VALID_EFFORTS
            assert dcr_settings._MODEL_RE.fullmatch(got["model"])
            assert isinstance(got["keep_on_claude"], list)


def test_resolve_sanitized_values_would_pass_the_write_validator(tmp_path):
    """The contract can only ever hand out values jacked itself could write —
    that is what keeps a shell-unsafe model off the `codex exec` command line."""
    _write_config(tmp_path, {
        "engine": "gemini", "effort": "turbo",
        "model": 'gpt"; rm -rf ~; echo "', "keep_on_claude": "Security",
    })
    got = dcr_settings.resolve(tmp_path)
    dcr_settings._validate({k: got[k] for k in
                            ("engine", "model", "effort", "keep_on_claude")})


def test_resolve_bad_stored_model_still_reports_codex_preflight(tmp_path):
    """A substituted field must not swallow the preflight explanation."""
    _write_config(tmp_path, {**dcr_settings.DEFAULTS, "engine": "codex", "model": "a b"})
    with patch("jacked.dcr_settings.codex_preflight", return_value={
        "codex_installed": True, "codex_logged_in": False, "codex_path": "/bin/codex",
        "reason": "Codex CLI is not signed in. Run: codex login",
    }):
        got = dcr_settings.resolve(tmp_path)
    assert got["usable"] is False
    assert "model" in got["reason"]
    assert "codex login" in got["reason"]
    assert got["codex_path"] == "/bin/codex"


def test_resolve_never_writes_a_config_file(tmp_path):
    dcr_settings.resolve(tmp_path)
    assert not dcr_settings.config_path(tmp_path).exists()


def test_resolve_keep_on_claude_is_a_fresh_list(tmp_path):
    """Callers mutating the returned list must not corrupt shared state."""
    got = dcr_settings.resolve(tmp_path)
    got["keep_on_claude"].append("Mutated")
    assert dcr_settings.resolve(tmp_path)["keep_on_claude"] == [
        "Security", "Frontend Design",
    ]


# --------------------------------------------------------------------------- #
# jacked_home
# --------------------------------------------------------------------------- #

def test_jacked_home_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    assert dcr_settings.jacked_home() == tmp_path


def test_jacked_home_matches_the_vault_resolution(monkeypatch, tmp_path):
    from jacked.memory import vault

    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    assert dcr_settings.jacked_home() == vault.jacked_home()
    monkeypatch.delenv("JACKED_HOME")
    assert dcr_settings.jacked_home() == vault.jacked_home()


# --------------------------------------------------------------------------- #
# Cross-language parity
#
# The effort list lives in Python (here) and again in the dashboard's JS, which
# cannot import it. Nothing but a test stops the two copies from drifting, and a
# drifted copy shows the user a menu entry the API then rejects with a 422.
# --------------------------------------------------------------------------- #

def test_the_dashboard_js_effort_list_matches_the_python_one():
    settings_js = (
        Path(dcr_settings.__file__).parent
        / "data" / "web" / "js" / "components" / "settings.js"
    )
    source = settings_js.read_text(encoding="utf-8")
    match = re.search(r"const DCR_EFFORT_LEVELS\s*=\s*\[(.*?)\]\s*;", source, re.S)
    assert match, f"DCR_EFFORT_LEVELS array literal not found in {settings_js}"
    js_levels = re.findall(r"'([^']*)'", match.group(1))
    assert js_levels == list(dcr_settings.EFFORT_CHOICES)

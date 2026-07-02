"""Tray-on-by-default + the first-run install nag.

The tray is now a CORE dependency, so `jacked install` registers/starts it by
default; `--no-tray` opts out. And because `pip`/`uv tool install` run no code,
a loud banner + offer fires the first time `jacked` runs before `jacked install`
has completed. That banner must NEVER fire for internal shims, for `install`
itself, when already installed, or for any non-interactive caller — otherwise it
would corrupt scripts, CI, the test runner, and (critically) Claude Code hook
I/O.
"""
import io
from types import SimpleNamespace

from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked.cli import main


# --- _is_headless -----------------------------------------------------------

def test_is_headless_false_on_mac_and_windows(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli._is_headless() is False
    monkeypatch.setattr(cli.sys, "platform", "win32")
    assert cli._is_headless() is False


def test_is_headless_true_on_linux_without_display(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert cli._is_headless() is True


def test_is_headless_false_on_linux_with_display(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    assert cli._is_headless() is False


# --- _already_installed -----------------------------------------------------

def test_already_installed_reflects_manifest(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    assert cli._already_installed() is False
    (fake_home / ".claude" / "jacked-manifest.json").write_text("{}", encoding="utf-8")
    assert cli._already_installed() is True


# --- first-run nag: when it fires and when it stays silent ------------------

def _ctx(sub):
    return SimpleNamespace(invoked_subcommand=sub)


def _interactive(monkeypatch, *, installed: bool):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_already_installed", lambda: installed)
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=100))
    return buf


def test_nag_fires_and_offers_when_uninstalled_and_interactive(monkeypatch):
    buf = _interactive(monkeypatch, installed=False)
    asked = {"v": False}

    def fake_confirm(*a, **k):
        asked["v"] = True
        return False  # decline so it never invokes install

    monkeypatch.setattr(cli.click, "confirm", fake_confirm)
    cli._maybe_prompt_first_run(_ctx("doctor"))
    out = buf.getvalue()
    assert "jacked install" in out
    assert "ONE MORE STEP" in out
    assert asked["v"] is True


def test_nag_suppressed_for_internal_shims_and_install(monkeypatch):
    buf = _interactive(monkeypatch, installed=False)
    # click.confirm must never be reached for these — make it loud if it is.
    monkeypatch.setattr(cli.click, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))
    for sub in ("_hook", "_update_status", "_update_status_init", "install"):
        cli._maybe_prompt_first_run(_ctx(sub))
    assert buf.getvalue() == ""


def test_nag_silent_for_non_tty(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_already_installed", lambda: False)
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf))
    cli._maybe_prompt_first_run(_ctx("doctor"))
    assert buf.getvalue() == ""


def test_nag_silent_when_already_installed(monkeypatch):
    buf = _interactive(monkeypatch, installed=True)
    cli._maybe_prompt_first_run(_ctx("doctor"))
    assert buf.getvalue() == ""


# --- tray on by default / --no-tray opt-out ---------------------------------

def test_install_starts_tray_by_default(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    calls = []
    monkeypatch.setattr(cli, "_setup_tray_autostart", lambda: calls.append(1))
    r = CliRunner().invoke(main, ["install", "--no-rules"])
    assert r.exit_code == 0, r.output
    assert calls == [1]  # tray set up by default


def test_install_no_tray_skips_tray(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    calls = []
    monkeypatch.setattr(cli, "_setup_tray_autostart", lambda: calls.append(1))
    r = CliRunner().invoke(main, ["install", "--no-rules", "--no-tray"])
    assert r.exit_code == 0, r.output
    assert calls == []  # opted out


def test_bare_jacked_shows_help_not_usage_error(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    r = CliRunner().invoke(main, [])
    assert r.exit_code == 0
    assert "Usage:" in r.output or "Commands:" in r.output

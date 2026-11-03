"""The autouse guards in tests/conftest.py: no test reaches the real Claude Code CLI.

``_no_real_claude_binary`` hides ``claude`` from ``jacked.findbin.find_bin``,
and ``_no_real_claude_spawn`` refuses a spawn of it, which covers the bare
name that ``find_bin("claude") or "claude"`` falls back to. Before the
guards, one CliRunner ``jacked install`` on a developer machine ran the real
``claude mcp get`` and four ``claude plugin install`` calls.

These tests put stubs first on PATH, so a ``claude`` is found on every
machine, CI included. A stub writes a ``<name>.ran`` file next to itself
when it runs. Without a guard, the stub runs, never the developer's CLI.
"""

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Bound at collection, before any fixture runs: the real resolver.
from jacked.findbin import find_bin as real_find_bin

# The modules that bound ``find_bin`` at import and look up "claude".
_CLAUDE_LOOKUP_MODULES = (
    "jacked.credentials.runtime",
    "jacked.launch",
    "jacked.memory.capture",
    "jacked.skill_listing",
)
_REFUSAL = "the tests must not run the real claude binary"


def _stub(bin_dir: Path, name: str) -> Path:
    """An executable that writes ``<name>.ran`` next to itself and exits 0."""
    marker = bin_dir / f"{name}.ran"
    if sys.platform == "win32":
        path = bin_dir / f"{name}.cmd"
        path.write_text(f'@echo ran>>"{marker}"\r\n@exit /b 0\r\n', encoding="utf-8")
    else:
        path = bin_dir / name
        path.write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _ran(stub: Path) -> bool:
    """True when the stub has run (its marker file exists)."""
    name = stub.stem if stub.suffix == ".cmd" else stub.name
    return (stub.parent / f"{name}.ran").exists()


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """Put a ``claude`` stub and a ``jacked-probe-tool`` stub first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    made = {name: _stub(bin_dir, name) for name in ("claude", "jacked-probe-tool")}
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    return made


def _bound_modules():
    import importlib

    return [importlib.import_module(name) for name in _CLAUDE_LOOKUP_MODULES]


def _assert_refused(caught, program):
    assert caught.value.errno == errno.ENOENT
    assert caught.value.strerror == _REFUSAL
    assert caught.value.filename == program


# --------------------------------------------------------------------------- #
# The resolver guard
# --------------------------------------------------------------------------- #

def test_no_module_resolves_claude(stubs):
    import jacked.findbin as findbin

    # The real resolver finds the stub, so the guard has something to hide.
    assert Path(real_find_bin("claude")) == stubs["claude"]
    assert findbin.find_bin("claude") is None
    for module in _bound_modules():
        assert module.find_bin("claude") is None, module.__name__
    # Every other name resolves as before.
    assert Path(findbin.find_bin("jacked-probe-tool")) == stubs["jacked-probe-tool"]


def test_the_opt_in_fixture_gives_the_real_resolver(stubs, real_claude_binary):
    import jacked.findbin as findbin

    assert findbin.find_bin is real_find_bin
    assert Path(findbin.find_bin("claude")) == stubs["claude"]
    for module in _bound_modules():
        assert module.find_bin is real_find_bin, module.__name__


def test_jacked_install_starts_no_claude_process(stubs, tmp_path, monkeypatch):
    from click.testing import CliRunner

    import jacked.cli as cli

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setattr(cli, "_setup_tray_autostart", lambda: None)
    started = []
    spawn = subprocess.Popen  # subprocess.run spawns through this name too

    class _Spy(spawn):
        def __init__(self, args, *popen_args, **popen_kwargs):
            head = args[0] if isinstance(args, (list, tuple)) else str(args).split(" ")[0]
            if Path(str(head)).stem.lower() == "claude":
                started.append([Path(str(head)).name, *map(str, list(args)[1:3])])
            super().__init__(args, *popen_args, **popen_kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Spy)

    result = CliRunner().invoke(cli.main, ["install", "--no-rules", "--no-packs"])

    assert result.exit_code == 0, result.output
    assert started == []


# --------------------------------------------------------------------------- #
# The spawn tripwire
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "form", ["run-bare", "run-path", "run-shell-line", "popen-bare", "popen-executable"]
)
def test_a_claude_spawn_fails_as_a_missing_binary(stubs, form):
    stub = stubs["claude"]
    calls = {
        "run-bare": (lambda: subprocess.run(["claude", "--version"]), "claude"),
        "run-path": (lambda: subprocess.run([str(stub), "--version"]), str(stub)),
        "run-shell-line": (
            lambda: subprocess.run("claude --version", shell=True), "claude"
        ),
        "popen-bare": (lambda: subprocess.Popen(["claude"]), "claude"),
        # The program that runs is `executable`, whatever argv[0] says.
        "popen-executable": (
            lambda: subprocess.Popen(["anything"], executable=str(stub)), str(stub)
        ),
    }
    spawn, program = calls[form]

    with pytest.raises(FileNotFoundError) as caught:
        spawn()

    _assert_refused(caught, program)
    assert not _ran(stub)


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs extra rights on Windows")
def test_a_link_to_claude_under_another_name_is_refused(stubs):
    link = stubs["claude"].parent / "cc"
    link.symlink_to(stubs["claude"])

    with pytest.raises(FileNotFoundError) as caught:
        subprocess.run(["cc", "--version"])

    _assert_refused(caught, "cc")
    assert not _ran(stubs["claude"])


def _root_conftest(request):
    """The tests/conftest.py module object that pytest loaded (never a re-import)."""
    target = Path(__file__).resolve().parents[1] / "conftest.py"
    for plugin in request.config.pluginmanager.get_plugins():
        path = getattr(plugin, "__file__", None)
        if path and Path(path).resolve() == target:
            return plugin
    raise LookupError(target)


def test_the_developer_cli_file_is_refused_by_its_real_path(request, tmp_path, monkeypatch):
    # ~/.local/bin/claude links to a versioned file whose name is not
    # "claude", and jacked/credentials/runtime.py spawns that file.
    versions = tmp_path / "share" / "claude" / "versions"
    versions.mkdir(parents=True)
    versioned = _stub(versions, "2.1.317")
    conftest = _root_conftest(request)
    monkeypatch.setattr(
        conftest, "_DEVELOPER_CLAUDE_FILES", frozenset({os.path.realpath(versioned)})
    )

    with pytest.raises(FileNotFoundError) as caught:
        subprocess.run([str(versioned), "--version"])

    _assert_refused(caught, str(versioned))
    assert not _ran(versioned)


def test_other_programs_still_run(stubs):
    tool = stubs["jacked-probe-tool"]

    assert subprocess.run([str(tool)]).returncode == 0
    assert subprocess.Popen([str(tool)]).wait() == 0
    # A bare name in a command line, which the shell finds on PATH.
    assert subprocess.run("jacked-probe-tool", shell=True).returncode == 0
    assert subprocess.run([sys.executable, "-c", "pass"]).returncode == 0
    assert _ran(tool)


def test_the_opt_in_fixture_lets_a_stub_run(stubs, real_claude_binary):
    assert subprocess.run([str(stubs["claude"]), "--version"]).returncode == 0
    assert _ran(stubs["claude"])


# --------------------------------------------------------------------------- #
# The two bare-name fallbacks, `find_bin("claude") or "claude"`
# --------------------------------------------------------------------------- #

def test_memory_triage_reports_claude_as_missing(stubs):
    """jacked/memory/capture.py `call_claude` spawns the bare name."""
    from jacked.memory import capture

    text, cause = capture.call_claude("prompt", "haiku")

    assert text is None
    assert cause.startswith("claude binary not found or failed to launch:"), cause
    assert _REFUSAL in cause
    assert not _ran(stubs["claude"])


def test_the_claude_launch_fails_as_a_missing_binary(stubs, tmp_path):
    """jacked/launch.py `launch_claude` spawns the bare name."""
    from jacked.launch import launch_claude

    with pytest.raises(FileNotFoundError) as caught:
        launch_claude(tmp_path / "7", ("--version",))

    _assert_refused(caught, "claude")
    assert not _ran(stubs["claude"])

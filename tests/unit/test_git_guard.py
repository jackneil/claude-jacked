"""Tests for jacked.git_guard: git reads in a checkout that can be untrusted."""
from __future__ import annotations

import logging
import shutil
import subprocess

import pytest

from jacked import git_guard

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="drives the real git binary")


def test_the_overrides_close_every_program_a_read_can_run():
    args = " ".join(git_guard.GIT_CONFIG_OVERRIDE_ARGS)
    for key in ("core.fsmonitor=false", "core.untrackedCache=false", "core.hooksPath=",
                "log.showSignature=false", "protocol.allow=never"):
        assert key in args, key
    assert all(a == "-c" for a in git_guard.GIT_CONFIG_OVERRIDE_ARGS[::2])


def _fake_run(returncode=0, stdout=b"", exc=None, calls=None):
    def run(argv, **kwargs):
        if calls is not None:
            calls.append((list(argv), dict(kwargs.get("env") or {})))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, stdout, b"")
    return run


def test_the_config_call_has_the_overrides_and_no_filter_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(git_guard.subprocess, "run", _fake_run(
        stdout=b"filter.evil.clean\n/x\0", calls=calls))

    env = git_guard.guarded_git_env("/usr/bin/git", "/repo", {"PATH": "/bin"})

    ((argv, call_env),) = calls
    assert argv[:3] == ["/usr/bin/git", "-C", "/repo"]
    assert tuple(argv[3:3 + len(git_guard.GIT_CONFIG_OVERRIDE_ARGS)]) == git_guard.GIT_CONFIG_OVERRIDE_ARGS
    assert argv[-4:] == list(git_guard.FILTER_CONFIG_ARGS)
    assert "GIT_CONFIG_COUNT" not in call_env
    assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["PATH"] == "/bin"
    assert env["GIT_CONFIG_KEY_0"] == "filter.evil.clean" and env["GIT_CONFIG_VALUE_0"] == ""


def test_no_filter_key_gives_the_plain_guarded_environment(monkeypatch):
    monkeypatch.setattr(git_guard.subprocess, "run", _fake_run(returncode=1))
    env = git_guard.guarded_git_env("git", "/repo", {})
    assert env == {"GIT_OPTIONAL_LOCKS": "0"}


@pytest.mark.parametrize("fake", [
    _fake_run(returncode=128),
    _fake_run(exc=subprocess.TimeoutExpired(["git"], 3)),
    _fake_run(exc=FileNotFoundError(2, "no git")),
])
def test_a_config_failure_gives_none_and_never_raises(monkeypatch, caplog, fake):
    monkeypatch.setattr(git_guard.subprocess, "run", fake)
    caplog.set_level(logging.DEBUG, logger="jacked.git_guard")
    assert git_guard.guarded_git_env("git", "/repo", {}) is None
    assert any("git config" in r.getMessage() for r in caplog.records)


def test_a_bad_inherited_count_gives_none(monkeypatch):
    monkeypatch.setattr(git_guard.subprocess, "run", _fake_run(stdout=b"filter.x.clean\nc\0"))
    assert git_guard.guarded_git_env("git", "/repo", {"GIT_CONFIG_COUNT": "lots"}) is None


@requires_git
def test_a_real_repo_config_gives_every_driver_an_override(tmp_path, monkeypatch):
    (tmp_path / "gc").write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gc"))
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key in ("filter.a=b.c.clean", "filter.Proc.process"):
        subprocess.run(["git", "-C", str(repo), "config", key, "/x"], check=True)

    env = git_guard.guarded_git_env(shutil.which("git"), str(repo))

    keys = {v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY_")}
    assert {"filter.a=b.c.clean", "filter.a=b.c.process", "filter.Proc.clean",
            "filter.Proc.required"} <= keys


def _argv_spy(monkeypatch, module):
    seen = []

    def spy(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "main\n", "")

    monkeypatch.setattr(module.subprocess, "run", spy)
    return seen


def test_the_capture_branch_read_carries_the_overrides(monkeypatch, tmp_path):
    from jacked.memory import capture

    seen = _argv_spy(monkeypatch, capture)
    capture.current_branch(str(tmp_path))
    ((argv),) = seen
    assert tuple(argv[3:3 + len(git_guard.GIT_CONFIG_OVERRIDE_ARGS)]) == git_guard.GIT_CONFIG_OVERRIDE_ARGS


def test_the_merge_capture_git_reads_carry_the_overrides(monkeypatch, tmp_path):
    """merge_capture runs git log in the user's repo, where log.showSignature
    and gpg.program in the repo config would run a program."""
    from jacked.memory import merge_capture

    seen = _argv_spy(monkeypatch, merge_capture)
    merge_capture._git(tmp_path, ["log", "--merges", "--format=%H", "ORIG_HEAD..HEAD"])
    ((argv),) = seen
    assert tuple(argv[3:3 + len(git_guard.GIT_CONFIG_OVERRIDE_ARGS)]) == git_guard.GIT_CONFIG_OVERRIDE_ARGS

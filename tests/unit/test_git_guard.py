"""Tests for jacked.git_guard: git reads in a checkout that can be untrusted."""
from __future__ import annotations

import logging
import os
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
    """A stand-in for ``git_guard.run_git``: it feeds ``stdout`` to the consumer."""
    def run(argv, *, name, env, timeout, consumer, cwd=None):
        if calls is not None:
            calls.append((list(argv), dict(env)))
        if exc is not None:
            raise exc
        if stdout and not consumer.feed(stdout):
            return -9
        consumer.close()
        return returncode
    return run


def test_the_config_call_has_the_overrides_and_no_filter_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(git_guard, "run_git", _fake_run(
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
    monkeypatch.setattr(git_guard, "run_git", _fake_run(returncode=1))
    env = git_guard.guarded_git_env("git", "/repo", {})
    assert env == {"GIT_OPTIONAL_LOCKS": "0"}


@pytest.mark.parametrize("fake", [
    _fake_run(returncode=128),
    _fake_run(exc=subprocess.TimeoutExpired(["git"], 3)),
    _fake_run(exc=FileNotFoundError(2, "no git")),
])
def test_a_config_failure_gives_none_and_never_raises(monkeypatch, caplog, fake):
    monkeypatch.setattr(git_guard, "run_git", fake)
    caplog.set_level(logging.DEBUG, logger="jacked.git_guard")
    assert git_guard.guarded_git_env("git", "/repo", {}) is None
    assert any("git config" in r.getMessage() for r in caplog.records)


def test_a_bad_inherited_count_gives_none(monkeypatch):
    monkeypatch.setattr(git_guard, "run_git", _fake_run(stdout=b"filter.x.clean\nc\0"))
    assert git_guard.guarded_git_env("git", "/repo", {"GIT_CONFIG_COUNT": "lots"}) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX passes environment bytes as they are")
def test_a_driver_name_that_is_not_utf8_reaches_the_override_byte_for_byte(monkeypatch):
    monkeypatch.setattr(git_guard, "run_git", _fake_run(stdout=b"filter.e\xffvil.clean\n/x\0"))
    env = git_guard.guarded_git_env("git", "/repo", {})
    assert os.fsencode(env["GIT_CONFIG_KEY_0"]) == b"filter.e\xffvil.clean"
    assert os.fsencode(env["GIT_CONFIG_KEY_3"]) == b"filter.e\xffvil.required"


def test_a_driver_name_that_cannot_pass_through_the_environment_gives_none(monkeypatch, caplog):
    """On Windows ``os.fsdecode`` is strict UTF-8, so a name with 0xFF cannot
    reach git as the same bytes. Then no override is made and the read does not run."""
    monkeypatch.setattr(git_guard, "run_git", _fake_run(stdout=b"filter.e\xffvil.clean\n/x\0"))
    monkeypatch.setattr(git_guard.os, "fsdecode", lambda raw: raw.decode("utf-8"))
    caplog.set_level(logging.DEBUG, logger="jacked.git_guard")
    assert git_guard.guarded_git_env("git", "/repo", {}) is None
    assert any("cannot pass through the environment" in r.getMessage() for r in caplog.records)


def test_config_text_refuses_text_that_does_not_give_back_the_bytes(monkeypatch):
    monkeypatch.setattr(git_guard.os, "fsdecode", lambda raw: raw.decode("utf-8", errors="replace"))
    assert git_guard.config_text(b"filter.e\xffvil.clean\n/x\0") is None
    assert git_guard.config_text(b"filter.ok.clean\n/x\0") == "filter.ok.clean\n/x\0"


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


# --------------------------------------------------------------------------- #
# W9: run_git bounds what it reads, and the whole call ends within its timeout
# --------------------------------------------------------------------------- #

def _python(code: str) -> list[str]:
    import sys

    return [sys.executable, "-c", code]


def test_a_flood_of_output_is_cut_at_the_limit_and_git_is_ended(caplog):
    """A fake git writes 30 MB, then sleeps. The read stops at OUTPUT_LIMIT,
    the child is ended, and the memory stays small."""
    import time
    import tracemalloc

    flood = _python(
        "import sys, time\n"
        "sys.stdout.buffer.write(b'x' * 30_000_000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    out = git_guard.Capture()
    caplog.set_level(logging.DEBUG, logger="jacked.git_guard")
    tracemalloc.start()
    start = time.monotonic()
    try:
        code = git_guard.run_git(flood, name="log", env=os.environ, timeout=5, consumer=out)
        elapsed = time.monotonic() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert elapsed < 1, f"{elapsed:.2f}s"
    assert out.truncated and len(out.data) == git_guard.OUTPUT_LIMIT == 1024 * 1024
    assert code != 0  # the child was ended
    assert peak < 16 * 1024 * 1024, f"peak {peak >> 20} MiB"
    (record,) = [r for r in caplog.records if "stopped the output" in r.getMessage()]
    assert record.getMessage().startswith("git guard: stopped the output of git log after")
    assert "x" * 10 not in record.getMessage()  # never the content


def test_an_output_under_the_limit_is_read_whole_with_its_exit_code():
    out = git_guard.Capture()
    code = git_guard.run_git(
        _python("import sys; sys.stdout.write('a' * 5000); sys.exit(3)"),
        name="log", env=os.environ, timeout=10, consumer=out,
    )
    assert code == 3
    assert bytes(out.data) == b"a" * 5000 and not out.truncated


def test_a_silent_slow_git_times_out_within_the_timeout():
    import time

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        git_guard.run_git(_python("import time; time.sleep(30)"), name="status",
                          env=os.environ, timeout=0.5, consumer=git_guard.Capture())
    assert time.monotonic() - start < 2


def test_run_git_closes_stdin_and_stderr_and_hides_the_console(monkeypatch):
    from jacked.winproc import NO_WINDOW

    seen = {}
    real_popen = subprocess.Popen

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(git_guard.subprocess, "Popen", spy)
    git_guard.run_git(_python("pass"), name="branch", env={**os.environ, "K": "v"},
                      timeout=10, consumer=git_guard.Capture(), cwd=os.getcwd())
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is subprocess.PIPE
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["creationflags"] == NO_WINDOW
    assert seen["env"]["K"] == "v" and seen["cwd"] == os.getcwd()


def test_a_consumer_error_goes_up_to_the_caller():
    class Broken:
        def feed(self, chunk):
            raise ValueError("bad chunk")

        def close(self):
            return None

    with pytest.raises(ValueError, match="bad chunk"):
        git_guard.run_git(_python("print('x')"), name="log", env=os.environ, timeout=10,
                          consumer=Broken())


def test_a_config_over_the_limit_gives_none(monkeypatch, caplog):
    monkeypatch.setattr(git_guard, "OUTPUT_LIMIT", 8)
    monkeypatch.setattr(git_guard, "run_git", _fake_run(stdout=b"filter.x.clean\n/a\0"))
    caplog.set_level(logging.DEBUG, logger="jacked.git_guard")
    assert git_guard.guarded_git_env("git", "/repo", {}) is None
    assert any("more than 8 bytes" in r.getMessage() for r in caplog.records)

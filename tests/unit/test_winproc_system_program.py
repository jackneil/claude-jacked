"""Tests for jacked.winproc.system_program: absolute paths for Windows system programs."""
from __future__ import annotations

import pytest

from jacked import winproc


@pytest.mark.parametrize(("name", "expected"), [
    ("schtasks", r"D:\Win\System32\schtasks.exe"),
    ("schtasks.exe", r"D:\Win\System32\schtasks.exe"),
    ("TaskKill", r"D:\Win\System32\taskkill.exe"),
    ("powershell.exe", r"D:\Win\System32\WindowsPowerShell\v1.0\powershell.exe"),
    ("explorer", r"D:\Win\explorer.exe"),
    ("cmd", r"D:\Win\System32\cmd.exe"),
])
def test_system_programs_resolve_under_system_root(name, expected):
    assert winproc.system_program(name, {"SystemRoot": "D:\\Win"}) == expected


def test_cmd_comes_from_comspec_when_it_is_an_absolute_cmd():
    env = {"SystemRoot": "D:\\Win", "ComSpec": r"E:\Tools\cmd.exe"}
    assert winproc.system_program("cmd", env) == r"E:\Tools\cmd.exe"


@pytest.mark.parametrize("comspec", ["cmd.exe", r"\cmd.exe", r"C:\evil\pwn.exe", ""])
def test_a_relative_or_foreign_comspec_is_ignored(comspec):
    env = {"SystemRoot": "D:\\Win", "ComSpec": comspec}
    assert winproc.system_program("cmd", env) == r"D:\Win\System32\cmd.exe"


@pytest.mark.parametrize("env", [{}, {"SystemRoot": "Windows"}, {"SystemRoot": "\\Windows"}])
def test_a_missing_or_relative_system_root_falls_back(env):
    assert winproc.system_program("reg", env) == r"C:\Windows\System32\reg.exe"


def test_windir_is_the_second_choice():
    assert winproc.system_program("sc", {"windir": "F:\\W"}) == r"F:\W\System32\sc.exe"


def test_an_unknown_program_raises():
    with pytest.raises(ValueError):
        winproc.system_program("notepad")


def test_the_launch_junction_runs_the_resolved_cmd(tmp_path, monkeypatch):
    """launch falls back to ``mklink /J`` when a directory symlink fails on Windows."""
    from pathlib import Path

    from jacked import launch

    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    config = tmp_path / "config"
    config.mkdir()
    calls = []

    def no_symlink(*args, **kwargs):
        raise OSError(1314, "a required privilege is not held")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("ComSpec", r"Z:\Sys\cmd.exe")
    monkeypatch.setattr(launch.os, "name", "nt")
    monkeypatch.setattr(launch.os, "symlink", no_symlink)
    monkeypatch.setattr(launch.subprocess, "run", lambda argv, **k: calls.append(list(argv)))

    launch._ensure_shared_symlinks(config)

    assert calls, "the junction fallback did not run"
    assert calls[0][:3] == [r"Z:\Sys\cmd.exe", "/c", "mklink"]

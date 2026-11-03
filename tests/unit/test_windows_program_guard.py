"""Guard: jacked never spawns a Windows system program by its bare name.

CreateProcess looks for a bare program name in the process cwd before
System32. A jacked command that runs in a cloned repository (a hook, the CLI)
would then run a ``cmd.exe`` or ``schtasks.exe`` that the repository holds.
Every such argv starts with ``jacked.winproc.system_program(name)``.

This test parses every module in ``jacked/`` and fails on a list or tuple
literal whose first element is a bare system program name, and on an exec
call whose program (or shell string) starts with one. A line may opt out with
``# winproc: allow <reason>``.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[2] / "jacked"
_PROGRAM_RE = re.compile(
    r"(cmd|powershell|pwsh|schtasks|taskkill|reg|sc|cscript|wscript|explorer)(\.exe)?",
    re.IGNORECASE,
)
_EXEC_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output", "system",
                         "create_subprocess_exec", "create_subprocess_shell", "startfile"})
_ALLOW_RE = re.compile(r"#\s*winproc:\s*allow\s+\S")


def _bare(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        words = node.value.split()
        return bool(words) and bool(_PROGRAM_RE.fullmatch(words[0]))
    return False


def violations_in(source: str, label: str) -> list[str]:
    lines = source.splitlines()
    found = []
    for node in ast.walk(ast.parse(source)):
        hit = None
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and _bare(node.elts[0]):
            hit = node.elts[0]
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in _EXEC_FUNCS and node.args and _bare(node.args[0]):
                hit = node.args[0]
        if hit is None:
            continue
        line = lines[hit.lineno - 1] if hit.lineno - 1 < len(lines) else ""
        if not _ALLOW_RE.search(line):
            found.append(f"{label}:{hit.lineno}: bare {hit.value!r}: use winproc.system_program")
    return found


def test_no_bare_windows_system_program_in_jacked():
    problems = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG.parent).as_posix()
        problems.extend(violations_in(path.read_text(encoding="utf-8"), rel))
    assert not problems, "\n  ".join(["bare Windows system programs:", *problems])


@pytest.mark.parametrize("snippet", [
    'x = ["cmd", "/c", "mklink"]\n',
    'x = ["cmd.exe", "/c", "b.bat"]\n',
    'x = ("schtasks.exe", "/Run")\n',
    'x = ["PowerShell.exe", "-Command", "s"]\n',
    'import subprocess\nsubprocess.run("taskkill /PID 1", shell=True)\n',
    'import os\nos.startfile("explorer")\n',
])
def test_the_guard_flags_a_bare_name(snippet):
    assert violations_in(snippet, "x.py")


@pytest.mark.parametrize("snippet", [
    'x = [system_program("cmd"), "/c", "mklink"]\n',
    'x = {"cmd": "shell-in-shell"}\n',
    'x = ["schtasks.exe", "/Run"]  # winproc: allow a fixture name\n',
    'x = ["scope", "x"]\n',
])
def test_the_guard_accepts_a_resolved_name(snippet):
    assert not violations_in(snippet, "x.py")

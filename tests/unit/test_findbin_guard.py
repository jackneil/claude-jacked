"""Guard: code the service can reach never looks up a user CLI by bare name.

The background service runs with a short, system-only PATH
(``jacked.service.environment._SAFE_PATHS``). ``shutil.which("codex")`` or a
bare ``["gh", ...]`` argv therefore fails inside the service even though the
CLI is installed in ``~/.local/bin``: that froze the Codex usage cache for
seven weeks. Every such lookup goes through ``jacked.findbin.find_bin``.

This test parses the service-reachable modules and fails on:

- ``shutil.which("<user cli>")`` (or ``which(...)`` imported from shutil);
- a subprocess / exec call whose program is a bare user-CLI name, either as
  the first string argument or the first element of a literal argv list.

A line may opt out with ``# findbin: allow <reason>``; the reason is
required, so every exception says why it is safe.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "jacked"

# User-installed CLIs: none of them lives in the service's system PATH by
# default (Homebrew installs do, standalone and npm/uv/pipx installs do not).
USER_CLIS = frozenset({
    "agent-browser", "claude", "codex", "firecrawl", "gh", "grok", "jacked",
    "node", "npm", "npx", "pipx", "uv", "uvx",
})

# The whole package is service-reachable (the service, its hooks, and the
# children it spawns with the service env such as ``jacked install --force``
# from the updater) except these CLI-only modules, which run in the user's
# own shell. A new module is guarded by default.
CLI_ONLY = frozenset({
    "findbin.py",  # the resolver itself
})

_EXEC_FUNCS = frozenset({
    "run", "Popen", "call", "check_call", "check_output", "getoutput",
    "getstatusoutput", "system", "startfile",
    "create_subprocess_exec", "create_subprocess_shell",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
    "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl",
    "spawnle", "spawnlp", "spawnlpe", "posix_spawn", "posix_spawnp",
})

_ALLOW_RE = re.compile(r"#\s*findbin:\s*allow\s+\S")


def _service_files() -> list[Path]:
    return [
        f for f in sorted(PKG.rglob("*.py"))
        if f.name not in CLI_ONLY and f.relative_to(PKG).as_posix() not in CLI_ONLY
    ]


def _which_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to the shutil module, names bound to shutil.which)."""
    modules, funcs = {"shutil"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "shutil":
                    modules.add(alias.asname or "shutil")
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            for alias in node.names:
                if alias.name == "which":
                    funcs.add(alias.asname or "which")
    return modules, funcs


def _call_name(func: ast.expr) -> tuple[str | None, str | None]:
    """(owner, attribute) for ``owner.attr(...)``; (None, name) for ``name(...)``."""
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _shell_program(command: str) -> str | None:
    """The program a shell string runs first, skipping ``env``, ``NAME=value``
    assignments, leading whitespace and a leading ``cd x &&``."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    while words:
        head = words[0]
        if head == "env" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", head):
            words = words[1:]
            continue
        if head == "cd" and "&&" in words:
            words = words[words.index("&&") + 1:]
            continue
        return head
    return None


def _bare_cli(node: ast.expr | None) -> str | None:
    """The user CLI a literal names: ``"codex"``, ``"codex login"`` (a shell
    string whose program is a user CLI), or the first element of a literal
    argv list/tuple."""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        return _bare_cli(node.elts[0])
    if isinstance(node, ast.JoinedStr) and node.values:
        return _bare_cli(node.values[0])
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        head = _shell_program(node.value)
        return head if head in USER_CLIS else None
    return None


def _program_arg(node: ast.Call) -> ast.expr | None:
    """The argument that names the program: the first positional, else ``args=``."""
    if node.args:
        return node.args[0]
    for kw in node.keywords:
        if kw.arg == "args":
            return kw.value
    return None


def violations_in(source: str, label: str) -> list[str]:
    """Return one ``label:line: reason`` string per banned lookup in ``source``."""
    lines = source.splitlines()
    found: list[str] = []
    tree = ast.parse(source)
    modules, which_funcs = _which_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        owner, name = _call_name(node.func)
        first = _program_arg(node)
        if first is None:
            continue
        reason = None
        is_which = (name == "which" and owner in modules) or (
            owner is None and name in which_funcs
        )
        if is_which:
            cli = _bare_cli(first)
            if cli:
                reason = f'shutil.which("{cli}"): use find_bin("{cli}")'
        elif name in _EXEC_FUNCS:
            cli = _bare_cli(first)
            if cli:
                reason = f'bare "{cli}" program in {name}(): run find_bin("{cli}")'
        if reason is None:
            continue
        line = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
        if _ALLOW_RE.search(line):
            continue
        found.append(f"{label}:{node.lineno}: {reason}")
    return found


def test_service_reachable_code_uses_find_bin_for_user_clis():
    problems: list[str] = []
    for path in _service_files():
        rel = path.relative_to(REPO).as_posix()
        problems.extend(violations_in(path.read_text(encoding="utf-8"), rel))
    assert not problems, (
        "The service runs with a system-only PATH, so these lookups miss CLIs "
        "installed in ~/.local/bin. Resolve them with jacked.findbin.find_bin, "
        "or mark the line '# findbin: allow <reason>':\n  " + "\n  ".join(problems)
    )


@pytest.mark.parametrize("snippet", [
    'import shutil\nshutil.which("codex")\n',
    'from shutil import which\nwhich("gh")\n',
    'import shutil as sh\nsh.which("codex")\n',
    'from shutil import which as w\nw("codex")\n',
    'import subprocess\nsubprocess.run(["pipx", "install", "x"])\n',
    'import subprocess\nsubprocess.run(args=["codex", "login"])\n',
    'import subprocess\nsubprocess.run("codex login", shell=True)\n',
    'import subprocess\nsubprocess.run(f"codex login", shell=True)\n',
    'import subprocess\nsubprocess.Popen(("claude", "-p"))\n',
    'import asyncio\nasyncio.create_subprocess_exec("codex", "app-server")\n',
    'import asyncio\nasyncio.create_subprocess_shell("gh pr view")\n',
    'import os\nos.system("gh auth status")\n',
    'import subprocess\nsubprocess.run("env codex login", shell=True)\n',
    'import subprocess\nsubprocess.run("CODEX_HOME=/x codex login", shell=True)\n',
    'import subprocess\nsubprocess.run("cd /tmp && gh pr view", shell=True)\n',
    'import subprocess\nsubprocess.run("  codex login", shell=True)\n',
    'import os\nos.posix_spawnp("codex", ["codex"], {})\n',
    'import os\nos.execvp("claude", ["claude"])\n',
])
def test_guard_flags_bare_user_cli_lookups(snippet):
    assert violations_in(snippet, "x.py")


@pytest.mark.parametrize("snippet", [
    'import shutil\nshutil.which("git")\n',  # system tool, on the safe PATH
    'import shutil\nshutil.which(name)\n',  # dynamic: caller owns the name
    'import subprocess\nsubprocess.run([codex, "login"])\n',  # resolved path
    'import subprocess\nsubprocess.run(["launchctl", "print"])\n',
    'import subprocess\nsubprocess.run("git status", shell=True)\n',
    'import shutil\nshutil.which("codex")  # findbin: allow user-shell only\n',
])
def test_guard_accepts_resolved_or_system_lookups(snippet):
    assert violations_in(snippet, "x.py") == []


def test_allow_marker_needs_a_reason():
    snippet = 'import shutil\nshutil.which("codex")  # findbin: allow\n'
    assert violations_in(snippet, "x.py")

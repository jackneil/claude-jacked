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

# Modules the service process (or a child it spawns with the service env,
# such as ``jacked install --force`` from the updater) can execute.
SERVICE_REACHABLE = (
    "api", "web", "codex", "service", "memory",
    "dcr_settings.py", "skill_listing.py", "launch.py", "cli.py",
)

_EXEC_FUNCS = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "create_subprocess_exec",
    "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp",
    "execlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl",
    "spawnle", "spawnlp", "spawnlpe",
})

_ALLOW_RE = re.compile(r"#\s*findbin:\s*allow\s+\S")


def _service_files() -> list[Path]:
    files: list[Path] = []
    for entry in SERVICE_REACHABLE:
        path = PKG / entry
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.is_file():
            files.append(path)
        else:  # pragma: no cover - a renamed module must update this list
            raise AssertionError(f"service-reachable entry is missing: {entry}")
    return [f for f in files if f.name != "findbin.py"]


def _call_name(func: ast.expr) -> tuple[str | None, str | None]:
    """(owner, attribute) for ``owner.attr(...)``; (None, name) for ``name(...)``."""
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, None


def _bare_cli(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in USER_CLIS else None
    return None


def violations_in(source: str, label: str) -> list[str]:
    """Return one ``label:line: reason`` string per banned lookup in ``source``."""
    lines = source.splitlines()
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        owner, name = _call_name(node.func)
        first = node.args[0]
        reason = None
        if name == "which" and owner in (None, "shutil"):
            cli = _bare_cli(first)
            if cli:
                reason = f'shutil.which("{cli}"): use find_bin("{cli}")'
        elif name in _EXEC_FUNCS:
            cli = _bare_cli(first)
            if cli is None and isinstance(first, (ast.List, ast.Tuple)) and first.elts:
                cli = _bare_cli(first.elts[0])
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
    'import subprocess\nsubprocess.run(["pipx", "install", "x"])\n',
    'import subprocess\nsubprocess.Popen(("claude", "-p"))\n',
    'import asyncio\nasyncio.create_subprocess_exec("codex", "app-server")\n',
    'import os\nos.execvp("claude", ["claude"])\n',
])
def test_guard_flags_bare_user_cli_lookups(snippet):
    assert violations_in(snippet, "x.py")


@pytest.mark.parametrize("snippet", [
    'import shutil\nshutil.which("git")\n',  # system tool, on the safe PATH
    'import shutil\nshutil.which(name)\n',  # dynamic: caller owns the name
    'import subprocess\nsubprocess.run([codex, "login"])\n',  # resolved path
    'import subprocess\nsubprocess.run(["launchctl", "print"])\n',
    'import shutil\nshutil.which("codex")  # findbin: allow user-shell only\n',
])
def test_guard_accepts_resolved_or_system_lookups(snippet):
    assert violations_in(snippet, "x.py") == []


def test_allow_marker_needs_a_reason():
    snippet = 'import shutil\nshutil.which("codex")  # findbin: allow\n'
    assert violations_in(snippet, "x.py")

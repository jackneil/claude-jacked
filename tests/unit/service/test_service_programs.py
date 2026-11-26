"""The pinned contract of the bare POSIX programs that the service runs.

On macOS and Linux the service PATH is a fixed list of system folders, never
inherited. A few OS programs are run by a bare name on that PATH. They are
listed in ``SERVICE_POSIX_PROGRAMS``; any other bare program name in the
service code fails here.
"""

import ast
import os
import shutil
import sys
from pathlib import Path

import pytest

from jacked.service.environment import _SAFE_PATHS, SERVICE_POSIX_PROGRAMS
from jacked.winproc import _SYSTEM32

ROOT = Path(__file__).resolve().parents[3]
SERVICE_CODE = [ROOT / "jacked" / "service", ROOT / "jacked" / "api"]
OS_FOLDERS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
# Folders that the fixed PATH also lists, after the OS folders.
TOOL_FOLDERS = ("/usr/local/bin", "/opt/homebrew/bin")
EXEC = {
    "run", "Popen", "call", "check_call", "check_output", "create_subprocess_exec",
    "run_tree", "_run", "_spawn_detached", "run_process_tree", "_invoke",
}
_WINDOWS = {"cmd", "powershell", "pwsh", *_SYSTEM32}


def _command_returns(tree):
    """Return statements of functions whose name says they build a command."""
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            word in function.name for word in ("command", "argv", "cmd")
        ):
            yield from (n for n in ast.walk(function) if isinstance(n, ast.Return))


def _argv_heads(tree):
    """The constant first element of every argv-shaped literal."""
    returns = set(map(id, _command_returns(tree)))
    for node in ast.walk(tree):
        literal = None
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in EXEC and node.args:
                literal = node.args[0]
            elif name in EXEC:
                continue
            for argument in node.args[1:]:
                if isinstance(argument, (ast.List, ast.Tuple)) and name in EXEC:
                    literal = literal or argument
        elif isinstance(node, ast.Return) and id(node) in returns:
            literal = node.value
        elif isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in {"cmd", "command", "argv", "args"}
            for t in node.targets
        ):
            literal = node.value
        if isinstance(literal, (ast.List, ast.Tuple)) and len(literal.elts) > 1:
            head = literal.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                yield node.lineno, head.value


def _bare_programs():
    found = set()
    for root in SERVICE_CODE:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for line, head in _argv_heads(tree):
                word = head.strip()
                if not word or "/" in word or "\\" in word or word.startswith("-"):
                    continue
                if " " in word or not word.replace("-", "").replace("_", "").isalnum():
                    continue
                found.add((word, f"{path.relative_to(ROOT)}:{line}"))
    return found


def test_every_bare_program_in_the_service_code_is_in_the_pinned_tuple():
    unknown = sorted(
        where
        for word, where in _bare_programs()
        if word not in SERVICE_POSIX_PROGRAMS and word.lower() not in _WINDOWS
    )
    assert unknown == [], "a new bare program name: add it to SERVICE_POSIX_PROGRAMS or use an absolute path: " + ", ".join(unknown)


def test_the_scan_sees_each_pinned_program():
    seen = {word for word, _where in _bare_programs()}
    assert set(SERVICE_POSIX_PROGRAMS) <= seen, "a pinned name is no longer used: remove it"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_the_service_path_holds_only_system_folders(platform):
    folders = _SAFE_PATHS[platform].split(":")
    assert set(folders) <= set(OS_FOLDERS + TOOL_FOLDERS)
    assert set(folders) & set(OS_FOLDERS)


def test_the_scan_catches_a_new_bare_program():
    tree = ast.parse(
        "import subprocess\n"
        "subprocess.run(['tailscale', 'ip'])\n"
        "def build_command():\n    return ['curl', '-s']\n"
        "def status():\n    return ('failed_at', None)\n"
    )
    assert sorted(word for _line, word in _argv_heads(tree)) == ["curl", "tailscale"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX service PATH")
def test_each_pinned_program_on_this_os_resolves_under_an_os_folder():
    service_path = _SAFE_PATHS["darwin" if sys.platform == "darwin" else "linux"]
    for name in SERVICE_POSIX_PROGRAMS:
        found = shutil.which(name, path=service_path)
        if found is None:
            continue  # not on this OS (launchctl on Linux, systemctl on macOS)
        assert found.startswith(tuple(folder + "/" for folder in OS_FOLDERS)), (name, found)

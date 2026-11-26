"""Tests for the documentation of the ``jacked tests`` group.

The reference text (jacked/data/rules/jacked-reference.md) and the README each
have one section about the gate recorder. These tests run the gate line of
each file with stand-in programs, and compare the commands and options that
the text names with the real click group.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from jacked.cli import main
from tests._testdiet import private_hits

REPO = Path(__file__).resolve().parents[2]
REFERENCE = REPO / "jacked" / "data" / "rules" / "jacked-reference.md"
README = REPO / "README.md"
CAPTURE = REPO / "tests" / "fixtures" / "testdiet" / "capture.py"
DOCS = {"reference": (REFERENCE, "\n## Gate recorder (jacked tests)\n"),
        "readme": (README, "\n### Gate recorder (jacked tests)\n")}
GATE_MARKER = "<!-- gate-line -->"
NOT_INSTALLED = "jacked tests is not installed: the gate runs with no record"
FENCE_RE = re.compile(r"^```[^\n]*\n.*?^```[ \t]*$", re.M | re.S)
INLINE_RE = re.compile(r"`[^`\n]+`")
COMMAND_RE = re.compile(r"\bjacked tests ([a-z][a-z-]*)([^\n`]*)")


def _section(name: str) -> str:
    """Return the gate recorder section of one file, up to the next heading of its level."""
    path, heading = DOCS[name]
    text = path.read_text(encoding="utf-8")
    assert text.count(heading) == 1, f"{path.name} must have one section {heading.strip()!r}"
    level = heading.strip().split(" ", 1)[0]
    body = text.split(heading, 1)[1]
    stops = [i for i in (body.find(f"\n{level} "), body.find("\n## ")) if i >= 0]
    return heading.strip() + "\n" + (body[:min(stops)] if stops else body)


def _gate_block(name: str) -> bytes:
    """Return the sh block that comes directly after the gate-line comment, as bytes."""
    section = _section(name)
    assert section.count(GATE_MARKER) == 1, f"{name}: one {GATE_MARKER} comment"
    after = section.split(GATE_MARKER, 1)[1]
    match = re.match(r"\n```sh\n(.*?\n)```\n", after, re.S)
    assert match, f"{name}: an sh block must come directly after {GATE_MARKER}"
    return match.group(1).encode("utf-8")


def _gate_env(tmp_path: Path, standins: dict[str, bytes]) -> tuple[Path, dict[str, str]]:
    """Write each stand-in program and return the sh path and an environment.

    The PATH holds only the stand-in folder and the folder of ``sh``. So a
    stand-in that is not found can never start the real ``jacked`` or ``uv``.
    """
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("the gate line is an sh block, and this machine has no sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in standins.items():
        program = bin_dir / name
        program.write_bytes(b"#!/bin/sh\n" + body)
        program.chmod(program.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    env["PATH"] = os.pathsep.join([str(bin_dir), str(Path(sh).parent)])
    return Path(sh), env


def _run_gate(tmp_path: Path, name: str, standins: dict[str, bytes]) -> subprocess.CompletedProcess:
    sh, env = _gate_env(tmp_path, standins)
    script = tmp_path / "gate.sh"
    script.write_bytes(_gate_block(name))
    return subprocess.run([str(sh), str(script)], cwd=tmp_path, env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60)


def _code_text(text: str) -> list[str]:
    """Return the lines of each code block and each inline code span, with quoted text removed.

    The gate line prints a sentence that starts with "jacked tests". That
    sentence is in quotes, so it is not read as a command.
    """
    parts = [m.group(0) for m in FENCE_RE.finditer(text)]
    parts += [m.group(0) for m in INLINE_RE.finditer(FENCE_RE.sub("", text))]
    lines: list[str] = []
    for part in parts:
        lines += [re.sub(r'"[^"]*"', "", line) for line in part.splitlines()]
    return lines


def _group():
    group = main.commands["tests"]
    return group, {name: group.commands[name] for name in group.list_commands(None)}


def _options(command) -> set[str]:
    names = {"--help"}
    for param in command.params:
        names.update(opt for opt in (*param.opts, *param.secondary_opts) if opt.startswith("--"))
    return names


@pytest.mark.parametrize("name", sorted(DOCS))
def test_the_gate_line_runs_the_bare_command_and_prints_one_line_with_an_older_jacked(tmp_path, name):
    proc = _run_gate(tmp_path, name, {
        "jacked": b"exit 2\n",
        "uv": b'printf \'%s\\n\' "$@" > uv-args\nexit 3\n',
    })
    assert proc.returncode == 3, proc.stderr
    assert proc.stderr.splitlines() == [NOT_INSTALLED]
    args = (tmp_path / "uv-args").read_bytes().decode().splitlines()
    assert args == ["run", "python", "-m", "pytest", "-n", "auto"]


@pytest.mark.parametrize("name", sorted(DOCS))
def test_the_gate_line_uses_jacked_tests_run_when_it_exists(tmp_path, name):
    proc = _run_gate(tmp_path, name, {
        "jacked": (b'if [ "$1 $2 $3" = "tests run --help" ]; then exit 0; fi\n'
                   b'printf \'%s\\n\' "$@" > jacked-args\nexit 7\n'),
        "uv": b": > uv-ran\nexit 3\n",
    })
    assert proc.returncode == 7, proc.stderr
    args = (tmp_path / "jacked-args").read_bytes().decode().splitlines()
    assert "--junitxml={junit}" in args
    assert args == ["tests", "run", "--kind", "full", "--", "uv", "run", "python", "-m", "pytest",
                    "-n", "auto", "--junitxml={junit}"]
    assert not (tmp_path / "uv-ran").exists()
    assert proc.stderr == ""


def test_the_two_files_have_the_same_gate_line():
    assert _gate_block("reference") == _gate_block("readme")


@pytest.mark.parametrize("path", [REFERENCE, README], ids=["reference", "readme"])
def test_each_jacked_tests_command_in_the_reference_exists_in_the_cli(path):
    _, commands = _group()
    found = 0
    for line in _code_text(path.read_text(encoding="utf-8")):
        for match in COMMAND_RE.finditer(line):
            found += 1
            sub = match.group(1)
            assert sub in commands, f"{path.name}: jacked tests {sub} is not a command"
            tokens = match.group(2).split()
            if "--" in tokens:
                tokens = tokens[:tokens.index("--")]
            for token in tokens:
                option = token.strip("[]|.,;()").split("=", 1)[0]
                if option.startswith("--"):
                    assert option in _options(commands[sub]), f"{path.name}: jacked tests {sub} {option}"
    assert found, f"{path.name} names no jacked tests command"


@pytest.mark.parametrize("name", sorted(DOCS))
def test_each_command_of_the_group_is_in_the_reference(name):
    section = "\n".join(_code_text(_section(name)))
    _, commands = _group()
    assert set(commands) == {"run", "record", "seen", "profile", "history", "prune", "loss"}
    for sub in commands:
        assert f"jacked tests {sub}" in section, f"{name}: jacked tests {sub}"


@pytest.mark.parametrize("name", sorted(DOCS))
def test_the_new_sections_have_no_em_dash_and_name_no_host(name):
    section = _section(name)
    assert "\u2014" not in section
    prose = INLINE_RE.sub("", FENCE_RE.sub("", section)).replace(GATE_MARKER, "")
    assert "--" not in prose, "a double hyphen in prose is a dash; put an option in a code span"
    assert " \u2013 " not in prose
    host = socket.gethostname()
    # The login is not checked: on hosted CI it is "runner", a word that the
    # text uses for the --runner option.
    assert private_hits(section, host=host, login="", home=str(Path.home())) == []
    for shape in ("/Users/", "/home/", "C:\\Users", "/private/var/", "/var/folders/"):
        assert shape not in section, shape
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", section)


def test_the_reference_names_the_jest_junit_settings_of_the_reader():
    tree = ast.parse(CAPTURE.read_text(encoding="utf-8"))
    (env,) = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign)
              and any(getattr(t, "id", None) == "JEST_JUNIT_ENV" for t in node.targets)]
    section = _section("reference")
    for key in ("JEST_JUNIT_ADD_FILE_ATTRIBUTE", "JEST_JUNIT_CLASSNAME", "JEST_JUNIT_TITLE",
                "JEST_JUNIT_ANCESTOR_SEPARATOR"):
        assert f'{key}="{env[key]}"' in section, key
    for option, value in (("addFileAttribute", '"true"'), ("classNameTemplate", '"{classname}"'),
                          ("titleTemplate", '"{title}"'), ("ancestorSeparator", '" > "')):
        assert f"{option}: {value}" in section, option


@pytest.mark.parametrize("name", sorted(DOCS))
def test_the_sections_say_where_the_history_is(name):
    section = _section(name)
    assert "jacked-tests" in section
    assert "git rev-parse --git-common-dir" in section
    assert "jacked tests prune --before" in section

"""Every program that a service batch runs is named by an absolute path.

The service environment holds PATH=``<SystemRoot>\\System32;<SystemRoot>``.
The tray update batch runs in that environment, and ``cmd.exe`` looks up a
bare name on PATH. So a bare ``jacked`` gives "'jacked' is not recognized"
(exit 9009), and ``if errorlevel 2`` then reads it as "another updater is
running". A bare ``powershell`` is not found either, because
``WindowsPowerShell\\v1.0`` is not on that PATH.

:func:`write_batch` fixes the class for each batch that jacked writes. It
resolves each program when it builds the script:

- ``jacked``: the quoted path that ``find_bin`` gives (else the quoted
  interpreter with ``-m jacked``), in a ``JACKED`` variable that the batch
  sets after ``@echo off``. Every command ``jacked`` becomes ``%JACKED%``.
- ``powershell``, ``ping``: ``jacked.winproc.system_program``.

:func:`bare_commands` lists each command in a batch that is not a cmd.exe
built-in and is not an absolute path. The tests use it on every batch.
"""

from __future__ import annotations

import ntpath
import os
import re
import sys
import tempfile
from pathlib import Path

#: The cmd.exe built-in commands that a batch may name without a path.
CMD_BUILTINS = frozenset(
    {
        "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date",
        "del", "dir", "echo", "endlocal", "erase", "exit", "for", "ftype", "goto",
        "if", "md", "mkdir", "mklink", "move", "path", "pause", "popd", "prompt",
        "pushd", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift",
        "start", "time", "title", "type", "ver", "verify", "vol",
    }
)

JACKED_VARIABLE = "%JACKED%"
#: The programs that a batch names by a bare word, and how each one is found.
_SYSTEM_PROGRAMS = ("powershell", "ping")  # winproc: allow names for system_program, not an argv


def resolve_jacked() -> str:
    """Return the jacked command for a batch: a quoted absolute path, then options.

    ``find_bin`` finds the uv, pipx or pip entry point. When it finds none,
    the command is the console interpreter beside the running one, with
    ``-m jacked``. A path that a batch cannot carry raises ``ValueError``.
    """
    from jacked.findbin import find_bin

    found = find_bin("jacked")
    suffix = ""
    if not found:
        interpreter = Path(sys.executable)
        console = interpreter.with_name("python.exe")
        found = str(console if console.is_file() else interpreter)
        suffix = " -m jacked"
    if any(char in found for char in '"\r\n') or not ntpath.isabs(found):
        raise ValueError("the jacked program path cannot go into a batch file")
    return f'"{found}"{suffix}'


_COMMAND_AT = re.compile(r'^(?P<lead>\s*(?:start "" /B )?)(?P<name>jacked|powershell|ping)(?= )')


def pin_programs(body: str, jacked: str) -> str:
    """Return ``body`` with every program named by an absolute path.

    >>> body = '@echo off\\r\\njacked _x\\r\\n  ping -n 2 h\\r\\n'
    >>> pin_programs(body, '"C:\\\\b\\\\jacked.exe"').splitlines()[:4]
    ['@echo off', 'set JACKED="C:\\\\b\\\\jacked.exe"', '%JACKED% _x', '  "C:\\\\Windows\\\\System32\\\\ping.exe" -n 2 h']
    """
    from jacked.winproc import system_program

    programs = {name: f'"{system_program(name)}"' for name in _SYSTEM_PROGRAMS}
    programs["jacked"] = JACKED_VARIABLE

    def pin(match: re.Match) -> str:
        return match.group("lead") + programs[match.group("name")]

    lines = [_COMMAND_AT.sub(pin, line) for line in body.split("\r\n")]
    setter = "set JACKED=" + jacked.replace("%", "%%")
    at = 1 if lines and lines[0].lower().startswith("@echo off") else 0
    lines.insert(at, setter)
    return "\r\n".join(lines)


def write_batch(body: str, prefix: str) -> str:
    """Pin the programs of ``body``, write it to a new temporary .bat file, return its path.

    The batch deletes itself at the end. ``newline=""``: the body already
    carries explicit CRLF line ends, and a second translation writes CR CR LF.
    """
    pinned = pin_programs(body, resolve_jacked())
    descriptor, path = tempfile.mkstemp(suffix=".bat", prefix=prefix)
    try:
        with os.fdopen(descriptor, "w", newline="") as handle:
            handle.write(pinned)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path


_IF_PREFIX = re.compile(
    r'^if\s+(?:/i\s+)?(?:not\s+)?(?:errorlevel\s+\S+|exist\s+(?:"[^"]*"|\S+)|defined\s+\S+|'
    r'(?:"[^"]*"|\S+)\s*==\s*(?:"[^"]*"|\S+))\s+',
    re.IGNORECASE,
)


def _first_word(segment: str) -> str | None:
    segment = segment.strip().lstrip("@").strip()
    while segment.startswith("("):
        segment = segment[1:].strip()
    if not segment or segment.startswith((":", ")")):
        return None
    while True:
        match = _IF_PREFIX.match(segment)
        if not match:
            break
        segment = segment[match.end():].strip()
        while segment.startswith("("):
            segment = segment[1:].strip()
    if not segment:
        return None
    if segment.lower().startswith("else "):
        segment = segment[5:].strip()
    start = re.match(r'^start\s+"[^"]*"\s+(?:/B\s+)?', segment, re.IGNORECASE)
    if start:
        segment = segment[start.end():]
    if segment.startswith('"'):
        return segment[: segment.index('"', 1) + 1] if '"' in segment[1:] else segment
    return segment.split()[0] if segment.split() else None


def _segments(line: str) -> list[str]:
    """Split a batch line at ``&``, ``&&``, ``|`` and ``||`` outside quotes.

    ``2>&1`` and ``>&2`` are redirections, not separators.

    >>> _segments('a "x & y" & b 2>&1 | c')
    ['a "x & y" ', ' b 2>&1 ', ' c']
    """
    parts, current, quoted, index = [], "", False, 0
    while index < len(line):
        char = line[index]
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "&|" and not (char == "&" and current[-1:] in "<>" and current):
            parts.append(current)
            current = ""
            if line[index + 1 : index + 2] == char:
                index += 1
            index += 1
            continue
        current += char
        index += 1
    parts.append(current)
    return parts


def _absolute(word: str) -> bool:
    bare = word.strip('"')
    return word == JACKED_VARIABLE or ntpath.isabs(bare) and bool(ntpath.splitdrive(bare)[0]) or (
        bare.upper().startswith("%SYSTEMROOT%\\")
    )


def bare_commands(body: str) -> list[str]:
    """Return each command line of ``body`` that names a program by a bare word.

    A word is bare when it is not a cmd.exe built-in and not an absolute path.
    Text after ``echo`` and ``rem`` is not a command.
    """
    found = []
    for line in body.split("\r\n"):
        for segment in _segments(line):
            word = _first_word(segment)
            if word is None:
                continue
            lowered = word.strip('"').lower().rstrip(".")
            if lowered in CMD_BUILTINS or lowered in {"(goto)", "goto)"}:
                if lowered in {"echo", "rem"}:
                    break
                continue
            if not _absolute(word):
                found.append(line.strip())
                break
    return found


__all__ = [
    "CMD_BUILTINS",
    "bare_commands",
    "pin_programs",
    "resolve_jacked",
    "write_batch",
]

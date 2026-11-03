"""The integrity snapshot: what ``jacked.integrity`` compares (#99i).

A snapshot of ``<home>/.claude/settings.json`` holds the SHA-256 of the whole
file, every hook entry (event -> matcher -> list of {command, digest}), the
state of each hook script that a hook command names, and the SHA-256 of the
value of each settings key. It never holds a settings value.
``jacked.integrity`` keeps the baseline, compares and reports; this module
only reads and hashes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from jacked.memory import settings_io
from jacked.regular_file import NotRegularFileError, open_regular_fd, read_regular

logger = logging.getLogger(__name__)

#: The largest hook script that a snapshot hashes. A real hook script is a few
#: KB. The limit is about the time of a session start, a dashboard load and a
#: settings write, which all take a snapshot. A larger file is still watched,
#: through its size and modification time (``too_large:<size>:<mtime_ns>``),
#: so a change to it is still a finding.
SCRIPT_HASH_LIMIT = 8 * 1024 * 1024
#: The most bytes that one snapshot hashes over all scripts. After that, each
#: further script is watched through its size and modification time
#: (``over_budget:<size>:<mtime_ns>``), never skipped.
SNAPSHOT_HASH_BUDGET = 64 * 1024 * 1024
#: The state of a script path that does not exist. It is recorded too, so a
#: script that appears later (a drop-in) is a finding.
MISSING = "missing"
NOT_REGULAR = "not_regular"
UNREADABLE = "unreadable"
_CHUNK = 1024 * 1024
#: Options after which an interpreter runs inline code or a module, not a file.
_INLINE_CODE = frozenset({"-c", "-m", "-e", "--eval", "-command", "/c", "/k"})
#: Subcommands of a runner (``uv run``, ``npm exec``) that come before the script.
_SUBCOMMANDS = frozenset({"run", "exec", "x"})
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: The options of each interpreter or runner that take the next word as
#: their operand (``python3 -W ignore``, ``uv run --project /p``). The parser
#: skips the option and its operand, so the operand is never the script. An
#: option in the ``--name=value`` form is one word and needs no entry.
#: PowerShell and cmd options are compared in lowercase (``_NO_CASE``).
_OPERAND_OPTIONS: dict[str, frozenset[str]] = {
    "python": frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    "uv": frozenset({
        "--project", "--directory", "--with", "-w", "--with-editable", "--with-requirements",
        "--python", "-p", "--package", "--from", "--extra", "--group", "--env-file",
        "--index", "--index-url", "--extra-index-url", "--config-file", "--cache-dir",
    }),
    "node": frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader"}),
    "shell": frozenset({"-o", "+o", "-O", "+O"}),
    "ruby": frozenset({"-I", "-r"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "powershell": frozenset({"-executionpolicy", "-ep", "-windowstyle", "-configurationname", "-workingdirectory"}),
}
_NO_CASE = frozenset({"powershell", "cmd"})
#: The interpreter names of each ``_OPERAND_OPTIONS`` family that is not its own name.
_FAMILY = {
    "python3": "python", "pythonw": "python", "uvx": "uv", "bun": "node", "deno": "node",
    "bash": "shell", "sh": "shell", "zsh": "shell", "fish": "shell", "pwsh": "powershell",
}

#: Program names that run a script; the script, not the program, is watched.
_INTERPRETERS = frozenset({
    "bash", "bun", "cmd", "deno", "env", "fish", "node", "npm", "npx", "perl",
    "powershell", "pwsh", "python", "python3", "pythonw", "ruby", "sh", "uv", "uvx", "zsh",
})



def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _value_digest(value) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


@dataclass
class _Budget:
    """The bytes that the snapshot may still hash."""

    remaining: int


def _script_state(path: Path, budget: _Budget) -> str:
    """The watched state of one hook script path. Never reads past the limits.

    ``sha256:<hex>`` for a regular file that is hashed, ``too_large:`` or
    ``over_budget:`` with the size and modification time for a regular file
    that is not hashed, else ``missing``, ``not_regular`` or ``unreadable``.
    A FIFO is never opened.
    """
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except OSError as exc:
        logger.warning("integrity: cannot stat %s (%s)", path.name, type(exc).__name__)
        return UNREADABLE
    if not stat.S_ISREG(st.st_mode):
        return NOT_REGULAR
    if st.st_size > SCRIPT_HASH_LIMIT:
        return f"too_large:{st.st_size}:{st.st_mtime_ns}"
    if st.st_size > budget.remaining:
        return f"over_budget:{st.st_size}:{st.st_mtime_ns}"
    try:
        fd = open_regular_fd(path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except NotRegularFileError:
        return NOT_REGULAR
    except OSError as exc:
        logger.warning("integrity: cannot open %s (%s)", path.name, type(exc).__name__)
        return UNREADABLE
    hasher = hashlib.sha256()
    total = 0
    try:
        while chunk := os.read(fd, min(_CHUNK, SCRIPT_HASH_LIMIT + 1 - total)):
            hasher.update(chunk)
            total += len(chunk)
            if total > SCRIPT_HASH_LIMIT:
                return f"too_large:{os.fstat(fd).st_size}:{st.st_mtime_ns}"
    finally:
        os.close(fd)
    budget.remaining -= total
    return "sha256:" + hasher.hexdigest()


def _hook_items(settings: dict):
    """Yield ``(event, matcher, command, digest)`` for every hook entry."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else [entries]:
            if not isinstance(entry, dict):
                yield str(event), "", json.dumps(entry, sort_keys=True), _value_digest(entry)
                continue
            matcher = entry.get("matcher") or ""
            extra = {k: v for k, v in entry.items() if k not in ("hooks", "matcher")}
            inner = entry.get("hooks")
            for hook in inner if isinstance(inner, list) else [inner]:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    command = json.dumps(hook, sort_keys=True)
                yield str(event), str(matcher), command, _value_digest({"entry": extra, "hook": hook})


def _expand_home(token: str, home: Path) -> str:
    token = token.strip("\"'")
    if token.startswith(("~/", "~\\")):
        return str(home) + token[1:]
    for var in ("${HOME}", "$HOME"):
        if token.startswith(var):
            return str(home) + token[len(var):]
    return token


def _interpreter(token: str) -> str | None:
    """The ``_OPERAND_OPTIONS`` family of ``token`` when it is an interpreter, else None.

    >>> _interpreter("/usr/bin/python3.12"), _interpreter("uvx"), _interpreter("check.py")
    ('python', 'uv', None)
    """
    name = Path(token).name.lower().removesuffix(".exe")
    if name not in _INTERPRETERS:
        name = name.rstrip("0123456789.")
        if name not in _INTERPRETERS:
            return None
    return _FAMILY.get(name, name)


def _script_paths(command: str, home: Path) -> list[Path]:
    """The hook script that a hook command names: an empty list or one absolute path.

    Only the script token is watched: the program when it is not an
    interpreter, else the first operand after the interpreters, their
    options (with the operand of an option in ``_OPERAND_OPTIONS``),
    ``VAR=value`` words and a runner subcommand (``uv run``). A later
    argument is data, never a script (``python3 /h/check.py /repo/.git/index``
    watches only ``check.py``). Inline code (``-c``, ``-m``) and a relative
    or ``$VAR`` script are not watched as a file; the digest of the command
    still watches them.

    >>> [p.name for p in _script_paths("python3 -u /h/check.py /repo/.git/index", Path("/home"))]
    ['check.py']
    >>> [p.name for p in _script_paths("uv run --project /p python3 -W ignore /h/c.py", Path("/home"))]
    ['c.py']
    >>> _script_paths("python3 -m tool /etc/hosts", Path("/home"))
    []
    """
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    family = None
    operand_next = False
    for raw in tokens:
        if operand_next:
            operand_next = False
            continue
        token = _expand_home(raw, home)
        if _ASSIGNMENT_RE.match(token):
            continue
        found = _interpreter(token)
        if found:
            family = found
            continue
        if family:
            option = token.lower() if family in _NO_CASE else token
            if option in _OPERAND_OPTIONS.get(family, ()):
                operand_next = True
                continue
            if option in _INLINE_CODE:
                return []
            if token.startswith(("-", "+")) or token in _SUBCOMMANDS:
                continue
        path = Path(token)
        return [path] if path.is_absolute() else []
    return []


_PROGRAM_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}")


def describe_command(command: str, home: Path) -> tuple[str, str, str]:
    """``(program, script, digest)`` that name a hook command without its text.

    A hook command can hold a token (``curl -H 'Authorization: Bearer ...'``),
    so a finding never shows the command. It shows the base name of the
    program (``(other)`` when the name is not a plain file name), the watched
    script path (``""`` when there is none) and the first 8 hex digits of the
    SHA-256 of the command.

    >>> describe_command("curl -H 'Authorization: Bearer x' https://h.example", Path("/home"))[:2]
    ('curl', '')
    >>> describe_command("A=1 /usr/bin/python3 /h/x.py --key k", Path("/home"))[:2]
    ('python3', '/h/x.py')
    """
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    first = next((t for t in tokens if not _ASSIGNMENT_RE.match(t)), "")
    name = Path(first.strip("\"'")).name if first else ""
    program = name if _PROGRAM_RE.fullmatch(name or "") else "(other)"
    scripts = _script_paths(command, home)
    digest = hashlib.sha256(command.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    return program, str(scripts[0]) if scripts else "", digest


def snapshot(settings_file: Path | str) -> dict:
    """Return the integrity snapshot of ``settings_file`` and its hook scripts.

    Raises ``settings_io.SettingsUnreadableError`` when the file is not valid.
    """
    settings_file = Path(settings_file)
    home = settings_file.parent.parent
    try:
        raw = read_regular(settings_file, settings_io.SETTINGS_MAX_BYTES)
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise settings_io.SettingsUnreadableError(f"cannot read {settings_file.name}: {exc}") from exc
    settings = settings_io.read_settings(settings_file) if raw else {}
    hooks: dict = {}
    scripts: dict = {}
    budget = _Budget(SNAPSHOT_HASH_BUDGET)
    for event, matcher, command, digest in _hook_items(settings):
        hooks.setdefault(event, {}).setdefault(matcher, []).append({"command": command, "digest": digest})
        for path in _script_paths(command, home):
            if str(path) not in scripts:
                scripts[str(path)] = _script_state(path, budget)
    values: dict = {}
    for key, value in settings.items():
        if key == "hooks":
            continue
        if key == "env" and isinstance(value, dict):
            values.update({f"env.{name}": _value_digest(v) for name, v in value.items()})
        else:
            values[key] = _value_digest(value)
    return {"file": _digest(raw), "hooks": hooks, "scripts": scripts, "settings": values}

"""The integrity snapshot: what ``jacked.integrity`` compares (#99i).

A snapshot of ``<home>/.claude/settings.json`` holds the SHA-256 of the whole
file, every hook entry (event -> matcher -> list of {command, digest}), the
state of each hook script that a hook command names (and of each file that
an option of the command loads, such as ``node -r``), and the SHA-256 of the
value of each settings key. It never holds a settings value.
``jacked.integrity`` keeps the baseline, compares and reports; this module
only reads and hashes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import ntpath
import os
import posixpath
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


@dataclass(frozen=True)
class _Options:
    """The options of one interpreter or runner that take an operand.

    ``files``: the operand is a file that the program runs or reads (a
    preload module, a requirements file, an env file, a config file), so it
    is watched like the script. ``values``: the operand is not a watched file
    (a warning filter, a Python version, a package name, a folder). Both take
    the next word, or the rest of the word after ``=`` (``--require=/p.js``).
    ``equals_files``: a file only in the ``--name=value`` form, because the
    program never takes the next word for it (``deno run --env-file``).
    A short file option (``-r``) can carry its operand in the same word
    (``ruby -r/h/pre.rb``). PowerShell and cmd options are in lowercase
    (``_NO_CASE``).
    """

    files: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    equals_files: frozenset[str] = frozenset()


#: The option table of each interpreter family, from the documented options
#: (``node --help``, ``bun run --help``, ``uv run --help`` and the deno CLI
#: argument definitions, libs/cli_parser/src/defs.rs, read 2026-11-03).
_OPTIONS: dict[str, _Options] = {
    "python": _Options(values=frozenset({"-W", "-X", "--check-hash-based-pycs"})),
    "uv": _Options(
        files=frozenset({"--with-requirements", "--env-file", "--config-file"}),
        values=frozenset({
            "--project", "--directory", "--with", "-w", "--with-editable", "--python", "-p", "--package",
            "--from", "--extra", "--no-extra", "--group", "--no-group", "--only-group", "--index",
            "--default-index", "--index-url", "-i", "--extra-index-url", "--find-links", "-f",
            "--cache-dir", "--python-platform", "--index-strategy", "--keyring-provider", "--resolution",
            "--prerelease", "--fork-strategy", "--exclude-newer", "--link-mode", "--config-setting", "-C",
            "--upgrade-package", "-P", "--reinstall-package", "--refresh-package", "--color",
            "--allow-insecure-host",
        }),
    ),
    "node": _Options(
        files=frozenset({
            "-r", "--require", "--import", "--loader", "--experimental-loader", "--env-file",
            "--env-file-if-exists", "--openssl-config", "--snapshot-blob", "--test-global-setup",
        }),
        values=frozenset({
            "-C", "--conditions", "--input-type", "--title", "--disable-warning", "--dns-result-order",
            "--unhandled-rejections", "--watch-path", "--diagnostic-dir", "--icu-data-dir",
            "--redirect-warnings", "--report-filename", "--report-signal", "--max-http-header-size",
            "--secure-heap", "--secure-heap-min", "--localstorage-file", "--inspect-publish-uid",
            "--cpu-prof-dir", "--cpu-prof-name", "--cpu-prof-interval", "--heap-prof-dir",
            "--heap-prof-name", "--heap-prof-interval", "--disable-proto", "--use-largepages",
            "--v8-pool-size", "--watch-kill-signal", "--tls-cipher-list", "--tls-keylog",
            "--trace-event-categories", "--trace-event-file-pattern",
        }),
    ),
    # deno ``-r`` is ``--reload`` and takes no operand. ``--env-file`` and the
    # permission options take a value only after ``=``.
    "deno": _Options(
        files=frozenset({"-c", "--config", "--import-map", "--lock", "--cert", "--preload", "--require"}),
        values=frozenset({
            "--ext", "--location", "--seed", "-L", "--log-level", "--conditions", "--min-dep-age",
            "--inspect-publish-uid", "--cpu-prof-dir", "--cpu-prof-name", "--cpu-prof-interval",
        }),
        equals_files=frozenset({"--env-file", "--env"}),
    ),
    "bun": _Options(
        files=frozenset({
            "-r", "--preload", "--require", "--import", "-c", "--config", "--env-file", "--tsconfig-override",
        }),
        values=frozenset({
            "--cwd", "-d", "--define", "-l", "--loader", "--title", "--port", "--conditions", "--shell",
            "-F", "--filter", "--elide-lines", "--install", "--main-fields", "--extension-order", "--drop",
            "--feature", "--console-depth", "--user-agent", "--unhandled-rejections", "--dns-result-order",
            "--max-http-header-size", "--fetch-preconnect", "--jsx-factory", "--jsx-fragment",
            "--jsx-import-source", "--jsx-runtime", "--cpu-prof-name", "--cpu-prof-dir",
            "--cpu-prof-interval", "--heap-prof-name", "--heap-prof-dir",
        }),
    ),
    "shell": _Options(values=frozenset({"-o", "+o", "-O", "+O"})),
    # ruby ``-I`` and perl ``-I`` name a library folder: it is watched for a
    # swap (a folder reads as ``not_regular``), not for a file edit in it.
    "ruby": _Options(files=frozenset({"-r", "-I"}), values=frozenset({"-E", "--encoding", "-C"})),
    "perl": _Options(files=frozenset({"-I"})),
    "env": _Options(values=frozenset({"-u", "--unset", "-C", "--chdir"})),
    "powershell": _Options(
        files=frozenset({"-settingsfile", "-configurationfile"}),
        values=frozenset({
            "-executionpolicy", "-ep", "-windowstyle", "-configurationname", "-workingdirectory",
            "-inputformat", "-outputformat",
        }),
    ),
}
_NO_OPTIONS = _Options()
_NO_CASE = frozenset({"powershell", "cmd"})
#: The interpreter names of each ``_OPTIONS`` family that is not its own name.
_FAMILY = {
    "python3": "python", "pythonw": "python", "uvx": "uv",
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
    """The ``_OPTIONS`` family of ``token`` when it is an interpreter, else None.

    >>> _interpreter("/usr/bin/python3.12"), _interpreter("uvx"), _interpreter("check.py")
    ('python', 'uv', None)
    """
    name = Path(token).name.lower().removesuffix(".exe")
    if name not in _INTERPRETERS:
        name = name.rstrip("0123456789.")
        if name not in _INTERPRETERS:
            return None
    return _FAMILY.get(name, name)


def _loaded_operand(option: str, token: str, options: _Options) -> str | None:
    """The file operand inside one option word, else None.

    The word is ``--name=value`` with a file option name, or a short file
    option with its operand attached (``-r/h/pre.rb``, ``-r=/h/pre.js``).

    >>> _loaded_operand("--require=/h/p.js", "--require=/h/p.js", _OPTIONS["node"])
    '/h/p.js'
    >>> _loaded_operand("-r/h/p.rb", "-r/h/p.rb", _OPTIONS["ruby"])
    '/h/p.rb'
    >>> _loaded_operand("--title=t", "--title=t", _OPTIONS["node"]) is None
    True
    """
    name, equals, _ = option.partition("=")
    if equals and (name in options.files or name in options.equals_files):
        return token[len(name) + 1:]
    short = option[:2]
    if len(option) > 2 and short in options.files and not short.startswith("--"):
        return token[2:].removeprefix("=")
    return None


def _parse_command(command: str, home: Path) -> tuple[Path | None, list[Path]]:
    """``(script, loaded)``: the files that a hook command runs or reads.

    ``script`` is the program when it is not an interpreter, else the first
    operand after the interpreters, their options, ``VAR=value`` words and a
    runner subcommand (``uv run``). A later argument is data, never a script
    (``python3 /h/check.py /repo/.git/index`` gives only ``check.py``).
    Inline code (``-c``, ``-m``) and a relative or ``$VAR`` script give no
    script; the digest of the command still watches them. ``loaded`` holds
    each absolute file operand of a file option (``_Options.files``) before
    the script: the program runs or reads it like the script. The operand of
    any other option is skipped. An option operand never starts with ``-``,
    so a word such as ``-A`` after ``deno run --lock`` is the next option.

    >>> script, loaded = _parse_command("node -r /h/pre.js /h/x.js /etc/hosts", Path("/home"))
    >>> script.name, [p.name for p in loaded]
    ('x.js', ['pre.js'])
    >>> script, loaded = _parse_command("deno run -r /h/x.ts", Path("/home"))
    >>> script.name, loaded
    ('x.ts', [])
    """
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    family = None
    options = _NO_OPTIONS
    loaded: list[Path] = []
    pending = None  # "file" or "value": the next word is an option operand

    def load(operand: str) -> None:
        token = _expand_home(operand, home)
        path = Path(token)
        if _is_absolute(token) and path not in loaded:
            loaded.append(path)

    for raw in tokens:
        token = _expand_home(raw, home)
        if pending and not token.startswith("-"):
            if pending == "file":
                load(token)
            pending = None
            continue
        pending = None
        if _ASSIGNMENT_RE.match(token):
            continue
        found = _interpreter(token)
        if found:
            family, options = found, _OPTIONS.get(found, _NO_OPTIONS)
            continue
        if family:
            option = token.lower() if family in _NO_CASE else token
            if option in options.files or option in options.values:
                pending = "file" if option in options.files else "value"
                continue
            operand = _loaded_operand(option, token, options)
            if operand is not None:
                load(operand)
                continue
            if option in _INLINE_CODE:
                return None, loaded
            if token.startswith(("-", "+")) or token in _SUBCOMMANDS:
                continue
        return (Path(token) if _is_absolute(token) else None), loaded
    return None, loaded


def _is_absolute(token: str) -> bool:
    """True for an absolute path in POSIX or Windows form, on every platform.

    ``Path.is_absolute`` answers for the host only: on Windows ``/h/x.py`` has
    no drive and is not absolute, so a hook written with POSIX paths would go
    unwatched there. The watch takes both forms.

    >>> _is_absolute("/h/x.py"), _is_absolute("C:/h/x.py"), _is_absolute("x.py"), _is_absolute("-r")
    (True, True, False, False)
    """
    return posixpath.isabs(token) or ntpath.isabs(token) or bool(ntpath.splitdrive(token)[0] and ntpath.splitdrive(token)[1].startswith(("\\", "/")))


def _script_paths(command: str, home: Path) -> list[Path]:
    """The watched files of a hook command: the script first, then each loaded file.

    See ``_parse_command``. A preload module, a requirements file or an env
    file that an option names is run or read like the script, so an edit of
    it is a finding too.

    >>> [p.name for p in _script_paths("python3 -u /h/check.py /repo/.git/index", Path("/home"))]
    ['check.py']
    >>> [p.name for p in _script_paths("uv run --project /p python3 -W ignore /h/c.py", Path("/home"))]
    ['c.py']
    >>> [p.name for p in _script_paths("uv run --with-requirements /h/r.txt python3 /h/c.py", Path("/home"))]
    ['c.py', 'r.txt']
    >>> _script_paths("python3 -m tool /etc/hosts", Path("/home"))
    []
    """
    script, loaded = _parse_command(command, home)
    if script is None:
        return loaded
    return [script, *(path for path in loaded if path != script)]


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
    script, _ = _parse_command(command, home)
    digest = hashlib.sha256(command.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    return program, script.as_posix() if script else "", digest  # one form on every platform


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
            if path.as_posix() not in scripts:
                scripts[path.as_posix()] = _script_state(path, budget)
    values: dict = {}
    for key, value in settings.items():
        if key == "hooks":
            continue
        if key == "env" and isinstance(value, dict):
            values.update({f"env.{name}": _value_digest(v) for name, v in value.items()})
        else:
            values[key] = _value_digest(value)
    return {"file": _digest(raw), "hooks": hooks, "scripts": scripts, "settings": values}

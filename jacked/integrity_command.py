"""Read a hook command: which files does it run, and how does a finding name it (#99i).

``jacked.integrity_snapshot`` hashes the files that this module names. A hook
command can hold a token, so this module never lets text from the command
become a watched path or a finding (``_watchable``, ``describe_command``).
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

#: Subcommands of a runner (``uv run``, ``npm exec``) that come before the script.
_SUBCOMMANDS = frozenset({"run", "exec", "x"})
#: The families that have those subcommands.
_SUBCOMMAND_FAMILIES = frozenset({"uv", "npm", "deno", "bun"})
#: The families that run another program, so an interpreter name after them is
#: the program. After any other family, the next word is its script, also when
#: the script has the name of an interpreter (``bash /h/node``).
_RUNNERS = frozenset({"env", "uv", "npm"})
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

    ``inline``: after this option the program runs code from the command, not
    a file. The table is per family, because one letter has different meanings:
    ``-e`` is code for node and ruby, and "stop on an error" for a shell.
    ``clusters``: the program reads a word such as ``-lc`` as ``-l -c``.
    ``attached``: letters whose short option takes the rest of the word
    (``perl -Mstrict``), so the letters after them are not options.
    """

    files: frozenset[str] = frozenset()
    values: frozenset[str] = frozenset()
    equals_files: frozenset[str] = frozenset()
    inline: frozenset[str] = frozenset()
    clusters: bool = False
    attached: str = ""


#: The option table of each interpreter family, from the documented options
#: (``node --help``, ``bun run --help``, ``uv run --help`` and the deno CLI
#: argument definitions, libs/cli_parser/src/defs.rs, read 2026-11-03).
_OPTIONS: dict[str, _Options] = {
    "python": _Options(
        values=frozenset({"-W", "-X", "--check-hash-based-pycs"}),
        inline=frozenset({"-c", "-m"}), clusters=True,
    ),
    "uv": _Options(
        inline=frozenset({"-m", "--module"}),
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
    "npm": _Options(
        values=frozenset({"-p", "--package", "-w", "--workspace", "--prefix"}),
        inline=frozenset({"-c", "--call"}),
    ),
    # node does not join short options, except the documented ``-pe``.
    "node": _Options(
        inline=frozenset({"-e", "--eval", "-p", "--print", "-pe"}),
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
        inline=frozenset({"-e", "--eval", "-p", "--print"}),
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
    # A shell runs the word after its options as code when ``-c`` is in the
    # options, and a word such as ``-euo`` takes its ``-o`` operand from the
    # next word. ``-e`` is not code here.
    "shell": _Options(
        values=frozenset({"-o", "+o", "-O", "+O"}),
        inline=frozenset({"-c", "--command"}), clusters=True,
    ),
    # ruby ``-I`` and perl ``-I`` name a library folder: it is watched for a
    # swap (a folder reads as ``not_regular``), not for a file edit in it.
    "ruby": _Options(
        files=frozenset({"-r", "-I"}), values=frozenset({"-E", "--encoding", "-C"}),
        inline=frozenset({"-e"}), clusters=True, attached="FKTWXix0",
    ),
    "perl": _Options(
        files=frozenset({"-I"}), inline=frozenset({"-e", "-E"}), clusters=True,
        attached="CDFMimx0",
    ),
    "env": _Options(
        values=frozenset({"-u", "--unset", "-C", "--chdir"}),
        inline=frozenset({"-S", "--split-string"}), clusters=True,
    ),
    "cmd": _Options(inline=frozenset({"/c", "/k"})),
    "powershell": _Options(
        # PowerShell takes any start of a parameter name that is not ambiguous.
        inline=frozenset({
            "-c", "-e", "-ec", *("-command"[:n] for n in range(4, 9)),
            *("-encodedcommand"[:n] for n in range(3, 16)),
        }),
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
    "npx": "npm",
}

#: Program names that run a script; the script, not the program, is watched.
_INTERPRETERS = frozenset({
    "bash", "bun", "cmd", "deno", "env", "fish", "node", "npm", "npx", "perl",
    "powershell", "pwsh", "python", "python3", "pythonw", "ruby", "sh", "uv", "uvx", "zsh",
})


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


def _cluster(option: str, options: _Options) -> str | None:
    """What a word of joined short options does: ``inline``, ``file``, ``value`` or None.

    The letters are read from the left. An inline option makes the word
    ``inline``. A value or file option as the last letter takes the next word
    (``file`` or ``value``). A letter that takes the rest of the word ends the
    read.

    >>> [_cluster(word, _OPTIONS["shell"]) for word in ("-lc", "-ceu", "-ex", "-euo")]
    ['inline', 'inline', None, 'value']
    >>> [_cluster(word, _OPTIONS["python"]) for word in ("-uc", "-Im", "-Werror::x", "-uW")]
    ['inline', 'inline', None, 'value']
    >>> [_cluster(word, _OPTIONS["perl"]) for word in ("-ne", "-Mstrict", "-i.bake")]
    ['inline', None, None]
    >>> _cluster("-lc", _OPTIONS["node"]) is None
    True
    """
    if not options.clusters or option.startswith("--") or not option.startswith("-"):
        return None
    letters = option[1:]
    for index, letter in enumerate(letters):
        short = f"-{letter}"
        if short in options.inline:
            return "inline"
        is_file = short in options.files
        if is_file or short in options.values:
            if index < len(letters) - 1:
                return None  # the operand is the rest of this word
            return "file" if is_file else "value"
        if letter in options.attached:
            return None
    return None


def _watchable(token: str) -> bool:
    """True when ``token`` can be a watched path, not text from a command.

    The path must be absolute and hold no NUL. A word with white space is a
    path only when it exists: a real folder can have a space
    (``/Library/Application Support``), but an option that this table does not
    know can put a code string here (``bash --new-flag '/x --token SECRET'``),
    and a finding prints a watched path.

    >>> _watchable("/h/x.py"), _watchable("/no/such --token abc"), _watchable("/h/x\\0y"), _watchable("x.py")
    (True, False, False, False)
    """
    if not _is_absolute(token) or "\0" in token:
        return False
    return not any(c.isspace() for c in token) or os.path.lexists(token)


def _parse_command(command: str, home: Path) -> tuple[Path | None, list[Path]]:
    """``(script, loaded)``: the files that a hook command runs or reads.

    ``script`` is the program when it is not an interpreter, else the first
    operand after the interpreters, their options, ``VAR=value`` words and a
    runner subcommand (``uv run``). A later argument is data, never a script
    (``python3 /h/check.py /repo/.git/index`` gives only ``check.py``).
    Inline code (``_Options.inline``, also in joined options such as ``-lc``)
    and a relative or ``$VAR`` script give no script; the digest of the command
    still watches them. Text from the command never becomes a script
    (``_watchable``), because a finding prints the script path. ``loaded`` holds
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
        if _watchable(token) and path not in loaded:
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
        found = _interpreter(token) if family is None or family in _RUNNERS else None
        if found:
            family, options = found, _OPTIONS.get(found, _NO_OPTIONS)
            continue
        if family:
            option = token.lower() if family in _NO_CASE else token
            if option in options.files or option in options.values:
                pending = "file" if option in options.files else "value"
                continue
            if option.partition("=")[0] in options.inline:
                return None, loaded
            operand = _loaded_operand(option, token, options)
            if operand is not None:
                load(operand)
                continue
            joined = _cluster(option, options)
            if joined == "inline":
                return None, loaded
            if joined:
                pending = joined
                continue
            if family == "cmd" and option.startswith("/"):
                if option[1:2] in ("c", "k"):  # cmd also takes /ccommand
                    return None, loaded
                continue
            if token.startswith(("-", "+")) or (family in _SUBCOMMAND_FAMILIES and token in _SUBCOMMANDS):
                continue
        return (Path(token) if _watchable(token) else None), loaded
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

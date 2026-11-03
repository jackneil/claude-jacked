"""Measure the files Claude Code loads at session start against its load caps.

Claude Code loads the auto memory index ``MEMORY.md`` only up to its first 200
lines or its first 25 KB, whichever comes first. The rest does not load, so an
index that grows past that point loses its tail and the user sees no sign of
it. A CLAUDE.md loads whole up to 4 MiB, and Claude Code skips a larger one.
For a CLAUDE.md the 200-line figure is advice: the docs advise under 200 lines
for each file, because a longer file costs context and adherence.

Sources (read 2026-11-03): https://code.claude.com/docs/en/memory and the
Claude Code 2.1.317 binary, which holds the MEMORY.md caps as ``uL=200`` and
``KJ=25000`` and counts the characters of the trimmed text. This module counts
the bytes on disk and every line. Those counts are never less than the Claude
Code counts, so a warning can come early but never late.

``jacked.memory.claude_paths`` finds the files, the way Claude Code does. This
module measures them and writes the warning lines for SessionStart, ``jacked
memory status`` and the dashboard. It only reads: it never writes, moves or
edits a file, and it never opens a FIFO.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

from jacked.memory.claude_paths import (
    MEMORY_INDEX,
    Env,
    auto_memory_directory,
    auto_memory_enabled,
    config_dir,
    git_root,
    native_memory_path,
    open_regular,
    project_dir_name,
    repo_root,
    session_folder,
)
from jacked.memory.vault import _CONTROL_CHARS_RE

__all__ = [
    "BYTE_CAP", "CLAUDE_MD_ADVISED_LINES", "CLAUDE_MD_MAX_BYTES", "LINE_CAP", "WARN_RATIO",
    "always_loaded_files", "auto_memory_enabled", "config_dir", "machine_warnings", "measure",
    "native_memory_path", "over_cap", "project_dir_name", "render_block", "repo_root",
    "session_folder", "warnings",
]

logger = logging.getLogger(__name__)

# The MEMORY.md caps that Claude Code applies at session start.
LINE_CAP = 200
BYTE_CAP = 25_000
# jacked warns above this part of a cap, so there is time to groom the file.
WARN_RATIO = 0.9
# The CLAUDE.md length the docs advise, and the size above which Claude Code
# skips a CLAUDE.md and loads none of it.
CLAUDE_MD_ADVISED_LINES = 200
CLAUDE_MD_MAX_BYTES = 4 * 1024 * 1024

BLOCK_HEADING = "=== LOAD BUDGET (jacked) ==="
BLOCK_FRAMING = (
    "jacked measured these files at session start. Tell the user about each "
    "line. Do not edit a file unless the user asks."
)

# The instruction files Claude Code reads in each folder, in its read order.
_INSTRUCTION_FILES = (("CLAUDE.md",), (".claude", "CLAUDE.md"), ("CLAUDE.local.md",))
_CHUNK = 1 << 20
# Text that can break a name out of its line, or hide its real order.
_LINE_BREAKS_RE = re.compile("[\t\n\x85\u2028\u2029]")
_INVISIBLE_RE = re.compile("[\x80-\x9f\u202a-\u202e\u2066-\u2069]")


def measure(path) -> tuple[int, int] | None:
    """Return ``(lines, bytes)`` for the file at ``path`` as it is on disk.

    A CRLF line end counts 2 bytes and a UTF-8 character counts each of its
    bytes. A last line without a newline counts as a line. The file is read in
    chunks. Return None when the path is missing, unreadable or not a file.

    >>> measure("/nonexistent-jacked-path/MEMORY.md") is None
    True
    """
    fd = open_regular(path)
    if fd is None:
        return None
    newlines = size = 0
    last = b""
    try:
        while chunk := os.read(fd, _CHUNK):
            newlines += chunk.count(b"\n")
            size += len(chunk)
            last = chunk[-1:]
    except OSError as exc:
        logger.warning("load budget: cannot read %s (%s)", Path(path).name, type(exc).__name__)
        return None
    finally:
        os.close(fd)
    return newlines + (1 if size and last != b"\n" else 0), size


def over_cap(lines: int, nbytes: int, *, line_cap: int | None = LINE_CAP,
             byte_cap: int | None = BYTE_CAP, warn_ratio: float = WARN_RATIO) -> bool:
    """Return True when ``lines`` or ``nbytes`` is above ``warn_ratio`` of its cap.

    A cap of None turns that check off. With ``warn_ratio=1.0`` the check is
    the cap itself: Claude Code loads 200 lines, so 200 is in and 201 is over.

    >>> over_cap(180, 22_500), over_cap(181, 0), over_cap(0, 22_501)
    (False, True, True)
    >>> over_cap(200, 25_000, warn_ratio=1.0), over_cap(201, 0, warn_ratio=1.0)
    (False, True)
    """
    return (line_cap is not None and lines > line_cap * warn_ratio) or (
        byte_cap is not None and nbytes > byte_cap * warn_ratio
    )


def _candidates(cwd: Path, home: Path, env: Env) -> list[tuple[str, Path]]:
    """Return ``(kind, path)`` for each file that exists and loads for ``cwd``.

    The kinds: ``memory`` (the auto memory index), ``project`` (the instruction
    files from ``cwd`` up to the repo root, nearest first), ``user`` (the
    CLAUDE.md in the config folder).
    """
    found = []
    if auto_memory_enabled(cwd, home, env):
        found.append(("memory", native_memory_path(cwd, home, env)))
    top = git_root(cwd) or cwd
    here = cwd
    while True:
        found.extend(("project", here.joinpath(*parts)) for parts in _INSTRUCTION_FILES)
        if here == top or here.parent == here:
            break
        here = here.parent
    found.append(("user", config_dir(home, env) / "CLAUDE.md"))
    seen: set[str] = set()
    out = []
    for kind, path in found:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            out.append((kind, path))
    return out


def always_loaded_files(cwd, home, env: Env | None = None) -> list[Path]:
    """Return the files that Claude Code loads at session start for ``cwd``.

    The list holds the auto memory index (when auto memory is on), each
    CLAUDE.md, ``.claude/CLAUDE.md`` and CLAUDE.local.md from ``cwd`` up to
    the repo root, and the user CLAUDE.md, for the files that exist. A folder
    above the repo root is not part of the project, so its files are not in
    the list. Outside git, only ``cwd`` is read.
    """
    env = os.environ if env is None else env
    return [path for _kind, path in _candidates(session_folder(cwd), Path(home), env)]


def _clean(text: str) -> str:
    """Return a file or folder name that is safe to show on one line in backticks.

    >>> _clean("a" + chr(10) + "b`c" + chr(27) + "[0m" + chr(0x202E))
    "a b'c[0m"
    """
    text = _LINE_BREAKS_RE.sub(" ", _CONTROL_CHARS_RE.sub("", text))
    return _INVISIBLE_RE.sub("", text).replace("`", "'")


def _label(kind: str, path: Path, home: Path, env: Env) -> str:
    """Name ``path`` by its folder and its name, never by the full home path."""
    if kind == "memory":
        return MEMORY_INDEX
    if kind == "user":
        if config_dir(home, env) == home / ".claude":
            return "~/.claude/CLAUDE.md"
        return "CLAUDE.md in CLAUDE_CONFIG_DIR"
    folder, rel = path.parent, path.name
    if folder.name == ".claude":
        folder, rel = folder.parent, f".claude/{path.name}"
    if folder == session_folder(home):
        return f"~/{rel}"
    return f"`{_clean(folder.name) or folder.anchor}/{rel}`"


def _size(nbytes: int) -> str:
    """Return a size in KB (1,000 bytes) as the docs write "25KB", or in MiB from 1 MB.

    >>> _size(27_000), _size(2_090), _size(3_774_874)
    ('27.0 KB', '2.1 KB', '3.6 MiB')
    """
    if nbytes < 1_000_000:
        return f"{nbytes / 1000:.1f} KB"
    return f"{nbytes / (1 << 20):.1f} MiB"


def _tail(kind: str, lines: int, nbytes: int) -> str | None:
    """Return what Claude Code does with a file of this size, or None under the warn line."""
    if kind == "memory":
        if not over_cap(lines, nbytes):
            return None
        return (f"Claude Code loads only the first {LINE_CAP} lines / {BYTE_CAP // 1000} KB; "
                "run memory-librarian")
    if over_cap(lines, nbytes, line_cap=None, byte_cap=CLAUDE_MD_MAX_BYTES):
        return (f"Claude Code skips a file over {CLAUDE_MD_MAX_BYTES >> 20} MiB and loads none "
                "of it; run /audit-rules")
    if over_cap(lines, nbytes, line_cap=CLAUDE_MD_ADVISED_LINES, byte_cap=None):
        return ("Claude Code loads all of it, but its docs advise under "
                f"{CLAUDE_MD_ADVISED_LINES} lines per file; run /audit-rules")
    return None


def _warn(kind: str, path: Path, label: str | Callable[[], str]) -> str | None:
    """Return the warning line for ``path``, or None.

    ``label`` is the name to show, or a function that makes it. The function
    runs only when the file is over its warn line.
    """
    size = measure(path)
    tail = None if size is None else _tail(kind, *size)
    if tail is None:
        return None
    lines, nbytes = size
    name = label() if callable(label) else label
    return f"{name} is {lines} {'line' if lines == 1 else 'lines'} / {_size(nbytes)}; {tail}"


def warnings(cwd, home, env: Env | None = None) -> list[str]:
    """Return one line for each file that loads for ``cwd`` and is over its warn line.

    ``cwd`` None means the process folder. When that folder is gone, there is
    nothing to measure, and the list is empty.
    """
    env = os.environ if env is None else env
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            return []
    home = Path(home)
    out = []
    for kind, path in _candidates(session_folder(cwd), home, env):
        line = _warn(kind, path, _label(kind, path, home, env))
        if line:
            out.append(line)
    return out


def render_block(lines: list[str]) -> str:
    """Return the SessionStart text for ``lines``, or "" when there are none.

    A heading and a framing line come first and the warning lines come last,
    so a session with no warnings gets zero bytes.

    >>> render_block([])
    ''
    """
    if not lines:
        return ""
    return "\n".join([BLOCK_HEADING, BLOCK_FRAMING, *lines])


def _project_folders(config: Path) -> list[Path]:
    """Return each folder under ``<config>/projects/``, sorted by name."""
    try:
        with os.scandir(config / "projects") as entries:
            return sorted(Path(entry.path) for entry in entries if entry.is_dir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("load budget: cannot list the projects folder (%s)", type(exc).__name__)
        return []


def _project_label(folder: Path) -> str:
    """Return a readable name for a ``projects/`` folder.

    It is the last part of the folder that the newest session there ran in,
    else the decoded folder name.
    """
    from jacked import recover
    from jacked.web.analytics_paths import decode_project_hash

    try:
        for transcript in recover._newest_jsonls(folder, n=1):
            cwd = recover._read_cwd(transcript)
            if cwd:
                return _clean(cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or cwd)
    except OSError as exc:
        logger.debug("load budget: cannot read a transcript (%s)", type(exc).__name__)
    return _clean(decode_project_hash(folder.name)["name"] or folder.name)


def machine_warnings(home=None, env: Env | None = None) -> list[str]:
    """Return the warning lines that the dashboard shows, for all projects at once.

    The dashboard has no session folder. So this reads the memory index of
    each project under ``<config dir>/projects/`` (or the one index that a
    user ``autoMemoryDirectory`` names) and the user CLAUDE.md. A project's
    own CLAUDE.md files need a session folder: SessionStart and ``jacked
    memory status`` report them.
    """
    from jacked.memory import vault

    home = vault.jacked_home() if home is None else Path(home)
    env = os.environ if env is None else env
    config = config_dir(home, env)
    lines = []
    if auto_memory_enabled(None, home, env):
        override = auto_memory_directory(None, home, env)
        if override is not None:
            lines.append(_warn("memory", override / MEMORY_INDEX, "MEMORY.md (autoMemoryDirectory)"))
        else:
            for folder in _project_folders(config):
                lines.append(_warn("memory", folder / "memory" / MEMORY_INDEX,
                                   lambda folder=folder: f"MEMORY.md ({_project_label(folder)})"))
    user = config / "CLAUDE.md"
    lines.append(_warn("user", user, _label("user", user, home, env)))
    return [line for line in lines if line]

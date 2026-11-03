"""Find the files Claude Code loads at session start, the way Claude Code does.

Auto memory lives in ``<config dir>/projects/<name>/memory/MEMORY.md``. The
config folder is ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``. The name comes from
the main checkout of the git repo, so all the worktrees and subfolders of a
repo share one index; outside git the name comes from the session folder. An
``autoMemoryDirectory`` setting moves the folder, and
``CLAUDE_CODE_PROJECT_DIR_NAME`` (with ``CLAUDE_CONFIG_DIR`` set) pins the
name. ``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` or ``autoMemoryEnabled: false``
turns auto memory off.

Sources (read 2026-11-03): https://code.claude.com/docs/en/memory, the
sessions page of the same docs, and the Claude Code 2.1.317 binary (its folder
namer, its string hash and its worktree checks). ``jacked.memory.load_budget``
measures what these functions find.

This module only reads. Not seen here: a ``--settings`` file and managed
policy settings, because a hook or the dashboard cannot know them.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from jacked.regular_file import NotRegularFileError, open_regular_fd

logger = logging.getLogger(__name__)

Env = Mapping[str, str]

MEMORY_INDEX = "MEMORY.md"
# Claude Code keeps a project folder name to 200 characters, then adds a hash.
_NAME_LIMIT = 200
# A git pointer file holds one short path, and Claude Code also refuses a large
# .git file. So a larger file is not a pointer.
_POINTER_LIMIT = 4096
_PINNED_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEVICE_NAME_RE = re.compile(r"^(?:con|prn|aux|nul|com[0-9]|lpt[0-9])$", re.IGNORECASE)
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


def open_regular(path) -> int | None:
    """Open ``path`` read-only when it is a regular file, else return None.

    The type is checked before the open and on the open handle
    (``jacked.regular_file.open_regular_fd``), so a FIFO, a device or a folder
    is never read. A FIFO read would block a session start.
    """
    try:
        return open_regular_fd(path)
    except (FileNotFoundError, NotADirectoryError, NotRegularFileError):
        return None
    except OSError as exc:
        logger.warning("claude paths: cannot open %s (%s)", Path(path).name, type(exc).__name__)
        return None


def session_folder(cwd) -> Path:
    """Return ``cwd`` as Claude Code sees it: the physical path on POSIX.

    POSIX ``getcwd()`` resolves symlinks, so Claude Code names a project
    folder from the physical path (on macOS ``/tmp`` gives ``-private-tmp-``).
    On Windows the path stays as it is given.
    """
    return Path(os.path.realpath(cwd) if os.name == "posix" else os.path.abspath(cwd))


def config_dir(home, env: Env | None = None) -> Path:
    """Return ``$CLAUDE_CONFIG_DIR``, else ``<home>/.claude``.

    >>> config_dir("/h", {}).as_posix(), config_dir("/h", {"CLAUDE_CONFIG_DIR": "/c"}).as_posix()
    ('/h/.claude', '/c')
    """
    configured = (os.environ if env is None else env).get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path(home) / ".claude"


def project_dir_name(path: str) -> str:
    """Return the folder name that Claude Code gives ``path`` under ``projects/``.

    Each UTF-16 unit that is not an ASCII letter or digit becomes ``-``
    (``jacked.recover._encode_cwd``), so a character outside the BMP gives two.
    The name is then ASCII, so its length counts UTF-16 units, as the
    JavaScript length does. A name over 200 units keeps its first 200 and gets
    ``-`` and the base 36 of a 32-bit hash of the path over its UTF-16 units,
    as Claude Code 2.1.317 writes it.

    >>> project_dir_name("/Users/jack.neil/Github/claude-jacked")
    '-Users-jack-neil-Github-claude-jacked'
    >>> project_dir_name("/tmp/" + "a" * 196)[195:]
    'aaaaa-r0traf'
    """
    from jacked.recover import _encode_cwd

    name = _encode_cwd(path)
    if len(name) <= _NAME_LIMIT:
        return name
    return f"{name[:_NAME_LIMIT]}-{_base36(abs(_string_hash(path)))}"


def _string_hash(text: str) -> int:
    """Return the signed 32-bit hash of ``text`` over UTF-16 units (the Claude Code hash).

    >>> _string_hash("abc"), _string_hash("")
    (96354, 0)
    """
    value = 0
    units = text.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(units), 2):
        value = ((value << 5) - value + (units[i] | units[i + 1] << 8)) & 0xFFFFFFFF
    return value - (1 << 32) if value & 0x80000000 else value


def _base36(number: int) -> str:
    """Return ``number`` in lowercase base 36, as JavaScript ``toString(36)`` does.

    >>> _base36(0), _base36(35), _base36(36)
    ('0', 'z', '10')
    """
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return out


def repo_root(cwd) -> Path | None:
    """Return the folder that Claude Code keys auto memory on for ``cwd``.

    That is the git root of ``cwd`` (see ``git_root``), and for a linked
    worktree it is the main checkout. Return None outside git.
    """
    root = git_root(cwd)
    return None if root is None else _main_checkout(root)


def git_root(cwd) -> Path | None:
    """Return the nearest folder at or above ``cwd`` with a ``.git`` folder or file.

    A symlinked ``.git`` stops the search and gives None, as it does in
    Claude Code. Return None outside git.
    """
    here = session_folder(cwd)
    while True:
        try:
            mode = os.lstat(here / ".git").st_mode
        except (FileNotFoundError, NotADirectoryError):
            mode = None
        except OSError as exc:
            logger.debug("claude paths: cannot stat .git (%s)", type(exc).__name__)
            return None
        if mode is not None:
            if stat.S_ISLNK(mode):
                return None
            if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                return here
        if here.parent == here:
            return None
        here = here.parent


def _main_checkout(root: Path) -> Path:
    """Return the main checkout of the linked worktree at ``root``, else ``root``.

    These are the Claude Code checks: the ``.git`` pointer names a gitdir in
    ``<common dir>/worktrees/``, and the ``gitdir`` file there points back at
    this worktree. When a check fails, the answer is ``root``.
    """
    pointer = _read_pointer(root / ".git")
    if pointer is None or not pointer.startswith("gitdir:"):
        return root
    gitdir = os.path.normpath(os.path.join(root, pointer[len("gitdir:"):].strip()))
    common_text = _read_pointer(Path(gitdir) / "commondir")
    back_text = _read_pointer(Path(gitdir) / "gitdir")
    if not common_text or not back_text:
        return root
    common = os.path.normpath(os.path.join(gitdir, common_text))
    if os.sep == "/" and any("\\" in part for part in (str(root), gitdir, common, back_text)):
        return root
    if os.path.dirname(gitdir) != os.path.join(common, "worktrees"):
        return root
    back = os.path.realpath(os.path.join(gitdir, back_text))
    if back != os.path.join(os.path.realpath(root), ".git"):
        return root
    if os.path.basename(common) != ".git":
        return root if os.path.exists(os.path.join(common, ".git")) else Path(common)
    return Path(os.path.dirname(common))


def _read_pointer(path: Path) -> str | None:
    """Return the stripped text of a small git pointer file, else None."""
    fd = open_regular(path)
    if fd is None:
        return None
    try:
        data = os.read(fd, _POINTER_LIMIT + 1)
    except OSError as exc:
        logger.debug("claude paths: cannot read %s (%s)", path.name, type(exc).__name__)
        return None
    finally:
        os.close(fd)
    if len(data) > _POINTER_LIMIT:
        logger.debug("claude paths: %s is too large for a git pointer", path.name)
        return None
    try:
        return data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        logger.debug("claude paths: %s is not a git pointer (%s)", path.name, type(exc).__name__)
        return None


def _settings(path: Path) -> dict:
    """Return a settings file as a dict, or {} when it is missing or unreadable."""
    from jacked.memory import settings_io

    try:
        return settings_io.read_settings(path)
    except settings_io.SettingsUnreadableError:
        logger.debug("claude paths: %s is not readable JSON, so it is skipped", path.name)
        return {}


def _is_true(value: object) -> bool:
    return value is not None and str(value).strip().lower() in _TRUE_WORDS


def auto_memory_enabled(cwd, home, env: Env | None = None) -> bool:
    """Return False when auto memory is off, because then no MEMORY.md loads.

    ``CLAUDE_CODE_DISABLE_AUTO_MEMORY`` in the environment or in a settings
    ``env`` block turns it off. Then ``autoMemoryEnabled`` decides: local
    settings first, then project, then user. With ``cwd`` None only the user
    settings count. With no setting, auto memory is on.
    """
    env = os.environ if env is None else env
    scopes = []
    if cwd is not None:
        folder = Path(cwd) / ".claude"
        scopes = [_settings(folder / "settings.local.json"), _settings(folder / "settings.json")]
    scopes.append(_settings(config_dir(home, env) / "settings.json"))
    blocks = [env, *(s["env"] for s in scopes if isinstance(s.get("env"), dict))]
    if any(_is_true(block.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY")) for block in blocks):
        return False
    for scope in scopes:
        if isinstance(scope.get("autoMemoryEnabled"), bool):
            return scope["autoMemoryEnabled"]
    return True


def auto_memory_directory(cwd, home, env: Env | None = None) -> Path | None:
    """Return the ``autoMemoryDirectory`` folder, or None for the default folder.

    Claude Code reads the setting from local settings, then user settings, and
    ignores it in checked-in project settings. With ``cwd`` None only the user
    settings count. The first setting decides, and a value that is not valid
    keeps the default. ``auto_memory_setting`` also names the source.
    """
    return auto_memory_setting(cwd, home, env)[0]


def auto_memory_setting(cwd, home, env: Env | None = None) -> tuple[Path | None, str]:
    """Return ``(folder, source)`` for the ``autoMemoryDirectory`` setting.

    ``source`` is ``"local"`` when the repo's ``.claude/settings.local.json``
    sets the folder, ``"user"`` when the user settings set it, and ``""``
    when no valid setting does (the folder is then None). A repo can ship or
    plant a local settings file, so a jacked write treats a ``"local"``
    folder as read-only.
    """
    env = os.environ if env is None else env
    files = [(config_dir(home, env) / "settings.json", "user")]
    if cwd is not None:
        files.insert(0, (Path(cwd) / ".claude" / "settings.local.json", "local"))
    for path, source in files:
        value = _settings(path).get("autoMemoryDirectory")
        if value is not None:
            folder = _memory_dir_value(value, Path(home))
            return (folder, source) if folder is not None else (None, "")
    return None, ""


def _memory_dir_value(value: object, home: Path) -> Path | None:
    """Return the folder an ``autoMemoryDirectory`` value names (absolute or ``~/``), else None.

    >>> _memory_dir_value("~/mem", Path("/h")).as_posix(), _memory_dir_value("rel", Path("/h"))
    ('/h/mem', None)
    """
    if not isinstance(value, str) or not value:
        return None
    if value.startswith(("~/", "~\\")):
        rest = os.path.normpath(value[2:] or ".")
        if rest in (".", "..") or rest.startswith(("../", ".." + os.sep)):
            return None
        return home / rest
    return Path(value) if os.path.isabs(value) else None


def native_memory_path(cwd, home, env: Env | None = None) -> Path:
    """Return the ``MEMORY.md`` path that Claude Code loads for a session in ``cwd``.

    The order is the Claude Code order: an ``autoMemoryDirectory`` setting,
    then ``CLAUDE_CODE_PROJECT_DIR_NAME`` (only with ``CLAUDE_CONFIG_DIR``
    set), then the name of the main checkout of the repo, or of ``cwd``.
    """
    return native_memory_location(cwd, home, env)[0]


def native_memory_location(cwd, home, env: Env | None = None) -> tuple[Path, str]:
    """Return ``(MEMORY.md path, source)`` for a session in ``cwd``.

    ``source`` is ``"local"`` or ``"user"`` when an ``autoMemoryDirectory``
    setting chose the folder (see ``auto_memory_setting``), else
    ``"default"`` (a folder under ``<config dir>/projects/``).
    """
    env = os.environ if env is None else env
    folder = session_folder(cwd)
    override, source = auto_memory_setting(folder, home, env)
    if override is not None:
        return override / MEMORY_INDEX, source
    pinned = env.get("CLAUDE_CODE_PROJECT_DIR_NAME") if env.get("CLAUDE_CONFIG_DIR") else None
    if pinned and _PINNED_NAME_RE.match(pinned) and not _DEVICE_NAME_RE.match(pinned):
        name = pinned
    else:
        name = project_dir_name(str(repo_root(folder) or folder))
    return config_dir(home, env) / "projects" / name / "memory" / MEMORY_INDEX, "default"

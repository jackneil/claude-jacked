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
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from jacked.integrity_command import (  # noqa: F401 -- re-exported: the parser moved, its callers did not
    _interpreter,
    _parse_command,
    _script_paths,
    describe_command,
)
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
    except ValueError as exc:  # a NUL in the path; the path can hold command text, so no name
        logger.warning("integrity: cannot stat a watched path (%s)", type(exc).__name__)
        return UNREADABLE
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


def home_of(settings_file: Path) -> Path:
    """The folder that ``~/`` means in the hook commands of ``settings_file``.

    It is ``<home>`` for ``<home>/.claude/settings.json``. A file in a
    ``CLAUDE_CONFIG_DIR`` folder says nothing about the home, so it is the
    home of the user.

    >>> home_of(Path("/h/.claude/settings.json")).as_posix()
    '/h'
    >>> home_of(Path("/c/settings.json")) == Path.home()
    True
    """
    return settings_file.parent.parent if settings_file.parent.name == ".claude" else Path.home()


def snapshot(settings_file: Path | str) -> dict:
    """Return the integrity snapshot of ``settings_file`` and its hook scripts.

    Raises ``settings_io.SettingsUnreadableError`` when the file is not valid.
    """
    settings_file = Path(settings_file)
    home = home_of(settings_file)
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

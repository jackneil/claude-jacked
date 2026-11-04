"""Single settings.json reader/writer for the memory vault.

The enable/disable engine (``setup.py``) and the plugin-retirement path
(``migrate.py``) both mutate ``<home>/.claude/settings.json``. A tolerant reader
that returned ``{}`` on a corrupt/unreadable file was a latent data-loss bug: a
transient read error (a JSON typo, a partial write, a permissions hiccup) would
read as "empty settings", and the next write would then CLOBBER every real hook,
permission, and env var the user had. So reads here distinguish the two cases
sharply:

* a genuinely absent file -> ``{}`` (a fresh install; safe to create), while
* an existing-but-unreadable file -> ``SettingsUnreadableError`` (STOP; refuse
  to touch it).

Callers that mutate settings must let ``SettingsUnreadableError`` propagate and
abort loudly rather than swallow it. ``write_settings`` is atomic (writer-unique
tmp + the shared replace) and re-reads the result to prove the write is valid JSON
before returning.

Every writer writes the UTF-8 bytes of ``settings_text`` with LF line endings,
on every platform (Claude Code itself writes LF). A text-mode write would add a
CR to each line on Windows, and the file would then be larger than the size
that ``settings_text`` checked.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

from jacked.regular_file import FileTooLargeError, read_regular

logger = logging.getLogger(__name__)

#: The largest settings file that a read accepts. A real settings.json is a
#: few KB. The limit only stops a huge or growing file from taking the memory
#: of a session start or of the service; a larger file is an error, never
#: silently cut.
SETTINGS_MAX_BYTES = 4 * 1024 * 1024


class SettingsUnreadableError(Exception):
    """settings.json exists but cannot be safely read or was written corrupt.

    Raised instead of silently returning ``{}`` so a mutation never overwrites a
    settings file we failed to parse.
    """


class SettingsTooLargeError(SettingsUnreadableError):
    """The new settings would make a file over ``SETTINGS_MAX_BYTES``.

    Nothing was written. ``read_settings`` refuses a file over the limit, so
    a write of that file would make the settings unreadable.
    """


def settings_text(data: dict) -> str:
    """Return ``data`` as settings.json text, or raise ``SettingsTooLargeError``.

    Every settings.json writer calls this before it writes, so the check
    happens before the original file changes. Each writer writes the UTF-8
    bytes of this text (``atomic_write_bytes``, LF line endings), so the size
    on disk is the UTF-8 byte count on every platform.

    >>> settings_text({"a": 1})
    '{\\n  "a": 1\\n}'
    """
    text = json.dumps(data, indent=2)
    size = len(text.encode("utf-8"))
    if size > SETTINGS_MAX_BYTES:
        logger.warning(
            "settings: the new settings.json would be %d bytes, larger than the limit of %d bytes,"
            " so it is not written", size, SETTINGS_MAX_BYTES,
        )
        raise SettingsTooLargeError(
            f"the new settings.json would be {size} bytes, larger than the limit of"
            f" {SETTINGS_MAX_BYTES} bytes; the file is not changed"
        )
    return text


def settings_path(home: Path | str) -> Path:
    """``<home>/.claude/settings.json``."""
    return Path(home) / ".claude" / "settings.json"


def read_settings(path: Path | str) -> dict:
    """Read settings.json. ``{}`` ONLY for a missing file; raise otherwise.

    A ``FileNotFoundError`` is the benign fresh-install case. A JSON parse error,
    JSON nested too deep to decode, any other OSError, or a top-level value
    that is not a JSON object all raise ``SettingsUnreadableError`` -- the
    caller must abort rather than clobber.

    Only a regular file is read (``jacked.regular_file``): a FIFO, a symlink to
    a FIFO, a device or a folder raises at once and never blocks. A file over
    ``SETTINGS_MAX_BYTES`` raises too, and its size goes to the log.
    """
    path = Path(path)
    try:
        raw = _read_text(path)
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
        # ValueError: not valid JSON (JSONDecodeError is a ValueError).
        # RecursionError: valid JSON that is nested too deep to decode.
        raise SettingsUnreadableError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise SettingsUnreadableError(f"{path} is not a JSON object")
    return data


def _read_text(path: Path) -> str:
    """Return the UTF-8 text of the settings file at ``path``.

    A missing file raises ``FileNotFoundError``. Every other failure raises
    ``SettingsUnreadableError``.
    """
    try:
        raw = read_regular(path, SETTINGS_MAX_BYTES)
    except FileNotFoundError:
        raise
    except FileTooLargeError as e:
        logger.warning(
            "settings: %s is %d bytes, larger than the limit of %d bytes, so it is not read",
            path.name, e.size, SETTINGS_MAX_BYTES,
        )
        raise SettingsUnreadableError(f"{path} is larger than {SETTINGS_MAX_BYTES} bytes") from e
    except OSError as e:
        raise SettingsUnreadableError(f"cannot read {path}: {e}") from e
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SettingsUnreadableError(f"{path} is not valid UTF-8: {e}") from e


def write_settings(path: Path | str, data: dict, *, reason: str = "jacked settings write") -> None:
    """Atomically write settings.json, then verify it re-reads as a JSON object.

    This is the one jacked writer of settings.json (a static test holds every
    other module to it). After a verified write it calls
    ``jacked.integrity.rebaseline`` with ``reason`` and a snapshot from before
    the write, so the integrity baseline takes in this change and only this
    change. An integrity failure is logged and never blocks the write.

    The atomic write itself (writer-unique tmp + the shared replace) is delegated
    to the shared ``atomic_write_bytes`` (``jacked.atomic_io``) so every atomic
    write goes through one implementation. The bytes are the UTF-8 of
    ``settings_text``, with LF line endings on every platform, so the file has
    exactly the size that the check measured. On top of that shared write this keeps its own
    post-write verification: the file is re-read and parsed, and a verification
    failure raises ``SettingsUnreadableError`` so a corrupt write is surfaced
    loudly instead of being trusted.

    A settings.json that is a symlink (a dotfiles setup) stays a symlink: the
    write goes to the regular file that the link points at
    (``_write_target``). The integrity baseline keeps the path of the link.
    The size check (``settings_text``) comes before the write, so settings
    over the limit leave the original file as it is.
    """
    from jacked.atomic_io import atomic_write_bytes

    from jacked import integrity

    path = Path(path)
    text = settings_text(data)  # raises before anything is touched
    target = _write_target(path)
    # One lock over the snapshot, the write and the rebaseline: two jacked
    # writers that interleave would lose a log entry and report each other.
    with integrity.baseline_lock(path):
        before = _integrity_snapshot(path)
        atomic_write_bytes(target, text.encode("utf-8"))  # LF on every platform

        try:
            verify = json.loads(_read_text(path))
        except (OSError, SettingsUnreadableError, json.JSONDecodeError) as e:
            raise SettingsUnreadableError(f"post-write verification failed for {path}: {e}") from e
        if not isinstance(verify, dict):
            raise SettingsUnreadableError(f"post-write verification failed for {path}: not a JSON object")
        _rebaseline(path, reason, before)


def _write_target(path: Path) -> Path:
    """Return the file that a write of ``path`` replaces.

    That is ``path``, or the final target when ``path`` is a symlink to a
    regular file, so the link stays and its target gets the new content. A
    link to a missing file, to a FIFO, to a device or to a folder raises
    ``SettingsUnreadableError`` and nothing is written. The log names the
    reason and the settings file, never the target of the link.
    """
    try:
        if not stat.S_ISLNK(os.lstat(path).st_mode):
            return path
    except FileNotFoundError:
        return path
    except OSError as e:
        raise SettingsUnreadableError(f"cannot read {path}: {e}") from e
    try:
        mode = os.stat(path).st_mode
    except OSError as e:
        reason = "a missing file" if isinstance(e, FileNotFoundError) else type(e).__name__
        logger.warning("settings: %s is a symlink to %s, so jacked does not write it", path.name, reason)
        raise SettingsUnreadableError(f"{path} is a symlink that cannot be followed") from e
    if not stat.S_ISREG(mode):
        logger.warning(
            "settings: %s is a symlink to a path that is not a regular file, so jacked does not write it",
            path.name,
        )
        raise SettingsUnreadableError(f"{path} is a symlink to a path that is not a regular file")
    return Path(os.path.realpath(path))


def _integrity_snapshot(path: Path) -> dict | None:
    """The integrity snapshot from before a write, or None when it cannot be taken."""
    from jacked import integrity

    try:
        return integrity.snapshot(path)
    except Exception as e:  # noqa: BLE001 -- the tripwire must never block a settings write
        logger.warning("settings: integrity snapshot before the write failed (%s)", type(e).__name__)
        return None


def _rebaseline(path: Path, reason: str, before: dict | None) -> None:
    """Rebaseline after a verified write. A failure goes to the log, never up.

    With no snapshot from before the write, the baseline stays as it is: this
    write then shows as a change, which is loud, and a full rebaseline would
    silently take in any earlier change from outside jacked.
    """
    from jacked import integrity

    if before is None:
        return
    try:
        integrity.rebaseline(path, reason=reason, previous=before)
    except Exception as e:  # noqa: BLE001 -- the tripwire must never block a settings write
        logger.warning("settings: integrity rebaseline after the write failed (%s)", type(e).__name__)

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
"""
from __future__ import annotations

import json
import logging
import os
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
    happens before the original file changes. The size is the size on disk:
    the UTF-8 bytes, plus the extra byte of each line ending when a text-mode
    write uses CRLF (Windows).

    >>> settings_text({"a": 1})
    '{\\n  "a": 1\\n}'
    """
    text = json.dumps(data, indent=2)
    size = len(text.encode("utf-8")) + text.count("\n") * (len(os.linesep) - 1)
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
    any other OSError, or a top-level value that is not a JSON object all raise
    ``SettingsUnreadableError`` -- the caller must abort rather than clobber.

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
    except json.JSONDecodeError as e:
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


def write_settings(path: Path | str, data: dict) -> None:
    """Atomically write settings.json, then verify it re-reads as a JSON object.

    The atomic write itself (writer-unique tmp + the shared replace) is delegated
    to the shared ``vault.atomic_write_text`` (``jacked.atomic_io``) so every
    atomic write goes through one implementation. On top of that shared write this keeps its own
    post-write verification: the file is re-read and parsed, and a verification
    failure raises ``SettingsUnreadableError`` so a corrupt write is surfaced
    loudly instead of being trusted. The size check (``settings_text``) comes
    before the write, so settings over the limit leave the original file as
    it is.
    """
    from jacked.memory import vault as vault_mod

    path = Path(path)
    vault_mod.atomic_write_text(path, settings_text(data))

    try:
        verify = json.loads(_read_text(path))
    except (OSError, SettingsUnreadableError, json.JSONDecodeError) as e:
        raise SettingsUnreadableError(f"post-write verification failed for {path}: {e}") from e
    if not isinstance(verify, dict):
        raise SettingsUnreadableError(f"post-write verification failed for {path}: not a JSON object")

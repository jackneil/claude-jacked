"""Say when a Claude config write is a ``/login`` in progress (#154i).

A ``/login`` writes the ``oauthAccount`` identity keys a moment before it saves
the new tokens. A republish inside that window would put the old account back,
and the stamped-rotation guard would then read a stale verdict and undo the
user's choice. Every running Claude Code session also rewrites
``~/.claude.json`` all the time, so the file's mtime alone says nothing: only a
change of the identity keys opens the window.
"""

from __future__ import annotations

import time
from pathlib import Path

CONFIG_FRESH_SECONDS = 15.0
_IDENTITY_KEYS = ("emailAddress", "organizationUuid", "accountUuid")


def config_identity_keys(config: dict) -> tuple:
    """The values of the identity keys of an ``oauthAccount`` block.

    >>> config_identity_keys({"emailAddress": "a@b.c", "displayName": "x"})
    ('a@b.c', None, None)
    """
    return tuple(config.get(key) for key in _IDENTITY_KEYS)


def _config_age_seconds(home: Path) -> float | None:
    """Seconds since the Claude config file was last written, by wall clock.

    None when there is no file or it cannot be read.
    """
    from jacked.api.claude_config_store import claude_config_path

    path = claude_config_path(home)
    if path is None:
        return None
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _config_mtime(home: Path) -> str:
    from jacked.api.claude_config_store import claude_config_path

    path = claude_config_path(home)
    try:
        return f"{path.stat().st_mtime:.0f}" if path is not None else "unknown"
    except OSError:
        return "unknown"


def login_in_progress(home: Path, keys_changed_at: float | None, now: float) -> bool:
    """True while the identity keys changed inside the window.

    ``keys_changed_at`` is the time the watcher last saw the keys change (its
    first look at an account counts). A file that nobody wrote inside the
    window cannot hold a change from inside the window, so it answers False
    even when the watcher saw the change only now.

    >>> login_in_progress(Path("/nonexistent-home"), 100.0, 101.0)
    False
    """
    if keys_changed_at is None or now - keys_changed_at >= CONFIG_FRESH_SECONDS:
        return False
    age = _config_age_seconds(home)
    return age is not None and age < CONFIG_FRESH_SECONDS

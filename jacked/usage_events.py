"""In-process change signal for account usage / active-account state.

The menu-bar agent and the uvicorn API server run in the SAME process
(``jacked service start`` runs uvicorn in a daemon thread), so the pill
doesn't need a push channel to learn "usage just changed" — it can watch a
monotonically increasing version counter that the write paths bump:

* ``Database.update_account_usage_cache`` — every usage-cache write
  (manual refresh, background poll, codex, oauth backfill).
* the account-switch paths — the pill tracks the ACTIVE account, so a
  switch must re-render it even though no usage row changed.

The mac agent polls :func:`version` on a cheap 1s timer (an int read — no
HTTP, no DB) and only performs its full HTTP summary fetch when the value
moved; a slow full poll remains as the heartbeat for cross-process writers
(a separately-launched ``jacked webux`` writing the shared SQLite file
can't bump this process's counter).

Deliberately a leaf module: stdlib-only, no jacked imports, safe to import
from the database layer, API routes, and the service agent alike.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_version = 0


def bump() -> int:
    """Record that usage / active-account state changed; returns the new version.

    >>> before = version()
    >>> bump() > before
    True
    """
    global _version
    with _lock:
        _version += 1
        return _version


def version() -> int:
    """Current change counter — equal values mean "nothing changed since".

    >>> isinstance(version(), int)
    True
    """
    return _version

"""Close the tray's update status record when the update changes nothing.

The tray writes a pre-init status record when the user clicks Update, so the
update page has something to show at once. The updater that follows claims
that record. When the updater refuses an install that would go back to an
older version (#295i), nothing failed: the record closes as done at the
installed version, with the refusal as its note. Then the tray shows no
"Jacked Update Failed Earlier" toast at its next start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def is_unclaimed_preinit(data: Optional[dict]) -> bool:
    """True when ``data`` is the tray's pre-init record and no updater claimed it.

    The tray writes this record (``preinit=True``, no phases) when the user
    clicks Update. The updater that follows claims it and removes the marker.

    >>> is_unclaimed_preinit({"preinit": True, "phases": [], "current_phase": None})
    True
    >>> is_unclaimed_preinit({"phases": []}), is_unclaimed_preinit(None)
    (False, False)
    >>> is_unclaimed_preinit({"preinit": True, "phases": [{"name": "x"}]})
    False
    """
    return (
        isinstance(data, dict)
        and data.get("preinit") is True
        and not (data.get("phases") or [])
        and data.get("current_phase") is None
    )


def close_preinit_as_noop(path: Path, installed: str, note: str) -> bool:
    """Close the tray's pre-init record as a finished update that changed nothing.

    The updater calls this with the update lock held. The record closes as
    ``succeeded`` at the ``installed`` version, and ``note`` tells the update
    page why nothing changed. A record that is not the tray's unclaimed
    pre-init (for example, the record of an earlier update) does not change.
    Returns True when the record closed.

    >>> import tempfile
    >>> from jacked.service import update_status
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     update_status.init_status(p, "0.111.2", "0.111.3", "uv", preinit=True)
    ...     closed = close_preinit_as_noop(p, "0.111.2", "Nothing to upgrade.")
    ...     data = update_status._read_raw(p)
    ...     again = close_preinit_as_noop(p, "0.111.2", "Nothing to upgrade.")
    >>> closed, again, data["overall"], data["to_version"], data["note"], "preinit" in data
    (True, False, 'succeeded', '0.111.2', 'Nothing to upgrade.', False)
    """
    from jacked.service import update_status

    data = update_status._read_raw(path)
    if not is_unclaimed_preinit(data):
        return False
    data.pop("preinit", None)
    data["to_version"] = installed
    data["note"] = note
    update_status._set_succeeded(data)
    update_status._atomic_write(path, data)
    return True

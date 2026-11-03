"""The schema open deletes the rows of retired settings.

A retired setting is a setting that no code reads now, for example
``auto_swap_7d_threshold`` (the old "7-Day Threshold" slider on the
dashboard). A stored row of it would still show in the generic settings list
as a value that seems to do something. Each time ``Database`` opens a
database, it deletes these rows. It keeps every other row as it was.
"""

from __future__ import annotations

import sqlite3

from jacked.api.routes.settings_swap import SWAP_SETTING_KEYS
from jacked.web.database import Database

RETIRED = "auto_swap_7d_threshold"
# Live settings beside the retired one, two of them auto_swap_* keys, so an
# over-broad rule (a prefix match, a delete of every row) loses them.
KEPT = {
    "auto_swap_5h_critical": "90",
    "auto_swap_5h_rules_enabled": "false",
    "usage_check_interval": "300",
    "remote_access_enabled": "false",
}


def _rows(path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT key, value, updated_at FROM settings ORDER BY key"
        ).fetchall()
    finally:
        conn.close()


def _seed(path, values):
    """Write rows after an open, as an old jacked did with no cleanup step."""
    db = Database(str(path))
    db.set_settings(values)
    db.close()


def test_open_deletes_the_row_of_a_retired_setting(tmp_path):
    path = tmp_path / "jacked.db"
    _seed(path, {RETIRED: "85", **KEPT})
    assert RETIRED in {key for key, _, _ in _rows(path)}
    others = [row for row in _rows(path) if row[0] != RETIRED]

    db = Database(str(path))
    assert db.get_setting(RETIRED) is None
    db.close()
    assert _rows(path) == others  # the other rows keep value and updated_at


def test_open_leaves_a_database_without_retired_rows_unchanged(tmp_path):
    path = tmp_path / "jacked.db"
    _seed(path, KEPT)
    before = _rows(path)

    Database(str(path)).close()
    assert _rows(path) == before


def test_a_retired_key_is_not_a_live_swap_setting():
    """Each open deletes a retired key, so a live setting must never be one."""
    assert RETIRED in Database.RETIRED_SETTING_KEYS
    assert not set(Database.RETIRED_SETTING_KEYS) & set(SWAP_SETTING_KEYS)

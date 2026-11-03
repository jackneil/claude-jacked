"""A cross-account mark recorded before the binding column is bound once (#154i).

The verdict lives in ``foreign_token_fp`` alone. A row that carries only the
text (written by code from before the column) would otherwise lose the
verdict: the heal sweep runs its exchange leg before its validation leg, so
it would spend the foreign refresh token before validation could bind the
mark. The schema open binds such rows once; a valid row, a row with no
refresh token and a bound row are left alone.
"""

from __future__ import annotations

import sqlite3

from jacked.web.database import Database
from jacked.web.refresh_verdict import FOREIGN_TOKEN_ERROR, primary_tokens_foreign, token_fingerprint


def _seed(path, rows):
    db = Database(str(path))
    for account_id, email in rows:
        db.create_account(email, "at", 9_999_999_999)
    db.close()
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE accounts SET refresh_token = 'rt-' || id")
        conn.execute("UPDATE accounts SET last_error = ?, validation_status = 'invalid', foreign_token_fp = NULL",
                     (FOREIGN_TOKEN_ERROR,))
        conn.execute("UPDATE accounts SET validation_status = 'valid' WHERE id = 2")
        conn.execute("UPDATE accounts SET refresh_token = NULL WHERE id = 3")
        conn.execute("UPDATE accounts SET last_error = 'Token invalid (HTTP 401)' WHERE id = 4")
        # The state of a database written by the code from before the column:
        # the one-shot pass has not run on it yet.
        conn.execute("DELETE FROM settings WHERE key = '_foreign_marks_bound'")
        conn.commit()


def test_the_schema_open_binds_text_only_marks(tmp_path):
    path = tmp_path / "t.db"
    _seed(path, [(1, "a@x.com"), (2, "b@x.com"), (3, "c@x.com"), (4, "d@x.com")])

    db = Database(str(path))  # a second open: the migration pass runs again
    rows = {row["id"]: row for row in db.list_accounts(include_inactive=True)}
    db.close()

    assert rows[1]["foreign_token_fp"] == token_fingerprint("rt-1")
    assert primary_tokens_foreign(rows[1]) is True
    assert rows[2]["foreign_token_fp"] is None  # valid: no verdict to bind
    assert rows[3]["foreign_token_fp"] is None  # nothing to exchange
    assert rows[4]["foreign_token_fp"] is None  # another error, not a verdict


def test_the_binding_is_idempotent_and_keeps_a_released_row_released(tmp_path):
    path = tmp_path / "t.db"
    _seed(path, [(1, "a@x.com")])
    Database(str(path)).close()
    with sqlite3.connect(str(path)) as conn:
        conn.execute("UPDATE accounts SET refresh_token = 'rt-new' WHERE id = 1")  # a re-auth: the trigger releases
        conn.commit()
        released = conn.execute("SELECT foreign_token_fp FROM accounts WHERE id = 1").fetchone()[0]
    assert released is None

    db = Database(str(path))  # another open must not re-bind a released row by its stale text
    row = db.get_account(1)
    assert db.get_setting("_foreign_marks_bound") == "1"
    db.close()
    assert row["foreign_token_fp"] is None

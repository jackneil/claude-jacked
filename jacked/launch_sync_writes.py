"""The compare-and-swap writes of the launch token sync.

``launch._sync_tokens_from_file`` decides whether the tokens in a per-account
.credentials.json are the row's; this module writes what it proved. Each
write is one compare-and-swap UPDATE on the columns the decision read, so a
concurrent writer (a refresh, a re-auth, a chain that died meanwhile) wins
and the next pass decides again from the new row.

A dead chain (``invalid_grant``) follows the dead-chain rule that reconcile
also applies (``credential_helpers.dead_chain_adoption``, #244i): a NEW
refresh token is adopted with its pass-through fields and a cleared breaker
in the same write; any other refresh token gives the access token and its
expiry only.

The functions use raw sqlite3, because the sync runs in a thread.
"""

from __future__ import annotations

import logging
import time

from jacked.api.credential_helpers import (
    ADOPTED_LINEAGE_MESSAGE,
    cc_chain_dead,
    dead_chain_adoption,
    expiry_seconds,
    forget_ownership_verdicts,
)

logger = logging.getLogger(__name__)

# A new access token is not the one the API refused: a rotation drops an
# auth_rejected verdict (it would mark the replacement dead). Any other verdict
# stays. SQLite evaluates both against the row as it was before the update.
_CLEAR_AUTH_REJECTED = (
    "cc_refresh_last_failed_at = CASE WHEN cc_refresh_failure_type = "
    "'auth_rejected' THEN NULL ELSE cc_refresh_last_failed_at END",
    "cc_refresh_failure_type = CASE WHEN cc_refresh_failure_type = "
    "'auth_rejected' THEN NULL ELSE cc_refresh_failure_type END",
)
# The columns a sync write was decided from: the access token, the identity
# the ownership proof was made against (a primary re-auth can move the row to
# another organization without touching cc_access_token), and the lineage
# columns the dead-chain rule reads.
_SYNC_GUARD_COLS = (
    "cc_access_token",
    "email",
    "organization_uuid",
    "account_uuid",
    "cc_refresh_token",
    "cc_refresh_failure_type",
)


def _cas_update(conn, account_id: int, sets: dict, guards: dict, raw: tuple = ()) -> bool:
    """One compare-and-swap UPDATE of an accounts row. True when it landed.

    The column names in ``sets`` and ``guards`` come from this module, never
    from input. A guard compares with IS, so NULL matches NULL. ``raw`` holds
    fixed SQL assignments with no parameter.

    >>> import sqlite3
    >>> conn = sqlite3.connect(":memory:")
    >>> _ = conn.execute("CREATE TABLE accounts (id INTEGER, a TEXT, b TEXT)")
    >>> _ = conn.execute("INSERT INTO accounts VALUES (1, 'x', NULL)")
    >>> _cas_update(conn, 1, {"a": "y"}, {"b": "other"}), _cas_update(conn, 1, {"a": "y"}, {"b": None})
    (False, True)
    """
    clauses = [f"{column} = ?" for column in sets] + list(raw)
    where = "".join(f" AND {column} IS ?" for column in guards)
    cursor = conn.execute(
        f"UPDATE accounts SET {', '.join(clauses)} WHERE id = ?{where}",
        [*sets.values(), account_id, *guards.values()],
    )
    if cursor.rowcount > 0:
        conn.commit()
        return True
    return False


def _sync_guards(account: dict, cols: set) -> dict:
    """The compare-and-swap guard of a sync write: what its decision read.

    >>> _sync_guards({"cc_access_token": "a", "email": "e"}, {"cc_access_token"})
    {'cc_access_token': 'a'}
    """
    return {column: account.get(column) for column in _SYNC_GUARD_COLS if column in cols}


def _rotation_sets(account: dict, oauth: dict, cols: set) -> tuple[dict, tuple, bool]:
    """The columns a proven rotation writes: ``(values, raw SQL, adopted)``.

    A dead chain follows the shared dead-chain rule. A live chain takes the
    refresh token as it is, NULL included: when Claude Code consumed it,
    should_refresh_cc must see that it is gone. It also takes the
    pass-through fields (#174i) and drops an auth_rejected verdict.

    >>> dead = {"cc_refresh_failure_type": "invalid_grant", "cc_refresh_token": "spent"}
    >>> _rotation_sets(dead, {"accessToken": "a2", "refreshToken": "spent"}, set())
    ({'cc_access_token': 'a2'}, (), False)
    >>> _rotation_sets(dead, {"accessToken": "a2", "refreshToken": "r2"}, {"cc_refresh_token"})
    ({'cc_access_token': 'a2', 'cc_refresh_token': 'r2'}, (), True)
    """
    sets = {"cc_access_token": oauth.get("accessToken")}
    expires_at = expiry_seconds(oauth.get("expiresAt"))
    if expires_at:  # only a real value replaces the row's expiry
        sets["cc_expires_at"] = expires_at
    if cc_chain_dead(account):
        adoption = dead_chain_adoption(account, oauth) or {}
        sets.update({column: value for column, value in adoption.items() if column in cols})
        return sets, (), bool(adoption)
    refresh = oauth.get("refreshToken")
    sets["cc_refresh_token"] = refresh
    if "cc_oauth_extra" in cols:
        from jacked.credentials.oauth_extra import capture_oauth_extra

        sets["cc_oauth_extra"] = capture_oauth_extra(oauth, refresh)
    raw = _CLEAR_AUTH_REJECTED if "cc_refresh_failure_type" in cols else ()
    return sets, raw, False


def write_rotation(conn, account: dict, oauth: dict, cols: set, account_id: int) -> str:
    """Write a proven rotation in one compare-and-swap; the sync result.

    The write lands only while the columns the decision read hold what it
    read. Otherwise the result is "unchanged", and the next pass proves the
    token again against the new row.

    >>> # Tested via test_launch_token_sync*.py and test_launch_sync_dead_chain.py
    """
    sets, raw, adopted = _rotation_sets(account, oauth, cols)
    sets["validation_status"] = "valid"
    sets["last_validated_at"] = int(time.time())
    if not _cas_update(conn, account_id, sets, _sync_guards(account, cols), raw):
        forget_ownership_verdicts(account_id)
        logger.debug("CAS skip for account %d: another writer updated first", account_id)
        return "unchanged"
    logger.debug("Synced CC tokens for account %d", account_id)
    if adopted:
        logger.info(ADOPTED_LINEAGE_MESSAGE, account_id)
    return "imported"


def sync_held_access(conn, account: dict, oauth: dict, cols: set, account_id: int) -> str:
    """Sync a file whose access token the row already holds (proven offline).

    A dead chain follows the shared dead-chain rule: a NEW refresh token is
    adopted with a cleared breaker, and the failed one takes nothing, not
    even the pass-through fields of the dead lineage. A live chain takes the
    pass-through fields the row lacks (a row from before #174i, or a field
    Claude Code changed without rotating), bound to the row's own refresh
    token. A lost compare-and-swap is "unchanged".

    >>> # Tested via test_launch_token_sync_rules.py and test_launch_sync_dead_chain.py
    """
    refresh = oauth.get("refreshToken")
    if cc_chain_dead(account):
        adoption = dead_chain_adoption(account, oauth)
        if adoption is None:
            return "imported"
        sets = {column: value for column, value in adoption.items() if column in cols}
        if not _cas_update(conn, account_id, sets, _sync_guards(account, cols)):
            return "unchanged"
        logger.info(ADOPTED_LINEAGE_MESSAGE, account_id)
        return "imported"
    if "cc_oauth_extra" in cols and refresh == account.get("cc_refresh_token"):
        from jacked.credentials.oauth_extra import capture_oauth_extra

        extra = capture_oauth_extra(oauth, refresh)
        # CAS on BOTH tokens: an invalid_grant recovery keeps the access
        # token but replaces the lineage.
        conn.execute(
            "UPDATE accounts SET cc_oauth_extra = ? "
            "WHERE id = ? AND cc_access_token IS ? "
            "AND cc_refresh_token IS ? AND cc_oauth_extra IS NOT ?",
            (extra, account_id, oauth.get("accessToken"), refresh, extra),
        )
        conn.commit()
    return "imported"  # the row holds the tokens

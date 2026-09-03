"""Small SQLite operations used by the standalone session hook."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_THROTTLE_SECONDS = 300


def match_snapshot_account(
    db_path: Path, snapshot: dict | None
) -> tuple[int | None, str | None]:
    """Match a resolved identity by account id, email, organization and provider."""
    if not db_path.exists() or not isinstance(snapshot, dict):
        return None, None
    observed = snapshot.get("observed")
    if snapshot.get("state") != "resolved" or not isinstance(observed, dict):
        return None, None
    account_id = observed.get("account_id")
    email = observed.get("email")
    organization_id = observed.get("organization_id")
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        return None, None
    if not isinstance(email, str) or not email:
        return None, None
    if organization_id is not None and not isinstance(organization_id, str):
        return None, None
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            row = conn.execute(
                "SELECT id, email FROM accounts "
                "WHERE id = ? AND LOWER(email) = LOWER(?) "
                "AND organization_uuid = ? AND is_deleted = 0 "
                "AND COALESCE(provider, 'claude') = 'claude' LIMIT 1",
                (account_id, email, organization_id or ""),
            ).fetchone()
            return (row[0], row[1]) if row else (None, None)
        finally:
            conn.close()
    except Exception:
        return None, None


def record_session(
    db_path: Path,
    session_id: str,
    account_id: int | None,
    email: str | None,
    method: str,
    repo_path: str | None,
    pid: int | None = None,
    *,
    credential_scope: str | None = None,
    observed_at: str | None = None,
    evidence: str | None = None,
    observation_state: str | None = None,
    credential_revision: str | None = None,
    launch_nonce: str | None = None,
    event_idempotency_key: str | None = None,
    force_new_span: bool = False,
) -> str | None:
    """Insert or refresh one session configuration span."""
    if not db_path.exists():
        return None
    try:
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(session_accounts)")
            }
            supports_evidence = "credential_scope" in columns
            if force_new_span:
                conn.execute(
                    "UPDATE session_accounts SET ended_at = ? "
                    "WHERE session_id = ? AND ended_at IS NULL",
                    (ts, session_id),
                )
            elif account_id is not None:
                conn.execute(
                    """UPDATE session_accounts SET ended_at = ?
                       WHERE session_id = ? AND ended_at IS NULL
                         AND (account_id != ? OR account_id IS NULL)""",
                    (ts, session_id, account_id),
                )
            existing = None
            if not force_new_span:
                existing = conn.execute(
                    """SELECT id FROM session_accounts
                       WHERE session_id = ? AND account_id IS ? AND ended_at IS NULL
                       LIMIT 1""",
                    (session_id, account_id),
                ).fetchone()
            if existing:
                if supports_evidence:
                    conn.execute(
                        """UPDATE session_accounts
                           SET last_activity_at = ?,
                               credential_scope = COALESCE(?, credential_scope),
                               observed_at = COALESCE(?, observed_at),
                               evidence = COALESCE(?, evidence),
                               observation_state = COALESCE(?, observation_state),
                               credential_revision = COALESCE(?, credential_revision),
                               launch_nonce = COALESCE(?, launch_nonce),
                               event_idempotency_key = COALESCE(?, event_idempotency_key)
                           WHERE id = ?""",
                        (
                            ts,
                            credential_scope,
                            observed_at,
                            evidence,
                            observation_state,
                            credential_revision,
                            launch_nonce,
                            event_idempotency_key,
                            existing[0],
                        ),
                    )
                else:
                    conn.execute(
                        "UPDATE session_accounts SET last_activity_at = ? WHERE id = ?",
                        (ts, existing[0]),
                    )
            else:
                base = (session_id, account_id, email, ts, ts, method, repo_path, pid)
                if supports_evidence:
                    conn.execute(
                        """INSERT OR IGNORE INTO session_accounts
                           (session_id, account_id, email, detected_at,
                            last_activity_at, detection_method, repo_path, pid,
                            credential_scope, observed_at, evidence,
                            observation_state, credential_revision, launch_nonce,
                            event_idempotency_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        base
                        + (
                            credential_scope,
                            observed_at,
                            evidence,
                            observation_state,
                            credential_revision,
                            launch_nonce,
                            event_idempotency_key,
                        ),
                    )
                else:
                    conn.execute(
                        """INSERT OR IGNORE INTO session_accounts
                           (session_id, account_id, email, detected_at,
                            last_activity_at, detection_method, repo_path, pid)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        base,
                    )
            conn.commit()
            return ts
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            conn.close()
    except Exception:
        return None


def tag_subagent(
    db_path: Path,
    session_id: str,
    detected_at: str | None,
    parent_session_id: str | None,
    agent_type: str | None,
) -> None:
    if not detected_at or not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                """UPDATE session_accounts
                   SET is_subagent = 1, parent_session_id = ?, agent_type = ?
                   WHERE session_id = ? AND detected_at = ?""",
                (parent_session_id, agent_type, session_id, detected_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def end_session(db_path: Path, session_id: str) -> None:
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                "UPDATE session_accounts SET ended_at = ? "
                "WHERE session_id = ? AND ended_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def heartbeat_session(db_path: Path, session_id: str) -> None:
    if not db_path.exists():
        return
    try:
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            row = conn.execute(
                "SELECT last_activity_at FROM session_accounts "
                "WHERE session_id = ? AND ended_at IS NULL "
                "ORDER BY detected_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return
            if row[0]:
                try:
                    previous = datetime.fromisoformat(row[0])
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=timezone.utc)
                    if (now - previous).total_seconds() < HEARTBEAT_THROTTLE_SECONDS:
                        return
                except (TypeError, ValueError):
                    pass
            conn.execute(
                """UPDATE session_accounts SET last_activity_at = ?
                   WHERE id = (
                       SELECT id FROM session_accounts
                       WHERE session_id = ? AND ended_at IS NULL
                       ORDER BY detected_at DESC LIMIT 1
                   )""",
                (now.isoformat(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

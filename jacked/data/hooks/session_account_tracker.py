#!/usr/bin/env python3
"""Session-account tracker hook for Claude Code.

Handles: SessionStart, Notification(auth_success), SessionEnd, Stop,
UserPromptSubmit.  Reads credentials to identify the active token, then
matches against jacked's accounts DB.  Fire-and-forget via daemon thread.
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "jacked.db"
CRED_PATH = Path.home() / ".claude" / ".credentials.json"
ACCOUNTS_DIR = Path.home() / ".claude" / "accounts"
_ACCOUNT_DIR_RE = re.compile(r"/accounts/(\d+)/?$")


def _get_cred_data() -> tuple[str | None, dict | None]:
    """Read the credential file, return (access_token, full_data).

    Checks CLAUDE_CONFIG_DIR first (set by ``jacked claude``), then global
    file, then macOS Keychain fallback.

    >>> token, data = _get_cred_data()
    >>> token is None or isinstance(token, str)
    True
    """
    # Per-account dir set by ``jacked claude`` — read from there first
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        cred_path = Path(config_dir) / ".credentials.json"
        if cred_path.exists() and not cred_path.is_symlink():
            try:
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                token = data.get("claudeAiOauth", {}).get("accessToken")
                return token, data
            except (json.JSONDecodeError, OSError):
                pass

    # Global file (works on Linux, Windows, and macOS if jacked created it)
    try:
        if CRED_PATH.exists():
            data = json.loads(CRED_PATH.read_text(encoding="utf-8"))
            token = data.get("claudeAiOauth", {}).get("accessToken")
            return token, data
    except (json.JSONDecodeError, OSError, AttributeError):
        pass

    # Fallback: macOS Keychain (Claude Code stores creds here on Mac)
    if sys.platform == "darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout.strip())
                token = data.get("claudeAiOauth", {}).get("accessToken")
                return token, data
        except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as exc:
            print(f"[jacked] keychain read failed: {exc}", file=sys.stderr)

    return None, None


CLAUDE_CONFIG = Path.home() / ".claude.json"


# SYNC: keep in sync with credential_sync.py:40
LAYER3_FRESHNESS_SECONDS = 60


def _match_token_to_account(
    token: str | None,
    cred_data: dict | None = None,
) -> tuple[int | None, str | None]:
    """Match the active account using layered matching.

    If CLAUDE_CONFIG_DIR points to ~/.claude/accounts/<id>/, returns that
    account directly (no matching layers needed — path is authoritative).

    Otherwise uses layered matching (see credential_sync.py for full docs).

    Returns (account_id, email) or (None, None) if no match.

    >>> _match_token_to_account("nonexistent-token")
    (None, None)
    """
    if not DB_PATH.exists():
        return None, None

    # Path-based shortcut: CLAUDE_CONFIG_DIR → account_id from directory name
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    m = _ACCOUNT_DIR_RE.search(config_dir) if config_dir else None
    if m:
        acct_id = int(m.group(1))
        if acct_id > 0:
            try:
                conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
                try:
                    row = conn.execute(
                        "SELECT id, email FROM accounts WHERE id = ? AND is_deleted = 0",
                        (acct_id,),
                    ).fetchone()
                    if row:
                        return row[0], row[1]
                finally:
                    conn.close()
            except Exception:
                pass

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")

            # Layer 1: _jackedAccountId stamp (strongest — user's explicit choice)
            if cred_data is not None:
                jacked_id = cred_data.get("_jackedAccountId")
                if jacked_id is not None:
                    row = conn.execute(
                        "SELECT id, email FROM accounts WHERE id = ? AND is_deleted = 0",
                        (jacked_id,),
                    ).fetchone()
                    if row:
                        return row[0], row[1]

            # Layer 2: Exact access_token match (cryptographically unique)
            if token:
                row = conn.execute(
                    "SELECT id, email FROM accounts WHERE access_token = ? AND is_deleted = 0",
                    (token,),
                ).fetchone()
                if row:
                    return row[0], row[1]

            # Layer 2.5: Exact refresh_token match (current DB RT)
            if cred_data is not None:
                cred_rt = cred_data.get("claudeAiOauth", {}).get("refreshToken")
                if cred_rt:
                    row = conn.execute(
                        "SELECT id, email FROM accounts WHERE refresh_token = ? AND is_deleted = 0",
                        (cred_rt,),
                    ).fetchone()
                    if row:
                        return row[0], row[1]

            # Layer 2.75: known_refresh_tokens table (may not exist yet)
            if cred_data is not None:
                cred_rt = cred_data.get("claudeAiOauth", {}).get("refreshToken")
                if cred_rt:
                    try:
                        row = conn.execute(
                            "SELECT account_id FROM known_refresh_tokens WHERE refresh_token = ?",
                            (cred_rt,),
                        ).fetchone()
                        if row:
                            acct_row = conn.execute(
                                "SELECT id, email FROM accounts WHERE id = ? AND is_deleted = 0",
                                (row[0],),
                            ).fetchone()
                            if acct_row:
                                return acct_row[0], acct_row[1]
                    except sqlite3.OperationalError:
                        pass  # Table doesn't exist yet — skip layer

            # Layer 2.85: Single-account optimization (unambiguous when only 1 OAuth account)
            try:
                oauth_rows = conn.execute(
                    "SELECT id, email FROM accounts "
                    "WHERE refresh_token IS NOT NULL AND is_deleted = 0",
                ).fetchall()
                if len(oauth_rows) == 1:
                    return oauth_rows[0][0], oauth_rows[0][1]
            except sqlite3.OperationalError:
                pass

            # Layer 3: Staleness-gated email from ~/.claude.json
            if CLAUDE_CONFIG.exists() and not CLAUDE_CONFIG.is_symlink():
                try:
                    config_mtime = CLAUDE_CONFIG.stat().st_mtime
                    if time.time() - config_mtime <= LAYER3_FRESHNESS_SECONDS:
                        config = json.loads(
                            CLAUDE_CONFIG.read_text(encoding="utf-8")
                        )
                        email = config.get("oauthAccount", {}).get(
                            "emailAddress"
                        )
                        if email:
                            row = conn.execute(
                                "SELECT id, email FROM accounts "
                                "WHERE LOWER(email) = LOWER(?) AND is_deleted = 0 "
                                "ORDER BY priority ASC, id ASC LIMIT 1",
                                (email,),
                            ).fetchone()
                            if row:
                                return row[0], row[1]
                except (json.JSONDecodeError, OSError):
                    pass
        finally:
            conn.close()
    except Exception:
        pass
    return None, None


def _detect_subagent() -> tuple[bool, str | None, str | None]:
    """Return (is_subagent, parent_session_id, agent_type) from env vars.

    >>> import os; [os.environ.pop(k, None) for k in ['CLAUDE_CODE_PARENT_SESSION_ID', 'CLAUDE_CODE_AGENT_TYPE', 'CLAUDE_CODE_AGENT_NAME']]
    [None, None, None]
    >>> _detect_subagent()
    (False, None, None)
    """
    parent_sid = os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID")
    agent_type = os.environ.get("CLAUDE_CODE_AGENT_TYPE")
    agent_name = os.environ.get("CLAUDE_CODE_AGENT_NAME")
    is_sub = bool(parent_sid or agent_type or agent_name)
    return is_sub, parent_sid, (agent_type or agent_name)


def _record_session(
    session_id: str,
    account_id: int | None,
    email: str | None,
    method: str,
    repo_path: str | None,
) -> str | None:
    """Insert or refresh a session-account record. Returns detected_at or None.

    >>> _record_session("test", None, None, "test", None) is None
    True
    """
    if not DB_PATH.exists():
        return None
    try:
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")

            # End any open records for this session under a DIFFERENT account
            # (account_id != ? doesn't match NULLs, so OR account_id IS NULL)
            if account_id is not None:
                conn.execute(
                    """UPDATE session_accounts SET ended_at = ?
                       WHERE session_id = ? AND ended_at IS NULL
                         AND (account_id != ? OR account_id IS NULL)""",
                    (ts, session_id, account_id),
                )

            # Check if open record already exists for same session+account
            # (IS used instead of = for NULL-safe comparison)
            existing = conn.execute(
                """SELECT id FROM session_accounts
                   WHERE session_id = ? AND account_id IS ? AND ended_at IS NULL
                   LIMIT 1""",
                (session_id, account_id),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE session_accounts SET last_activity_at = ? WHERE id = ?",
                    (ts, existing[0]),
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO session_accounts
                       (session_id, account_id, email, detected_at, last_activity_at,
                        detection_method, repo_path)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (session_id, account_id, email, ts, ts, method, repo_path),
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


def _tag_subagent(session_id: str, detected_at: str | None):
    """Best-effort tag of a session as subagent. Fails silently.

    >>> _tag_subagent("nonexistent", "2025-01-01T00:00:00Z")
    """
    if not detected_at:
        return
    is_sub, parent_sid, agent_type = _detect_subagent()
    if not is_sub:
        return
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                """UPDATE session_accounts
                   SET is_subagent = 1, parent_session_id = ?, agent_type = ?
                   WHERE session_id = ? AND detected_at = ?""",
                (parent_sid, agent_type, session_id, detected_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _end_session(session_id: str):
    """Set ended_at on the latest open record for this session.

    >>> _end_session("nonexistent")
    """
    if not DB_PATH.exists():
        return
    try:
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                """UPDATE session_accounts SET ended_at = ?
                   WHERE session_id = ? AND ended_at IS NULL""",
                (ts, session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


HEARTBEAT_THROTTLE_SECONDS = (
    300  # 5 min — must stay well under SESSION_STALENESS_MINUTES (see web/database.py)
)


def _heartbeat_session(session_id: str):
    """Update last_activity_at, throttled to every 5 min.

    >>> _heartbeat_session("nonexistent")
    """
    if not DB_PATH.exists():
        return
    try:
        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
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
            last = row[0]
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if (now - last_dt).total_seconds() < HEARTBEAT_THROTTLE_SECONDS:
                        return  # throttled — skip write
                except (ValueError, TypeError):
                    pass  # unparseable — update it
            conn.execute(
                """UPDATE session_accounts SET last_activity_at = ?
                   WHERE id = (
                       SELECT id FROM session_accounts
                       WHERE session_id = ? AND ended_at IS NULL
                       ORDER BY detected_at DESC LIMIT 1
                   )""",
                (ts, session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _clear_account_error(account_id: int):
    """Clear stale error when a live session proves creds work.

    >>> _clear_account_error(99999)
    """
    if not DB_PATH.exists() or account_id is None:
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            ts = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE accounts SET
                    validation_status = 'valid',
                    last_error = NULL, last_error_at = NULL,
                    consecutive_failures = 0,
                    last_validated_at = ?,
                    updated_at = ?
                   WHERE id = ? AND validation_status IN ('invalid', 'unknown')""",
                (int(time.time()), ts, account_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _handle_event(event: str, session_id: str, repo_path: str | None):
    """Route the hook event to the appropriate handler.

    >>> _handle_event("SessionEnd", "test-sess", None)
    >>> _handle_event("Stop", "test-sess", None)
    >>> _handle_event("UserPromptSubmit", "test-sess", None)
    """
    if event == "SessionEnd":
        _end_session(session_id)
        return

    if event in ("Stop", "UserPromptSubmit"):
        _heartbeat_session(session_id)
        return

    # SessionStart or Notification(auth_success) — detect account
    token, cred_data = _get_cred_data()
    account_id, email = _match_token_to_account(token, cred_data)

    method = "auth_success" if event == "Notification" else "session_start"

    if event == "Notification":
        _end_session(session_id)
        _record_session(session_id, account_id, email, method, repo_path)
    else:
        ts = _record_session(session_id, account_id, email, method, repo_path)
        _tag_subagent(session_id, ts)

    if account_id is not None:
        _clear_account_error(account_id)


def main():
    """Read hook input from stdin, dispatch in fire-and-forget thread.

    >>> # main() reads stdin — can't easily doctest, but structure is tested above
    """
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    repo_path = data.get("cwd")

    if not session_id:
        return

    # Only handle our events
    if event not in ("SessionStart", "Notification", "SessionEnd", "Stop", "UserPromptSubmit"):
        return

    # Fire-and-forget: daemon thread so we don't block Claude Code
    t = threading.Thread(
        target=_handle_event,
        args=(event, session_id, repo_path),
        daemon=True,
    )
    t.start()
    t.join(timeout=2.0)


if __name__ == "__main__":
    main()

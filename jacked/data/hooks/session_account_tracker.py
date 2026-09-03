#!/usr/bin/env python3
"""Fast session hook consuming only the canonical secret-free snapshot."""

import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from jacked.resolver_snapshot import SNAPSHOT_FILENAME, read_resolver_snapshot
from jacked.session_tracking_store import (
    end_session,
    heartbeat_session,
    match_snapshot_account,
    record_session,
    tag_subagent,
)

DB_PATH = Path.home() / ".claude" / "jacked.db"
OBSERVATION_INBOX_LIMIT = 512


def _config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else Path.home() / ".claude"


def _read_resolver_snapshot(now: float | None = None) -> dict | None:
    return read_resolver_snapshot(
        _config_dir() / SNAPSHOT_FILENAME, now=now, require_fresh=True
    )


def _get_cred_data() -> tuple[None, dict | None]:
    """Compatibility wrapper returning only the secret-free snapshot."""
    return None, _read_resolver_snapshot()


def _match_token_to_account(
    token: str | None, cred_data: dict | None = None
) -> tuple[int | None, str | None]:
    """Match account id, email, organization and provider evidence exactly."""
    del token
    return match_snapshot_account(DB_PATH, cred_data)


def _detect_subagent() -> tuple[bool, str | None, str | None]:
    parent = os.environ.get("CLAUDE_CODE_PARENT_SESSION_ID")
    agent_type = os.environ.get("CLAUDE_CODE_AGENT_TYPE") or os.environ.get(
        "CLAUDE_CODE_AGENT_NAME"
    )
    return bool(parent or agent_type), parent, agent_type


def _record_session(
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
    return record_session(
        DB_PATH,
        session_id,
        account_id,
        email,
        method,
        repo_path,
        pid,
        credential_scope=credential_scope,
        observed_at=observed_at,
        evidence=evidence,
        observation_state=observation_state,
        credential_revision=credential_revision,
        launch_nonce=launch_nonce,
        event_idempotency_key=event_idempotency_key,
        force_new_span=force_new_span,
    )


def _tag_subagent(session_id: str, detected_at: str | None) -> None:
    is_subagent, parent, agent_type = _detect_subagent()
    if is_subagent:
        tag_subagent(DB_PATH, session_id, detected_at, parent, agent_type)


def _end_session(session_id: str) -> None:
    end_session(DB_PATH, session_id)


def _heartbeat_session(session_id: str) -> None:
    heartbeat_session(DB_PATH, session_id)


def _iso_from_epoch(value) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _observation_context(snapshot: dict | None) -> dict:
    """Build nonsecret, evidence-qualified fields for a hook event."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    scope = os.environ.get("JACKED_CREDENTIAL_SCOPE") or snapshot.get("scope")
    certified_scoped = os.environ.get("JACKED_SCOPED_CREDENTIAL_CERTIFIED") == "1"
    if scope == "scoped" and not certified_scoped:
        scope = "unknown"
    if scope not in {"global", "scoped", "unknown"}:
        scope = "unknown"

    state = snapshot.get("state")
    observation_state = (
        "observed"
        if state == "resolved"
        else "conflict"
        if state == "conflict"
        else "unknown"
    )
    evidence = snapshot.get("evidence")
    if scope == "scoped" and certified_scoped:
        evidence = "launch_binding"
    elif isinstance(evidence, list) and all(
        isinstance(item, str) and item for item in evidence
    ):
        evidence = ",".join(evidence)
    elif not isinstance(evidence, str) or not evidence:
        evidence = "unknown"

    revision = os.environ.get("JACKED_CREDENTIAL_REVISION") or snapshot.get(
        "credential_revision"
    )
    launch_nonce = os.environ.get("JACKED_LAUNCH_NONCE")
    return {
        "credential_scope": scope,
        "observed_at": _iso_from_epoch(snapshot.get("published_at")),
        "evidence": evidence,
        "observation_state": observation_state,
        "credential_revision": revision if isinstance(revision, str) else None,
        "launch_nonce": launch_nonce if launch_nonce else None,
    }


def _event_idempotency_key(
    session_id: str,
    event: str,
    credential_revision: str | None,
    launch_nonce: str | None,
) -> str:
    material = "\x00".join(
        (session_id, event, credential_revision or "unknown", launch_nonce or "")
    )
    return sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _enqueue_observation(
    session_id: str, event: str, repo_path: str | None, context: dict
) -> str | None:
    """Coalesce one nonsecret request for off-thread resolver processing."""
    if not DB_PATH.exists():
        return None
    key = _event_idempotency_key(
        session_id,
        event,
        context.get("credential_revision"),
        context.get("launch_nonce"),
    )
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'session_observation_inbox'"
            ).fetchone()
            if not table:
                return None
            conn.execute(
                """INSERT INTO session_observation_inbox
                   (session_id, event_kind, credential_revision, launch_nonce,
                    repo_path, requested_at, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(idempotency_key) DO UPDATE SET
                       repo_path = excluded.repo_path,
                       requested_at = excluded.requested_at""",
                (
                    session_id,
                    event,
                    context.get("credential_revision"),
                    context.get("launch_nonce"),
                    repo_path,
                    datetime.now(timezone.utc).isoformat(),
                    key,
                ),
            )
            conn.execute(
                """DELETE FROM session_observation_inbox
                   WHERE id IN (
                       SELECT id FROM session_observation_inbox
                       ORDER BY requested_at DESC, id DESC LIMIT -1 OFFSET ?
                   )""",
                (OBSERVATION_INBOX_LIMIT,),
            )
            conn.commit()
            return key
        finally:
            conn.close()
    except Exception:
        return None


def _latest_session_revision(session_id: str) -> tuple[bool, str | None]:
    if not DB_PATH.exists():
        return False, None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(session_accounts)")
            }
            if "credential_revision" not in columns:
                return False, None
            row = conn.execute(
                """SELECT credential_revision FROM session_accounts
                   WHERE session_id = ? AND ended_at IS NULL
                   ORDER BY detected_at DESC LIMIT 1""",
                (session_id,),
            ).fetchone()
            revision = row[0] if row and isinstance(row[0], str) else None
            return True, revision
        finally:
            conn.close()
    except Exception:
        return False, None


def _mark_session_observation_state(session_id: str, context: dict) -> None:
    """Update evidence on the open span without changing its account label."""
    if not DB_PATH.exists():
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=2.0)
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(session_accounts)")
            }
            if "observation_state" not in columns:
                return
            conn.execute(
                """UPDATE session_accounts
                   SET last_activity_at = ?, observation_state = ?,
                       credential_scope = COALESCE(?, credential_scope),
                       evidence = COALESCE(?, evidence),
                       observed_at = COALESCE(?, observed_at)
                   WHERE id = (
                       SELECT id FROM session_accounts
                       WHERE session_id = ? AND ended_at IS NULL
                       ORDER BY detected_at DESC LIMIT 1
                   )""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    context.get("observation_state"),
                    context.get("credential_scope"),
                    context.get("evidence"),
                    context.get("observed_at"),
                    session_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def _handle_event(event: str, session_id: str, repo_path: str | None) -> None:
    if event == "SessionEnd":
        _end_session(session_id)
        return
    token, snapshot = _get_cred_data()
    context = _observation_context(snapshot)
    context["event_idempotency_key"] = _enqueue_observation(
        session_id, event, repo_path, context
    )

    if event in {"Stop", "UserPromptSubmit"}:
        if context["observation_state"] != "observed":
            _mark_session_observation_state(session_id, context)
            return
        account_id, email = _match_token_to_account(token, snapshot)
        supported, previous = _latest_session_revision(session_id)
        revision = context.get("credential_revision")
        if not supported:
            _heartbeat_session(session_id)
        elif account_id is None:
            context.update(
                observation_state="unknown", evidence="resolver_identity_unmatched"
            )
            _mark_session_observation_state(session_id, context)
        elif not revision or revision == previous:
            _mark_session_observation_state(session_id, context)
        else:
            _record_session(
                session_id,
                account_id,
                email,
                "resolver_observation",
                repo_path,
                os.getppid(),
                **context,
                force_new_span=True,
            )
        return

    account_id, email = _match_token_to_account(token, snapshot)
    if event == "Notification":
        _end_session(session_id)
        _record_session(
            session_id,
            account_id,
            email,
            "auth_success",
            repo_path,
            os.getppid(),
            **context,
        )
    else:
        detected_at = _record_session(
            session_id,
            account_id,
            email,
            "session_start",
            repo_path,
            os.getppid(),
            **context,
        )
        _tag_subagent(session_id, detected_at)


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return
    event = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    if not session_id or event not in {
        "SessionStart",
        "Notification",
        "UserPromptSubmit",
        "SessionEnd",
        "Stop",
    }:
        return
    thread = threading.Thread(
        target=_handle_event,
        args=(event, session_id, data.get("cwd")),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=2.0)


if __name__ == "__main__":
    main()

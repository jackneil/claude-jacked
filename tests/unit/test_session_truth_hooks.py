"""Evidence and coalescing contracts for the Claude session hook."""

import json
import os
import sqlite3
import sys
import time
from io import StringIO
from pathlib import Path

import jacked.data.hooks.session_account_tracker as tracker
from jacked.web.database import Database
from tests._platform import requires_symlinks


def _snapshot(config_dir, *, account_id=1, email="a@x.com", org="org-a", **overrides):
    identity = {
        "account_id": account_id,
        "email": email,
        "organization_id": org,
    }
    data = {
        "schema_version": 1,
        "published_at": time.time() - 1,
        "fresh_until": time.time() + 300,
        "scope": "global",
        "state": "resolved",
        "evidence": ["store_consensus"],
        "credential_revision": "rev-1",
        "desired": identity,
        "observed": identity,
    }
    data.update(overrides)
    path = config_dir / tracker.SNAPSHOT_FILENAME
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    return path


def _database(path):
    db = Database(str(path))
    db.create_account(
        "a@x.com",
        "not-a-real-token-a",
        4102444800,
        provider="claude",
        organization_uuid="org-a",
    )
    db.create_account(
        "b@x.com",
        "not-a-real-token-b",
        4102444800,
        provider="claude",
        organization_uuid="org-b",
    )
    db.close()


def test_snapshot_reader_rejects_permissions_and_secret_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = _snapshot(tmp_path)
    assert tracker._read_resolver_snapshot() is not None

    if os.name != "nt":
        path.chmod(0o644)
        assert tracker._read_resolver_snapshot() is None
        path.chmod(0o600)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["access_token"] = "must-not-enter-session-state"
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)
    assert tracker._read_resolver_snapshot() is None


@requires_symlinks
def test_snapshot_reader_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    path = tmp_path / tracker.SNAPSHOT_FILENAME
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)
    assert tracker._read_resolver_snapshot() is None


def test_start_records_evidence_and_activity_coalesces(tmp_path, monkeypatch):
    db_path = tmp_path / "jacked.db"
    _database(db_path)
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _snapshot(tmp_path)

    tracker._handle_event("SessionStart", "session-1", "/repo")
    tracker._handle_event("UserPromptSubmit", "session-1", "/repo")
    tracker._handle_event("UserPromptSubmit", "session-1", "/repo")

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM session_accounts WHERE session_id = 'session-1'"
        ).fetchone()
        inbox = con.execute(
            "SELECT * FROM session_observation_inbox "
            "WHERE session_id = 'session-1' AND event_kind = 'UserPromptSubmit'"
        ).fetchall()
    finally:
        con.close()

    assert row["account_id"] == 1
    assert row["credential_scope"] == "global"
    assert row["evidence"] == "store_consensus"
    assert row["observation_state"] == "observed"
    assert row["credential_revision"] == "rev-1"
    assert len(inbox) == 1


def test_conflict_marks_pending_span_without_relabeling(tmp_path, monkeypatch):
    db_path = tmp_path / "jacked.db"
    _database(db_path)
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _snapshot(tmp_path)
    tracker._handle_event("SessionStart", "session-1", "/repo")

    _snapshot(
        tmp_path,
        account_id=2,
        email="b@x.com",
        org="org-b",
        state="conflict",
        observed=None,
        credential_revision="rev-2",
    )
    tracker._handle_event("UserPromptSubmit", "session-1", "/repo")

    db = Database(str(db_path))
    rows = db.get_session_accounts("session-1")
    db.close()
    assert len(rows) == 1
    assert rows[0]["account_id"] == 1
    assert rows[0]["observation_state"] == "conflict"
    assert rows[0]["credential_revision"] == "rev-1"


def test_new_observed_revision_opens_an_immutable_span(tmp_path, monkeypatch):
    db_path = tmp_path / "jacked.db"
    _database(db_path)
    monkeypatch.setattr(tracker, "DB_PATH", db_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _snapshot(tmp_path)
    tracker._handle_event("SessionStart", "session-1", "/repo")

    _snapshot(
        tmp_path,
        account_id=2,
        email="b@x.com",
        org="org-b",
        credential_revision="rev-2",
    )
    tracker._handle_event("Stop", "session-1", "/repo")

    db = Database(str(db_path))
    rows = db.get_session_accounts("session-1")
    db.close()
    assert len(rows) == 2
    assert rows[0]["account_id"] == 2
    assert rows[0]["ended_at"] is None
    assert rows[0]["credential_revision"] == "rev-2"
    assert rows[1]["account_id"] == 1
    assert rows[1]["ended_at"] is not None


def test_scoped_label_requires_launch_certification(monkeypatch):
    snapshot = {"scope": "scoped", "state": "resolved", "evidence": "launch_binding"}
    monkeypatch.delenv("JACKED_SCOPED_CREDENTIAL_CERTIFIED", raising=False)
    assert tracker._observation_context(snapshot)["credential_scope"] == "unknown"

    monkeypatch.setenv("JACKED_SCOPED_CREDENTIAL_CERTIFIED", "1")
    context = tracker._observation_context(snapshot)
    assert context["credential_scope"] == "scoped"
    assert context["evidence"] == "launch_binding"


def test_user_prompt_submit_is_an_explicit_hook_event(monkeypatch):
    calls = []
    monkeypatch.setattr(tracker, "_handle_event", lambda *args: calls.append(args))
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "session-1",
                    "cwd": "/repo",
                }
            )
        ),
    )
    tracker.main()
    assert calls == [("UserPromptSubmit", "session-1", "/repo")]


def test_hook_source_never_reads_legacy_identity_files():
    source = Path(tracker.__file__).read_text(encoding="utf-8")
    assert ".credentials.json" not in source
    assert ".claude.json" not in source

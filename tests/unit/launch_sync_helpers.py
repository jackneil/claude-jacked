"""Shared helpers for the launch token sync tests (#158i).

These modules import the helpers: test_launch_token_sync.py,
test_launch_token_sync_rules.py and test_launch_token_sync_lifecycle.py.
This file is a plain module, not a test module. Pytest collects only the
``test_*.py`` files. Each test module keeps its own autouse fixture that
clears the ownership cache.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from jacked.api.authority_guard import ProfileLookup
from jacked.web.database import Database

FUTURE_MS = 9_999_999_999_000
OWNER = "owner@test.com"
ORG = "org-own"
UUID = "uuid-own"
INTRUDER = "intruder@test.com"
VERIFIED_AT = 1_798_000_000_000


class _Lookup:
    """A fake profile endpoint that counts its requests."""

    def __init__(self, result, on_call=None):
        self.result = result
        self.on_call = on_call
        self.calls = 0

    def __call__(self, access_token):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        return self.result


def _profile(email, org=ORG, uuid=UUID):
    return ProfileLookup(
        200, {"account": {"email": email, "uuid": uuid}, "organization": {"uuid": org}}
    )


def _patched(lookup):
    return mock.patch("jacked.api.credential_helpers._ownership_lookup", new=lookup)


def _make_row(
    tmp_path,
    *,
    account_id=1,
    email=OWNER,
    org=ORG,
    uuid=UUID,
    cc_at="row-at-1",
    cc_rt="row-rt-1",
    cc_expires=None,
    failure=None,
    status="unknown",
) -> Database:
    db = Database(str(tmp_path / "test.db"))
    future = int(time.time()) + 3600
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, account_uuid, access_token,
                refresh_token, expires_at, cc_access_token, cc_refresh_token,
                cc_expires_at, cc_refresh_failure_type, is_active, is_deleted,
                validation_status, consecutive_failures)
               VALUES (?, ?, ?, ?, 'primary-at', 'primary-rt', ?, ?, ?, ?, ?,
                       1, 0, ?, 0)""",
            (account_id, email, org, uuid, future, cc_at, cc_rt,
             future if cc_expires is None else cc_expires, failure, status),
        )
    return db


def _account_dir(tmp_path, name="1") -> Path:
    config_dir = tmp_path / "accounts" / str(name)
    config_dir.mkdir(parents=True)
    return config_dir


def _write_credentials(config_dir, access, refresh, *, expires_ms=FUTURE_MS, **fields):
    oauth = {"accessToken": access, "expiresAt": expires_ms, **fields}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


def _write_claude_json(path, email, org=ORG, uuid=UUID):
    path.write_text(json.dumps({"oauthAccount": {
        "emailAddress": email, "organizationUuid": org, "accountUuid": uuid,
    }}))


def _sync(config_dir, tmp_path):
    from jacked.launch import _sync_tokens_from_file

    return _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))


def _cc(db, account_id=1):
    row = db.get_account(account_id)
    return row["cc_access_token"], row["cc_refresh_token"], row["validation_status"]


def _exit_claude(config_dir, tmp_path):
    """Run launch_claude with a Claude Code that stops at once, so only the
    exit-time sync reads the file."""
    from jacked.launch import launch_claude

    proc = mock.MagicMock(pid=42, returncode=0)
    proc.wait.return_value = 0
    proc.poll.return_value = 0
    with (
        mock.patch("jacked.launch.subprocess.Popen", return_value=proc),
        mock.patch("jacked.launch.find_bin", return_value=None),
        pytest.raises(SystemExit),
    ):
        launch_claude(config_dir, (), db_path=str(tmp_path / "test.db"))


def _next_launch(db, tmp_path):
    from jacked.launch import prepare_account_dir

    with (
        mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"),
        mock.patch("jacked.launch.should_refresh", return_value=False),
    ):
        return prepare_account_dir(db.get_account(1), db)


def _file_pair(config_dir):
    oauth = json.loads((config_dir / ".credentials.json").read_text())["claudeAiOauth"]
    return oauth["accessToken"], oauth["refreshToken"]

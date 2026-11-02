"""#154i item 3: validate_account never reports a row valid on another
account's profile.

A profile 200 proves the token works, not that it is this row's token.
When the profile names another account, nothing is applied and the row is
marked invalid with ``Token belongs to a different account``. Both 200
branches (the first request and the retry after a refresh) share one rule.
No test reaches the network or a credential store.
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.web import auth
from jacked.web.auth import ProfileApply
from jacked.web.database import Database

FOREIGN = "Token belongs to a different account"
OWNER = "alice@test.com"


@pytest.fixture(autouse=True)
def _fresh_locks():
    auth.reset_locks()
    yield
    auth.reset_locks()


def _resp(status, body=None):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    resp.headers = {}
    return resp


def _client(*gets, post=None):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=[_resp(*g) for g in gets])
    client.post = AsyncMock(return_value=_resp(*(post or (500, {}))))
    return client


def _profile(email):
    return {"account": {"email": email, "uuid": f"uuid-{email}"},
            "organization": {"uuid": None, "organization_type": "claude_max"}}


def _db(tmp_path):
    db = Database(str(tmp_path / "validate.db"))
    acct = db.create_account(OWNER, "alice_at", int(time.time()) + 3600,
                             refresh_token="alice_rt")
    db.update_account(acct["id"], validation_status="unknown")
    return db, acct["id"]


def _validate(db, account_id, client):
    with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
        return asyncio.run(auth.validate_account(account_id, db))


class TestFirstRequest:
    def test_foreign_profile_marks_the_row_invalid(self, tmp_path, caplog):
        db, aid = _db(tmp_path)
        client = _client((200, _profile("mallory@test.com")))
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            verdict = _validate(db, aid, client)

        assert verdict == {"valid": False, "error": FOREIGN}
        row = db.get_account(aid)
        assert row["validation_status"] == "invalid"
        assert row["last_error"] == FOREIGN
        assert row["last_error_at"] is not None
        # Nothing of the foreign profile landed on the row.
        assert row["profile_json"] is None and row["account_uuid"] is None
        assert row["email"] == OWNER
        client.post.assert_not_awaited()
        messages = [r.getMessage() for r in caplog.records]
        assert f"Account {aid}: validation failed; the token belongs to a different account" \
            in messages
        assert not any("mallory" in m for m in messages)

    def test_matching_profile_still_marks_valid(self, tmp_path):
        db, aid = _db(tmp_path)
        verdict = _validate(db, aid, _client((200, _profile(OWNER))))

        assert verdict == {"valid": True, "error": None}
        row = db.get_account(aid)
        assert row["validation_status"] == "valid"
        assert row["account_uuid"] == f"uuid-{OWNER}"
        assert row["profile_json"] is not None

    def test_another_organization_is_another_account(self, tmp_path):
        db, aid = _db(tmp_path)
        db.update_account(aid, organization_uuid="org-a")
        other_org = {**_profile(OWNER), "organization": {"uuid": "org-b"}}
        verdict = _validate(db, aid, _client((200, other_org)))

        assert verdict == {"valid": False, "error": FOREIGN}
        assert db.get_account(aid)["validation_status"] == "invalid"

    @pytest.mark.parametrize("profile", [{}, {"account": {}}, {"account": {"email": " "}},
                                         {"account": None}])
    def test_an_unreadable_profile_accuses_nobody(self, tmp_path, profile):
        """A 200 proves the token works; a profile with no owner value to
        compare is not evidence of another account (behavior as before)."""
        db, aid = _db(tmp_path)
        verdict = _validate(db, aid, _client((200, profile)))

        assert verdict == {"valid": True, "error": None}
        row = db.get_account(aid)
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None
        assert row["profile_json"] is None  # still nothing applied

    def test_a_reauth_that_landed_meanwhile_is_not_marked(self, tmp_path):
        """The invalid mark is a compare-and-set on the token that was
        checked: a re-auth that stored a new token keeps its own status."""
        db, aid = _db(tmp_path)
        real_apply = auth._apply_profile

        def reauth_lands(account_id, data, db_):
            db_.update_account(account_id, access_token="reauth_at",
                               validation_status="valid", last_error=None)
            return real_apply(account_id, data, db_)

        with patch("jacked.web.auth._apply_profile", side_effect=reauth_lands):
            verdict = _validate(db, aid, _client((200, _profile("mallory@test.com"))))

        assert verdict == {"valid": False, "error": FOREIGN}
        row = db.get_account(aid)
        assert row["access_token"] == "reauth_at"
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None


class TestRetryAfterRefresh:
    _ROTATED = (200, {"access_token": "alice_at2", "refresh_token": "alice_rt2",
                      "expires_in": 3600})

    def test_foreign_profile_on_the_retry_marks_invalid(self, tmp_path):
        db, aid = _db(tmp_path)
        client = _client((401, {}), (200, _profile("mallory@test.com")), post=self._ROTATED)
        verdict = _validate(db, aid, client)

        assert client.post.await_count == 1
        assert client.get.await_count == 2
        sent = client.get.await_args_list[1].kwargs["headers"]["Authorization"]
        assert sent == "Bearer alice_at2"
        assert verdict == {"valid": False, "error": FOREIGN}
        row = db.get_account(aid)
        assert row["validation_status"] == "invalid"
        assert row["last_error"] == FOREIGN
        assert row["profile_json"] is None

    def test_matching_profile_on_the_retry_marks_valid(self, tmp_path):
        db, aid = _db(tmp_path)
        client = _client((401, {}), (200, _profile(OWNER)), post=self._ROTATED)
        verdict = _validate(db, aid, client)

        assert verdict == {"valid": True, "error": None}
        row = db.get_account(aid)
        assert row["validation_status"] == "valid"
        assert row["account_uuid"] == f"uuid-{OWNER}"


class TestApplyProfileReason:
    """The sibling reports WHY a profile was not applied; the bool wrapper
    keeps its contract for its other callers."""

    def test_reasons(self, tmp_path):
        db, aid = _db(tmp_path)
        assert auth._apply_profile(9999, _profile(OWNER), db) is ProfileApply.ROW_UNKNOWN
        assert auth._apply_profile(aid, _profile("mallory@test.com"), db) is (
            ProfileApply.OWNER_MISMATCH)
        assert auth._apply_profile(aid, {}, db) is ProfileApply.PROFILE_UNREADABLE
        assert auth._apply_profile(aid, {"account": {"uuid": "u"}}, db) is (
            ProfileApply.PROFILE_UNREADABLE)
        assert auth._apply_profile(aid, _profile(OWNER), db) is ProfileApply.APPLIED

    def test_identity_change_during_the_apply(self, tmp_path):
        db, aid = _db(tmp_path)
        with patch.object(db, "update_account_if_unchanged", return_value=False):
            assert auth._apply_profile(aid, _profile(OWNER), db) is (
                ProfileApply.IDENTITY_CHANGED)

    def test_bool_wrapper_is_unchanged(self, tmp_path):
        db, aid = _db(tmp_path)
        assert auth._update_profile_metadata(9999, _profile(OWNER), db) is False
        assert auth._update_profile_metadata(aid, _profile("mallory@test.com"), db) is False
        assert auth._update_profile_metadata(aid, _profile(OWNER), db) is True

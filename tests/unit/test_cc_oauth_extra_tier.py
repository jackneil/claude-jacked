"""subscriptionTypeVerifiedAt goes out only for the tier it verified (#230i).

Claude Code 2.1.316 writes subscriptionTypeVerifiedAt beside
subscriptionTypeSource when a profile fetch verifies the tier, and keeps it
across a refresh only while the tier is unchanged. jacked publishes the tier
from its own profile poll, so the stored timestamp is bound to the tier it
verified: a switch writes it back only while the row's subscription_type still
equals that tier. Otherwise the source goes out alone, the shape Claude Code
itself emits and re-verifies.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from jacked.api.credential_helpers import build_oauth_data
from jacked.credentials.oauth_extra import (
    capture_oauth_extra,
    captured_tier,
    parse_oauth_extra,
)
from jacked.web.database import Database

VERIFIED_AT = 1_798_000_000_000
RT_EXPIRES = 1_900_000_000_000
EMAIL = "owner@test.com"

# What Claude Code holds after a profile fetch verified the "max" tier.
VERIFIED_MAX = {
    "subscriptionType": "max",
    "subscriptionTypeSource": "profile",
    "subscriptionTypeVerifiedAt": VERIFIED_AT,
}


def _account(tmp_path, *, tier="max", **columns):
    """A real row: cc chain cc-at/cc-rt, owned by org-a/uuid-a, on ``tier``."""
    db = Database(str(tmp_path / "t.db"))
    aid = db.create_account(EMAIL, "primary-at", int(time.time()) + 3600)["id"]
    db.update_account(aid, **{
        "cc_access_token": "cc-at",
        "cc_refresh_token": "cc-rt",
        "cc_expires_at": int(time.time()) + 3600,
        "subscription_type": tier,
        "organization_uuid": "org-a",
        "account_uuid": "uuid-a",
        **columns,
    })
    return db, aid


def _reconcile(db, aid, live):
    """Capture: read ``live`` as the platform store, owned by this row."""
    from jacked.api.authority_guard import ProfileLookup
    from jacked.api.credential_helpers import (
        invalidate_live_cred_cache,
        reconcile_credentials_from_live_store,
    )

    owner = ProfileLookup(200, {"account": {"email": EMAIL, "uuid": "uuid-a"},
                                "organization": {"uuid": "org-a"}})
    invalidate_live_cred_cache()
    with patch("jacked.api.credential_helpers.read_platform_credentials", return_value=live), \
         patch("jacked.api.credential_helpers._ownership_lookup", return_value=owner):
        return reconcile_credentials_from_live_store(aid, db)


def _poll_profile(db, aid, organization_type):
    """jacked's own profile poll, applied to the row as fetch_profile does."""
    from jacked.web.auth import _update_profile_metadata

    data = {"account": {"email": EMAIL, "uuid": "uuid-a"},
            "organization": {"uuid": "org-a", "organization_type": organization_type}}
    assert _update_profile_metadata(aid, data, db) is True


def _live(db, aid, **fields):
    """Claude Code's object after it rotated the chain and verified the tier."""
    oauth = {**build_oauth_data(db.get_account(aid)), "accessToken": "cc-at-2",
             "refreshToken": "cc-rt-2", "expiresAt": (int(time.time()) + 7200) * 1000,
             **VERIFIED_MAX, **fields}
    return {"_jackedAccountId": aid, "claudeAiOauth": oauth}


# ---------------------------------------------------------------------------
# Publication: the appendix's acceptance case
# ---------------------------------------------------------------------------


def test_the_timestamp_goes_out_only_while_the_row_publishes_the_verified_tier(tmp_path):
    db, aid = _account(tmp_path, cc_oauth_extra=capture_oauth_extra(VERIFIED_MAX, "cc-rt"))
    oauth = build_oauth_data(db.get_account(aid))
    assert oauth["subscriptionType"] == "max"
    assert oauth["subscriptionTypeSource"] == "profile"
    assert oauth["subscriptionTypeVerifiedAt"] == 1798000000000

    db.update_account(aid, subscription_type="pro")
    oauth = build_oauth_data(db.get_account(aid))
    assert oauth["subscriptionType"] == "pro"
    assert oauth["subscriptionTypeSource"] == "profile"
    assert "subscriptionTypeVerifiedAt" not in oauth


# ---------------------------------------------------------------------------
# (a) capture -> switch away -> switch back, tier unchanged
# ---------------------------------------------------------------------------


def test_switching_away_and_back_writes_the_identical_verified_pair(tmp_path):
    db, aid = _account(tmp_path)
    assert "subscriptionTypeVerifiedAt" not in build_oauth_data(db.get_account(aid))
    live = _live(db, aid)

    # Capture: Claude Code rotated and verified the tier; reconcile reads it.
    assert _reconcile(db, aid, live) is True
    stored = db.get_account(aid)["cc_oauth_extra"]
    # Switch away: the outgoing save reads the same live object again.
    assert _reconcile(db, aid, live) is True
    assert db.get_account(aid)["cc_oauth_extra"] == stored
    # jacked's own profile poll confirms the same tier while it is parked.
    _poll_profile(db, aid, "claude_max")

    # Switch back: the pair goes out exactly as Claude Code wrote it.
    back = build_oauth_data(db.get_account(aid))
    assert back["subscriptionTypeSource"] == live["claudeAiOauth"]["subscriptionTypeSource"]
    assert back["subscriptionTypeVerifiedAt"] == live["claudeAiOauth"]["subscriptionTypeVerifiedAt"]
    # A reconcile of what jacked wrote changes nothing.
    assert _reconcile(db, aid, {"_jackedAccountId": aid, "claudeAiOauth": back}) is True
    assert db.get_account(aid)["cc_oauth_extra"] == stored
    assert build_oauth_data(db.get_account(aid)) == back


# ---------------------------------------------------------------------------
# (b) the profile poll changes the tier after the capture
# ---------------------------------------------------------------------------


def test_a_tier_change_from_the_profile_poll_withholds_the_timestamp(tmp_path):
    db, aid = _account(tmp_path)
    assert _reconcile(db, aid, _live(db, aid)) is True
    assert build_oauth_data(db.get_account(aid))["subscriptionTypeVerifiedAt"] == VERIFIED_AT

    _poll_profile(db, aid, "claude_pro")
    oauth = build_oauth_data(db.get_account(aid))
    assert oauth["subscriptionType"] == "pro"
    assert oauth["subscriptionTypeSource"] == "profile"
    assert "subscriptionTypeVerifiedAt" not in oauth


# ---------------------------------------------------------------------------
# (f) jacked's own cc refresh: Claude Code's merge rule
# ---------------------------------------------------------------------------


def _refresh(db, aid, result):
    from jacked.web import auth

    with patch.object(auth, "_exchange_refresh_token", AsyncMock(return_value=result)):
        return asyncio.run(auth._refresh_token_flow(aid, db, auth.RefreshMode.CC))


@pytest.mark.parametrize("tier_at_refresh, kept", [("max", True), ("pro", False)])
def test_a_cc_refresh_keeps_the_timestamp_only_while_the_tier_is_unchanged(
    tmp_path, tier_at_refresh, kept
):
    """A refresh rotates the lineage, not the tier: the verification of an
    unchanged tier is re-bound to the rotated token; a verification of a tier
    the row no longer publishes is dropped, as Claude Code's merge does."""
    from jacked.web.auth import TokenExchangeResult

    captured = capture_oauth_extra({**VERIFIED_MAX, "refreshTokenExpiresAt": RT_EXPIRES}, "cc-rt")
    db, aid = _account(tmp_path, tier=tier_at_refresh, cc_oauth_extra=captured,
                       cc_expires_at=int(time.time()) - 10)
    result = TokenExchangeResult(success=True, access_token="cc-at-2",
                                 refresh_token="cc-rt-2", expires_in=3600)
    _refresh(db, aid, result)
    row = db.get_account(aid)
    assert row["cc_refresh_token"] == "cc-rt-2"

    # Read for the verified tier: what survived in storage, not what one
    # publication shows.
    stored = parse_oauth_extra(row["cc_oauth_extra"], "cc-rt-2", current_tier="max")
    assert stored["subscriptionTypeSource"] == "profile"
    assert stored["refreshTokenExpiresAt"] == RT_EXPIRES  # kept: none in the response
    published = build_oauth_data(row)
    assert published["subscriptionTypeSource"] == "profile"
    if kept:
        assert stored["subscriptionTypeVerifiedAt"] == VERIFIED_AT
        assert captured_tier(row["cc_oauth_extra"], "cc-rt-2") == "max"
        assert published["subscriptionTypeVerifiedAt"] == VERIFIED_AT
    else:
        assert "subscriptionTypeVerifiedAt" not in stored
        assert captured_tier(row["cc_oauth_extra"], "cc-rt-2") is None
        assert "subscriptionTypeVerifiedAt" not in published


# ---------------------------------------------------------------------------
# (g) the invalid_grant live recovery
# ---------------------------------------------------------------------------


def test_the_invalid_grant_recovery_binds_the_timestamp_to_the_live_tier(tmp_path):
    from jacked.web import auth

    db, aid = _account(tmp_path, cc_refresh_token="cc-rt-dead")
    live_oauth = {"accessToken": "live-at", "refreshToken": "live-rt",
                  "expiresAt": (int(time.time()) + 3600) * 1000,
                  "refreshTokenExpiresAt": RT_EXPIRES, **VERIFIED_MAX}
    with patch("jacked.web.auth._live_recovery_payload", return_value=live_oauth):
        result = asyncio.run(auth._cc_chain_died(aid, db, db.get_account(aid), "cc-rt-dead"))
    assert result.success and result.access_token == "live-at"
    row = db.get_account(aid)
    assert row["cc_refresh_token"] is None
    assert captured_tier(row["cc_oauth_extra"], None) == "max"

    oauth = build_oauth_data(row)
    assert oauth["accessToken"] == "live-at" and oauth["refreshToken"] is None
    assert "refreshTokenExpiresAt" not in oauth  # no refresh token beside it
    assert oauth["subscriptionTypeSource"] == "profile"
    assert oauth["subscriptionTypeVerifiedAt"] == VERIFIED_AT

    db.update_account(aid, subscription_type="pro")
    oauth = build_oauth_data(db.get_account(aid))
    assert oauth["subscriptionTypeSource"] == "profile"
    assert "subscriptionTypeVerifiedAt" not in oauth

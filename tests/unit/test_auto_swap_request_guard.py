"""The auto-swap request carries the refresh-downgrade check (#149i).

Every engine activation must refuse to put a null refresh token over a
live one of the same account. The auto-swap path builds its own
``SwitchRequest``; without the check, a target row whose cc chain is dead
would publish ``refreshToken: null`` over a working token that a session
of that account still holds.
"""

from __future__ import annotations

import time

from jacked.api.usage_monitor import _auto_swap_request
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.refresh_guard import REFRESH_DOWNGRADE_REASON


def _dead_chain_target():
    return {
        "id": 7,
        "email": "owner@test.com",
        "organization_uuid": "org-a",
        "access_token": "primary-at",
        "cc_access_token": "cc-at",
        "cc_refresh_token": None,
        "cc_expires_at": int(time.time()) + 3600,
        "cc_refresh_failure_type": "invalid_grant",
    }


def _authority(access, refresh):
    return CredentialPayload.from_mapping(
        {"_jackedAccountId": 7, "claudeAiOauth": {"accessToken": access, "refreshToken": refresh}}
    )


def test_the_request_refuses_a_null_refresh_token_over_a_live_one():
    request = _auto_swap_request(_dead_chain_target())

    assert request.refresh_downgrade_check is not None
    # The authority holds this account's own tokens (the access token is the
    # row's, so the owner is proven offline) with a working refresh token.
    assert request.refresh_downgrade_check(_authority("cc-at", "live-rt")) == REFRESH_DOWNGRADE_REASON


def test_the_request_allows_a_write_over_an_empty_or_foreign_store():
    request = _auto_swap_request(_dead_chain_target())

    assert request.refresh_downgrade_check(None) is None
    assert request.refresh_downgrade_check(_authority("cc-at", None)) is None


def test_a_healthy_target_is_never_refused():
    target = {**_dead_chain_target(), "cc_refresh_token": "cc-rt", "cc_refresh_failure_type": None}
    request = _auto_swap_request(target)

    assert request.refresh_downgrade_check(_authority("cc-at", "live-rt")) is None

"""The switch response body carries the refusal code (#149i).

`SwitchResult.code` names the stable reason behind a refusal
(OUTGOING_ACCOUNT_BUSY, CC_REAUTH_REQUIRED). The dashboard can act on the
code only when the body carries it; a committed result carries null.
"""

from __future__ import annotations

from jacked.api.routes.auth import _switch_content
from jacked.credentials.models import (
    CredentialIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchOutcome,
    SwitchResult,
)
from jacked.credentials.refresh_guard import CC_REAUTH_REQUIRED_CODE, REFRESH_DOWNGRADE_REASON


def _result(outcome, *, code="", message=""):
    return SwitchResult(
        operation_id="op-1",
        outcome=outcome,
        desired_default=IdentityAxis(3, "desired"),
        storage=IdentityAxis(None, outcome.value),
        committed_authority=IdentityAxis(None, "unchanged"),
        existing_session_activation=SessionActivationState.UNCHANGED,
        provider_verification=ProviderVerificationState.UNVERIFIED,
        observed_identity=CredentialIdentity(),
        message=message,
        code=code,
    )


def test_a_refusal_carries_its_code():
    body = _switch_content(
        _result(SwitchOutcome.UNUSABLE, code=CC_REAUTH_REQUIRED_CODE, message=REFRESH_DOWNGRADE_REASON),
        "owner@test.com",
    )
    assert body["status"] == "unusable"
    assert body["code"] == "CC_REAUTH_REQUIRED"
    assert body["message"] == REFRESH_DOWNGRADE_REASON


def test_a_result_without_a_code_carries_null():
    body = _switch_content(_result(SwitchOutcome.COMMITTED), "owner@test.com")
    assert body["code"] is None

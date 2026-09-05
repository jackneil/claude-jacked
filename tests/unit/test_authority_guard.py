"""Tests for repairing a foreign write to the credential authority."""

from __future__ import annotations

import pytest

from jacked.api import authority_guard
from jacked.api.authority_guard import (
    HealResult,
    heal_foreign_authority,
    reset_authority_guard_state,
)
from jacked.credentials.models import (
    CredentialIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchContext,
    SwitchOutcome,
    SwitchResult,
)


@pytest.fixture(autouse=True)
def _clean_guard_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


class FakeDatabase:
    def __init__(self, accounts: list[dict], settings: dict[str, str]) -> None:
        self.accounts = {int(row["id"]): dict(row) for row in accounts}
        self.settings = dict(settings)
        self.updates: list[tuple[int, dict]] = []

    def list_accounts(self, include_inactive: bool = False, include_deleted: bool = False):
        return [
            dict(row)
            for row in self.accounts.values()
            if (include_deleted or not row.get("is_deleted"))
            and (include_inactive or row.get("is_active"))
        ]

    def get_account(self, account_id: int):
        row = self.accounts.get(int(account_id))
        return dict(row) if row else None

    def update_account(self, account_id: int, **fields) -> bool:
        self.accounts[int(account_id)].update(fields)
        self.updates.append((int(account_id), dict(fields)))
        return True

    def get_setting(self, key: str):
        return self.settings.get(key)


def _account(account_id: int, email: str, **overrides) -> dict:
    row = {
        "id": account_id,
        "email": email,
        "provider": "claude",
        "organization_uuid": f"org-{account_id}",
        "is_active": 1,
        "is_deleted": 0,
        "validation_status": "valid",
        "cc_access_token": f"db_at_{account_id}",
        "cc_refresh_token": f"db_rt_{account_id}",
        "cc_expires_at": 1000,
        "refresh_failure_type": None,
        "refresh_last_failed_at": None,
    }
    row.update(overrides)
    return row


def _db(**settings) -> FakeDatabase:
    return FakeDatabase(
        [_account(3, "carol@test.com"), _account(11, "kim@test.com")],
        {"desired_account_id": "11", **settings},
    )


def _foreign_payload(access="live_at_3", refresh="live_rt_3", expires=1_800_000_000_000):
    return {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": expires,
            "refreshTokenExpiresAt": expires + 1000,
        }
    }


def _profile(email: str, organization_uuid: str):
    def lookup(access_token: str) -> dict:
        lookup.calls.append(access_token)
        return {
            "account": {"email": email},
            "organization": {"uuid": organization_uuid},
        }

    lookup.calls = []
    return lookup


def _switch_result(outcome=SwitchOutcome.OBSERVED_TARGET_UNFENCED, account_id=11):
    return SwitchResult(
        operation_id="reassert-test",
        outcome=outcome,
        desired_default=IdentityAxis(account_id, "desired"),
        storage=IdentityAxis(account_id, outcome.value),
        committed_authority=IdentityAxis(account_id, outcome.value),
        existing_session_activation=SessionActivationState.PENDING_NEXT_ACTIVITY,
        provider_verification=ProviderVerificationState.UNVERIFIED,
        observed_identity=CredentialIdentity(account_id),
    )


class _Activator:
    def __init__(self, outcome=SwitchOutcome.OBSERVED_TARGET_UNFENCED) -> None:
        self.outcome = outcome
        self.calls: list[tuple] = []

    def __call__(self, db, account, context, operation_id):
        self.calls.append((account, context, operation_id))
        return _switch_result(self.outcome, int(account["id"]))


def test_foreign_write_adopts_tokens_and_reasserts_desired():
    db = _db()
    lookup = _profile("carol@test.com", "org-3")
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result == HealResult(
        "reasserted", foreign_account_id=3, desired_account_id=11
    )
    assert db.accounts[3]["cc_access_token"] == "live_at_3"
    assert db.accounts[3]["cc_refresh_token"] == "live_rt_3"
    assert db.accounts[3]["cc_expires_at"] == 1_800_000_000
    assert db.accounts[11]["cc_access_token"] == "db_at_11", (
        "the desired row must never receive another account's tokens"
    )
    assert len(activate.calls) == 1
    account, context, operation_id = activate.calls[0]
    assert account["id"] == 11
    assert context is SwitchContext.REASSERT
    assert operation_id.startswith("reassert-")
    assert lookup.calls == ["live_at_3"]


def test_foreign_write_from_desired_account_is_restamped():
    db = _db()
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("kim@test.com", "org-11"),
        activate=activate,
        read_authority=lambda: _foreign_payload(access="live_at_11", refresh="live_rt_11"),
    )

    assert result.action == "adopted"
    assert result.foreign_account_id == result.desired_account_id == 11
    assert db.accounts[11]["cc_refresh_token"] == "live_rt_11"
    assert len(activate.calls) == 1


def test_unknown_account_is_never_overwritten():
    db = _db()
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("stranger@test.com", "org-99"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "unknown_account"
    assert db.updates == []
    assert activate.calls == []


def test_stamped_payload_is_a_jacked_write():
    db = _db()
    lookup = _profile("carol@test.com", "org-3")
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: {"_jackedAccountId": 3, **_foreign_payload()},
    )

    assert result.action == "none"
    assert result.foreign_account_id == 3
    assert lookup.calls == []
    assert db.updates == []
    assert activate.calls == []


def test_unchanged_foreign_payload_is_handled_once():
    db = _db()
    lookup = _profile("carol@test.com", "org-3")
    activate = _Activator()
    payload = _foreign_payload()

    first = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: payload,
    )
    second = heal_foreign_authority(
        db,
        now=110.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: payload,
    )

    assert first.action == "reasserted"
    assert second.action == "skipped"
    assert "already handled" in second.reason
    assert len(lookup.calls) == 1
    assert len(activate.calls) == 1


def test_second_reassert_within_the_rate_limit_is_skipped():
    db = _db()
    activate = _Activator()

    first = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(access="live_at_3a"),
    )
    second = heal_foreign_authority(
        db,
        now=140.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(access="live_at_3b"),
    )
    third = heal_foreign_authority(
        db,
        now=200.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(access="live_at_3c"),
    )

    assert first.action == "reasserted"
    assert second.action == "skipped"
    assert "rate limit" in second.reason
    assert third.action == "reasserted"
    assert len(activate.calls) == 2
    # The rate limit stops the reassert, never the token adoption.
    assert db.accounts[3]["cc_access_token"] == "live_at_3c"


def test_invalid_grant_keeps_the_refresh_token_out_of_the_database():
    db = _db()
    db.accounts[3]["refresh_failure_type"] = "invalid_grant"
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "reasserted"
    assert db.accounts[3]["cc_access_token"] == "live_at_3"
    assert db.accounts[3]["cc_refresh_token"] == "db_rt_3"
    assert db.accounts[3]["refresh_failure_type"] == "invalid_grant"


def test_a_live_rotation_clears_the_failure_breaker():
    db = _db()
    db.accounts[3]["refresh_failure_type"] = "server_error"
    db.accounts[3]["refresh_last_failed_at"] = 12345.0
    activate = _Activator()

    heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert db.accounts[3]["cc_refresh_token"] == "live_rt_3"
    assert db.accounts[3]["refresh_failure_type"] is None
    assert db.accounts[3]["refresh_last_failed_at"] is None


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"validation_status": "invalid"}, "invalid"),
        ({"cc_refresh_token": None}, "no Claude Code tokens"),
        ({"is_active": 0}, "disabled"),
        ({"is_deleted": 1}, "deleted"),
    ],
)
def test_an_unusable_desired_account_imports_without_reasserting(
    overrides, expected_reason
):
    db = _db()
    db.accounts[11].update(overrides)
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    # The desired account cannot be reasserted, so the guard re-stamps the
    # authority for the account that holds it: tokens unchanged, identity
    # usable, and the user sees the truth instead of "runtime unknown".
    assert result.action == "adopted"
    assert expected_reason in result.reason
    assert result.foreign_account_id == 3
    assert result.desired_account_id == 11
    assert db.accounts[3]["cc_access_token"] == "live_at_3"
    assert [call[0]["id"] for call in activate.calls] == [3]
    assert activate.calls[0][1] is SwitchContext.REASSERT


def test_an_unusable_desired_account_restamp_failure_is_a_skip():
    db = _db()
    db.accounts[11].update({"cc_refresh_token": None})
    activate = _Activator(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS)

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "skipped"
    assert "no Claude Code tokens" in result.reason
    assert db.accounts[3]["cc_access_token"] == "live_at_3"


def test_a_missing_desired_row_imports_without_reasserting():
    db = _db()
    db.settings["desired_account_id"] = "404"
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    # No desired row to reassert: the holder is re-stamped instead.
    assert result.action == "adopted"
    assert "missing" in result.reason
    assert db.accounts[3]["cc_access_token"] == "live_at_3"
    assert [call[0]["id"] for call in activate.calls] == [3]


def test_a_held_switch_lease_skips_the_reassert():
    db = _db()
    activate = _Activator(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS)

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "skipped"
    assert "switch lease" in result.reason


def test_the_active_pointer_is_used_when_no_desired_account_is_set():
    db = FakeDatabase(
        [_account(3, "carol@test.com"), _account(11, "kim@test.com")],
        {"active_account_id": "11"},
    )
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "reasserted"
    assert result.desired_account_id == 11


def test_a_failing_authority_read_never_raises():
    def explode():
        raise RuntimeError("keychain is locked")

    result = heal_foreign_authority(
        _db(),
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=_Activator(),
        read_authority=explode,
    )

    assert result.action == "skipped"
    assert "keychain is locked" in result.reason


def test_a_failed_profile_lookup_writes_nothing():
    db = _db()
    activate = _Activator()

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lambda token: None,
        activate=activate,
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "skipped"
    assert "profile" in result.reason
    assert db.updates == []
    assert activate.calls == []


def test_no_database_is_a_skipped_heal():
    assert heal_foreign_authority(None).action == "skipped"


def test_an_ambiguous_email_is_not_matched():
    db = FakeDatabase(
        [
            _account(3, "carol@test.com", organization_uuid=None),
            _account(4, "carol@test.com", organization_uuid=None),
            _account(11, "kim@test.com"),
        ],
        {"desired_account_id": "11"},
    )

    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=_profile("carol@test.com", "org-3"),
        activate=_Activator(),
        read_authority=lambda: _foreign_payload(),
    )

    assert result.action == "unknown_account"
    assert db.updates == []


def test_the_default_profile_lookup_reads_the_oauth_profile_endpoint():
    from jacked.web.oauth import PROFILE_URL

    captured = {}

    class _Response:
        status_code = 200

        @staticmethod
        def json():
            return {"account": {"email": "carol@test.com"}}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            captured["url"] = url
            captured["headers"] = headers
            return _Response()

    class _Httpx:
        @staticmethod
        def Client(timeout):
            captured["timeout"] = timeout
            return _Client()

    import sys

    saved = sys.modules.get("httpx")
    sys.modules["httpx"] = _Httpx
    try:
        profile = authority_guard._default_profile_lookup("tok")
    finally:
        if saved is None:
            del sys.modules["httpx"]
        else:
            sys.modules["httpx"] = saved

    assert profile == {"account": {"email": "carol@test.com"}}
    assert captured["url"] == PROFILE_URL
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["timeout"] == authority_guard.PROFILE_TIMEOUT_SECONDS


def test_a_rate_limited_repair_retries_without_a_second_profile_call():
    """A repair the rate limit held off is still owed. The next pass retries
    it from the cached identification, so no profile call is repeated."""
    db = _db()
    lookup = _profile("carol@test.com", "org-3")
    activate = _Activator()
    payload = _foreign_payload()

    heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: _foreign_payload(access="earlier"),
    )
    held = heal_foreign_authority(
        db,
        now=120.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: payload,
    )
    retried = heal_foreign_authority(
        db,
        now=200.0,
        profile_lookup=lookup,
        activate=activate,
        read_authority=lambda: payload,
    )

    assert held.action == "skipped"
    assert "rate limit" in held.reason
    assert retried.action == "reasserted"
    assert len(lookup.calls) == 2, "the retry must reuse the cached identification"
    assert len(activate.calls) == 2


def test_a_held_switch_lease_is_retried_on_the_next_pass():
    db = _db()
    lookup = _profile("carol@test.com", "org-3")
    busy = _Activator(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS)
    payload = _foreign_payload()

    first = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lookup,
        activate=busy,
        read_authority=lambda: payload,
    )
    free = _Activator()
    second = heal_foreign_authority(
        db,
        now=200.0,
        profile_lookup=lookup,
        activate=free,
        read_authority=lambda: payload,
    )

    assert first.action == "skipped"
    assert second.action == "reasserted"
    assert len(lookup.calls) == 1
    assert len(free.calls) == 1

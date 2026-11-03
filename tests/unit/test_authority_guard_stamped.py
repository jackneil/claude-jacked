"""A stamped rotation follows the account that ``oauthAccount`` names (#154i).

Claude Code keeps jacked's ``_jackedAccountId`` stamp when it rewrites the
credential authority. A ``/login`` to account 5 in one session, or a refresh
by a stale session that holds account 5, leaves an authority stamped for
account 1 that holds account 5's tokens. The guard adopts the tokens into
row 5 first. Then it reads ``~/.claude.json``:

- ``oauthAccount`` names account 5: the user chose it. Account 5 becomes the
  desired and the active account, and the authority is re-stamped for it.
- ``oauthAccount`` still names account 1: a stale session wrote the payload.
  Account 1 is reasserted.
- Anything else: the guard only adopts the tokens, as before.

Each test runs in its own temporary HOME (tests/conftest.py), so
``Path.home() / ".claude.json"`` is this test's file. The fingerprint of
jacked's own writes and the re-stamp token check are in
tests/unit/test_authority_guard_publication.py.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from jacked.api import authority_guard
from jacked.api.authority_guard import (
    REASSERT_MIN_INTERVAL_SECONDS,
    RESTAMP_MAX_ATTEMPTS,
    HealResult,
    ProfileLookup,
    heal_foreign_authority,
    reset_authority_guard_state,
)
from jacked.credentials.models import CredentialIdentity, SwitchContext, SwitchOutcome
from tests.unit.test_authority_guard import (
    FakeDatabase,
    _account,
    _foreign_payload,
    _profile,
    _switch_result,
)

GUARD = "jacked.api.authority_guard"
STAMPED = "jacked.api.stamped_rotation"
FP_SETTING = "_last_authority_write_fp"
OWNER_CONFIG = {
    "accountUuid": "uuid-5",
    "emailAddress": "erin@test.com",
    "organizationUuid": "org-5",
}
STAMP_CONFIG = {
    "accountUuid": "uuid-1",
    "emailAddress": "alice@test.com",
    "organizationUuid": "org-1",
}
ADOPTION_LINE = (
    "Account 5 adopted as the active account after a stamped rotation "
    "(oauthAccount names it)"
)
RESTORE_LINE = (
    "Account 1 restored after a stale-session rotation stamped for it "
    "(oauthAccount still names it)"
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


class _Db(FakeDatabase):
    """The guard's fake database, plus the setting writes a switch uses."""

    def set_setting(self, key: str, value: str) -> None:
        self.settings[key] = value

    def set_settings(self, values: dict) -> None:
        for key, value in values.items():
            if value is None:
                self.settings.pop(key, None)
            else:
                self.settings[key] = value


class _Sequence:
    """An engine that returns the given outcomes in order; the last repeats."""

    def __init__(self, *outcomes: SwitchOutcome) -> None:
        self.outcomes = list(outcomes) or [SwitchOutcome.COMMITTED]
        self.calls: list[tuple] = []
        self.republished: list[tuple] = []

    def __call__(self, db, account, context, operation_id, **repair):
        self.calls.append((account, context, operation_id))
        self.republished.append((repair.get("republish"), repair.get("expected_digest")))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        return _switch_result(outcome, int(account["id"]))


def _db(one: dict | None = None, five: dict | None = None) -> _Db:
    """Account 1 is stamped, desired and active. Account 5 owns the payload."""
    return _Db(
        [
            _account(1, "alice@test.com", **{"account_uuid": "uuid-1", **(one or {})}),
            _account(5, "erin@test.com", **{"account_uuid": "uuid-5", **(five or {})}),
        ],
        {"desired_account_id": "1", "active_account_id": "1"},
    )


def _payload(access: str = "live_at_5", refresh: str = "live_rt_5", stamp: int = 1):
    return {"_jackedAccountId": stamp, **_foreign_payload(access=access, refresh=refresh)}


def _config(block) -> None:
    """Write this test's ``~/.claude.json``; a str is written as raw text."""
    text = block if isinstance(block, str) else json.dumps({"oauthAccount": block})
    (Path.home() / ".claude.json").write_text(text, encoding="utf-8")


def _heal(db, activate, now: float, payload: dict | None = None, lookup=None):
    payload = payload or _payload()
    return heal_foreign_authority(
        db,
        now=now,
        profile_lookup=lookup or _profile("erin@test.com", "org-5"),
        activate=activate,
        read_authority=lambda: payload,
    )


@contextmanager
def _logs(caplog, level: int = logging.INFO):
    with caplog.at_level(level, logger=GUARD), caplog.at_level(level, logger=STAMPED):
        yield


def _messages(caplog, needle: str, level: int | None = None) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if needle in r.getMessage() and (level is None or r.levelno == level)
    ]


def _pointers(db) -> tuple[str, str]:
    return db.settings.get("desired_account_id"), db.settings.get("active_account_id")


def _assert_values_free(caplog) -> None:
    assert "@" not in caplog.text, "a log line names an email"
    for secret in ("live_at_", "live_rt_", "db_at_", "db_rt_", "old_at_", "old_rt_"):
        assert secret not in caplog.text, f"a log line carries a token ({secret})"


# --- (1) oauthAccount names the owner: the user chose it --------------------


def test_a_login_to_the_owner_makes_it_active_and_restamps_for_it(caplog):
    db = _db()
    _config(OWNER_CONFIG)
    lookup = _profile("erin@test.com", "org-5")
    activate = _Sequence(SwitchOutcome.COMMITTED)

    with _logs(caplog):
        first = _heal(db, activate, now=100.0, lookup=lookup)
        again = _heal(db, activate, now=161.0, lookup=lookup)

    assert first == HealResult("adopted", foreign_account_id=5, desired_account_id=5)
    assert again == HealResult("none", foreign_account_id=1)
    assert (db.accounts[5]["cc_access_token"], db.accounts[5]["cc_refresh_token"]) == (
        "live_at_5",
        "live_rt_5",
    )
    assert (db.accounts[1]["cc_access_token"], db.accounts[1]["cc_refresh_token"]) == (
        "db_at_1",
        "db_rt_1",
    )
    assert _pointers(db) == ("5", "5")
    # The re-stamp publishes row 5; account 1 is never reasserted.
    assert [(call[0]["id"], call[1]) for call in activate.calls] == [
        (5, SwitchContext.REASSERT)
    ]
    assert activate.calls[0][2].startswith("restamp-")
    assert lookup.calls == ["live_at_5"]
    assert _messages(caplog, "adopted as the active account") == [ADOPTION_LINE]
    _assert_values_free(caplog)


def test_a_raise_in_the_follow_keeps_the_repair_owed(caplog):
    """The tokens are adopted, then a read raises. The next pass must still
    follow the block, with no second profile request."""
    db = _db()
    _config(OWNER_CONFIG)
    lookup = _profile("erin@test.com", "org-5")
    activate = _Sequence(SwitchOutcome.COMMITTED)

    with (
        mock.patch(f"{STAMPED}.read_config_identity", side_effect=OSError("disk")),
        _logs(caplog),
    ):
        first = _heal(db, activate, now=100.0, lookup=lookup)

    assert first.action == "skipped"
    assert db.accounts[5]["cc_refresh_token"] == "live_rt_5"
    assert activate.calls == []
    assert _pointers(db) == ("1", "1")

    with _logs(caplog):
        second = _heal(db, activate, now=161.0, lookup=lookup)

    assert second == HealResult("adopted", foreign_account_id=5, desired_account_id=5)
    assert _pointers(db) == ("5", "5")
    assert lookup.calls == ["live_at_5"]
    _assert_values_free(caplog)


def test_an_owner_row_without_a_uuid_is_named_by_its_email_and_org():
    db = _db(five={"account_uuid": None})
    _config({"emailAddress": "Erin@Test.COM", "organizationUuid": "org-5"})
    activate = _Sequence()

    result = _heal(db, activate, now=100.0)

    assert result == HealResult("adopted", foreign_account_id=5, desired_account_id=5)
    assert _pointers(db) == ("5", "5")
    assert [call[0]["id"] for call in activate.calls] == [5]


def test_one_person_in_two_organizations_is_told_apart_by_the_org():
    """Rows of one person in two organizations share the account uuid, so
    the uuid alone names both rows. The organization uuid decides."""
    db = _Db(
        [
            _account(1, "erin@test.com", account_uuid="uuid-erin"),
            _account(5, "erin@test.com", account_uuid="uuid-erin"),
        ],
        {"desired_account_id": "1", "active_account_id": "1"},
    )
    _config({**OWNER_CONFIG, "accountUuid": "uuid-erin"})
    activate = _Sequence()

    result = _heal(db, activate, now=100.0)

    assert result == HealResult("adopted", foreign_account_id=5, desired_account_id=5)
    assert [call[0]["id"] for call in activate.calls] == [5]


def test_a_floored_login_adoption_moves_the_pointers_and_restamps_after_the_floor(
    caplog,
):
    db = _db()
    _config(OWNER_CONFIG)
    activate = _Sequence()
    lookup = _profile("erin@test.com", "org-5")
    assert authority_guard._claim_repair_slot("e" * 64, 3, 90.0)  # an earlier repair

    with _logs(caplog):
        floored = _heal(db, activate, now=100.0, lookup=lookup)
        assert _pointers(db) == ("5", "5"), "the pointers follow the user at once"
        assert activate.calls == []
        retried = _heal(db, activate, now=151.0, lookup=lookup)

    assert floored == HealResult(
        "skipped",
        foreign_account_id=5,
        desired_account_id=5,
        reason="reassert rate limit is active",
    )
    assert retried == HealResult("adopted", foreign_account_id=5, desired_account_id=5)
    assert [call[0]["id"] for call in activate.calls] == [5]
    assert lookup.calls == ["live_at_5"], "the retry reuses the identification"
    assert _messages(caplog, "adopted as the active account") == [ADOPTION_LINE]


def test_a_failing_restamp_for_the_owner_settles_after_the_attempt_cap(caplog):
    db = _db()
    _config(OWNER_CONFIG)
    activate = _Sequence(SwitchOutcome.FAILED_PRESERVED)
    times = [100.0 + 61.0 * index for index in range(RESTAMP_MAX_ATTEMPTS + 1)]

    with _logs(caplog):
        results = [_heal(db, activate, now=now) for now in times]

    assert len(activate.calls) == RESTAMP_MAX_ATTEMPTS
    assert [r.reason for r in results[:-1]] == ["re-stamp outcome failed_preserved"] * 3
    assert results[-1] == HealResult("none", foreign_account_id=1)
    assert len(_messages(caplog, "did not commit after 3 attempts", logging.WARNING)) == 1
    assert FP_SETTING not in db.settings, "only a committed repair records a fingerprint"
    _assert_values_free(caplog)


def test_an_owed_repair_whose_owner_row_disappeared_is_settled():
    db = _db()
    _config(OWNER_CONFIG)
    activate = _Sequence(SwitchOutcome.BUSY)
    assert _heal(db, activate, now=100.0).reason == "re-stamp outcome busy"
    del db.accounts[5]

    gone = _heal(db, activate, now=161.0)
    after = _heal(db, activate, now=222.0)

    assert gone == HealResult(
        "skipped", foreign_account_id=1, reason="identified account row disappeared"
    )
    assert after == HealResult("none", foreign_account_id=1)
    assert len(activate.calls) == 1


# --- (2) oauthAccount still names the stamped account: a stale session ------


def test_a_stale_session_rotation_restores_the_stamped_account(caplog):
    db = _db()
    _config(STAMP_CONFIG)
    lookup = _profile("erin@test.com", "org-5")
    activate = _Sequence(SwitchOutcome.COMMITTED)

    with _logs(caplog):
        first = _heal(db, activate, now=100.0, lookup=lookup)
        early = _heal(db, activate, now=130.0, lookup=lookup)
        later = _heal(db, activate, now=161.0, lookup=lookup)

    assert first == HealResult("reasserted", foreign_account_id=5, desired_account_id=1)
    assert early == later == HealResult("none", foreign_account_id=1)
    # Account 5's lineage is saved before account 1 is reasserted.
    assert db.accounts[5]["cc_refresh_token"] == "live_rt_5"
    assert [(call[0]["id"], call[1]) for call in activate.calls] == [
        (1, SwitchContext.REASSERT)
    ]
    assert activate.calls[0][2].startswith("reassert-")
    assert _pointers(db) == ("1", "1")
    assert lookup.calls == ["live_at_5"]
    assert _messages(caplog, "restored after a stale-session rotation") == [RESTORE_LINE]
    assert _messages(caplog, "adopted as the active account") == []
    _assert_values_free(caplog)


def test_a_stale_restore_is_retried_once_a_minute():
    db = _db()
    _config(STAMP_CONFIG)
    lookup = _profile("erin@test.com", "org-5")
    activate = _Sequence(SwitchOutcome.BUSY, SwitchOutcome.COMMITTED)

    first = _heal(db, activate, now=100.0, lookup=lookup)
    held = _heal(db, activate, now=130.0, lookup=lookup)
    assert len(activate.calls) == 1
    retried = _heal(
        db, activate, now=100.0 + REASSERT_MIN_INTERVAL_SECONDS + 1, lookup=lookup
    )

    assert first == HealResult(
        "skipped", foreign_account_id=5, desired_account_id=1, reason="reassert outcome busy"
    )
    assert held == HealResult(
        "skipped",
        foreign_account_id=5,
        desired_account_id=1,
        reason="reassert rate limit is active",
    )
    assert retried == HealResult("reasserted", foreign_account_id=5, desired_account_id=1)
    assert [call[0]["id"] for call in activate.calls] == [1, 1]
    assert lookup.calls == ["live_at_5"], "the retries reuse the identification"


def test_a_held_switch_lease_holds_the_stale_restore():
    db = _db()
    _config(STAMP_CONFIG)

    result = _heal(db, _Sequence(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS), now=100.0)

    assert result == HealResult(
        "skipped",
        foreign_account_id=5,
        desired_account_id=1,
        reason="another credential operation holds the switch lease",
    )


@pytest.mark.parametrize(
    ("db_factory", "why"),
    [
        (lambda: _db(one={"cc_refresh_token": None}), "has no Claude Code tokens"),
        (lambda: _db_with(desired="9"), "is not the stamped account"),
    ],
    ids=["stamped-account-unusable", "another-desired-account"],
)
def test_a_stale_rotation_that_cannot_restore_the_stamped_account_only_adopts(
    db_factory, why, caplog
):
    db = db_factory()
    _config(STAMP_CONFIG)
    activate = _Sequence()

    with _logs(caplog):
        result = _heal(db, activate, now=100.0)
        again = _heal(db, activate, now=161.0)

    assert result == HealResult("rotation_adopted", foreign_account_id=5)
    assert again == HealResult("none", foreign_account_id=1)
    assert activate.calls == []
    lines = _messages(caplog, "only adopts the tokens", logging.INFO)
    assert len(lines) == 1 and why in lines[0]
    assert "cannot restore account 1" in lines[0]
    _assert_values_free(caplog)


def _db_with(desired: str) -> _Db:
    db = _db()
    db.settings["desired_account_id"] = desired
    return db


# --- (3) oauthAccount cannot decide: adopt only ------------------------------


@pytest.mark.parametrize(
    ("config", "why"),
    [
        (None, "missing or unreadable"),
        ("{not json", "missing or unreadable"),
        ({"accountUuid": "uuid-9", "emailAddress": "zed@test.com"}, "names neither"),
        ({"emailAddress": "erin@test.com", "organizationUuid": "org-9"}, "names neither"),
    ],
    ids=["no-file", "not-json", "third-account", "owner-email-other-org"],
)
def test_an_undecidable_config_only_adopts(config, why, caplog):
    db = _db(five={"account_uuid": None})
    if config is not None:
        _config(config)
    activate = _Sequence()

    with _logs(caplog):
        first = _heal(db, activate, now=100.0)
        again = _heal(db, activate, now=161.0)

    assert first == HealResult("rotation_adopted", foreign_account_id=5)
    assert again == HealResult("none", foreign_account_id=1)
    assert activate.calls == []
    assert db.accounts[5]["cc_refresh_token"] == "live_rt_5"
    assert _pointers(db) == ("1", "1")
    lines = _messages(caplog, "cannot classify", logging.INFO)
    assert len(lines) == 1 and why in lines[0]
    _assert_values_free(caplog)


def test_a_config_that_names_both_accounts_only_adopts(caplog):
    db = _Db(
        [
            _account(1, "erin@test.com", account_uuid="uuid-erin"),
            _account(5, "erin@test.com", account_uuid="uuid-erin"),
        ],
        {"desired_account_id": "1", "active_account_id": "1"},
    )
    _config({"accountUuid": "uuid-erin", "emailAddress": "erin@test.com"})
    activate = _Sequence()

    with _logs(caplog):
        result = _heal(db, activate, now=100.0)

    assert result == HealResult("rotation_adopted", foreign_account_id=5)
    assert activate.calls == []
    assert any("names both" in line for line in _messages(caplog, "cannot classify"))


# --- (identity watcher) the mismatch clears once the stamp follows ----------


def test_the_watcher_mismatch_clears_once_the_authority_is_restamped(
    tmp_path, monkeypatch, caplog
):
    """While the authority is stamped for 7 and holds 5's token, the watcher
    reports a stamp mismatch. After the guard re-stamps the authority for 5
    (and 5 is the desired account), no snapshot may carry that mismatch."""
    from jacked.api.identity_watcher import AuthorityView
    from tests.unit.test_identity_watcher import NOW, TOKEN, Harness, _row
    from tests.unit.test_identity_watcher import _config as _watcher_config
    from tests.unit.test_identity_watcher import _profile as _watcher_profile

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    kim = {
        "email": "kim@example.com",
        "org": "9e8d7c6b-0000-4000-8000-000000000005",
        "account_uuid": "5a5a5a5a-0000-4000-8000-000000000005",
    }
    harness = Harness(
        tmp_path,
        rows=[
            _row(7),
            _row(
                5,
                email=kim["email"],
                organization_uuid=kim["org"],
                account_uuid=kim["account_uuid"],
                profile_json=None,
            ),
        ],
        config=_watcher_config(
            emailAddress=kim["email"],
            organizationUuid=kim["org"],
            accountUuid=kim["account_uuid"],
        ),
    )
    harness.lookup_result = ProfileLookup(200, _watcher_profile(**kim))

    with caplog.at_level(logging.WARNING, logger="jacked.api.identity_watcher"):
        assert "authority:stamp-mismatch" in harness.run()
        harness.views = [AuthorityView(5, "digest-2", TOKEN, (NOW + 3600) * 1000)]
        harness.block = SimpleNamespace(
            fields={}, complete=True, account_id=5, email=kim["email"],
            organization_uuid=kim["org"], account_uuid=kim["account_uuid"],
        )
        five = CredentialIdentity(5, kim["email"], kim["org"])
        assert "authority:stamp-mismatch" not in harness.run(observed=five, desired=five)
        assert "authority:stamp-mismatch" not in harness.watcher.drain_evidence()

    assert len(_messages(caplog, "is stamped for account 7", logging.WARNING)) == 1
    assert harness.writes == []

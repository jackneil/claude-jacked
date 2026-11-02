"""What the authority guard publishes, and what it knows it published (#154i).

Two rules keep a repair from damaging the authority:

- A re-stamp republishes the row's own tokens, so it may add only the stamp.
  When the row would publish other tokens than the authority holds (for
  example, the Claude Code breaker kept a rotated refresh token out of the
  row), the re-stamp does not run, on the stamped and the unstamped path.
- After a committed repair, the guard keeps a fingerprint of the tokens
  that it published (``last_authority_write_fp``). A payload with these
  tokens is jacked's own write, so the guard does not examine it again.

The fixtures come from tests/unit/test_authority_guard_stamped.py.
"""

from __future__ import annotations

import logging

import pytest

from jacked.api import authority_guard
from jacked.api.authority_guard import (
    HealResult,
    heal_foreign_authority,
    reset_authority_guard_state,
)
from jacked.api.stamped_evidence import token_fingerprint
from jacked.web.refresh_verdict import token_fingerprint as verdict_fingerprint
from tests.unit.test_authority_guard import _db as _guard_db
from tests.unit.test_authority_guard import _foreign_payload, _profile
from tests.unit.test_authority_guard_stamped import (
    FP_SETTING,
    GUARD,
    OWNER_CONFIG,
    STAMP_CONFIG,
    _assert_values_free,
    _config,
    _db,
    _heal,
    _logs,
    _messages,
    _payload,
    _pointers,
    _Sequence,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


# --- jacked's own publication is not examined again --------------------------


def test_jackeds_own_publication_is_never_processed_again():
    """jacked re-stamped account 1 with tokens that row 1 no longer holds
    (for example, jacked refreshed the row afterwards). Identifying that
    payload again would adopt the superseded tokens back into the row."""
    payload = _payload(access="old_at_1", refresh="old_rt_1")
    lookup = _profile("alice@test.com", "org-1")

    control = _db()
    _heal(control, _Sequence(), now=100.0, payload=payload, lookup=lookup)
    assert lookup.calls == ["old_at_1"], "without the fingerprint the payload is examined"

    reset_authority_guard_state()
    lookup.calls.clear()
    db = _db()
    db.settings[FP_SETTING] = token_fingerprint(payload["claudeAiOauth"])
    activate = _Sequence()
    result = _heal(db, activate, now=200.0, payload=payload, lookup=lookup)

    assert result == HealResult("none", foreign_account_id=1)
    assert lookup.calls == []
    assert activate.calls == []
    assert db.updates == []


@pytest.mark.parametrize(
    ("config", "published"),
    [(OWNER_CONFIG, 5), (STAMP_CONFIG, 1)],
    ids=["restamp", "reassert"],
)
def test_a_committed_repair_records_what_it_published(config, published):
    from jacked.api.credential_helpers import build_oauth_data

    db = _db()
    _config(config)

    _heal(db, _Sequence(), now=100.0)

    expected = token_fingerprint(build_oauth_data(db.get_account(published)))
    assert db.settings[FP_SETTING] == expected
    assert len(expected) == 16
    assert "live_" not in expected and "db_" not in expected


def test_the_token_fingerprint_is_values_free_and_ordered():
    one = token_fingerprint({"accessToken": "a", "refreshToken": "b"})
    assert one == token_fingerprint({"accessToken": "a", "refreshToken": "b"})
    assert one != token_fingerprint({"accessToken": "b", "refreshToken": "a"})
    assert one != token_fingerprint({"accessToken": "ab", "refreshToken": ""})
    assert len(one) == 16 and set(one) <= set("0123456789abcdef")


# --- a re-stamp may add only the stamp ----------------------------------------


def test_a_restamp_that_would_drop_the_live_refresh_token_never_runs(caplog):
    """Row 5's Claude Code chain is dead and the authority still holds the
    token that died (its fingerprint condemns it), so the adoption keeps it
    out of the row, and a re-stamp would publish row 5 WITHOUT the refresh
    token the authority holds. A re-stamp may only add the stamp."""
    db = _db(five={
        "cc_refresh_failure_type": "invalid_grant",
        "cc_refresh_failed_token_fp": verdict_fingerprint("live_rt_5"),
    })
    _config(OWNER_CONFIG)
    activate = _Sequence()

    with _logs(caplog):
        first = _heal(db, activate, now=100.0)
        again = _heal(db, activate, now=161.0)

    assert activate.calls == []
    assert first.action == "skipped"
    assert "refreshToken" in first.reason
    assert again == HealResult("none", foreign_account_id=1)
    assert db.accounts[5]["cc_access_token"] == "live_at_5"
    assert db.accounts[5]["cc_refresh_token"] == "db_rt_5"
    assert _pointers(db) == ("5", "5"), "the pointers still follow the user"
    assert len(_messages(caplog, "cannot re-stamp", logging.WARNING)) == 1
    _assert_values_free(caplog)


def test_an_unstamped_restamp_that_would_drop_the_live_refresh_token_never_runs(caplog):
    """The desired account 11 cannot be reasserted, so the guard re-stamps the
    holder 3. Row 3's chain is dead and the authority holds the token that
    died (condemned), so the adoption keeps it out of the row, and that
    re-stamp would strip it from the authority."""
    db = _guard_db()
    db.accounts[11]["cc_refresh_token"] = None
    db.accounts[3]["cc_refresh_failure_type"] = "invalid_grant"
    db.accounts[3]["cc_refresh_failed_token_fp"] = verdict_fingerprint("live_rt_3")
    activate = _Sequence()

    def heal(now):
        return heal_foreign_authority(
            db,
            now=now,
            profile_lookup=_profile("carol@test.com", "org-3"),
            activate=activate,
            read_authority=lambda: _foreign_payload(),
        )

    with caplog.at_level(logging.WARNING, logger=GUARD):
        result = heal(100.0)
        again = heal(161.0)

    assert activate.calls == []
    assert result.action == "skipped"
    assert again.reason == "foreign payload already handled"
    assert authority_guard._last_reassert_at == 0.0, "the floor was not spent"
    assert any(
        "account 3 cannot republish the refreshToken" in m
        for m in _messages(caplog, "cannot be reasserted", logging.WARNING)
    )
    _assert_values_free(caplog)

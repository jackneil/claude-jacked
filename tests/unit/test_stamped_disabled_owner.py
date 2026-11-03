"""A ``/login`` to an account jacked has disabled is not followed (#154i).

The tokens are adopted into the owner's row either way. The pointers move
only to an account the user can use in jacked: a disabled or deleted owner
is reported with one WARNING and the guard adopts only.
"""

from __future__ import annotations

import logging

import pytest

from jacked.api.authority_guard import reset_authority_guard_state
from jacked.credentials.models import SwitchOutcome
from tests.unit.test_authority_guard_stamped import (
    OWNER_CONFIG,
    _config,
    _db,
    _heal,
    _pointers,
    _profile,
    _Sequence,
)

LOGGER = "jacked.api.stamped_rotation"


@pytest.fixture(autouse=True)
def _clean_guard_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


def test_a_disabled_owner_is_adopted_but_not_followed(caplog):
    """A deleted row is never matched as an owner at all (the unstamped and
    stamped identification both skip deleted rows), so the rule to pin is
    the disabled one: the tokens are adopted, the pointers stay."""
    db = _db(five={"is_active": 0})
    _config(OWNER_CONFIG)
    activate = _Sequence(SwitchOutcome.COMMITTED)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = _heal(db, activate, now=100.0, lookup=_profile("erin@test.com", "org-5"))

    assert result.action == "rotation_adopted"
    assert activate.calls == []
    assert _pointers(db) == ("1", "1")
    lines = [r.getMessage() for r in caplog.records if "disabled or deleted in jacked" in r.getMessage()]
    assert len(lines) == 1 and "account 5" in lines[0]
    assert "erin@test.com" not in caplog.text


def test_an_enabled_owner_is_still_followed():
    db = _db()
    _config(OWNER_CONFIG)
    activate = _Sequence(SwitchOutcome.COMMITTED)

    result = _heal(db, activate, now=100.0, lookup=_profile("erin@test.com", "org-5"))

    assert result.action == "adopted"
    assert _pointers(db) == ("5", "5")

"""A ``/login`` adoption arms the auto-swap cooldown like a manual switch (#154i).

When the guard follows ``oauthAccount`` to the account the user chose, it
moves the desired and active pointers. A manual switch also arms the
auto-swap cooldown and marks the open sessions pending, so the sweep does
not move off the chosen account within minutes and sessions learn that the
account changed under them. The adoption does the same.
"""

from __future__ import annotations

from unittest import mock

from jacked.api import usage_monitor
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


def test_a_login_adoption_arms_the_cooldown_and_marks_sessions_pending():
    db = _db()
    _config(OWNER_CONFIG)
    activate = _Sequence(SwitchOutcome.COMMITTED)

    with mock.patch.object(usage_monitor, "note_external_swap") as armed:
        result = _heal(db, activate, now=100.0, lookup=_profile("erin@test.com", "org-5"))

    assert result.action == "adopted"
    assert _pointers(db) == ("5", "5")
    armed.assert_called_once_with()
    assert db.pending_marks == 1


def test_an_adoption_that_moves_no_pointer_arms_nothing():
    """The pointers already name the owner (a restart after the adoption):
    no second cooldown, no second pending mark."""
    db = _db()
    db.settings.update({"desired_account_id": "5", "active_account_id": "5"})
    _config(OWNER_CONFIG)
    activate = _Sequence(SwitchOutcome.COMMITTED)

    with mock.patch.object(usage_monitor, "note_external_swap") as armed:
        _heal(db, activate, now=100.0, lookup=_profile("erin@test.com", "org-5"))

    armed.assert_not_called()
    assert getattr(db, "pending_marks", 0) == 0

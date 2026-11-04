"""Unknown credential holders are named as such, once (#149i).

When a tracked session has no account id, ``live_holder_account_ids``
returns None and every Claude Code refresh token exchange is refused. The
reason ``cc_rotation_block`` reports says so (``holders_unknown``, not a
store read that did not fail), and one INFO line names the cause until the
holders are known again.
"""

from __future__ import annotations

import logging
from unittest import mock

from jacked.api import credential_helpers as ch

LOGGER = "jacked.api.credential_helpers"


class _Db:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_accounts(self, include_inactive=False):
        return []

    def get_active_sessions(self, staleness_minutes=60):
        return self.sessions

    def get_stale_open_sessions(self, staleness_minutes=60):
        return []


def test_the_block_reason_names_unknown_holders():
    with (
        mock.patch.object(ch, "read_active_account_id", return_value=2),
        mock.patch.object(ch, "live_holder_account_ids", return_value=None),
    ):
        assert ch.cc_rotation_block(5, db=None) == "holders_unknown"


def test_the_cause_is_logged_once_until_the_holders_are_known(caplog):
    ch._note_holders_known()
    unknown = _Db([{"account_id": 9}, {"account_id": None}])
    known = _Db([{"account_id": 9}])

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert ch.live_holder_account_ids(unknown, {}) is None
        assert ch.live_holder_account_ids(unknown, {}) is None
        assert ch.live_holder_account_ids(known, {}) == {9}
        assert ch.live_holder_account_ids(unknown, {}) is None

    lines = [r for r in caplog.records if "Credential holders unknown" in r.getMessage()]
    assert len(lines) == 2  # once per run of unknown holders
    assert all(r.levelno == logging.INFO for r in lines)

"""The launch replaces a per-account file only on a final refusal (#158i).

A 401 or a 403 from the profile endpoint names no owner, and an edge or a
proxy can give it. The launch must not write the row's spent pair over the
file's only live rotation on that answer. A deferred sync over the row's own
pair is not a rotation to keep. An expired older pair does not outrank a row
whose refresh token still works. The replace WARNING is for a refusal only.
The downgrade guard keeps a pair nobody can prove, because the directory
names the row, unless an earlier session proved another owner.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from unittest import mock

import pytest

from jacked import launch
from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import clear_ownership_cache
from tests.unit.launch_sync_helpers import (
    INTRUDER,
    _account_dir,
    _cc,
    _file_pair,
    _Lookup,
    _make_row,
    _next_launch,
    _patched,
    _profile,
    _sync,
    _write_credentials,
)

LOGGER = "jacked.launch"
KEPT = "launch:scoped-file:unchecked-rotation-kept"
REPLACING = "replacing the per-account file's token pair"


@pytest.fixture(autouse=True)
def _fresh_state():
    clear_ownership_cache()
    launch._deferral_warned.clear()
    yield
    clear_ownership_cache()
    launch._deferral_warned.clear()


def _ago_ms(seconds: int) -> int:
    return (int(time.time()) - seconds) * 1000


def _intruder():
    return _profile(INTRUDER, org="org-x", uuid="uuid-x")


def _evidence(config_dir) -> tuple:
    snapshot = json.loads((config_dir / "jacked-resolver-snapshot.json").read_text())
    return tuple(snapshot.get("evidence") or ())


@contextlib.contextmanager
def _launch_env(lookup, refresh=None, *, due=False):
    """A file-authority platform, with the pre-launch refresh due when ``due``."""
    with (
        _patched(lookup),
        mock.patch("jacked.launch.scoped_launch_needs_global_activation", return_value=False),
        mock.patch("jacked.launch.should_refresh_cc", return_value=due),
        mock.patch("jacked.api.credential_helpers.cc_rotation_block", return_value=None),
        mock.patch("jacked.web.auth.refresh_cc_token", refresh or mock.AsyncMock()),
    ):
        yield


# --- a deferred sync keeps only a pair the row does not hold -----------------


def test_a_deferred_sync_over_the_rows_own_pair_writes_the_refreshed_pair(tmp_path):
    """A locked database defers the sync, and the file holds the row's own
    pair. The refresh runs and rotates the row, so the file must get the new
    pair: its old refresh token is spent."""
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "row-at-1", "row-rt-1")

    async def rotate(account_id, _db):
        db.update_account(account_id, cc_access_token="new-at", cc_refresh_token="new-rt")

    refresh = mock.AsyncMock(side_effect=rotate)
    with (
        _launch_env(_Lookup(ProfileLookup(503)), refresh, due=True),
        mock.patch("jacked.launch._sync_tokens_from_file", return_value="deferred"),
    ):
        _next_launch(db, tmp_path)

    refresh.assert_awaited_once()
    assert _file_pair(config_dir) == ("new-at", "new-rt")
    assert KEPT not in _evidence(config_dir)


# --- an expired older pair does not outrank a known-dead row ------------------


def test_an_expired_older_pair_stays_older_for_a_known_dead_row(tmp_path):
    """auth_rejected condemns only the row's access token. The file's older
    pair is expired, so it works for nobody, and it is not a rotation."""
    db = _make_row(tmp_path, failure="auth_rejected")
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "old-at", "old-rt", expires_ms=_ago_ms(60))
    lookup = _Lookup(_profile("owner@test.com"))

    with _launch_env(lookup):
        assert _sync(config_dir, tmp_path) == "unchanged"
        _next_launch(db, tmp_path)

    assert lookup.calls == 0
    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    assert KEPT not in _evidence(config_dir)


def test_a_known_dead_row_refreshes_past_an_expired_older_pair(tmp_path):
    db = _make_row(tmp_path, failure="auth_rejected", cc_expires=int(time.time()) - 60)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "old-at", "old-rt", expires_ms=_ago_ms(120))
    refresh = mock.AsyncMock()

    with _launch_env(_Lookup(_profile("owner@test.com")), refresh, due=True):
        _next_launch(db, tmp_path)

    refresh.assert_awaited_once()


def test_an_unexpired_older_pair_still_replaces_a_known_dead_row(tmp_path):
    """#179i: the owned token that merely expires sooner is the one that works."""
    db = _make_row(tmp_path, failure="auth_rejected", cc_expires=int(time.time()) + 7200)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "new-at", "new-rt", expires_ms=(int(time.time()) + 600) * 1000)

    with _launch_env(_Lookup(_profile("owner@test.com"))):
        assert _sync(config_dir, tmp_path) == "imported"

    assert _cc(db)[:2] == ("new-at", "new-rt")


# --- a 401 or a 403 names no owner --------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_a_status_refusal_keeps_the_file_and_skips_the_refresh(tmp_path, caplog, status):
    db = _make_row(tmp_path, cc_expires=int(time.time()) - 120)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")
    refresh = mock.AsyncMock()

    with (
        _launch_env(_Lookup(ProfileLookup(status)), refresh, due=True),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        _next_launch(db, tmp_path)

    refresh.assert_not_called()
    assert _file_pair(config_dir) == ("rotated-at", "rotated-rt")
    assert _cc(db)[:2] == ("row-at-1", "row-rt-1")
    assert KEPT in _evidence(config_dir)
    assert "keeping them for this launch" in caplog.text
    assert "without naming an owner" in caplog.text
    assert REPLACING not in caplog.text
    assert "rotated-rt" not in caplog.text
    assert "rotated-at" not in caplog.text


@pytest.mark.parametrize("status", [401, 403])
def test_a_status_refusal_beside_no_refresh_token_is_replaced(tmp_path, caplog, status):
    """With no refresh token in the file there is no lineage to lose: the local
    rule stands, and the launch publishes the row's pair."""
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "other-at", None)

    with (
        _launch_env(_Lookup(ProfileLookup(status))),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        assert _sync(config_dir, tmp_path) == "refused"
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    assert REPLACING in caplog.text


def test_a_final_refusal_still_replaces_the_file(tmp_path):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")

    with _launch_env(_Lookup(_intruder())):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    assert KEPT not in _evidence(config_dir)


# --- the replace WARNING is for a refusal only --------------------------------


def test_a_row_newer_than_the_file_replaces_it_with_no_warning(tmp_path, caplog):
    db = _make_row(tmp_path, cc_expires=int(time.time()) + 7200)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "old-at", "old-rt", expires_ms=(int(time.time()) + 600) * 1000)

    with _launch_env(_Lookup(ProfileLookup(503))), caplog.at_level(logging.WARNING, logger=LOGGER):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    assert REPLACING not in caplog.text


def test_a_row_with_no_claude_code_token_replaces_the_fallback_with_no_warning(tmp_path, caplog):
    db = _make_row(tmp_path, cc_at=None, cc_rt=None)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "primary-at", None)

    with _launch_env(_Lookup(ProfileLookup(503))), caplog.at_level(logging.WARNING, logger=LOGGER):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("primary-at", None)
    assert REPLACING not in caplog.text


# --- the downgrade guard keeps a pair nobody can prove ------------------------


@pytest.mark.parametrize(
    ("answer", "expires_ago"),
    [(ProfileLookup(200), 60), (ProfileLookup(401), None), (ProfileLookup(403), None)],
    ids=["expired", "401", "403"],
)
def test_a_null_refresh_token_never_replaces_an_unprovable_pair(tmp_path, answer, expires_ago):
    db = _make_row(tmp_path, cc_at=None, cc_rt=None, failure="invalid_grant")
    config_dir = _account_dir(tmp_path)
    expires_ms = _ago_ms(expires_ago) if expires_ago else 9_999_999_999_000
    _write_credentials(config_dir, "live-at", "live-rt", expires_ms=expires_ms)

    with _launch_env(_Lookup(answer)):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("live-at", "live-rt")


def test_a_null_refresh_token_replaces_a_pair_another_owner_holds(tmp_path):
    db = _make_row(tmp_path, cc_at=None, cc_rt=None, failure="invalid_grant")
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")

    with _launch_env(_Lookup(_intruder())):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("primary-at", None)


def test_a_null_refresh_token_replaces_a_pair_an_earlier_session_remembered(tmp_path):
    db = _make_row(tmp_path, cc_at=None, cc_rt=None, failure="invalid_grant")
    config_dir = _account_dir(tmp_path)
    oauth = {"accessToken": "intruder-at", "refreshToken": "intruder-rt"}
    launch._remember_foreign_pair(config_dir, oauth, db.get_account(1))
    _write_credentials(config_dir, "intruder-at", "intruder-rt", expires_ms=_ago_ms(60))
    lookup = _Lookup(ProfileLookup(503))

    with _launch_env(lookup):
        _next_launch(db, tmp_path)

    assert lookup.calls == 0
    assert _file_pair(config_dir) == ("primary-at", None)

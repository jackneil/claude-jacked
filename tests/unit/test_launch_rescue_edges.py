"""The launch token sync keeps what it cannot prove, and says what it
replaces (#158i).

Three edges of the pre-launch rescue. An expired rotation (a new refresh
token beside an access token that expired before this launch) is deferred
with no profile request, and the file keeps it: the row's refresh token may
be the one that rotation spent. A deferred rescue skips the pre-launch
Claude Code refresh for the same reason. A replaced pair, and a refused
exit-time sync, are logged with the row id and the sync result, never a
token.
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
    FUTURE_MS,
    INTRUDER,
    OWNER,
    _account_dir,
    _exit_claude,
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


def _past_ms() -> int:
    """An access-token expiry 60 s in the past, read at test time (an
    import-time value drifts behind a row expiry set later in a long run)."""
    return (int(time.time()) - 60) * 1000


@pytest.fixture(autouse=True)
def _fresh_state():
    clear_ownership_cache()
    launch._deferral_warned.clear()
    yield
    clear_ownership_cache()
    launch._deferral_warned.clear()


def _intruder():
    return _profile(INTRUDER, org="org-other", uuid="uuid-other")


# --- an expired rotation is deferred, not refused ----------------------------


def test_is_expired_rotation_needs_a_new_refresh_token_and_a_spent_access_token():
    row = {"cc_refresh_token": "row-rt-1"}
    past = _past_ms()
    assert launch._is_expired_rotation(row, {"refreshToken": "other", "expiresAt": past})
    assert not launch._is_expired_rotation(
        row, {"refreshToken": "row-rt-1", "expiresAt": past}
    )
    assert not launch._is_expired_rotation(
        row, {"refreshToken": "other", "expiresAt": FUTURE_MS}
    )
    assert not launch._is_expired_rotation(row, {"refreshToken": "", "expiresAt": past})
    assert not launch._is_expired_rotation(row, {"refreshToken": "other"})


def test_an_expired_rotation_is_deferred_with_no_request(tmp_path, caplog):
    # The row's own pair expired even earlier, so the file holds the newer
    # lineage (the sync ignores a file whose token expires before the row's).
    db = _make_row(tmp_path, cc_expires=int(time.time()) - 120)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt", expires_ms=_past_ms())
    lookup = _Lookup(_profile(OWNER))

    with _patched(lookup), caplog.at_level(logging.INFO, logger=LOGGER):
        first = _sync(config_dir, tmp_path)
        second = _sync(config_dir, tmp_path)

    assert (first, second) == ("deferred", "deferred")
    assert lookup.calls == 0
    row = db.get_account(1)
    assert (row["cc_access_token"], row["cc_refresh_token"]) == ("row-at-1", "row-rt-1")
    assert _file_pair(config_dir) == ("rotated-at", "rotated-rt")
    lines = [
        record.levelno
        for record in caplog.records
        if "expired before its owner could be checked" in record.getMessage()
    ]
    assert lines == [logging.WARNING, logging.INFO]
    assert "rotated-rt" not in caplog.text
    assert "rotated-at" not in caplog.text


# --- the next launch keeps the file and skips the Claude Code refresh --------


@contextlib.contextmanager
def _refresh_due(lookup, refresh):
    """A pre-launch Claude Code refresh is due and nothing else blocks it."""
    with (
        _patched(lookup),
        mock.patch("jacked.launch.should_refresh_cc", return_value=True),
        mock.patch("jacked.api.credential_helpers.cc_rotation_block", return_value=None),
        mock.patch("jacked.web.auth.refresh_cc_token", refresh),
    ):
        yield


@pytest.mark.parametrize("why", ["expired", "lookup_failed"])
def test_a_deferred_rescue_keeps_the_file_and_skips_the_cc_refresh(tmp_path, caplog, why):
    db = _make_row(tmp_path, cc_expires=int(time.time()) - 120)
    config_dir = _account_dir(tmp_path)
    if why == "expired":
        _write_credentials(config_dir, "rotated-at", "rotated-rt", expires_ms=_past_ms())
        lookup = _Lookup(_profile(OWNER))
    else:
        _write_credentials(config_dir, "rotated-at", "rotated-rt")
        lookup = _Lookup(ProfileLookup(503))
    refresh = mock.AsyncMock()

    with _refresh_due(lookup, refresh), caplog.at_level(logging.WARNING, logger=LOGGER):
        _next_launch(db, tmp_path)

    refresh.assert_not_called()
    assert _file_pair(config_dir) == ("rotated-at", "rotated-rt")
    assert "skipping the pre-launch Claude Code refresh" in caplog.text
    assert "keeping them for this launch" in caplog.text
    assert "rotated-rt" not in caplog.text


def test_a_proven_or_unchanged_file_still_gets_the_cc_refresh(tmp_path):
    """The skip is specific to an unchecked rotation: a file that holds the
    row's own pair gets the pre-launch refresh as before."""
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "row-at-1", "row-rt-1")
    refresh = mock.AsyncMock()

    with _refresh_due(_Lookup(ProfileLookup(503)), refresh):
        _next_launch(db, tmp_path)

    refresh.assert_awaited_once()


# --- a replaced pair is attributable -----------------------------------------


def test_replacing_a_refused_pair_is_logged_with_the_sync_result(tmp_path, caplog):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")

    with _patched(_Lookup(_intruder())), caplog.at_level(logging.WARNING, logger=LOGGER):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    assert (
        "replacing the per-account file's token pair with the row's (sync result: refused)"
        in caplog.text
    )
    for secret in ("intruder-at", "intruder-rt", "row-rt-1", INTRUDER):
        assert secret not in caplog.text


def test_a_proven_rotation_is_not_reported_as_replaced(tmp_path, caplog):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")

    with _patched(_Lookup(_profile(OWNER))), caplog.at_level(logging.WARNING, logger=LOGGER):
        _next_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("rotated-at", "rotated-rt")
    assert "replacing the per-account file" not in caplog.text


def test_scoped_pair_differs_reads_only_a_well_formed_file(tmp_path):
    row = {"cc_access_token": "a", "cc_refresh_token": "r"}
    path = tmp_path / ".credentials.json"
    assert launch._scoped_pair_differs(tmp_path, row) is False
    path.write_text("not json")
    assert launch._scoped_pair_differs(tmp_path, row) is False
    path.write_text(json.dumps({"claudeAiOauth": {}}))
    assert launch._scoped_pair_differs(tmp_path, row) is False
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r2"}}))
    assert launch._scoped_pair_differs(tmp_path, row) is True


# --- the exit-time sync says when it refused the file ------------------------


def test_a_refused_exit_sync_is_logged(tmp_path, caplog):
    _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")

    with _patched(_Lookup(_intruder())), caplog.at_level(logging.WARNING, logger=LOGGER):
        _exit_claude(config_dir, tmp_path)

    assert (
        "Account 1: the exit-time token sync refused the per-account file's tokens"
        in caplog.text
    )
    assert "intruder-at" not in caplog.text
    assert INTRUDER not in caplog.text


def test_a_proven_exit_sync_is_quiet(tmp_path, caplog):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")

    with _patched(_Lookup(_profile(OWNER))), caplog.at_level(logging.WARNING, logger=LOGGER):
        _exit_claude(config_dir, tmp_path)

    assert db.get_account(1)["cc_refresh_token"] == "rotated-rt"
    assert "exit-time token sync refused" not in caplog.text


# --- a file-authority platform reads back the pair the launch wrote ----------


def _snapshot_evidence(config_dir):
    snapshot = json.loads((config_dir / "jacked-resolver-snapshot.json").read_text())
    return tuple(snapshot.get("evidence") or ())


def _scoped_launch(db, tmp_path):
    """The launch on a platform whose authority is the launch directory's
    own file (Linux, Windows): no global activation, a strict readback."""
    with mock.patch("jacked.launch.scoped_launch_needs_global_activation", return_value=False):
        return _next_launch(db, tmp_path)


def test_a_kept_rotation_reads_back_on_a_file_authority_platform(tmp_path, caplog):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")

    with _patched(_Lookup(ProfileLookup(503))), caplog.at_level(logging.WARNING, logger=LOGGER):
        _scoped_launch(db, tmp_path)  # must not raise "did not read back"

    assert _file_pair(config_dir) == ("rotated-at", "rotated-rt")
    evidence = _snapshot_evidence(config_dir)
    assert "launch:scoped-file-readback" in evidence
    assert "launch:scoped-file:unchecked-rotation-kept" in evidence
    assert "keeping them for this launch" in caplog.text


def test_the_rows_pair_reads_back_without_the_kept_marker(tmp_path):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "row-at-1", "row-rt-1")

    with _patched(_Lookup(ProfileLookup(503))):
        _scoped_launch(db, tmp_path)

    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    evidence = _snapshot_evidence(config_dir)
    assert "launch:scoped-file-readback" in evidence
    assert "launch:scoped-file:unchecked-rotation-kept" not in evidence


def test_a_readback_that_differs_from_the_written_pair_still_refuses(tmp_path):
    import click

    from jacked.credentials import file_store

    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "row-at-1", "row-rt-1")
    real_read = file_store.FileCredentialStore.read

    def tampered(self, *args, **kwargs):
        """The strict store reads back a different access token than the
        launch wrote (a concurrent writer, a torn file)."""
        from jacked.credentials.canonical import CredentialPayload
        from jacked.credentials.models import StoreReadResult

        result = real_read(self, *args, **kwargs)
        if result.payload is None:
            return result
        tampered_map = result.payload.to_mapping()
        tampered_map["claudeAiOauth"]["accessToken"] = "someone-else"
        return StoreReadResult(
            result.status, payload=CredentialPayload.from_mapping(tampered_map), reason=result.reason
        )

    with (
        _patched(_Lookup(ProfileLookup(503))),
        mock.patch.object(file_store.FileCredentialStore, "read", tampered),
        pytest.raises(click.ClickException, match="did not read back"),
    ):
        _scoped_launch(db, tmp_path)

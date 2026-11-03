"""A pair proven to belong to another account is remembered beside the
per-account file (#158i).

A ``/login`` as another account inside a scoped session leaves that
account's pair in the directory's credentials file. The exit-time sync
proves it foreign with one profile request. That verdict used to die with
the process: the next launch, after the pair's access token expired, could
not prove it again and kept it as an unchecked rotation. The sync now
records a FINAL refusal (the profile named another owner) as digests of
the pair and of the row identity, never a token, and the next launch
replaces the pair without a request. A 401 or 403 refusal is not final and
is not recorded. A marker for another pair, for another row identity, or
an unreadable one is ignored.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time

import pytest

from jacked import launch
from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import clear_ownership_cache
from tests.unit.launch_sync_helpers import (
    INTRUDER,
    OWNER,
    _account_dir,
    _cc,
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
MARKER = launch._FOREIGN_PAIR_MARKER


@pytest.fixture(autouse=True)
def _fresh_state():
    clear_ownership_cache()
    launch._deferral_warned.clear()
    yield
    clear_ownership_cache()
    launch._deferral_warned.clear()


def _intruder():
    return _profile(INTRUDER, org="org-x", uuid="uuid-x")


def _past_ms() -> int:
    return (int(time.time()) - 60) * 1000


def test_a_final_refusal_is_remembered_as_digests(tmp_path, caplog):
    _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")

    with _patched(_Lookup(_intruder())), caplog.at_level(logging.INFO, logger=LOGGER):
        assert _sync(config_dir, tmp_path) == "refused"

    raw = (config_dir / MARKER).read_text()
    marker = json.loads(raw)
    assert set(marker) == {"access_sha256", "refresh_sha256", "identity_sha256", "recorded_at"}
    for secret in ("intruder-at", "intruder-rt", INTRUDER, OWNER):
        assert secret not in raw
    if os.name != "nt":
        assert (config_dir / MARKER).stat().st_mode & 0o777 == 0o600
    assert "the next launch replaces them" in caplog.text


def test_a_final_refusal_is_remembered_on_storage_that_refuses_chmod(tmp_path, monkeypatch):
    """Storage that creates files but refuses chmod (ENOTSUP) still gets the
    marker: the launch writers log the refused chmod and write on (#237i)."""
    from jacked import atomic_io

    _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")
    real_set_mode = atomic_io._set_mode
    refused: list = []

    def set_mode(fd, tmp_name, mode):
        if os.path.basename(tmp_name).startswith(f".{MARKER}."):
            refused.append(tmp_name)
            raise OSError(errno.ENOTSUP, "Operation not supported")
        return real_set_mode(fd, tmp_name, mode)

    monkeypatch.setattr(atomic_io, "_set_mode", set_mode)
    with _patched(_Lookup(_intruder())):
        assert _sync(config_dir, tmp_path) == "refused"

    assert len(refused) == 1
    assert set(json.loads((config_dir / MARKER).read_text())) == {
        "access_sha256", "refresh_sha256", "identity_sha256", "recorded_at",
    }


def test_a_remembered_pair_is_replaced_at_the_next_launch_without_a_request(tmp_path, caplog):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")
    with _patched(_Lookup(_intruder())):
        assert _sync(config_dir, tmp_path) == "refused"  # the last session's exit sync

    # The next launch comes after the intruder's access token expired, and
    # the profile endpoint is not even reachable: no request is needed.
    _write_credentials(config_dir, "intruder-at", "intruder-rt", expires_ms=_past_ms())
    clear_ownership_cache()  # a new process holds no verdict
    lookup = _Lookup(ProfileLookup(503))
    with _patched(lookup), caplog.at_level(logging.WARNING, logger=LOGGER):
        _next_launch(db, tmp_path)

    assert lookup.calls == 0
    assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
    assert not (config_dir / MARKER).exists()
    assert "remembered from an earlier check" in caplog.text
    assert "(sync result: refused)" in caplog.text
    assert "keeping them for this launch" not in caplog.text
    assert "intruder-rt" not in caplog.text


def test_a_remembered_pair_is_ignored_for_a_reauthed_row(tmp_path):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "other-at", "other-rt")
    with _patched(_Lookup(_intruder())):
        assert _sync(config_dir, tmp_path) == "refused"
    assert (config_dir / MARKER).exists()

    # The row is re-authed as the pair's owner: the marker names the old
    # identity, so a fresh request decides, and it proves the pair.
    with db._writer() as conn:
        conn.execute(
            "UPDATE accounts SET email = ?, organization_uuid = ?, account_uuid = ? WHERE id = 1",
            (INTRUDER, "org-x", "uuid-x"),
        )
    lookup = _Lookup(_intruder())
    with _patched(lookup):
        assert _sync(config_dir, tmp_path) == "imported"
    assert lookup.calls == 1
    assert _cc(db)[:2] == ("other-at", "other-rt")


def test_a_marker_for_another_pair_is_ignored(tmp_path):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")
    with _patched(_Lookup(_intruder())):
        assert _sync(config_dir, tmp_path) == "refused"

    # Claude Code rotated to a pair the marker does not name: a request decides.
    _write_credentials(config_dir, "rotated-at", "rotated-rt")
    lookup = _Lookup(_profile(OWNER))
    with _patched(lookup):
        assert _sync(config_dir, tmp_path) == "imported"
    assert lookup.calls == 1
    assert _cc(db)[:2] == ("rotated-at", "rotated-rt")


@pytest.mark.parametrize("status", [401, 403])
def test_a_status_refusal_is_not_remembered(tmp_path, status):
    _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")

    with _patched(_Lookup(ProfileLookup(status))):
        assert _sync(config_dir, tmp_path) == "refused"

    assert not (config_dir / MARKER).exists()


def test_an_unreadable_marker_is_ignored(tmp_path):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    (config_dir / MARKER).write_text("not json")
    _write_credentials(config_dir, "rotated-at", "rotated-rt")

    lookup = _Lookup(_profile(OWNER))
    with _patched(lookup):
        assert _sync(config_dir, tmp_path) == "imported"

    assert lookup.calls == 1
    assert _cc(db)[:2] == ("rotated-at", "rotated-rt")


def test_the_rows_own_pair_is_never_refused_by_a_marker(tmp_path):
    """A marker that happens to name the row's pair (a re-auth that landed
    the refused pair in the row) is never consulted: the row holds it."""
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "row-at-1", "row-rt-1")
    record = launch._pair_digests({"accessToken": "row-at-1", "refreshToken": "row-rt-1"}, db.get_account(1))
    (config_dir / MARKER).write_text(json.dumps({**record, "recorded_at": 0}))

    with _patched(_Lookup(ProfileLookup(503))):
        assert _sync(config_dir, tmp_path) == "imported"


def test_the_exit_sync_records_the_marker_for_the_next_launch(tmp_path):
    _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")

    with _patched(_Lookup(_intruder())):
        _exit_claude(config_dir, tmp_path)

    assert (config_dir / MARKER).exists()

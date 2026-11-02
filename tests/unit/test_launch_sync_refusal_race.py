"""A refusal holds only for the row it was decided against (#158i).

The launch token sync reads the row, sends one profile request, and
compares the profile with the row it read. A primary re-auth can move the
row to another organization during that request without touching the
Claude Code token. The import side already guards its compare-and-swap on
the identity. The refusal side has nothing to write, so it re-reads the
identity instead: a refusal decided against a row that changed is deferred,
and the next pass decides against the new row, with a fresh profile
request (the verdict cache is keyed by the identity).
"""

from __future__ import annotations

import logging

import pytest

from jacked.api.credential_helpers import clear_ownership_cache
from tests.unit.launch_sync_helpers import (
    OWNER,
    _account_dir,
    _cc,
    _Lookup,
    _make_row,
    _patched,
    _profile,
    _sync,
    _write_credentials,
)

LOGGER = "jacked.launch"
NEW_ORG = "org-new"
NEW_UUID = "uuid-new"


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_ownership_cache()
    yield
    clear_ownership_cache()


def _move_row(db, **identity):
    def move():
        db.update_account(1, **identity)

    return move


@pytest.mark.parametrize(
    "identity",
    [
        {"organization_uuid": NEW_ORG},
        {"account_uuid": NEW_UUID},
        {"organization_uuid": NEW_ORG, "account_uuid": NEW_UUID},
    ],
    ids=lambda d: "+".join(sorted(d)),
)
def test_a_refusal_decided_against_a_changed_row_is_deferred(tmp_path, caplog, identity):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")
    # The profile names the NEW identity; the re-auth lands during the request.
    profile = _profile(OWNER, org=identity.get("organization_uuid", "org-own"),
                       uuid=identity.get("account_uuid", "uuid-own"))
    lookup = _Lookup(profile, on_call=_move_row(db, **identity))

    with _patched(lookup), caplog.at_level(logging.INFO, logger=LOGGER):
        first = _sync(config_dir, tmp_path)

    assert first == "deferred"
    assert lookup.calls == 1
    assert _cc(db) == ("row-at-1", "row-rt-1", "unknown")
    assert "the row identity changed during the ownership check" in caplog.text
    assert "Token sync BLOCKED" not in caplog.text
    assert "rotated-at" not in caplog.text

    # The next pass proves the pair against the new row and imports it.
    with _patched(lookup):
        second = _sync(config_dir, tmp_path)

    assert second == "imported"
    assert lookup.calls == 2  # the new identity is a new cache key
    assert _cc(db)[:2] == ("rotated-at", "rotated-rt")


def test_a_refusal_against_an_unchanged_row_stays_refused(tmp_path, caplog):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "intruder-at", "intruder-rt")
    lookup = _Lookup(_profile("intruder@test.com", org="org-x", uuid="uuid-x"))

    with _patched(lookup), caplog.at_level(logging.INFO, logger=LOGGER):
        assert _sync(config_dir, tmp_path) == "refused"
        assert _sync(config_dir, tmp_path) == "refused"

    assert lookup.calls == 1  # the refusal is cached for the same row identity
    assert _cc(db)[:2] == ("row-at-1", "row-rt-1")
    assert "identity changed" not in caplog.text


def test_a_row_that_disappears_during_the_request_defers(tmp_path):
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    _write_credentials(config_dir, "rotated-at", "rotated-rt")

    def delete_row():
        with db._writer() as conn:
            conn.execute("DELETE FROM accounts WHERE id = 1")

    lookup = _Lookup(_profile("intruder@test.com", org="org-x", uuid="uuid-x"), on_call=delete_row)
    with _patched(lookup):
        assert _sync(config_dir, tmp_path) == "deferred"

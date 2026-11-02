"""The launch token sync imports only what the shared ownership rule proves (#158i).

``_sync_tokens_from_file`` copies a rotated token from the per-account
.credentials.json into its row: on every change of the file while Claude Code
runs, and once more when Claude Code exits. The per-account .claude.json names
the identity that a process wrote there last (the jacked seed or a Claude Code
profile write), so it does not prove who owns the token. The sync uses the
rule that every other import path uses,
``credential_helpers.live_payload_ownership``.

This module holds the ownership cases. test_launch_token_sync_rules.py holds
the offline proofs, the old schema, the polling loop and the tier capture.
test_launch_token_sync_lifecycle.py holds the stranded rotation and the
pre-launch rescue. The shared helpers are in launch_sync_helpers.py.
"""
from __future__ import annotations

import json
import logging
import time

import pytest

from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import clear_ownership_cache
from jacked.credentials.oauth_extra import captured_tier
from tests._platform import requires_symlinks
from tests.unit.launch_sync_helpers import (
    INTRUDER,
    OWNER,
    VERIFIED_AT,
    _account_dir,
    _cc,
    _Lookup,
    _make_row,
    _patched,
    _profile,
    _sync,
    _write_claude_json,
    _write_credentials,
)


@pytest.fixture(autouse=True)
def _fresh_ownership_cache():
    """Verdicts are cached per (account id, token hash): start each test clean."""
    clear_ownership_cache()
    yield
    clear_ownership_cache()


# ---------------------------------------------------------------------------
# The profile decides; .claude.json never does
# ---------------------------------------------------------------------------


class TestOwnershipDecidesTheImport:
    def test_a_rotation_whose_profile_names_the_row_is_imported(self, tmp_path):
        db = _make_row(tmp_path, status="invalid")
        config_dir = _account_dir(tmp_path)
        _write_credentials(
            config_dir, "rot-at-2", "rot-rt-2", subscriptionType="max",
            subscriptionTypeSource="profile", subscriptionTypeVerifiedAt=VERIFIED_AT,
        )
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert (row["cc_access_token"], row["cc_refresh_token"]) == ("rot-at-2", "rot-rt-2")
        assert row["validation_status"] == "valid"
        assert row["access_token"] == "primary-at"  # the primary lineage is untouched
        assert lookup.calls == 1
        # capture_oauth_extra binds the tier that the object carries (#230i).
        assert captured_tier(row["cc_oauth_extra"], "rot-rt-2") == "max"

    def test_a_foreign_profile_is_refused_even_when_claude_json_names_the_row(
        self, tmp_path, caplog
    ):
        db = _make_row(tmp_path, status="invalid")
        config_dir = _account_dir(tmp_path)
        _write_claude_json(config_dir / ".claude.json", OWNER)
        _write_credentials(config_dir, "foreign-at-9", "foreign-rt-9")
        lookup = _Lookup(_profile(INTRUDER, uuid="uuid-intruder"))
        caplog.set_level(logging.INFO)

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "refused"

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert lookup.calls == 1
        assert "Token sync BLOCKED for account 1: ownership not proven" in caplog.text
        assert "live token profile names another owner (account.email)" in caplog.text
        for value in ("owner@", "intruder@", "foreign-at-9", "foreign-rt-9", "row-at-1"):
            assert value not in caplog.text

    @pytest.mark.parametrize(
        "claude_json", ["missing", pytest.param("symlink", marks=requires_symlinks)]
    )
    def test_a_missing_or_symlinked_claude_json_no_longer_opens_the_door(
        self, tmp_path, claude_json
    ):
        """Before #158i a missing or symlinked .claude.json skipped every
        identity check, and the foreign token went into the row."""
        db = _make_row(tmp_path, status="invalid")
        config_dir = _account_dir(tmp_path)
        if claude_json == "symlink":
            target = tmp_path / "elsewhere.json"
            _write_claude_json(target, OWNER)
            (config_dir / ".claude.json").symlink_to(target)
        _write_credentials(config_dir, "foreign-at-9", "foreign-rt-9")
        lookup = _Lookup(_profile(INTRUDER, uuid="uuid-intruder"))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "refused"

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert lookup.calls == 1

    @pytest.mark.parametrize("status_code", [None, 503])
    def test_a_failed_lookup_defers_and_writes_nothing(self, tmp_path, caplog, status_code):
        db = _make_row(tmp_path, status="invalid")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        lookup = _Lookup(ProfileLookup(status_code))
        caplog.set_level(logging.INFO)

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "deferred"

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert lookup.calls == 1
        assert "Token sync deferred for account 1: ownership lookup failed" in caplog.text
        assert "BLOCKED" not in caplog.text

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_a_token_the_endpoint_refuses_is_refused_and_settles(
        self, tmp_path, caplog, status_code
    ):
        """A revoked token never becomes provable, so the result settles and
        the loop does not ask again every tick."""
        db = _make_row(tmp_path, status="invalid")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        lookup = _Lookup(ProfileLookup(status_code))
        caplog.set_level(logging.INFO)

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "refused"
            assert _sync(config_dir, tmp_path) == "refused"  # the cached verdict

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert lookup.calls == 1
        assert "Token sync BLOCKED for account 1: ownership not proven" in caplog.text

    def test_an_expired_rotation_is_deferred_with_zero_lookups(
        self, tmp_path, caplog, monkeypatch
    ):
        """Nobody can prove an expired access token, but the new refresh token
        beside it may be the only live lineage. It stays out of the row and in
        the file. The first deferral of the row is a WARNING, later ones INFO."""
        from jacked import launch

        monkeypatch.setattr(launch, "_deferral_warned", set())
        # The row's own pair expired even earlier, so the file is the newer one.
        db = _make_row(tmp_path, status="invalid", cc_expires=int(time.time()) - 120)
        config_dir = _account_dir(tmp_path)
        _write_credentials(
            config_dir, "rot-at-2", "rot-rt-2", expires_ms=(int(time.time()) - 60) * 1000
        )
        lookup = _Lookup(_profile(OWNER))
        caplog.set_level(logging.INFO)

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "deferred"
            assert _sync(config_dir, tmp_path) == "deferred"

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert lookup.calls == 0
        expected = (
            "Token sync deferred for account 1: the rotated access token expired "
            "before its owner could be checked; the file keeps the rotation"
        )
        levels = [r.levelno for r in caplog.records if r.getMessage() == expected]
        assert levels == [logging.WARNING, logging.INFO]
        assert "BLOCKED" not in caplog.text


# ---------------------------------------------------------------------------
# The other import rules reconcile applies (credential_helpers._reconcile_decision)
# ---------------------------------------------------------------------------


class TestTheSharedImportRules:
    def test_an_older_pair_never_replaces_newer_tokens(self, tmp_path, caplog):
        """On macOS the scoped file keeps the launch-time pair while reconcile
        takes Claude Code's newer pair from the Keychain. The exit-time sync
        must not roll the row back to the older, spent lineage."""
        now = int(time.time())
        db = _make_row(tmp_path, cc_expires=now + 7200, status="valid")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "launch-at-0", "launch-rt-0",
                           expires_ms=(now + 3600) * 1000)
        lookup = _Lookup(_profile(OWNER))
        caplog.set_level(logging.INFO)

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert _cc(db) == ("row-at-1", "row-rt-1", "valid")
        assert lookup.calls == 0
        assert "Token sync skipped for account 1: the file holds older tokens" in caplog.text

    def test_an_older_owned_pair_may_replace_a_known_dead_token(self, tmp_path):
        """The exception reconcile makes (#179i): when the API refused the
        row's token, an owned token that merely expires sooner is the one
        that works."""
        now = int(time.time())
        db = _make_row(tmp_path, cc_expires=now + 7200, failure="auth_rejected")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2",
                           expires_ms=(now + 3600) * 1000)
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "imported"

        row = db.get_account(1)
        assert (row["cc_access_token"], row["cc_refresh_token"]) == ("rot-at-2", "rot-rt-2")
        assert row["cc_refresh_failure_type"] is None
        assert lookup.calls == 1

    def test_jackeds_own_primary_publication_is_never_imported(self, tmp_path):
        """With no usable Claude Code token, the launch file carries the
        PRIMARY access token and no refresh token. Its profile names the row,
        but importing it into the cc_* columns would merge the two chains."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "primary-at", None)
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "unchanged"

        assert _cc(db)[:2] == ("row-at-1", "row-rt-1")
        assert lookup.calls == 0

    @pytest.mark.parametrize("expires", ["soon", -5, True])
    def test_an_unreadable_expiry_is_refused_even_with_the_rows_own_token(
        self, tmp_path, caplog, expires
    ):
        """Not a Claude Code rotation, so nothing in it is taken, not even
        pass-through fields beside the row's own token. The result settles,
        so the loop does not read the file again every tick."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "row-at-1", "row-rt-1", expires_ms=expires,
                           subscriptionTypeSource="profile")
        lookup = _Lookup(_profile(OWNER))
        caplog.set_level(logging.INFO)

        with _patched(lookup):
            assert _sync(config_dir, tmp_path) == "refused"

        row = db.get_account(1)
        assert row["cc_access_token"] == "row-at-1"
        assert row["cc_oauth_extra"] is None
        assert lookup.calls == 0
        assert "Token sync BLOCKED for account 1: unreadable expiry" in caplog.text


def test_appendix_acceptance_row_7_keeps_its_token(tmp_path, caplog):
    """The literal acceptance case from the #158i appendix."""
    db = _make_row(
        tmp_path, account_id=7, email="a@x.com", org="o1", uuid=None,
        cc_at="at1", cc_rt="rt1",
    )
    config_dir = _account_dir(tmp_path, 7)
    (config_dir / ".claude.json").write_text(json.dumps(
        {"oauthAccount": {"emailAddress": "a@x.com", "organizationUuid": "o1"}}
    ))
    _write_credentials(config_dir, "at9", "rt9")
    lookup = _Lookup(ProfileLookup(
        200, {"account": {"email": "b@x.com"}, "organization": {"uuid": "o1"}}
    ))
    caplog.set_level(logging.INFO)

    with _patched(lookup):
        _sync(config_dir, tmp_path)

    assert db.get_account(7)["cc_access_token"] == "at1"
    assert "Token sync BLOCKED for account 7" in caplog.text

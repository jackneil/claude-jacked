"""Tests for the Claude config identity watcher (AC5, revision 3)."""

from __future__ import annotations

import json
import logging
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from jacked.api.authority_guard import ProfileLookup
from jacked.api.identity_watcher import (
    ATTEMPT_MIN_INTERVAL_SECONDS,
    AuthorityView,
    IdentityWatcher,
)
from jacked.credentials.models import CredentialIdentity
from jacked.credentials.resolver import ResolverState, SnapshotUpdate

EMAIL = "Jack.Owner@Example.com"
ORG = "0b9f2a6e-51c4-4d0e-9a3b-6f1c2d3e4f50"
ACCOUNT_UUID = "7c3e9d21-8a4b-4f6c-b2d1-0e9f8a7b6c5d"
CREATED_AT = "2024-03-01T12:00:00.000000Z"
TOKEN = "sk-ant-oat01-secret-access-token"
NOW = 1_800_000_000.0


def _profile(
    email: str = EMAIL,
    org: str = ORG,
    account_uuid: str = ACCOUNT_UUID,
    created_at: str = CREATED_AT,
) -> dict:
    """Return a profile shaped like the live OAuth profile endpoint response."""
    return {
        "account": {
            "uuid": account_uuid,
            "email": email,
            "display_name": "Jack",
            "full_name": "Jack Owner",
            "created_at": created_at,
        },
        "organization": {
            "uuid": org,
            "name": "Owner Org",
            "billing_type": "stripe_subscription",
            "rate_limit_tier": "default_claude_max_20x",
            "seat_tier": None,
            "has_extra_usage_enabled": False,
            "subscription_created_at": "2024-03-02T00:00:00.000000Z",
            "claude_code_trial_ends_at": None,
            "claude_code_trial_duration_days": None,
        },
    }


def _row(account_id: int = 7, **overrides) -> dict:
    row = {
        "id": account_id,
        "email": EMAIL.lower(),
        "provider": "claude",
        "organization_uuid": ORG,
        "organization_name": "Owner Org",
        "account_uuid": ACCOUNT_UUID,
        "profile_json": json.dumps(_profile()),
        "profile_fetched_at": NOW - 100,
        "is_active": 1,
        "is_deleted": 0,
    }
    row.update(overrides)
    return row


def _config(**overrides) -> dict:
    block = {
        "accountUuid": ACCOUNT_UUID,
        "emailAddress": EMAIL.lower(),
        "organizationUuid": ORG,
        "organizationName": "Owner Org",
        "accountCreatedAt": CREATED_AT,
        "subscriptionCreatedAt": "2024-03-02T00:00:00.000000Z",
        "billingType": "stripe_subscription",
    }
    block.update(overrides)
    return {key: value for key, value in block.items() if value is not None}


class FakeDatabase:
    def __init__(self, rows: list[dict], settings: dict | None = None) -> None:
        self.rows = {int(row["id"]): dict(row) for row in rows}
        self.settings = {"desired_account_id": "7", **(settings or {})}

    def get_account(self, account_id: int):
        row = self.rows.get(int(account_id))
        return dict(row) if row else None

    def list_accounts(self, include_inactive: bool = False, include_deleted: bool = False):
        return [dict(row) for row in self.rows.values()]

    def get_setting(self, key: str):
        return self.settings.get(key)


class FakeClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeLease:
    def __init__(self, events: list[str], held: bool = False) -> None:
        self.events = events
        self.held = held

    @contextmanager
    def acquire(self):
        self.events.append("lease")
        yield not self.held


class Harness:
    """A watcher with every collaborator replaced by a recording fake."""

    def __init__(
        self, tmp_path, *, rows=None, config=None, settings=None, real_build=False
    ):
        self.home = tmp_path
        self.events: list[str] = []
        self.clock = FakeClock()
        self.db = FakeDatabase(rows if rows is not None else [_row()], settings)
        self.config = _config() if config is None else config
        self.views = [AuthorityView(7, "digest-1", TOKEN, (NOW + 3600) * 1000)]
        self.lookup_result = ProfileLookup(200, _profile())
        self.lookups: list[str] = []
        self.lease = FakeLease(self.events)
        self.write_status = "written"
        self.writes: list[dict] = []
        self.block = SimpleNamespace(
            fields={"accountCreatedAt": CREATED_AT}, complete=True, account_id=7,
            email=EMAIL.lower(), organization_uuid=ORG, account_uuid=ACCOUNT_UUID,
        )
        self.real_build = real_build
        self.builds: list[dict] = []
        self.watcher = IdentityWatcher(
            clock=self.clock,
            profile_lookup=self._lookup,
            lease_factory=lambda home: self.lease,
            resolve=self._resolve,
            writer=self._write,
            build=self._build,
            read_config=self._read_config,
        )

    def _read_config(self, home):
        assert home == self.home
        return self.config

    def _resolve(self, home):
        self.events.append("resolve")
        return self.views[0] if len(self.views) == 1 else self.views.pop(0)

    def _lookup(self, token):
        self.events.append("lookup")
        self.lookups.append(token)
        if isinstance(self.lookup_result, Exception):
            raise self.lookup_result
        return self.lookup_result

    def _build(self, row, *, db=None):
        self.builds.append(row)
        if self.real_build:
            from jacked.api.claude_identity import build_identity_block

            return build_identity_block(row, db=db)
        return self.block

    def _write(self, home, block, *, expected=None, lock_timeout=5.0):
        self.events.append("write")
        self.writes.append(
            {"home": home, "block": block, "expected": expected, "timeout": lock_timeout}
        )
        return SimpleNamespace(status=self.write_status)

    def update(self, **overrides) -> SnapshotUpdate:
        values = {
            "scope": "global",
            "state": ResolverState.RESOLVED,
            "evidence": (),
            "credential_revision": None,
            "desired": CredentialIdentity(7, EMAIL.lower(), ORG),
            "observed": CredentialIdentity(7, EMAIL.lower(), ORG),
        }
        values.update(overrides)
        return SnapshotUpdate(**values)

    def run(self, **overrides) -> tuple[str, ...]:
        self.watcher.run(self.update(**overrides), self.db, self.home)
        return self.watcher.drain_evidence()

    def advance(self, seconds: float) -> None:
        self.clock.now += seconds


@pytest.fixture(autouse=True)
def _no_config_dir(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


# --- divergence detection -------------------------------------------------


def test_an_in_sync_config_does_nothing(tmp_path):
    harness = Harness(tmp_path)

    assert harness.run() == ()
    assert harness.events == []


def test_an_email_that_differs_only_in_case_is_in_sync(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="  JACK.OWNER@example.COM "))

    assert harness.run() == ()
    assert harness.writes == []


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (_config(emailAddress="someone.else@example.com"), "emailAddress"),
        (_config(organizationUuid="9e8d7c6b-0000-4000-8000-000000000001"), "organizationUuid"),
        (_config(accountUuid="1a2b3c4d-0000-4000-8000-000000000002"), "accountUuid"),
        (_config(accountCreatedAt="2025-01-01T00:00:00.000000Z"), "accountCreatedAt"),
        (_config(accountCreatedAt=None), "accountCreatedAt"),
    ],
)
def test_each_divergence_kind_republishes_once(tmp_path, caplog, config, field):
    harness = Harness(tmp_path, config=config)

    with caplog.at_level(logging.INFO, logger="jacked.api.identity_watcher"):
        evidence = harness.run()

    assert len(harness.writes) == 1
    write = harness.writes[0]
    assert write["expected"] == config
    assert write["timeout"] == 1.0
    assert write["block"] is harness.block
    assert evidence == ("claude-config:identity:republished",)
    assert field in caplog.text


def test_an_empty_row_org_is_compared_through_the_profile_org(tmp_path):
    # The row has no org, so the builder takes the org from the verified
    # stored profile. A config with another org must be repaired.
    harness = Harness(
        tmp_path,
        rows=[_row(organization_uuid="")],
        config=_config(organizationUuid="9e8d7c6b-0000-4000-8000-000000000001"),
        real_build=True,
    )

    evidence = harness.run()

    assert evidence[0] == "claude-config:identity:republished"
    assert harness.writes[0]["block"].organization_uuid == ORG


def test_a_block_org_the_token_does_not_own_is_never_written(tmp_path):
    # CSO M1: an empty-org row whose stored profile says org Y while the
    # stored token belongs to org X. The row check alone would pass.
    other_org = "1a2b3c4d-0000-4000-8000-0000000000aa"
    harness = Harness(
        tmp_path,
        rows=[_row(organization_uuid="")],
        config=_config(organizationUuid=other_org),
        real_build=True,
    )
    harness.lookup_result = ProfileLookup(200, _profile(org=other_org))

    evidence = harness.run()

    assert harness.writes == []
    assert "authority:stamp-mismatch" in evidence


def test_the_account_uuid_is_compared_only_when_the_row_knows_it(tmp_path):
    # Neither the row nor a stored profile knows the uuid, so the block
    # carries none and a config uuid cannot diverge.
    harness = Harness(
        tmp_path,
        rows=[_row(account_uuid=None, profile_json=None)],
        config=_config(accountUuid="1a2b3c4d-0000-4000-8000-000000000002"),
        real_build=True,
    )

    assert harness.run() == ()
    assert harness.writes == []


def test_created_at_is_compared_only_when_a_profile_is_stored(tmp_path):
    harness = Harness(
        tmp_path,
        rows=[_row(profile_json=None)],
        config=_config(accountCreatedAt="2025-01-01T00:00:00.000000Z"),
        real_build=True,
    )

    assert harness.run() == ()
    assert harness.writes == []


def test_a_partial_block_without_a_uuid_says_so(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.block = SimpleNamespace(
        fields={}, complete=False, account_id=7, email=EMAIL.lower(),
        organization_uuid=ORG, account_uuid=None,
    )

    evidence = harness.run()

    assert evidence == (
        "claude-config:identity:republished",
        "claude-config:identity:partial",
        "claude-config:identity:uuid-unknown",
    )


@pytest.mark.parametrize("status", ["cas_aborted", "locked", "unreadable", "write_failed"])
def test_a_write_that_does_not_land_is_divergent(tmp_path, status):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.write_status = status

    assert harness.run() == ("claude-config:identity:divergent",)


# --- guards -----------------------------------------------------------------


def test_a_scoped_config_dir_disables_the_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "scoped"))
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    # The watcher does nothing, and says so in every snapshot.
    assert harness.run() == ("claude-config:identity:disabled",)
    assert harness.run() == ("claude-config:identity:disabled",)
    assert harness.events == []
    assert harness.watcher.status()["disabled"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "scoped"},
        {"state": ResolverState.CONFLICT},
        {"state": ResolverState.UNUSABLE},
        {"observed": None},
    ],
)
def test_only_a_resolved_global_observation_is_watched(tmp_path, overrides):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    assert harness.run(**overrides) == ()
    assert harness.events == []


def test_a_config_without_an_identity_block_is_left_alone(tmp_path):
    harness = Harness(tmp_path)
    harness.config = None

    assert harness.run() == ()
    assert harness.events == []


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_row(provider="codex")],
        [_row(is_deleted=1)],
    ],
)
def test_a_missing_or_foreign_provider_row_is_left_alone(tmp_path, rows):
    harness = Harness(tmp_path, rows=rows, config=_config(emailAddress="old@example.com"))

    assert harness.run() == ()
    assert harness.events == []


def test_an_authority_stamped_for_another_account_is_left_alone(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.views = [AuthorityView(8, "digest-1", TOKEN, (NOW + 3600) * 1000)]

    assert harness.run() == ()
    assert harness.lookups == []
    assert harness.writes == []


# --- rate floor and breaker ---------------------------------------------------


def test_the_rate_floor_is_stamped_at_the_attempt_even_on_failure(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.write_status = "cas_aborted"

    harness.run()
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS - 1)
    assert harness.run() == ()
    assert len(harness.writes) == 1

    harness.advance(2)
    harness.run()
    assert len(harness.writes) == 2


def test_the_rate_floor_is_per_account(tmp_path):
    other = _row(8, email="kim@example.com", organization_uuid="org-8", account_uuid=None,
                 profile_json=None)
    harness = Harness(
        tmp_path, rows=[_row(), other], config=_config(emailAddress="old@example.com"),
        settings={"desired_account_id": None}, real_build=True,
    )
    harness.run(desired=None)
    harness.views = [AuthorityView(8, "digest-8", TOKEN, (NOW + 3600) * 1000)]
    harness.lookup_result = ProfileLookup(
        200, _profile(email="kim@example.com", org="org-8", account_uuid="u-8")
    )

    harness.run(desired=None, observed=CredentialIdentity(8, "kim@example.com", "org-8"))

    assert len(harness.writes) == 2


def test_the_breaker_trips_after_three_republishes_and_warns_once(tmp_path, caplog):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    for _ in range(3):
        assert harness.run() == ("claude-config:identity:republished",)
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    with caplog.at_level(logging.WARNING, logger="jacked.api.identity_watcher"):
        assert harness.run() == ("claude-config:identity:flapping",)
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
        assert harness.run() == ("claude-config:identity:flapping",)

    assert len(harness.writes) == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "7" in warnings[0].getMessage()


def test_the_breaker_resets_when_the_authority_digest_changes(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    for _ in range(4):
        harness.run()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    assert len(harness.writes) == 3

    harness.views = [AuthorityView(7, "digest-2", TOKEN, (NOW + 3600) * 1000)]
    assert harness.run() == ("claude-config:identity:republished",)
    assert len(harness.writes) == 4


def test_the_breaker_resets_when_the_desired_account_changes(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    for _ in range(4):
        harness.run()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    assert len(harness.writes) == 3

    harness.db.settings = {}
    assert harness.run(desired=None) == ("claude-config:identity:republished",)
    assert len(harness.writes) == 4


def test_an_incomplete_block_counts_toward_the_breaker(tmp_path):
    harness = Harness(tmp_path, config=_config(accountCreatedAt=None))
    for _ in range(4):
        harness.run()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    assert len(harness.writes) == 3


# --- owner proof --------------------------------------------------------------


def test_an_expired_token_is_never_sent(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.views = [AuthorityView(7, "digest-1", TOKEN, (NOW - 1) * 1000)]

    assert harness.run() == ("claude-config:identity:owner-unproven",)
    assert harness.lookups == []
    assert harness.writes == []


@pytest.mark.parametrize("status", [401])
def test_a_rejected_token_is_unproven_and_cached_per_digest(tmp_path, status):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lookup_result = ProfileLookup(status)

    assert harness.run() == ("claude-config:identity:owner-unproven",)
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS * 5)
    assert harness.run() == ("claude-config:identity:owner-unproven",)

    assert len(harness.lookups) == 1
    assert harness.writes == []
    assert "lease" not in harness.events


@pytest.mark.parametrize(
    "result",
    [
        ProfileLookup(None),
        ProfileLookup(403),
        ProfileLookup(429),
        ProfileLookup(503),
        OSError("network down"),
    ],
)
def test_a_transient_lookup_failure_is_retried_after_the_floor(tmp_path, result):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lookup_result = result

    assert harness.run() == ("claude-config:identity:owner-unproven",)
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS - 1)
    harness.run()
    assert len(harness.lookups) == 1

    harness.advance(2)
    harness.lookup_result = ProfileLookup(200, _profile())
    assert harness.run() == ("claude-config:identity:republished",)
    assert len(harness.lookups) == 2


def test_a_proven_owner_is_cached_per_digest(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    harness.run()
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    harness.run()

    assert len(harness.lookups) == 1
    assert len(harness.writes) == 2


@pytest.mark.parametrize(
    "profile",
    [
        _profile(email="intruder@example.com"),
        _profile(org="9e8d7c6b-0000-4000-8000-000000000001"),
        _profile(account_uuid="1a2b3c4d-0000-4000-8000-000000000002"),
    ],
)
def test_a_token_owned_by_someone_else_is_never_published(tmp_path, caplog, profile):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lookup_result = ProfileLookup(200, profile)

    with caplog.at_level(logging.WARNING, logger="jacked.api.identity_watcher"):
        assert harness.run() == ("authority:stamp-mismatch",)

    assert harness.writes == []
    assert "lease" not in harness.events
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "7" in warnings[0].getMessage()
    assert "@" not in caplog.text


def test_the_owner_check_skips_the_org_and_uuid_the_row_does_not_know(tmp_path):
    # Neither the row nor the block knows an org or a uuid (no stored
    # profile), so the proof is checked on the email only.
    harness = Harness(
        tmp_path,
        rows=[_row(organization_uuid="", account_uuid=None, profile_json=None)],
        config=_config(emailAddress="old@example.com"),
        real_build=True,
    )
    harness.lookup_result = ProfileLookup(
        200, _profile(org="any-org-uuid", account_uuid="any-account-uuid")
    )

    assert harness.run() == (
        "claude-config:identity:republished",
        "claude-config:identity:partial",
        "claude-config:identity:uuid-unknown",
    )


def test_the_owner_proof_never_refreshes_or_calls_fetch_profile(tmp_path, monkeypatch):
    """Regression: the default lookup is one GET, with no refresh path at all."""
    import jacked.web.auth as auth

    calls: list[str] = []

    def forbidden(*args, **kwargs):
        # The watcher swallows lookup errors, so the raise alone proves
        # nothing; the recorded call is what the test asserts on.
        calls.append("refresh")
        raise AssertionError("refresh path reached from the identity watcher")

    monkeypatch.setattr(auth, "fetch_profile", forbidden)
    monkeypatch.setattr(auth, "_try_refresh_primary_token", forbidden)
    requests: list[str] = []

    class _Response:
        status_code = 401

        @staticmethod
        def json():
            return {}

    class _Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            requests.append(url)
            return _Response()

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_Client))
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.watcher = IdentityWatcher(
        clock=harness.clock,
        lease_factory=lambda home: harness.lease,
        resolve=harness._resolve,
        writer=harness._write,
        build=harness._build,
        read_config=harness._read_config,
    )

    assert harness.run() == ("claude-config:identity:owner-unproven",)
    assert calls == []
    assert len(requests) == 1
    assert harness.writes == []


# --- lease and re-resolve ----------------------------------------------------


def test_the_owner_proof_runs_before_the_lease(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    harness.run()

    assert harness.events == ["resolve", "lookup", "lease", "resolve", "write"]


def test_a_held_switch_lease_defers_without_writing(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lease.held = True

    assert harness.run() == ("claude-config:identity:deferred-switch",)
    assert harness.writes == []


def test_a_digest_change_under_the_lease_skips_the_write(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.views = [
        AuthorityView(7, "digest-1", TOKEN, (NOW + 3600) * 1000),
        AuthorityView(7, "digest-2", TOKEN, (NOW + 3600) * 1000),
    ]

    assert harness.run() == ()
    assert harness.writes == []


def test_a_desired_change_under_the_lease_skips_the_write(tmp_path):
    harness = Harness(
        tmp_path,
        rows=[_row(), _row(8, email="kim@example.com", organization_uuid="org-8")],
        config=_config(emailAddress="old@example.com"),
    )

    class _SwitchingLease(FakeLease):
        @contextmanager
        def acquire(self):
            harness.db.settings["desired_account_id"] = "8"
            yield True

    harness.lease = _SwitchingLease(harness.events)

    assert harness.run() == ()
    assert harness.writes == []


def test_an_identity_change_under_the_lease_skips_the_write(tmp_path):
    # The block is built before the lease, so the lease is held only for the
    # checks and the write. A row whose identity columns changed meanwhile
    # is not written; the next pass builds from the new row.
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    class _RenamingLease(FakeLease):
        @contextmanager
        def acquire(self):
            harness.db.rows[7]["organization_name"] = "Renamed Org"
            yield True

    harness.lease = _RenamingLease(harness.events)

    harness.run()
    assert harness.writes == []

    harness.lease = FakeLease(harness.events)
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    harness.run()

    assert harness.builds[-1]["organization_name"] == "Renamed Org"
    assert len(harness.writes) == 1


def test_a_usage_column_change_under_the_lease_still_writes(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    class _PollingLease(FakeLease):
        @contextmanager
        def acquire(self):
            harness.db.rows[7]["cached_usage_raw"] = "{}"
            harness.db.rows[7]["usage_cached_at"] = NOW
            yield True

    harness.lease = _PollingLease(harness.events)

    harness.run()

    assert len(harness.writes) == 1


# --- logging ------------------------------------------------------------------


def test_logs_never_carry_an_email_or_a_token(tmp_path, caplog):
    harness = Harness(
        tmp_path,
        rows=[_row(), _row(9, email="old@example.com", organization_uuid=ORG)],
        config=_config(emailAddress="old@example.com"),
    )
    (tmp_path / ".claude.json").write_text("{}")

    with caplog.at_level(logging.DEBUG, logger="jacked.api.identity_watcher"):
        harness.run()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
        harness.lookup_result = ProfileLookup(200, _profile(email="intruder@example.com"))
        harness.views = [AuthorityView(7, "digest-2", TOKEN, (NOW + 3600) * 1000)]
        harness.run()

    text = caplog.text
    assert "@" not in text
    assert TOKEN not in text
    assert ACCOUNT_UUID not in text
    republish = next(r for r in caplog.records if "Republished" in r.getMessage())
    message = republish.getMessage()
    assert "account 7" in message
    assert "account 9" in message
    assert ACCOUNT_UUID[:8] in message
    assert "emailAddress" in message


def test_skip_reasons_log_only_on_a_state_transition(tmp_path, caplog):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lease.held = True

    with caplog.at_level(logging.INFO, logger="jacked.api.identity_watcher"):
        for _ in range(3):
            harness.run()
            harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    deferred = [r for r in caplog.records if "deferred-switch" in r.getMessage()]
    assert len(deferred) == 1


def test_drained_evidence_is_cleared(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.watcher.run(harness.update(), harness.db, harness.home)

    assert harness.watcher.drain_evidence() == ("claude-config:identity:republished",)
    assert harness.watcher.drain_evidence() == ()


# --- authority guard logging (revision 3 item 7) -----------------------------


def test_the_unknown_account_warning_names_no_email(caplog):
    from jacked.api.authority_guard import (
        heal_foreign_authority,
        reset_authority_guard_state,
    )

    reset_authority_guard_state()
    db = FakeDatabase([_row()])
    payload = {"claudeAiOauth": {"accessToken": TOKEN, "refreshToken": "rt"}}
    try:
        with caplog.at_level(logging.WARNING, logger="jacked.api.authority_guard"):
            result = heal_foreign_authority(
                db,
                now=100.0,
                profile_lookup=lambda token: _profile(email="stranger@example.com"),
                activate=lambda *args: None,
                read_authority=lambda: payload,
            )
    finally:
        reset_authority_guard_state()

    assert result.action == "unknown_account"
    assert "@" not in caplog.text
    assert "@" not in result.reason
    assert ORG[:8] in caplog.text


# --- divergence is judged against the block jacked would publish -----------


def _apply_writes(harness: Harness) -> None:
    """Make the fake writer merge the block into the config, like the real one."""
    original = harness._write

    def write(home, block, *, expected=None, lock_timeout=5.0):
        result = original(home, block, expected=expected, lock_timeout=lock_timeout)
        harness.config = {**harness.config, **block.fields}
        return result

    harness.watcher._writer = write


def test_a_rejected_stored_profile_never_republishes_forever(tmp_path):
    """The builder ignores a stored profile of another person, so the block it
    publishes has no accountCreatedAt. The raw row check still sees one."""
    row = _row(profile_json=json.dumps(_profile(email="someone.else@example.com")))
    config = _config(accountCreatedAt=None, billingType=None)
    harness = Harness(tmp_path, rows=[row], config=config, real_build=True)

    for _ in range(3):
        assert harness.run() == ()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    assert harness.writes == []
    assert harness.lookups == []
    assert "resolve" not in harness.events


def test_a_dropped_shared_uuid_never_republishes_forever(tmp_path):
    """The builder omits a uuid that another person's row also carries."""
    other = _row(9, email="kim@example.com", organization_uuid="org-9")
    harness = Harness(
        tmp_path, rows=[_row(), other], config=_config(accountUuid=None), real_build=True
    )

    for _ in range(3):
        assert harness.run() == ()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    assert harness.writes == []
    assert harness.builds[0]["id"] == 7


def test_the_profile_identity_used_without_a_row_org_never_republishes_forever(tmp_path):
    """Without a row org the builder accepts the stored profile and takes its
    account uuid, so a stale row uuid must not count as a divergence."""
    harness = Harness(
        tmp_path,
        rows=[_row(organization_uuid="", account_uuid="stale-row-uuid")],
        config=_config(),
        real_build=True,
    )

    for _ in range(3):
        assert harness.run() == ()
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    assert harness.writes == []


def test_a_block_org_that_differs_from_the_config_is_republished(tmp_path):
    """Only the block decides what diverges: here the raw row check fires on
    accountCreatedAt, but the block agrees with the config on it and differs
    on the organization alone."""
    other_created = "2025-01-01T00:00:00.000000Z"
    harness = Harness(
        tmp_path,
        rows=[_row(organization_uuid="")],
        config=_config(
            organizationUuid="9e8d7c6b-0000-4000-8000-000000000001",
            accountCreatedAt=other_created,
        ),
    )
    harness.block = SimpleNamespace(
        fields={"accountCreatedAt": other_created, "organizationUuid": ORG},
        complete=True, account_id=7, email=EMAIL.lower(),
        organization_uuid=ORG, account_uuid=ACCOUNT_UUID,
    )
    _apply_writes(harness)

    assert harness.run() == ("claude-config:identity:republished",)
    assert harness.config["organizationUuid"] == ORG
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
    assert harness.run() == ()
    assert len(harness.writes) == 1


def test_a_block_that_matches_the_config_does_not_stamp_the_floor(tmp_path):
    other = _row(9, email="kim@example.com", organization_uuid="org-9")
    harness = Harness(
        tmp_path, rows=[_row(), other], config=_config(accountUuid=None), real_build=True
    )
    assert harness.run() == ()

    # A real divergence right after the no-op pass is not held back by a floor.
    harness.config = _config(accountUuid=None, emailAddress="old@example.com")
    assert harness.run()[0] == "claude-config:identity:republished"
    assert len(harness.writes) == 1


def test_a_settled_block_is_not_rebuilt_on_every_pass(tmp_path, caplog):
    """The shared-uuid builder warns on each build; a settled verdict is
    reused for an unchanged row and config instead of rebuilt per pass."""
    from jacked.api.identity_watcher import SETTLED_TTL_SECONDS

    other = _row(9, email="kim@example.com", organization_uuid="org-9")
    harness = Harness(
        tmp_path, rows=[_row(), other], config=_config(accountUuid=None), real_build=True
    )

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert harness.run() == ()
            harness.advance(10)
    assert len(harness.builds) == 1
    assert len([r for r in caplog.records if "uuid" in r.getMessage()]) == 1

    harness.advance(SETTLED_TTL_SECONDS)
    assert harness.run() == ()
    assert len(harness.builds) == 2

    harness.db.rows[7]["organization_name"] = "Renamed Org"
    assert harness.run() == ()
    assert len(harness.builds) == 3
    assert harness.writes == []


def test_a_republished_block_is_in_sync_on_the_next_pass(tmp_path):
    harness = Harness(
        tmp_path,
        config=_config(emailAddress="old@example.com", accountCreatedAt=None),
        real_build=True,
    )
    _apply_writes(harness)

    assert harness.run() == ("claude-config:identity:republished",)
    for _ in range(3):
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)
        assert harness.run() == ()

    assert len(harness.writes) == 1


# --- production collaborators -------------------------------------------------


def test_the_authority_view_reads_a_stamped_credential_file(tmp_path, monkeypatch):
    from jacked.api.identity_watcher import _read_authority_view
    from jacked.credentials.canonical import CredentialPayload

    monkeypatch.setattr(sys, "platform", "linux")
    payload = {
        "_jackedAccountId": 7,
        "claudeAiOauth": {
            "accessToken": TOKEN,
            "refreshToken": "sk-ant-ort01-secret-refresh-token",
            "expiresAt": 1_900_000_000_000,
        },
    }
    root = tmp_path / ".claude"
    root.mkdir()
    (root / ".credentials.json").write_text(json.dumps(payload))

    view = _read_authority_view(tmp_path)

    assert view is not None
    assert view.account_id == 7
    assert view.digest == CredentialPayload.from_mapping(payload).digest
    assert view.access_token == TOKEN
    assert view.expires_at_ms == 1_900_000_000_000.0
    assert TOKEN not in repr(view)
    assert "refresh-token" not in repr(view)


def test_the_authority_view_is_none_without_a_credential_file(tmp_path, monkeypatch):
    from jacked.api.identity_watcher import _read_authority_view

    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / ".claude").mkdir()

    assert _read_authority_view(tmp_path) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1_900_000_000_000, 1_900_000_000_000.0),
        (1_900_000_000, 1_900_000_000_000.0),
        (1_900_000_000.5, 1_900_000_000_500.0),
        (True, None),
        (False, None),
        (0, None),
        (-5, None),
        ("1900000000000", None),
        (None, None),
    ],
)
def test_expiry_is_normalized_to_milliseconds(raw, expected):
    from jacked.api.identity_watcher import _expiry_ms

    assert _expiry_ms(raw) == expected


class _FakeHttpResponse:
    def __init__(self, status_code, body=None, *, bad_json=False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


def _fake_httpx(monkeypatch, outcome, seen):
    class _Client:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers):
            seen.append(headers.get("Authorization"))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=_Client))


@pytest.mark.parametrize(
    ("outcome", "status", "profile"),
    [
        (OSError("connection reset"), None, None),
        (_FakeHttpResponse(503), 503, None),
        (_FakeHttpResponse(429), 429, None),
        (_FakeHttpResponse(401), 401, None),
        (_FakeHttpResponse(200, ["not", "an", "object"]), 200, None),
        (_FakeHttpResponse(200, bad_json=True), 200, None),
        (_FakeHttpResponse(200, {"account": {"email": EMAIL}}), 200, {"account": {"email": EMAIL}}),
    ],
)
def test_lookup_profile_maps_each_outcome(monkeypatch, outcome, status, profile):
    from jacked.api.authority_guard import lookup_profile

    seen: list[str] = []
    _fake_httpx(monkeypatch, outcome, seen)

    result = lookup_profile(TOKEN)

    assert result.status_code == status
    assert result.profile == profile
    assert seen == [f"Bearer {TOKEN}"]
    assert TOKEN not in repr(result)


# --- worker thread -------------------------------------------------------------


class _BlockingWatcher(IdentityWatcher):
    def __init__(self, evidence: str = "claude-config:identity:republished"):
        super().__init__()
        self.release = threading.Event()
        self.started = threading.Event()
        self.runs = 0
        self.evidence_item = evidence
        self.thread_names: list[str] = []
        self.daemon_flags: list[bool] = []

    def _run(self, update, db, home):
        self.runs += 1
        current = threading.current_thread()
        self.thread_names.append(current.name)
        self.daemon_flags.append(current.daemon)
        self.started.set()
        assert self.release.wait(5)
        self._emit(self.evidence_item)


def test_submit_runs_off_the_caller_thread_on_a_daemon_worker(tmp_path):
    watcher = _BlockingWatcher()

    assert watcher.submit(Harness(tmp_path).update(), None, tmp_path) is True
    assert watcher.started.wait(5)
    assert watcher.drain_evidence() == ()

    watcher.release.set()
    assert watcher.wait_idle(5)
    assert watcher.drain_evidence() == ("claude-config:identity:republished",)
    assert watcher.daemon_flags == [True]
    assert watcher.thread_names[0] != threading.current_thread().name


def test_an_overlapping_submit_is_skipped(tmp_path):
    watcher = _BlockingWatcher()
    update = Harness(tmp_path).update()

    assert watcher.submit(update, None, tmp_path) is True
    assert watcher.started.wait(5)
    assert watcher.submit(update, None, tmp_path) is False

    watcher.release.set()
    assert watcher.wait_idle(5)
    assert watcher.runs == 1
    assert watcher.submit(update, None, tmp_path) is True
    assert watcher.wait_idle(5)
    assert watcher.runs == 2


def test_a_failing_worker_run_is_logged_and_frees_the_worker(tmp_path, caplog):
    class _Exploding(IdentityWatcher):
        def _run(self, update, db, home):
            raise RuntimeError("worker exploded")

    watcher = _Exploding()

    with caplog.at_level(logging.WARNING, logger="jacked.api.identity_watcher"):
        assert watcher.submit(Harness(tmp_path).update(), None, tmp_path) is True
        assert watcher.wait_idle(5)

    assert "identity watcher failed" in caplog.text
    assert watcher.submit(Harness(tmp_path).update(), None, tmp_path) is True
    assert watcher.wait_idle(5)


# --- review wave 2 -------------------------------------------------------------


def test_another_orgs_subscription_merged_in_is_repaired(tmp_path):
    # Same person, same uuid and createdAt: only the org subscription date
    # shows that another org's profile was merged into the block.
    harness = Harness(
        tmp_path,
        config=_config(subscriptionCreatedAt="2026-01-01T00:00:00.000000Z"),
        real_build=True,
    )

    evidence = harness.run()

    assert evidence[0] == "claude-config:identity:republished"
    assert "subscriptionCreatedAt" in harness.writes[0]["block"].fields


def test_a_post_verify_mismatch_counts_toward_the_breaker(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.write_status = "post_verify_mismatch"

    for _ in range(3):
        assert harness.run() == ("claude-config:identity:divergent",)
        harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    assert harness.run() == ("claude-config:identity:flapping",)
    assert len(harness.writes) == 3
    assert harness.watcher.status()["accounts"]["7"]["breaker_tripped"] is True


def test_sticky_evidence_stays_in_every_snapshot(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lookup_result = ProfileLookup(401)

    assert harness.run() == ("claude-config:identity:owner-unproven",)
    # Inside the rate floor nothing runs, but the snapshot still says so.
    harness.advance(5)
    assert harness.run() == ("claude-config:identity:owner-unproven",)
    status = harness.watcher.status()["accounts"]["7"]
    assert status["reason"] == "owner-unproven"
    assert status["divergent_for_seconds"] is not None


def test_a_long_divergence_warns_once(tmp_path, caplog):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lookup_result = ProfileLookup(401)

    with caplog.at_level(logging.WARNING, logger="jacked.api.identity_watcher"):
        for _ in range(8):
            harness.run()
            harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    warnings = [r.getMessage() for r in caplog.records if "has diverged for" in r.getMessage()]
    assert len(warnings) == 1
    assert "emailAddress" in warnings[0]
    assert "@" not in warnings[0]


def test_in_sync_clears_the_divergence_clock(tmp_path):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))
    harness.lookup_result = ProfileLookup(401)
    harness.run()
    harness.config = _config()
    harness.advance(ATTEMPT_MIN_INTERVAL_SECONDS + 1)

    harness.run()

    status = harness.watcher.status()["accounts"]["7"]
    assert status["reason"] == "in-sync"
    assert status["divergent_for_seconds"] is None


def test_submit_reuses_one_worker_thread(tmp_path):
    harness = Harness(tmp_path, config=_config())
    names_before = {t.ident for t in threading.enumerate()}

    for _ in range(5):
        harness.watcher.submit(harness.update(), harness.db, harness.home)
        assert harness.watcher.wait_idle(2.0)

    new_threads = [
        t for t in threading.enumerate()
        if t.ident not in names_before and t.name == "jacked-identity-watcher"
    ]
    assert len(new_threads) == 1
    assert new_threads[0].daemon is True


def test_a_writer_exception_is_a_values_free_warning(tmp_path, caplog):
    harness = Harness(tmp_path, config=_config(emailAddress="old@example.com"))

    def boom(*args, **kwargs):
        raise OSError("disk full at /Users/someone@example.com")

    harness.watcher._writer = boom
    with caplog.at_level(logging.WARNING, logger="jacked.api.identity_watcher"):
        assert harness.run() == ("claude-config:identity:divergent",)

    assert "OSError" in caplog.text
    assert "@" not in caplog.text

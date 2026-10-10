"""Tests for the full Claude identity builder and the safe config writer.

Fixtures use the key shapes the live profile endpoint returns
(``account.uuid/email/display_name/full_name/created_at`` and
``organization.uuid/name/billing_type/rate_limit_tier/...``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jacked.api.claude_identity import (
    IdentityBlock,
    WriteResult,
    apply_identity,
    build_identity_block,
    is_same_account,
    read_claude_config_identity,
    write_identity,
)
from jacked.api.credential_helpers import ClaudeConfigUnwritable
from tests._platform import requires_symlinks

ACCOUNT_UUID = "5b0c7a1e-1111-4a2b-9c3d-000000000001"
OTHER_UUID = "9f8e7d6c-2222-4b3c-8d4e-000000000002"
ORG_UUID = "0d1e2f3a-3333-4c4d-9e5f-000000000003"


def _profile(**org_changes) -> dict:
    organization = {
        "uuid": ORG_UUID,
        "name": "Acme Research",
        "organization_type": "claude_max",
        "billing_type": "stripe_subscription",
        "rate_limit_tier": "default_claude_max_20x",
        "seat_tier": None,
        "has_extra_usage_enabled": True,
        "subscription_created_at": "2025-03-01T10:00:00Z",
        "claude_code_trial_ends_at": None,
        "claude_code_trial_duration_days": None,
        "cc_onboarding_flags": {"seen_intro": True},
    }
    organization.update(org_changes)
    return {
        "account": {
            "uuid": ACCOUNT_UUID,
            "email": "Alice@Example.com",
            "display_name": "Alice",
            "full_name": "Alice Example",
            "created_at": "2024-11-05T08:30:00Z",
        },
        "organization": organization,
    }


def _row(**changes) -> dict:
    row = {
        "id": 7,
        "provider": "claude",
        "email": "alice@example.com",
        "display_name": "Alice",
        "organization_uuid": ORG_UUID,
        "organization_name": "Acme Research",
        "rate_limit_tier": "default_claude_max_20x",
        "account_uuid": ACCOUNT_UUID,
        "profile_json": json.dumps(_profile()),
        "profile_fetched_at": 1_760_000_000.25,
    }
    row.update(changes)
    return row


def _write_config(home: Path, data) -> Path:
    path = home / ".claude.json"
    text = data if isinstance(data, str) else json.dumps(data)
    path.write_text(text, encoding="utf-8")
    return path


def _read_config(home: Path) -> dict:
    return json.loads((home / ".claude.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# build_identity_block
# ---------------------------------------------------------------------------


def test_complete_block_maps_every_profile_field() -> None:
    block = build_identity_block(_row())

    assert isinstance(block, IdentityBlock)
    assert block.complete is True
    assert block.account_id == 7
    assert block.email == "alice@example.com"
    assert block.organization_uuid == ORG_UUID
    assert block.account_uuid == ACCOUNT_UUID
    assert block.fields == {
        "accountUuid": ACCOUNT_UUID,
        "emailAddress": "alice@example.com",
        "organizationUuid": ORG_UUID,
        "hasExtraUsageEnabled": True,
        "billingType": "stripe_subscription",
        "accountCreatedAt": "2024-11-05T08:30:00Z",
        "subscriptionCreatedAt": "2025-03-01T10:00:00Z",
        "ccOnboardingFlags": {"seen_intro": True},
        "claudeCodeTrialEndsAt": None,
        "claudeCodeTrialDurationDays": None,
        "seatTier": None,
        "displayName": "Alice",
        "fullName": "Alice Example",
        "profileFetchedAt": 1_760_000_000_250,
        "organizationName": "Acme Research",
        "organizationRateLimitTier": "default_claude_max_20x",
    }


def test_complete_block_defaults_absent_optional_fields_like_claude_code() -> None:
    profile = _profile()
    for key in (
        "has_extra_usage_enabled",
        "subscription_created_at",
        "cc_onboarding_flags",
        "claude_code_trial_ends_at",
        "claude_code_trial_duration_days",
        "seat_tier",
    ):
        profile["organization"].pop(key)
    profile["account"].pop("display_name")
    profile["account"]["full_name"] = ""
    row = _row(
        profile_json=json.dumps(profile),
        profile_fetched_at=None,
        organization_name=None,
        rate_limit_tier=None,
    )

    fields = build_identity_block(row).fields

    assert fields["hasExtraUsageEnabled"] is False
    assert "subscriptionCreatedAt" not in fields
    assert fields["ccOnboardingFlags"] == {}
    assert fields["claudeCodeTrialEndsAt"] is None
    assert fields["claudeCodeTrialDurationDays"] is None
    assert fields["seatTier"] is None
    assert "planDisplayName" not in fields
    assert "displayName" not in fields
    assert "fullName" not in fields
    assert "profileFetchedAt" not in fields
    assert "organizationName" not in fields
    assert "organizationRateLimitTier" not in fields
    assert "userRateLimitTier" not in fields


def test_plan_display_name_is_kept_only_when_it_is_a_non_empty_string() -> None:
    named = build_identity_block(
        _row(profile_json=json.dumps(_profile(plan_display_name="Max 20x")))
    )
    blank = build_identity_block(
        _row(profile_json=json.dumps(_profile(plan_display_name="  ")))
    )
    wrong_type = build_identity_block(
        _row(profile_json=json.dumps(_profile(plan_display_name=20)))
    )

    assert named.fields["planDisplayName"] == "Max 20x"
    assert "planDisplayName" not in blank.fields
    assert "planDisplayName" not in wrong_type.fields


def test_personal_row_uses_the_user_tier_field_and_a_null_org() -> None:
    # Neither the row nor the verified profile names an org, so the RESOLVED
    # org is None and the tier goes under the personal key.
    profile = _profile()
    profile["organization"]["uuid"] = None
    row = _row(
        organization_uuid="",
        organization_name=None,
        rate_limit_tier="default_claude_max_5x",
        profile_json=json.dumps(profile),
    )

    block = build_identity_block(row)

    assert block.organization_uuid is None
    assert block.fields["organizationUuid"] is None
    assert block.fields["userRateLimitTier"] == "default_claude_max_5x"
    assert "organizationRateLimitTier" not in block.fields


def test_empty_row_org_adopts_the_verified_profile_org_and_its_tier_field() -> None:
    # The row has no org yet, but the stored profile passed the email guard
    # and names one. The tier field follows the RESOLVED org, so an org
    # account never publishes its tier under the personal key.
    row = _row(organization_uuid="", rate_limit_tier="default_claude_max_20x")
    profile = _profile(uuid=f"  {ORG_UUID} ")
    row["profile_json"] = json.dumps(profile)

    block = build_identity_block(row)

    assert block.complete is True
    assert block.organization_uuid == ORG_UUID
    assert block.fields["organizationUuid"] == ORG_UUID
    assert block.fields["organizationRateLimitTier"] == "default_claude_max_20x"
    assert "userRateLimitTier" not in block.fields


def test_profile_org_is_not_adopted_when_the_profile_fails_the_email_guard() -> None:
    profile = _profile()
    profile["account"]["email"] = "mallory@example.com"

    block = build_identity_block(
        _row(organization_uuid="", profile_json=json.dumps(profile))
    )

    assert block.complete is False
    assert block.organization_uuid is None
    assert "organizationUuid" not in block.fields


def test_profile_uuid_wins_and_row_uuid_fills_a_missing_one() -> None:
    profile = _profile()
    profile["account"].pop("uuid")

    block = build_identity_block(
        _row(profile_json=json.dumps(profile), account_uuid=OTHER_UUID)
    )

    assert block.fields["accountUuid"] == OTHER_UUID
    assert block.account_uuid == OTHER_UUID


def test_minimal_block_without_a_profile() -> None:
    block = build_identity_block(_row(profile_json=None))

    assert block.complete is False
    assert block.fields == {
        "accountUuid": ACCOUNT_UUID,
        "emailAddress": "alice@example.com",
        "organizationUuid": ORG_UUID,
        "organizationName": "Acme Research",
    }


def test_minimal_block_omits_unknown_uuid_and_org() -> None:
    block = build_identity_block(
        _row(
            profile_json=None,
            account_uuid=None,
            organization_uuid="",
            organization_name=None,
        )
    )

    assert block.complete is False
    assert block.account_uuid is None
    assert block.organization_uuid is None
    assert block.fields == {"emailAddress": "alice@example.com"}


@pytest.mark.parametrize(
    "profile_json",
    [
        "{not json",
        json.dumps(["account"]),
        json.dumps({"account": "x", "organization": {}}),
        json.dumps({"account": {"email": "alice@example.com"}}),
    ],
)
def test_unusable_profile_falls_back_to_the_minimal_block(profile_json) -> None:
    block = build_identity_block(_row(profile_json=profile_json))

    assert block.complete is False
    assert "billingType" not in block.fields


def test_profile_of_another_person_is_ignored(caplog) -> None:
    profile = _profile()
    profile["account"]["email"] = "mallory@example.com"
    caplog.set_level(logging.WARNING, logger="jacked.api.claude_identity")

    block = build_identity_block(_row(profile_json=json.dumps(profile)))

    assert block.complete is False
    assert block.fields["emailAddress"] == "alice@example.com"
    assert "mallory" not in caplog.text
    assert "alice" not in caplog.text


def test_profile_of_another_org_is_ignored() -> None:
    block = build_identity_block(
        _row(profile_json=json.dumps(_profile(uuid=OTHER_UUID)))
    )

    assert block.complete is False


@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_non_claude_provider_returns_none(provider) -> None:
    assert build_identity_block(_row(provider=provider)) is None


def test_missing_provider_means_claude() -> None:
    row = _row()
    row.pop("provider")
    assert build_identity_block(row) is not None


def test_row_without_email_returns_none() -> None:
    assert build_identity_block(_row(email="")) is None


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_accounts(self, include_inactive=False, include_deleted=False):
        self.calls += 1
        return [dict(row) for row in self.rows]

    def get_account(self, account_id):
        for row in self.rows:
            if row["id"] == account_id:
                return dict(row)
        return None


def test_uuid_shared_with_a_different_email_is_omitted(caplog) -> None:
    other = _row(id=9, email="bob@example.com", profile_json=None)
    db = _FakeDb([_row(), other])
    caplog.set_level(logging.WARNING, logger="jacked.api.claude_identity")

    block = build_identity_block(_row(), db=db)

    assert "accountUuid" not in block.fields
    assert block.account_uuid is None
    assert ACCOUNT_UUID[:8] in caplog.text
    assert ACCOUNT_UUID not in caplog.text
    assert "bob@" not in caplog.text and "alice@" not in caplog.text


class _BrokenDb:
    def list_accounts(self, include_inactive=False, include_deleted=False):
        raise RuntimeError("database is locked for alice@example.com")


def test_failed_shared_uuid_lookup_omits_the_uuid_and_logs_no_email(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="jacked.api.claude_identity")

    block = build_identity_block(_row(), db=_BrokenDb())

    assert "accountUuid" not in block.fields
    assert block.account_uuid is None
    assert block.complete is True
    assert ACCOUNT_UUID[:8] in caplog.text
    assert ACCOUNT_UUID not in caplog.text
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "alice" not in messages.lower()


def test_uuid_shared_by_the_same_person_in_two_orgs_is_kept() -> None:
    second_org = _row(id=8, email="ALICE@example.com", organization_uuid=OTHER_UUID)
    db = _FakeDb([_row(), second_org, _row(id=10, provider="codex", email="x@y.z")])

    block = build_identity_block(_row(), db=db)

    assert block.fields["accountUuid"] == ACCOUNT_UUID


def test_uuid_on_a_non_claude_row_does_not_count_as_shared() -> None:
    codex = _row(id=10, provider="codex", email="codex@example.com")
    block = build_identity_block(_row(), db=_FakeDb([_row(), codex]))

    assert block.fields["accountUuid"] == ACCOUNT_UUID


# ---------------------------------------------------------------------------
# is_same_account / apply_identity
# ---------------------------------------------------------------------------


def _existing(**changes) -> dict:
    block = {
        "accountUuid": ACCOUNT_UUID,
        "emailAddress": "alice@example.com",
        "organizationUuid": ORG_UUID,
        "organizationName": "Acme Research",
        "organizationRole": "admin",
        "workspaceRole": None,
        "billingType": "stripe_subscription",
    }
    block.update(changes)
    return block


def test_cross_person_block_is_replaced() -> None:
    config = {
        "numStartups": 3,
        "oauthAccount": _existing(
            accountUuid=OTHER_UUID, emailAddress="bob@example.com"
        ),
    }
    block = build_identity_block(_row())

    assert is_same_account(config["oauthAccount"], block) is False
    apply_identity(config, block)

    assert config["oauthAccount"] == block.fields
    assert "organizationRole" not in config["oauthAccount"]
    assert config["numStartups"] == 3


def test_same_email_in_another_org_is_replaced() -> None:
    config = {"oauthAccount": _existing(organizationUuid=OTHER_UUID)}
    block = build_identity_block(_row())

    apply_identity(config, block)

    assert config["oauthAccount"] == block.fields


def test_same_email_and_org_with_a_different_uuid_is_replaced() -> None:
    config = {"oauthAccount": _existing(accountUuid=OTHER_UUID)}
    block = build_identity_block(_row())

    assert is_same_account(config["oauthAccount"], block) is False
    apply_identity(config, block)
    assert "organizationRole" not in config["oauthAccount"]


def test_same_account_merge_keeps_roles_and_overwrites_known_fields() -> None:
    config = {
        "oauthAccount": _existing(
            emailAddress="Alice@Example.com",
            billingType="old_billing",
        )
    }
    block = build_identity_block(_row())

    assert is_same_account(config["oauthAccount"], block) is True
    apply_identity(config, block)

    merged = config["oauthAccount"]
    assert merged["organizationRole"] == "admin"
    assert merged["workspaceRole"] is None
    assert merged["billingType"] == "stripe_subscription"
    assert merged["accountCreatedAt"] == "2024-11-05T08:30:00Z"
    # A case-only difference is the same identity; the stored spelling stays.
    assert merged["emailAddress"] == "Alice@Example.com"


def test_unknown_row_uuid_merge_keeps_the_existing_uuid_and_fields() -> None:
    config = {"oauthAccount": _existing()}
    block = build_identity_block(_row(account_uuid=None, profile_json=None))
    assert block.account_uuid is None

    apply_identity(config, block)

    assert config["oauthAccount"]["accountUuid"] == ACCOUNT_UUID
    assert config["oauthAccount"]["billingType"] == "stripe_subscription"
    assert config["oauthAccount"]["organizationRole"] == "admin"


def test_existing_block_without_a_uuid_is_replaced_by_a_block_with_one() -> None:
    stale = _existing()
    stale.pop("accountUuid")
    config = {"oauthAccount": stale}
    block = build_identity_block(_row())

    assert is_same_account(stale, block) is False
    apply_identity(config, block)
    assert config["oauthAccount"] == block.fields


@pytest.mark.parametrize("org_value", ["", None, "missing"])
def test_empty_missing_and_null_orgs_are_the_same_org(org_value) -> None:
    existing = _existing()
    if org_value == "missing":
        existing.pop("organizationUuid")
    else:
        existing["organizationUuid"] = org_value
    block = build_identity_block(
        _row(organization_uuid=None, organization_name=None, profile_json=None)
    )

    assert is_same_account(existing, block) is True


def test_non_dict_existing_block_is_replaced() -> None:
    config = {"oauthAccount": "garbage", "theme": "dark"}
    block = build_identity_block(_row())

    assert is_same_account("garbage", block) is False
    apply_identity(config, block)

    assert config["oauthAccount"] == block.fields
    assert config["theme"] == "dark"


def test_apply_does_not_alias_the_block_fields() -> None:
    config: dict = {}
    block = build_identity_block(_row())
    apply_identity(config, block)
    config["oauthAccount"]["ccOnboardingFlags"]["mutated"] = True

    assert "mutated" not in block.fields["ccOnboardingFlags"]


# ---------------------------------------------------------------------------
# write_identity
# ---------------------------------------------------------------------------


def test_write_creates_an_absent_config_with_private_mode(tmp_path) -> None:
    block = build_identity_block(_row())

    result = write_identity(tmp_path, block)

    assert isinstance(result, WriteResult)
    assert result.status == "written"
    assert _read_config(tmp_path)["oauthAccount"] == block.fields
    if os.name != "nt":
        assert ((tmp_path / ".claude.json").stat().st_mode & 0o777) == 0o600
    assert not (tmp_path / ".claude.json.lock").exists()


def test_write_never_touches_other_top_level_keys(tmp_path) -> None:
    _write_config(
        tmp_path,
        {
            "projects": {"/repo": {"allowedTools": ["Bash"]}},
            "userID": "abc",
            "oauthAccount": _existing(accountUuid=OTHER_UUID, emailAddress="bob@x.io"),
        },
    )

    write_identity(tmp_path, build_identity_block(_row()))

    config = _read_config(tmp_path)
    assert config["projects"] == {"/repo": {"allowedTools": ["Bash"]}}
    assert config["userID"] == "abc"
    assert config["oauthAccount"]["emailAddress"] == "alice@example.com"


def test_second_identical_write_is_unchanged(tmp_path) -> None:
    block = build_identity_block(_row())
    write_identity(tmp_path, block)
    before = (tmp_path / ".claude.json").read_bytes()

    result = write_identity(tmp_path, block)

    assert result.status == "unchanged"
    assert (tmp_path / ".claude.json").read_bytes() == before


@pytest.mark.parametrize(
    "torn", ['{"oauthAccount": {"emailAddress": "a', "[1, 2]", "", "null"]
)
def test_unreadable_config_is_left_byte_identical(tmp_path, torn) -> None:
    path = _write_config(tmp_path, torn)
    before = path.read_bytes()

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "unreadable"
    assert path.read_bytes() == before
    assert not (tmp_path / ".claude.json.lock").exists()


def test_directory_at_the_config_path_is_unreadable(tmp_path) -> None:
    (tmp_path / ".claude.json").mkdir()

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "unreadable"


@requires_symlinks
def test_symlinked_config_is_refused(tmp_path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / ".claude.json").symlink_to(target)

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "symlink"
    assert json.loads(target.read_text(encoding="utf-8")) == {}


def test_held_lock_reports_locked_and_leaves_the_lock(tmp_path) -> None:
    path = _write_config(tmp_path, {"oauthAccount": _existing(emailAddress="b@x.io")})
    before = path.read_bytes()
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()

    started = time.monotonic()
    result = write_identity(tmp_path, build_identity_block(_row()), lock_timeout=0.2)

    assert result.status == "locked"
    assert time.monotonic() - started < 2.0
    assert lock.is_dir()
    assert path.read_bytes() == before


def test_stale_empty_lock_is_cleared(tmp_path) -> None:
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    old = time.time() - 60
    os.utime(lock, (old, old))

    result = write_identity(tmp_path, build_identity_block(_row()), lock_timeout=0.2)

    assert result.status == "written"
    assert not lock.exists()


def test_stale_non_empty_lock_dir_is_never_removed(tmp_path) -> None:
    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    (lock / "pid").write_text("123", encoding="utf-8")
    old = time.time() - 60
    os.utime(lock, (old, old))

    result = write_identity(tmp_path, build_identity_block(_row()), lock_timeout=0.2)

    assert result.status == "locked"
    assert (lock / "pid").read_text(encoding="utf-8") == "123"


def test_stale_lock_replaced_after_the_age_check_is_never_removed(
    tmp_path, monkeypatch
) -> None:
    """A holder that re-creates the lock inside the stale window keeps it."""
    from jacked.api import claude_config_store as claude_identity

    lock = tmp_path / ".claude.json.lock"
    lock.mkdir()
    old = time.time() - 60
    os.utime(lock, (old, old))

    real_lstat = os.lstat
    real_rmdir = os.rmdir
    lock_lstats = []
    removed = []

    def racing_lstat(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == lock:
            lock_lstats.append(result)
            if len(lock_lstats) == 1:
                # Between our age check and our rmdir, the stale holder's
                # lock goes away and a live process takes a fresh one.
                real_rmdir(lock)
                lock.mkdir()
                (lock / "marker").write_text("live", encoding="utf-8")
                (lock / "marker").unlink()
        return result

    def spying_rmdir(path, *args, **kwargs):
        removed.append(Path(path))
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(claude_identity.os, "lstat", racing_lstat)
    monkeypatch.setattr(claude_identity.os, "rmdir", spying_rmdir)

    assert claude_identity._clear_stale_lock(lock) is False

    assert lock.is_dir()
    assert lock not in removed
    assert len(lock_lstats) == 2
    assert time.time() - real_lstat(lock).st_mtime < 10


def test_failed_lock_release_logs_a_warning(tmp_path, monkeypatch, caplog) -> None:
    from jacked.api import claude_config_store as claude_identity

    real_rmdir = os.rmdir
    lock = tmp_path / ".claude.json.lock"

    def stuck_rmdir(path, *args, **kwargs):
        if Path(path) == lock:
            raise PermissionError("a scanner holds the directory")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(claude_identity.os, "rmdir", stuck_rmdir)
    monkeypatch.setattr(claude_identity.time, "sleep", lambda _s: None)
    caplog.set_level(logging.WARNING, logger="jacked.api.claude_config_store")

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "written"
    assert "Could not release the Claude config lock" in caplog.text
    assert "alice" not in caplog.text.lower()


class _FakeClock:
    """A monotonic clock that advances only when the writer sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _patch_clock(monkeypatch) -> _FakeClock:
    from jacked.api import claude_config_store as claude_identity

    clock = _FakeClock()
    fake_time = SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep, time=time.time
    )
    monkeypatch.setattr(claude_identity, "time", fake_time)
    return clock


def test_strict_writer_waits_longer_for_the_lock(tmp_path, monkeypatch) -> None:
    (tmp_path / ".claude.json.lock").mkdir()
    clock = _patch_clock(monkeypatch)
    start = clock.now

    with pytest.raises(ClaudeConfigUnwritable):
        write_identity(tmp_path, build_identity_block(_row()), strict=True)

    waited = clock.now - start
    assert waited > 5.0
    assert waited == pytest.approx(10.0, abs=0.1)


def test_non_strict_writer_keeps_the_short_lock_wait(tmp_path, monkeypatch) -> None:
    (tmp_path / ".claude.json.lock").mkdir()
    clock = _patch_clock(monkeypatch)
    start = clock.now

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "locked"
    assert clock.now - start == pytest.approx(5.0, abs=0.1)


def test_explicit_lock_timeout_wins_for_a_strict_writer(tmp_path, monkeypatch) -> None:
    (tmp_path / ".claude.json.lock").mkdir()
    clock = _patch_clock(monkeypatch)
    start = clock.now

    with pytest.raises(ClaudeConfigUnwritable):
        write_identity(
            tmp_path, build_identity_block(_row()), strict=True, lock_timeout=1.0
        )

    assert clock.now - start == pytest.approx(1.0, abs=0.1)


def _windows_mkdir(monkeypatch, failures: int | None):
    """Make the lock mkdir fail like a delete-pending directory on Windows."""
    from jacked.api import claude_config_store as claude_identity

    real_mkdir = os.mkdir
    calls = {"n": 0}

    def flaky_mkdir(path, *args, **kwargs):
        if Path(path).name == ".claude.json.lock":
            calls["n"] += 1
            if failures is None or calls["n"] <= failures:
                raise PermissionError("[WinError 5] Access is denied")
        return real_mkdir(path, *args, **kwargs)

    def must_not_clear(_lock_path):
        raise AssertionError("a delete-pending lock is not a stale lock")

    monkeypatch.setattr(claude_identity, "_is_windows", lambda: True)
    monkeypatch.setattr(claude_identity.os, "mkdir", flaky_mkdir)
    monkeypatch.setattr(claude_identity, "_clear_stale_lock", must_not_clear)
    return calls


def test_windows_delete_pending_lock_is_retried_until_mkdir_succeeds(
    tmp_path, monkeypatch
) -> None:
    calls = _windows_mkdir(monkeypatch, failures=2)

    result = write_identity(tmp_path, build_identity_block(_row()), lock_timeout=2.0)

    assert result.status == "written"
    assert calls["n"] == 3
    assert not (tmp_path / ".claude.json.lock").exists()


def test_windows_delete_pending_lock_past_the_deadline_is_locked(
    tmp_path, monkeypatch
) -> None:
    _windows_mkdir(monkeypatch, failures=None)

    result = write_identity(tmp_path, build_identity_block(_row()), lock_timeout=0.2)

    assert result.status == "locked"
    assert not (tmp_path / ".claude.json").exists()


def test_permission_error_off_windows_is_a_write_failure(tmp_path, monkeypatch) -> None:
    from jacked.api import claude_config_store as claude_identity

    _windows_mkdir(monkeypatch, failures=None)
    monkeypatch.setattr(claude_identity, "_is_windows", lambda: False)

    result = write_identity(tmp_path, build_identity_block(_row()), lock_timeout=0.2)

    assert result.status == "write_failed"


@pytest.mark.parametrize(
    ("os_name", "platform", "expected"),
    [("nt", "win32", True), ("posix", "win32", True), ("nt", "linux", True),
     ("posix", "darwin", False)],
)
def test_is_windows_reads_os_name_and_sys_platform(
    monkeypatch, os_name, platform, expected
) -> None:
    from jacked.api import claude_config_store as claude_identity

    monkeypatch.setattr(
        claude_identity, "os", SimpleNamespace(name=os_name)
    )
    monkeypatch.setattr(claude_identity, "sys", SimpleNamespace(platform=platform))

    assert claude_identity._is_windows() is expected


def test_compare_and_swap_aborts_when_the_block_changed(tmp_path) -> None:
    seen = _existing(accountUuid=OTHER_UUID, emailAddress="bob@x.io")
    current = dict(seen, billingType="changed_underneath")
    path = _write_config(tmp_path, {"oauthAccount": current})
    before = path.read_bytes()

    result = write_identity(tmp_path, build_identity_block(_row()), expected=seen)

    assert result.status == "cas_aborted"
    assert path.read_bytes() == before


def test_compare_and_swap_writes_when_the_block_is_as_expected(tmp_path) -> None:
    seen = _existing(accountUuid=OTHER_UUID, emailAddress="bob@x.io")
    _write_config(tmp_path, {"oauthAccount": seen})

    result = write_identity(tmp_path, build_identity_block(_row()), expected=seen)

    assert result.status == "written"


def test_post_verify_catches_a_concurrent_writer(tmp_path, monkeypatch) -> None:
    from jacked.api import claude_config_store as claude_identity

    real_release = claude_identity._release_lock

    def release_then_clobber(lock_path, inode):
        real_release(lock_path, inode)
        # Another process (a stale Claude Code) writes the old person back.
        _write_config(
            tmp_path,
            {"oauthAccount": _existing(accountUuid=OTHER_UUID, emailAddress="bob@x.io")},
        )

    monkeypatch.setattr(claude_identity, "_release_lock", release_then_clobber)

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "post_verify_mismatch"
    with pytest.raises(ClaudeConfigUnwritable):
        write_identity(tmp_path, build_identity_block(_row()), strict=True)


def test_write_failure_is_reported(tmp_path, monkeypatch) -> None:
    from jacked.api import claude_config_store as claude_identity

    def boom(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(claude_identity, "_safe_replace", boom)

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "write_failed"
    assert not (tmp_path / ".claude.json").exists()
    assert [p.name for p in tmp_path.iterdir()] == []


@pytest.mark.parametrize("status_setup", ["unreadable", "locked", "cas_aborted"])
def test_strict_raises_for_every_failure_status(tmp_path, status_setup) -> None:
    kwargs: dict = {"strict": True, "lock_timeout": 0.1}
    if status_setup == "unreadable":
        _write_config(tmp_path, "{torn")
    elif status_setup == "locked":
        (tmp_path / ".claude.json.lock").mkdir()
    else:
        _write_config(tmp_path, {"oauthAccount": _existing()})
        kwargs["expected"] = {"emailAddress": "someone-else@x.io"}

    with pytest.raises(ClaudeConfigUnwritable) as raised:
        write_identity(tmp_path, build_identity_block(_row()), **kwargs)

    assert status_setup in str(raised.value)
    assert "alice" not in str(raised.value)


@requires_symlinks
def test_strict_raises_for_a_symlinked_config(tmp_path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / ".claude.json").symlink_to(target)

    with pytest.raises(ClaudeConfigUnwritable) as raised:
        write_identity(tmp_path, build_identity_block(_row()), strict=True)

    assert "symlink" in str(raised.value)
    assert target.read_text(encoding="utf-8") == "{}"


def test_strict_raises_for_a_failed_write(tmp_path, monkeypatch) -> None:
    from jacked.api import claude_config_store as claude_identity

    def boom(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(claude_identity, "_safe_replace", boom)

    with pytest.raises(ClaudeConfigUnwritable) as raised:
        write_identity(tmp_path, build_identity_block(_row()), strict=True)

    assert "write_failed" in str(raised.value)
    assert not (tmp_path / ".claude.json").exists()


def test_strict_does_not_raise_for_written_or_unchanged(tmp_path) -> None:
    block = build_identity_block(_row())
    assert write_identity(tmp_path, block, strict=True).status == "written"
    assert write_identity(tmp_path, block, strict=True).status == "unchanged"


def test_write_logs_no_email_or_full_uuid(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="jacked.api.claude_config_store")
    _write_config(tmp_path, "{torn")
    write_identity(tmp_path, build_identity_block(_row()))
    (tmp_path / ".claude.json").unlink()
    write_identity(tmp_path, build_identity_block(_row()))

    assert "alice" not in caplog.text.lower()
    assert ACCOUNT_UUID not in caplog.text


# ---------------------------------------------------------------------------
# read_claude_config_identity
# ---------------------------------------------------------------------------


def test_read_identity_returns_the_block(tmp_path) -> None:
    _write_config(tmp_path, {"oauthAccount": _existing()})
    assert read_claude_config_identity(tmp_path) == _existing()


@pytest.mark.parametrize(
    "content", [None, "{torn", "[1]", json.dumps({"theme": "dark"}),
                json.dumps({"oauthAccount": "x"})]
)
def test_read_identity_returns_none_for_anything_else(tmp_path, content) -> None:
    if content is not None:
        _write_config(tmp_path, content)
    assert read_claude_config_identity(tmp_path) is None


@requires_symlinks
def test_read_identity_refuses_a_symlink(tmp_path) -> None:
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"oauthAccount": _existing()}), encoding="utf-8")
    (tmp_path / ".claude.json").symlink_to(target)

    assert read_claude_config_identity(tmp_path) is None


# ---------------------------------------------------------------------------
# Publisher (runtime) builds from the row
# ---------------------------------------------------------------------------


def _request(**changes):
    values = {
        "account_id": 7,
        "email": "alice@example.com",
        "organization_id": ORG_UUID,
        "display_name": "Alice",
        "organization_name": "Acme Research",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_publisher_with_db_writes_the_complete_block(tmp_path) -> None:
    from jacked.credentials.runtime import claude_config_identity_publisher

    _write_config(
        tmp_path,
        {"oauthAccount": _existing(accountUuid=OTHER_UUID, emailAddress="bob@x.io")},
    )
    publish = claude_config_identity_publisher(tmp_path, _FakeDb([_row()]))

    publish(_request())

    block = _read_config(tmp_path)["oauthAccount"]
    assert block["accountUuid"] == ACCOUNT_UUID
    assert block["billingType"] == "stripe_subscription"
    assert block["accountCreatedAt"] == "2024-11-05T08:30:00Z"
    assert block["profileFetchedAt"] == 1_760_000_000_250
    assert "organizationRole" not in block


def test_publisher_without_a_row_falls_back_to_the_request(tmp_path) -> None:
    from jacked.credentials.runtime import claude_config_identity_publisher

    publish = claude_config_identity_publisher(tmp_path, _FakeDb([]))
    publish(_request())

    block = _read_config(tmp_path)["oauthAccount"]
    assert block["emailAddress"] == "alice@example.com"
    assert block["organizationUuid"] == ORG_UUID
    assert "accountUuid" not in block


def test_publisher_without_db_uses_the_request(tmp_path) -> None:
    from jacked.credentials.runtime import claude_config_identity_publisher

    claude_config_identity_publisher(tmp_path)(_request())

    assert _read_config(tmp_path)["oauthAccount"]["emailAddress"] == "alice@example.com"


def test_publisher_ignores_a_row_that_no_longer_matches_the_request(tmp_path) -> None:
    from jacked.credentials.runtime import claude_config_identity_publisher

    publish = claude_config_identity_publisher(
        tmp_path, _FakeDb([_row(email="renamed@example.com")])
    )
    publish(_request())

    block = _read_config(tmp_path)["oauthAccount"]
    assert block["emailAddress"] == "alice@example.com"
    assert "billingType" not in block


def test_publisher_is_strict(tmp_path) -> None:
    from jacked.credentials.runtime import claude_config_identity_publisher

    _write_config(tmp_path, "{torn")
    with pytest.raises(ClaudeConfigUnwritable):
        claude_config_identity_publisher(tmp_path, _FakeDb([_row()]))(_request())


def test_publisher_refuses_a_non_claude_row(tmp_path) -> None:
    from jacked.credentials.runtime import claude_config_identity_publisher

    publish = claude_config_identity_publisher(tmp_path, _FakeDb([_row(provider="codex")]))
    with pytest.raises(ClaudeConfigUnwritable):
        publish(_request())
    assert not (tmp_path / ".claude.json").exists()


def test_engine_gets_a_db_backed_publisher(tmp_path, monkeypatch) -> None:
    from jacked.credentials import runtime
    from tests.unit.test_credential_runtime import _identity

    seen = {}

    def fake_publisher(home, db=None):
        seen["db"] = db
        return lambda request: None

    monkeypatch.setattr(runtime, "claude_config_identity_publisher", fake_publisher)
    db = object()
    runtime._engine_for(
        db,
        runtime.SHIPPED_REGISTRY.resolve(_identity(platform_system="linux")),
        tmp_path,
    )

    assert seen["db"] is db

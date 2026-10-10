"""Tests for the Claude config store: path, lock, compare-and-swap, verify."""

from __future__ import annotations

import json
import logging
import os
import time

import pytest

from jacked.api import claude_config_store as store
from jacked.api.claude_identity import (
    COMPLETENESS_GATE_FIELDS,
    apply_identity,
    build_identity_block,
    minimal_identity_block,
    write_identity,
)

UUID = "5b0c7a1e-1111-4a2b-9c3d-000000000001"
ORG = "0d1e2f3a-3333-4c4d-9e5f-000000000003"


def _profile(**org) -> dict:
    organization = {
        "uuid": ORG,
        "name": "Acme",
        "billing_type": "stripe_subscription",
        "rate_limit_tier": "default_claude_max_20x",
        "has_extra_usage_enabled": True,
        "subscription_created_at": "2025-03-01T10:00:00Z",
    }
    organization.update(org)
    return {
        "account": {
            "uuid": UUID,
            "email": "alice@example.com",
            "display_name": "Alice",
            "created_at": "2024-11-05T08:30:00Z",
        },
        "organization": organization,
    }


def _row(**overrides) -> dict:
    row = {
        "id": 4,
        "provider": "claude",
        "email": "alice@example.com",
        "organization_uuid": ORG,
        "organization_name": "Acme",
        "account_uuid": UUID,
        "profile_json": json.dumps(_profile()),
        "profile_fetched_at": 1_800_000_000.0,
        "rate_limit_tier": "default_claude_max_20x",
    }
    row.update(overrides)
    return row


def _write(home, data) -> None:
    (home / ".claude.json").write_text(json.dumps(data), encoding="utf-8")


def _read(home) -> dict:
    return json.loads((home / ".claude.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_custom_oauth(monkeypatch):
    monkeypatch.delenv(store.CUSTOM_OAUTH_ENV, raising=False)


# --- path -----------------------------------------------------------------


def test_the_default_config_is_home_dot_claude_json(tmp_path):
    assert store.claude_config_path(tmp_path) == tmp_path / ".claude.json"


def test_a_legacy_config_json_wins_like_claude_code(tmp_path):
    legacy = tmp_path / ".claude" / ".config.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"oauthAccount": {"emailAddress": "x@y.z"}}))

    assert store.claude_config_path(tmp_path) == legacy
    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "written"
    assert json.loads(legacy.read_text())["oauthAccount"]["emailAddress"] == "alice@example.com"
    assert not (tmp_path / ".claude.json").exists()


def test_a_custom_oauth_endpoint_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setenv(store.CUSTOM_OAUTH_ENV, "https://example.invalid")
    _write(tmp_path, {"theme": "dark"})

    result = write_identity(tmp_path, build_identity_block(_row()))

    assert result.status == "unsupported"
    assert _read(tmp_path) == {"theme": "dark"}
    assert store.read_config_identity(tmp_path) is None


def test_strict_raises_when_unsupported(tmp_path, monkeypatch):
    from jacked.api.credential_helpers import ClaudeConfigUnwritable

    monkeypatch.setenv(store.CUSTOM_OAUTH_ENV, "https://example.invalid")
    with pytest.raises(ClaudeConfigUnwritable):
        write_identity(tmp_path, build_identity_block(_row()), strict=True)


# --- whole-file compare-and-swap, hold limit, fsync -------------------------


def test_a_change_to_any_key_during_the_write_aborts_it(tmp_path, monkeypatch):
    _write(tmp_path, {"projects": {"a": 1}, "oauthAccount": {"emailAddress": "z@q.io"}})
    real_apply = None

    def racing_apply(config):
        # Claude Code saves another key without the lock in the meantime.
        _write(tmp_path, {"projects": {"a": 1, "b": 2}, "oauthAccount": {"emailAddress": "z@q.io"}})
        os.utime(tmp_path / ".claude.json", ns=(1, 1))
        return real_apply(config)

    block = build_identity_block(_row())
    from jacked.api import claude_identity

    real_apply = lambda config: claude_identity._apply(config, block)  # noqa: E731
    result = store.write_config_identity(
        tmp_path,
        racing_apply,
        lambda current: [],
        account_id=4,
        expected=None,
        lock_timeout=1.0,
    )

    assert result.status == "cas_aborted"
    assert _read(tmp_path)["projects"] == {"a": 1, "b": 2}


def test_a_lock_held_past_the_hold_limit_is_not_replaced(tmp_path, monkeypatch):
    _write(tmp_path, {"oauthAccount": {"emailAddress": "z@q.io"}})
    # The lock was taken at t=0; the replace would happen past the limit.
    monkeypatch.setattr(store.time, "monotonic", lambda: store.MAX_HOLD_SECONDS + 1.0)

    result = store._write_locked(
        tmp_path / ".claude.json",
        lambda config: config.update({"oauthAccount": {"emailAddress": "a@b.c"}}) or "replace",
        None,
        4,
        0.0,
    )

    assert result.status == "locked"
    assert _read(tmp_path)["oauthAccount"]["emailAddress"] == "z@q.io"


def test_the_write_is_fsynced_before_the_replace(tmp_path, monkeypatch):
    order: list[str] = []
    real_fsync = os.fsync
    real_replace = store._safe_replace

    monkeypatch.setattr(store.os, "fsync", lambda fd: order.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(
        store,
        "_safe_replace",
        lambda src, dst, **kw: order.append("replace") or real_replace(src, dst, **kw),
    )

    assert write_identity(tmp_path, build_identity_block(_row())).status == "written"
    assert order == ["fsync", "replace"]


# --- lock ownership and verify ----------------------------------------------


def test_a_lock_taken_over_by_another_process_is_left_in_place(tmp_path, caplog):
    lock = tmp_path / ".claude.json.lock"
    inode = store._acquire_lock(lock, 1.0)
    os.rmdir(lock)
    os.mkdir(lock)  # another process now owns a fresh lock

    with caplog.at_level(logging.WARNING, logger="jacked.api.claude_config_store"):
        store._release_lock(lock, inode)

    assert lock.exists()
    assert "taken over" in caplog.text


def test_the_post_write_verify_retries_a_torn_read(tmp_path, monkeypatch):
    reads = iter([None, {"emailAddress": "alice@example.com"}])
    monkeypatch.setattr(store, "read_config_identity", lambda home, **kw: next(reads))
    monkeypatch.setattr(store.time, "sleep", lambda s: None)

    assert store._verify_read(tmp_path) == {"emailAddress": "alice@example.com"}


def test_stale_temp_files_are_swept_and_fresh_ones_kept(tmp_path):
    old = tmp_path / f"{store.TMP_PREFIX}old.json"
    new = tmp_path / f"{store.TMP_PREFIX}new.json"
    other = tmp_path / "keep.json"
    for path in (old, new, other):
        path.write_text("{}")
    past = time.time() - 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))

    assert store.sweep_stale_temp_files(tmp_path) == 1
    assert not old.exists()
    assert new.exists() and other.exists()


# --- builder: Claude Code's own shapes --------------------------------------


def test_missing_billing_type_and_created_at_are_left_out(tmp_path):
    profile = _profile(billing_type=None)
    profile["account"].pop("created_at")
    block = build_identity_block(_row(profile_json=json.dumps(profile)))

    assert "billingType" not in block.fields
    assert "accountCreatedAt" not in block.fields
    assert block.complete is False


def test_a_block_is_complete_only_with_every_gate_field():
    assert build_identity_block(_row()).complete is True
    no_sub = build_identity_block(
        _row(profile_json=json.dumps(_profile(subscription_created_at=None)))
    )
    assert "subscriptionCreatedAt" not in no_sub.fields
    assert no_sub.complete is False
    assert set(COMPLETENESS_GATE_FIELDS) <= set(build_identity_block(_row()).fields)


def test_a_merge_keeps_profile_fields_claude_code_fetched_later():
    block = build_identity_block(_row())
    newer = block.fields["profileFetchedAt"] + 60_000
    config = {
        "oauthAccount": {
            "accountUuid": UUID,
            "emailAddress": "alice@example.com",
            "organizationUuid": ORG,
            "hasExtraUsageEnabled": False,
            "seatTier": "team_premium",
            "profileFetchedAt": newer,
        }
    }

    apply_identity(config, block)

    merged = config["oauthAccount"]
    assert merged["hasExtraUsageEnabled"] is False
    assert merged["seatTier"] == "team_premium"
    assert merged["profileFetchedAt"] == newer
    assert merged["organizationName"] == "Acme"


def test_a_merge_over_an_older_fetch_writes_the_stored_profile():
    block = build_identity_block(_row())
    config = {
        "oauthAccount": {
            "accountUuid": UUID,
            "emailAddress": "alice@example.com",
            "organizationUuid": ORG,
            "hasExtraUsageEnabled": False,
            "profileFetchedAt": block.fields["profileFetchedAt"] - 60_000,
        }
    }

    apply_identity(config, block)

    assert config["oauthAccount"]["hasExtraUsageEnabled"] is True


def test_a_minimal_block_never_claims_complete():
    assert minimal_identity_block(1, "a@b.c", ORG).complete is False


# --- test tripwires (tests/conftest.py) ---------------------------------------


def test_the_tripwire_refuses_the_real_config(monkeypatch):
    from tests.conftest import REAL_HOME

    monkeypatch.setenv("HOME", str(REAL_HOME))
    with pytest.raises(RuntimeError, match="test tripwire"):
        store._atomic_write_json(REAL_HOME / ".claude.json", {})
    with pytest.raises(RuntimeError, match="test tripwire"):
        store._acquire_lock(REAL_HOME / ".claude.json.lock", 0.0)


def test_the_tripwire_refuses_real_anthropic_requests():
    import httpx

    with pytest.raises(RuntimeError, match="test tripwire"):
        httpx.get("https://api.anthropic.com/api/oauth/profile")


def test_the_service_watcher_is_inert_under_test():
    from jacked.api import session_observer

    assert session_observer.IDENTITY_WATCHER.submit(None, None, None) is False


def test_the_debug_route_reports_the_watcher_and_coverage(tmp_path):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from jacked.api.routes.system import router
    from jacked.web.database import Database

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.db = Database(str(tmp_path / "t.db"))
    app.state.db.create_account("u@test.com", "tok", 9999999999)

    body = TestClient(app).get("/api/admin/debug/identity-watcher").json()

    assert body["watcher"] == {"disabled": False, "accounts": {}}
    assert body["coverage"]["total"] == 1
    assert body["coverage"]["without_profile"] == 1


def test_a_scoped_dir_uses_its_own_legacy_config(tmp_path):
    (tmp_path / ".config.json").write_text("{}")
    assert store.claude_config_path(tmp_path, scoped=True) == tmp_path / ".config.json"
    assert store.claude_config_path(tmp_path) == tmp_path / ".claude.json"

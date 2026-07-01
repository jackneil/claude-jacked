"""Tests for the in-process usage-change signal (jacked.usage_events).

The menu-bar pill watches usage_events.version() on a 1s timer; the write
paths must bump it so a dashboard-triggered refresh or an account switch
shows up in the tray within a second instead of the 30s poll heartbeat.
"""

import threading

from jacked import usage_events
from jacked.web.database import Database


class TestUsageEventsPrimitive:
    def test_bump_is_monotonic(self):
        before = usage_events.version()
        assert usage_events.bump() == before + 1
        assert usage_events.version() == before + 1

    def test_bump_returns_new_version(self):
        v1 = usage_events.bump()
        v2 = usage_events.bump()
        assert v2 == v1 + 1

    def test_concurrent_bumps_never_lose_a_count(self):
        before = usage_events.version()
        n_threads, bumps_each = 8, 250

        def hammer():
            for _ in range(bumps_each):
                usage_events.bump()

        threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert usage_events.version() == before + n_threads * bumps_each


class TestWritePathsBump:
    def test_usage_cache_write_bumps(self):
        db = Database(":memory:")
        acct = db.create_account("u@t.com", "tok", 9999999999)
        before = usage_events.version()
        assert db.update_account_usage_cache(acct["id"], five_hour=42.0)
        assert usage_events.version() == before + 1

    def test_failed_usage_cache_write_does_not_bump(self):
        db = Database(":memory:")
        before = usage_events.version()
        assert not db.update_account_usage_cache(999999, five_hour=42.0)
        assert usage_events.version() == before

    def test_credential_sync_bumps(self, tmp_path, monkeypatch):
        # Point every credential store at a sandbox HOME so the sync's file
        # writes can't touch the real ~/.claude.
        monkeypatch.setenv("HOME", str(tmp_path))
        import pathlib

        monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
        from jacked.api import credential_helpers

        monkeypatch.setattr(
            credential_helpers, "write_platform_credentials", lambda data: None
        )
        monkeypatch.setattr(
            credential_helpers,
            "update_claude_config_email",
            lambda *a, **k: None,
        )

        account = {
            "email": "u@t.com",
            "cc_access_token": "at",
            "cc_refresh_token": "rt",
            "cc_expires_at": 9999999999,
        }
        before = usage_events.version()
        credential_helpers.sync_credential_to_all_stores(1, account)
        assert usage_events.version() == before + 1

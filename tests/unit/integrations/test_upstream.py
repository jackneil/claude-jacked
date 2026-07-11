"""Unit tests for the agent-reach upstream freshness check (offline)."""

from pathlib import Path
from unittest import mock

import pytest

from jacked.integrations import upstream as up

# Captured before any monkeypatch so the scoping test can exercise the real fn.
_ORIG_CACHE_PATH = up._cache_path

PIN = "b" * 40
HEAD = "e" * 40
UPSTREAM = "https://github.com/Panniantong/Agent-Reach"


def test_cache_path_is_scoped_to_injected_home(tmp_path):
    # _cache_path derives from home (LOW-3): an injected home keeps the cache out
    # of the real ~/.claude; None falls back to the real home.
    assert _ORIG_CACHE_PATH(tmp_path / "injected") == \
        tmp_path / "injected" / ".claude" / "jacked-reach-upstream-cache.json"
    assert _ORIG_CACHE_PATH(None) == Path.home() / ".claude" / "jacked-reach-upstream-cache.json"


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    # Route the cache to a tmp file regardless of the home passed in, so no test
    # touches a real ~/.claude.
    monkeypatch.setattr(up, "_cache_path", lambda home: tmp_path / "reach-upstream-cache.json")


def test_api_url_mapping():
    assert up._api_url(UPSTREAM, "main") == "https://api.github.com/repos/Panniantong/Agent-Reach/commits/main"
    assert up._api_url("https://github.com/o/r.git", "main").endswith("/repos/o/r/commits/main")
    assert up._api_url("not-a-github-url", "main") is None


def test_behind_when_head_differs():
    with mock.patch.object(up, "_fetch_head_sha", return_value=HEAD):
        out = up.check_upstream(PIN, UPSTREAM, now=1000.0)
    assert out == {"head_sha": HEAD, "behind": True, "checked_at": 1000.0}


def test_current_when_head_matches():
    with mock.patch.object(up, "_fetch_head_sha", return_value=PIN):
        out = up.check_upstream(PIN, UPSTREAM, now=1000.0)
    assert out["behind"] is False


def test_unknown_returns_none_not_false_current():
    """A failed fetch must report None (could not check), never a false 'current'."""
    with mock.patch.object(up, "_fetch_head_sha", return_value=None):
        out = up.check_upstream(PIN, UPSTREAM, now=1000.0)
    assert out is None


def test_cache_hit_avoids_second_fetch():
    with mock.patch.object(up, "_fetch_head_sha", return_value=HEAD) as fetch:
        up.check_upstream(PIN, UPSTREAM, now=1000.0)
        up.check_upstream(PIN, UPSTREAM, now=1000.0 + 60)  # within TTL
    assert fetch.call_count == 1


def test_cached_probe_failure_returns_none_not_false_current():
    """A cached probe FAILURE (head_sha None) within TTL must report None, not a
    false {behind: False} 'up to date'."""
    with mock.patch.object(up, "_fetch_head_sha", return_value=None) as fetch:
        first = up.check_upstream(PIN, UPSTREAM, now=1000.0)   # caches the failure
        second = up.check_upstream(PIN, UPSTREAM, now=1000.0 + 60)  # cache hit
    assert first is None
    assert second is None
    assert fetch.call_count == 1  # served from cache, not re-fetched


def test_pin_bump_invalidates_cache():
    with mock.patch.object(up, "_fetch_head_sha", return_value=HEAD) as fetch:
        up.check_upstream(PIN, UPSTREAM, now=1000.0)
        up.check_upstream("c" * 40, UPSTREAM, now=1000.0 + 60)  # different pin -> refetch
    assert fetch.call_count == 2

"""Unit tests for the agent-reach upstream freshness check (offline)."""

from unittest import mock

import pytest

from jacked.integrations import upstream as up


PIN = "b" * 40
HEAD = "e" * 40
UPSTREAM = "https://github.com/Panniantong/Agent-Reach"


@pytest.fixture(autouse=True)
def _tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "CACHE_PATH", tmp_path / "reach-upstream-cache.json")


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


def test_pin_bump_invalidates_cache():
    with mock.patch.object(up, "_fetch_head_sha", return_value=HEAD) as fetch:
        up.check_upstream(PIN, UPSTREAM, now=1000.0)
        up.check_upstream("c" * 40, UPSTREAM, now=1000.0 + 60)  # different pin -> refetch
    assert fetch.call_count == 2

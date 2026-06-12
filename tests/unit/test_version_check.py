"""Tests for jacked.version_check module."""

import json
import time
from unittest.mock import patch, MagicMock

from jacked import version_check as vc


class TestParseVersionTuple:
    """Tests for _parse_version_tuple() module-level helper."""

    def test_parse_version_tuple_basic(self):
        assert vc._parse_version_tuple("0.45.3") == (0, 45, 3)

    def test_parse_version_tuple_strips_local(self):
        assert vc._parse_version_tuple("0.45.3+local.dev") == (0, 45, 3)

    def test_parse_version_tuple_strips_dash_suffix(self):
        assert vc._parse_version_tuple("0.45.3-beta") == (0, 45, 3)

    def test_parse_version_tuple_stops_at_non_numeric(self):
        assert vc._parse_version_tuple("0.45.3.dev1") == (0, 45, 3)

    def test_parse_version_tuple_unparseable_returns_empty(self):
        assert vc._parse_version_tuple("xyz") == ()


class TestIsNewer:
    """Tests for is_newer() version comparison."""

    def test_newer_patch(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("0.3.12", "0.3.11")
        True
        """
        assert vc.is_newer("0.3.12", "0.3.11") is True

    def test_newer_minor(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("0.4.0", "0.3.11")
        True
        """
        assert vc.is_newer("0.4.0", "0.3.11") is True

    def test_newer_major(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("1.0.0", "0.3.11")
        True
        """
        assert vc.is_newer("1.0.0", "0.3.11") is True

    def test_equal(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("0.3.11", "0.3.11")
        False
        """
        assert vc.is_newer("0.3.11", "0.3.11") is False

    def test_older(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("0.3.10", "0.3.11")
        False
        """
        assert vc.is_newer("0.3.10", "0.3.11") is False

    def test_malformed_latest(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("abc", "0.3.11")
        False
        """
        assert vc.is_newer("abc", "0.3.11") is False

    def test_malformed_current(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("0.3.12", "xyz")
        False
        """
        assert vc.is_newer("0.3.12", "xyz") is False

    def test_prerelease_still_compares(self):
        """Pre-release versions parse leading numeric parts and compare correctly.

        >>> from jacked.version_check import is_newer
        >>> is_newer("0.4.0rc1", "0.3.11")
        True
        """
        # "0.4.0rc1" → (0, 4) which is > (0, 3, 11)
        assert vc.is_newer("0.4.0rc1", "0.3.11") is True

    def test_empty_strings(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer("", "0.3.11")
        False
        """
        assert vc.is_newer("", "0.3.11") is False

    def test_none_input(self):
        """
        >>> from jacked.version_check import is_newer
        >>> is_newer(None, "0.3.11")
        False
        """
        assert vc.is_newer(None, "0.3.11") is False

    def test_dev_version_current(self):
        """Dev suffix on current version is stripped before comparison.

        >>> from jacked.version_check import is_newer
        >>> is_newer("0.5.0", "0.3.11.dev1")
        True
        """
        assert vc.is_newer("0.5.0", "0.3.11.dev1") is True

    def test_local_version_current(self):
        """Local suffix on current version is stripped before comparison.

        >>> from jacked.version_check import is_newer
        >>> is_newer("0.5.0", "0.3.11+local")
        True
        """
        assert vc.is_newer("0.5.0", "0.3.11+local") is True

    def test_dev_version_not_newer_when_equal_base(self):
        """Dev build of same base version is not considered newer.

        >>> from jacked.version_check import is_newer
        >>> is_newer("0.3.11", "0.3.11.dev1")
        False
        """
        assert vc.is_newer("0.3.11", "0.3.11.dev1") is False

    def test_hyphen_prerelease_stripped(self):
        """Hyphen-separated pre-release suffixes are stripped.

        >>> from jacked.version_check import is_newer
        >>> is_newer("0.5.0", "0.3.11-beta1")
        True
        """
        assert vc.is_newer("0.5.0", "0.3.11-beta1") is True


class TestGetLatestPypiVersion:
    """Tests for get_latest_pypi_version() with mocked network."""

    def test_success(self):
        """Successful PyPI response returns version string.

        >>> # With mock, get_latest_pypi_version returns the version from JSON
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "info": {"version": "0.4.0"},
            }
        ).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = vc.get_latest_pypi_version()
        assert result == "0.4.0"

    def test_timeout(self):
        """Network timeout returns None.

        >>> # Timeout returns None gracefully
        """
        from urllib.error import URLError

        with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
            result = vc.get_latest_pypi_version()
        assert result is None

    def test_bad_json(self):
        """Garbage response returns None.

        >>> # Bad JSON returns None gracefully
        """
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json at all"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = vc.get_latest_pypi_version()
        assert result is None

    def test_missing_info_key(self):
        """PyPI response missing 'info' key returns None.

        >>> # Missing structure returns None
        """
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"unexpected": "data"}).encode(
            "utf-8"
        )
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = vc.get_latest_pypi_version()
        assert result is None

    def test_connection_refused(self):
        """Connection refused returns None.

        >>> # Connection error returns None gracefully
        """
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            result = vc.get_latest_pypi_version()
        assert result is None


class TestGetLatestFromSimpleIndex:
    """Tests for get_latest_from_simple_index() with mocked network."""

    # Real-shaped PEP 691 JSON fixture (trimmed)
    SIMPLE_FIXTURE = {
        "name": "claude-jacked",
        "files": [
            {"filename": "claude_jacked-0.45.0-py3-none-any.whl", "url": "..."},
            {"filename": "claude_jacked-0.45.0.tar.gz", "url": "..."},
            {"filename": "claude_jacked-0.45.1-py3-none-any.whl", "url": "..."},
            {"filename": "claude_jacked-0.45.2-py3-none-any.whl", "url": "..."},
            {"filename": "claude_jacked-0.45.3-py3-none-any.whl", "url": "..."},
        ],
    }

    @staticmethod
    def _mock_urlopen_response(payload):
        """Build a context-manager-compatible urlopen mock returning JSON bytes."""
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    def test_get_latest_from_simple_parses_pep691(self):
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen_response(self.SIMPLE_FIXTURE),
        ):
            assert vc.get_latest_from_simple_index("claude-jacked") == "0.45.3"

    def test_get_latest_from_simple_picks_max_not_first(self):
        """Files listed in arbitrary order — must pick semver max."""
        shuffled = {
            "name": "claude-jacked",
            "files": [
                {"filename": "claude_jacked-0.10.0-py3-none-any.whl"},
                {"filename": "claude_jacked-0.45.3-py3-none-any.whl"},
                {"filename": "claude_jacked-0.45.0-py3-none-any.whl"},
                {"filename": "claude_jacked-0.9.0-py3-none-any.whl"},
            ],
        }
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen_response(shuffled),
        ):
            assert vc.get_latest_from_simple_index("claude-jacked") == "0.45.3"

    def test_get_latest_from_simple_handles_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            assert vc.get_latest_from_simple_index("claude-jacked") is None

    def test_get_latest_from_simple_handles_bad_json(self):
        resp = MagicMock()
        resp.read.return_value = b"not json {{"
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=cm):
            assert vc.get_latest_from_simple_index("claude-jacked") is None

    def test_get_latest_from_simple_empty_files_returns_none(self):
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen_response(
                {"name": "claude-jacked", "files": []}
            ),
        ):
            assert vc.get_latest_from_simple_index("claude-jacked") is None

    def test_get_latest_from_simple_returns_exact_version_string(self):
        """Regression: regex must capture ONLY the version, not '-py3-none-any' tail.
        Without this assertion the bug stays silent because parse_version_tuple
        recovers a clean tuple."""
        payload = {
            "name": "claude-jacked",
            "files": [
                {"filename": "claude_jacked-0.45.3-py3-none-any.whl"},
                {"filename": "claude_jacked-0.45.3.tar.gz"},
            ],
        }
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen_response(payload),
        ):
            result = vc.get_latest_from_simple_index("claude-jacked")
        assert result == "0.45.3", f"Expected exact '0.45.3', got {result!r}"

    def test_get_latest_from_simple_skips_yanked(self):
        """Yanked releases must NOT be advertised as available — uv refuses them."""
        payload = {
            "name": "claude-jacked",
            "files": [
                {"filename": "claude_jacked-0.45.0-py3-none-any.whl", "yanked": False},
                {"filename": "claude_jacked-0.45.3-py3-none-any.whl", "yanked": True},
                {"filename": "claude_jacked-0.45.3.tar.gz", "yanked": "broken release"},
                {"filename": "claude_jacked-0.45.2-py3-none-any.whl", "yanked": False},
            ],
        }
        with patch(
            "urllib.request.urlopen",
            return_value=self._mock_urlopen_response(payload),
        ):
            assert vc.get_latest_from_simple_index("claude-jacked") == "0.45.2"


class TestCheckVersionCached:
    """Tests for check_version_cached() with mocked cache and network."""

    def test_fresh_cache_no_network(self, tmp_path):
        """Fresh new-schema cache prevents network calls.

        >>> # Cache within TTL skips PyPI + /simple/
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time(),
                    "latest": "0.4.0",
                    "installable_latest": "0.4.0",
                    "pypi_latest": "0.4.0",
                    "simple_latest": "0.4.0",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(vc, "get_latest_pypi_version") as mock_pypi, patch.object(
                vc, "get_latest_from_simple_index"
            ) as mock_simple:
                result = vc.check_version_cached("0.3.11")
                mock_pypi.assert_not_called()
                mock_simple.assert_not_called()

        assert result["latest"] == "0.4.0"
        assert result["installable_latest"] == "0.4.0"
        assert result["outdated"] is True
        assert result["ahead"] is False
        assert "checked_at" in result
        assert "next_check_at" in result

    def test_fresh_cache_not_outdated(self, tmp_path):
        """Fresh new-schema cache shows not outdated when versions match.

        >>> # Same version = not outdated
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time(),
                    "latest": "0.3.11",
                    "installable_latest": "0.3.11",
                    "pypi_latest": "0.3.11",
                    "simple_latest": "0.3.11",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.3.11"
        assert result["outdated"] is False
        assert result["ahead"] is False

    def test_fresh_cache_ahead_of_pypi(self, tmp_path):
        """Local version ahead of PyPI shows ahead=True.

        >>> # Local 0.4.0 > PyPI 0.3.10 = ahead
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time(),
                    "latest": "0.3.10",
                    "installable_latest": "0.3.10",
                    "pypi_latest": "0.3.10",
                    "simple_latest": "0.3.10",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            result = vc.check_version_cached("0.4.0")

        assert result["latest"] == "0.3.10"
        assert result["outdated"] is False
        assert result["ahead"] is True

    def test_stale_cache_hits_pypi(self, tmp_path):
        """Stale cache (>24h) triggers PyPI + /simple/ re-probe.

        >>> # Old cache forces network call
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time() - 90000,  # >24h ago
                    "latest": "0.3.10",
                    "installable_latest": "0.3.10",
                    "pypi_latest": "0.3.10",
                    "simple_latest": "0.3.10",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value="0.4.0"
            ), patch.object(
                vc, "get_latest_from_simple_index", return_value="0.4.0"
            ):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.4.0"
        assert result["installable_latest"] == "0.4.0"
        assert result["outdated"] is True

    def test_no_cache_hits_pypi(self, tmp_path):
        """Missing cache file triggers PyPI + /simple/ probes.

        >>> # No cache = network call
        """
        cache_file = tmp_path / "nonexistent-cache.json"

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value="0.3.11"
            ), patch.object(
                vc, "get_latest_from_simple_index", return_value="0.3.11"
            ):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.3.11"
        assert result["installable_latest"] == "0.3.11"
        assert result["outdated"] is False
        # Verify cache was written with the new schema
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        assert cached["latest"] == "0.3.11"
        assert cached["installable_latest"] == "0.3.11"
        assert cached["pypi_latest"] == "0.3.11"
        assert cached["simple_latest"] == "0.3.11"

    def test_corrupt_cache_hits_pypi(self, tmp_path):
        """Corrupt cache file triggers PyPI + /simple/ re-probe.

        >>> # Bad JSON in cache = network call
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text("not valid json {{{")

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value="0.4.0"
            ), patch.object(
                vc, "get_latest_from_simple_index", return_value="0.4.0"
            ):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True

    def test_pypi_down_returns_outdated_false(self, tmp_path):
        """PyPI unreachable: conservative fallback — installable=current, outdated=false.

        Previously returned None; the new pre-flight contract is to never
        surface 'Update available' when we can't confirm an installable version.
        """
        cache_file = tmp_path / "nonexistent-cache.json"

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value=None
            ), patch.object(
                vc, "get_latest_from_simple_index", return_value=None
            ):
                result = vc.check_version_cached("0.3.11")

        assert result is not None
        assert result["outdated"] is False
        assert result["installable_latest"] == "0.3.11"
        assert result["pypi_latest"] is None
        assert result["simple_latest"] is None

    def test_future_timestamp_cache_treated_as_stale(self, tmp_path):
        """Cache with future timestamp is treated as stale and triggers re-probe.

        >>> # Future checked_at = cache expired, hit PyPI + /simple/
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time() + 999999,  # Far in the future
                    "latest": "0.1.0",
                    "installable_latest": "0.1.0",
                    "pypi_latest": "0.1.0",
                    "simple_latest": "0.1.0",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value="0.4.0"
            ), patch.object(
                vc, "get_latest_from_simple_index", return_value="0.4.0"
            ):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True

    def test_cache_empty_installable_triggers_refetch(self, tmp_path):
        """Cache with empty installable_latest is treated as a miss → re-probe.

        Previously this returned None because the old schema's empty 'latest'
        was a hard short-circuit. New schema treats missing installable_latest
        as cache miss and re-fetches.
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time(),
                    "installable_latest": "",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value="0.4.0"
            ), patch.object(
                vc, "get_latest_from_simple_index", return_value="0.4.0"
            ):
                result = vc.check_version_cached("0.3.11")

        assert result is not None
        assert result["installable_latest"] == "0.4.0"

    def test_force_bypasses_fresh_cache(self, tmp_path):
        """force=True hits PyPI + /simple/ even when cache is fresh.

        >>> # Fresh cache + force=True = still calls both probes
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(
            json.dumps(
                {
                    "checked_at": time.time(),
                    "latest": "0.3.10",
                    "installable_latest": "0.3.10",
                    "pypi_latest": "0.3.10",
                    "simple_latest": "0.3.10",
                }
            )
        )

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(
                vc, "get_latest_pypi_version", return_value="0.4.0"
            ) as mock_pypi, patch.object(
                vc, "get_latest_from_simple_index", return_value="0.4.0"
            ) as mock_simple:
                result = vc.check_version_cached("0.3.11", force=True)
                mock_pypi.assert_called_once()
                mock_simple.assert_called_once()

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True
        assert result["checked_at"] > 0
        assert result["next_check_at"] > result["checked_at"]


class TestCheckVersionCachedDualEndpoint:
    """Tests for check_version_cached() pre-flight /simple/ agreement check."""

    @staticmethod
    def _isolate_cache(tmp_path, monkeypatch):
        """Point VERSION_CACHE to a tmp file for the duration of the test."""
        cache = tmp_path / "vcache.json"
        monkeypatch.setattr("jacked.version_check.VERSION_CACHE", cache)
        return cache

    def test_outdated_false_when_simple_lags_pypi(self, tmp_path, monkeypatch):
        """Regression: v0.45.2 -> v0.45.3 propagation lag must NOT show outdated=True."""
        self._isolate_cache(tmp_path, monkeypatch)
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.3"
        ), patch(
            "jacked.version_check.get_latest_from_simple_index", return_value="0.45.2"
        ):
            result = vc.check_version_cached("0.45.2", force=True)
        assert result is not None
        assert result["outdated"] is False
        assert result["installable_latest"] == "0.45.2"
        assert result["pypi_latest"] == "0.45.3"
        assert result["simple_latest"] == "0.45.2"

    def test_outdated_true_when_both_endpoints_agree(self, tmp_path, monkeypatch):
        self._isolate_cache(tmp_path, monkeypatch)
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.3"
        ), patch(
            "jacked.version_check.get_latest_from_simple_index", return_value="0.45.3"
        ):
            result = vc.check_version_cached("0.45.2", force=True)
        assert result["outdated"] is True
        assert result["installable_latest"] == "0.45.3"

    def test_outdated_false_when_simple_unreachable(self, tmp_path, monkeypatch):
        self._isolate_cache(tmp_path, monkeypatch)
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.3"
        ), patch(
            "jacked.version_check.get_latest_from_simple_index", return_value=None
        ):
            result = vc.check_version_cached("0.45.2", force=True)
        assert result["outdated"] is False

    def test_outdated_false_when_pypi_unreachable(self, tmp_path, monkeypatch):
        self._isolate_cache(tmp_path, monkeypatch)
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value=None
        ), patch(
            "jacked.version_check.get_latest_from_simple_index", return_value="0.45.3"
        ):
            result = vc.check_version_cached("0.45.2", force=True)
        assert result["outdated"] is False

    def test_outdated_false_when_simple_returns_garbage_version(
        self, tmp_path, monkeypatch
    ):
        """Defensive: if /simple/ probe somehow returns an unparseable string,
        fall back to outdated=false instead of caching garbage as installable_latest."""
        self._isolate_cache(tmp_path, monkeypatch)
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.3"
        ), patch(
            "jacked.version_check.get_latest_from_simple_index",
            return_value="garbage-version-string",
        ):
            result = vc.check_version_cached("0.45.2", force=True)
        assert result["outdated"] is False
        assert result["installable_latest"] == "0.45.2"  # current_version, not garbage

    def test_probe_failure_uses_short_ttl(self, tmp_path, monkeypatch):
        """When either PyPI probe fails, next_check_at must be ~1h out (not 24h)
        so a transient outage doesn't lock the tray into 'no updates' for a day."""
        self._isolate_cache(tmp_path, monkeypatch)
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.3"
        ), patch(
            "jacked.version_check.get_latest_from_simple_index", return_value=None
        ):
            result = vc.check_version_cached("0.45.2", force=True)
        # Window between checked_at and next_check_at should equal the
        # probe-failure TTL (3600s), not the success TTL (86400s).
        window = result["next_check_at"] - result["checked_at"]
        assert window == vc.CACHE_TTL_PROBE_FAILURE
        assert window < vc.CACHE_TTL

    def test_probe_failure_cache_expires_at_short_ttl(self, tmp_path, monkeypatch):
        """A cached entry recorded during a probe failure must also EXPIRE at
        the short TTL — otherwise the read path would honor it for 24h even
        though we wrote next_check_at=1h."""
        cache = self._isolate_cache(tmp_path, monkeypatch)
        # Write a "probe failed" cache from 90 minutes ago (older than 1h, younger than 24h).
        cache.write_text(json.dumps({
            "checked_at": time.time() - 5400,   # 90 min ago
            "pypi_latest": None,                 # ← failure marker
            "simple_latest": "0.45.3",
            "installable_latest": "0.45.2",
            "current": "0.45.2",
            "outdated": False,
        }))
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.4"
        ) as p_pypi, patch(
            "jacked.version_check.get_latest_from_simple_index", return_value="0.45.4"
        ) as p_simple:
            result = vc.check_version_cached("0.45.2", force=False)
        # Must have re-probed (cache was stale per short TTL) and now sees 0.45.4
        assert p_pypi.called and p_simple.called
        assert result["installable_latest"] == "0.45.4"
        assert result["outdated"] is True

    def test_old_cache_schema_triggers_refetch(self, tmp_path, monkeypatch):
        """Old cache file only had 'latest' key. New schema needs pypi_latest+simple_latest.
        Reader must treat the old format as a cache miss and re-probe."""
        cache = self._isolate_cache(tmp_path, monkeypatch)
        cache.write_text(
            json.dumps(
                {
                    "latest": "0.45.3",
                    "checked_at": time.time(),  # fresh — would normally short-circuit
                }
            )
        )
        with patch(
            "jacked.version_check.get_latest_pypi_version", return_value="0.45.3"
        ) as p_pypi, patch(
            "jacked.version_check.get_latest_from_simple_index", return_value="0.45.3"
        ) as p_simple:
            result = vc.check_version_cached("0.45.2", force=False)
        # Must have re-probed both endpoints despite fresh cache
        assert p_pypi.called and p_simple.called
        assert result["installable_latest"] == "0.45.3"

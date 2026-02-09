"""Tests for jacked.version_check module."""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from jacked import version_check as vc


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
        mock_response.read.return_value = json.dumps({
            "info": {"version": "0.4.0"},
        }).encode("utf-8")
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
        mock_response.read.return_value = json.dumps({"unexpected": "data"}).encode("utf-8")
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


class TestCheckVersionCached:
    """Tests for check_version_cached() with mocked cache and network."""

    def test_fresh_cache_no_network(self, tmp_path):
        """Fresh cache file prevents network call.

        >>> # Cache within TTL skips PyPI
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(json.dumps({
            "checked_at": time.time(),
            "latest": "0.4.0",
        }))

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch.object(vc, "get_latest_pypi_version") as mock_pypi:
                result = vc.check_version_cached("0.3.11")
                mock_pypi.assert_not_called()

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True
        assert "checked_at" in result
        assert "next_check_at" in result

    def test_fresh_cache_not_outdated(self, tmp_path):
        """Fresh cache shows not outdated when versions match.

        >>> # Same version = not outdated
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(json.dumps({
            "checked_at": time.time(),
            "latest": "0.3.11",
        }))

        with patch.object(vc, "VERSION_CACHE", cache_file):
            result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.3.11"
        assert result["outdated"] is False

    def test_stale_cache_hits_pypi(self, tmp_path):
        """Stale cache (>24h) triggers PyPI check.

        >>> # Old cache forces network call
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(json.dumps({
            "checked_at": time.time() - 90000,  # >24h ago
            "latest": "0.3.10",
        }))

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "info": {"version": "0.4.0"},
        }).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch("urllib.request.urlopen", return_value=mock_response):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True

    def test_no_cache_hits_pypi(self, tmp_path):
        """Missing cache file triggers PyPI check.

        >>> # No cache = network call
        """
        cache_file = tmp_path / "nonexistent-cache.json"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "info": {"version": "0.3.11"},
        }).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch("urllib.request.urlopen", return_value=mock_response):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.3.11"
        assert result["outdated"] is False
        # Verify cache was written
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert cached["latest"] == "0.3.11"

    def test_corrupt_cache_hits_pypi(self, tmp_path):
        """Corrupt cache file triggers PyPI check.

        >>> # Bad JSON in cache = network call
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text("not valid json {{{")

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "info": {"version": "0.4.0"},
        }).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch("urllib.request.urlopen", return_value=mock_response):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True

    def test_pypi_down_returns_none(self, tmp_path):
        """PyPI unreachable with no cache returns None.

        >>> # No cache + no network = None
        """
        cache_file = tmp_path / "nonexistent-cache.json"

        from urllib.error import URLError
        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch("urllib.request.urlopen", side_effect=URLError("timeout")):
                result = vc.check_version_cached("0.3.11")

        assert result is None

    def test_future_timestamp_cache_treated_as_stale(self, tmp_path):
        """Cache with future timestamp is treated as stale and triggers PyPI check.

        >>> # Future checked_at = cache expired, hit PyPI
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(json.dumps({
            "checked_at": time.time() + 999999,  # Far in the future
            "latest": "0.1.0",
        }))

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "info": {"version": "0.4.0"},
        }).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch("urllib.request.urlopen", return_value=mock_response):
                result = vc.check_version_cached("0.3.11")

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True

    def test_cache_empty_latest_returns_none(self, tmp_path):
        """Cache with empty latest version returns None.

        >>> # Empty latest in cache = None
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(json.dumps({
            "checked_at": time.time(),
            "latest": "",
        }))

        with patch.object(vc, "VERSION_CACHE", cache_file):
            result = vc.check_version_cached("0.3.11")

        assert result is None

    def test_force_bypasses_fresh_cache(self, tmp_path):
        """force=True hits PyPI even when cache is fresh.

        >>> # Fresh cache + force=True = still calls PyPI
        """
        cache_file = tmp_path / "version-cache.json"
        cache_file.write_text(json.dumps({
            "checked_at": time.time(),
            "latest": "0.3.10",
        }))

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "info": {"version": "0.4.0"},
        }).encode("utf-8")
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.object(vc, "VERSION_CACHE", cache_file):
            with patch("urllib.request.urlopen", return_value=mock_response) as mock_pypi:
                result = vc.check_version_cached("0.3.11", force=True)
                mock_pypi.assert_called_once()

        assert result["latest"] == "0.4.0"
        assert result["outdated"] is True
        assert result["checked_at"] > 0
        assert result["next_check_at"] > result["checked_at"]

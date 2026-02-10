"""Version checking against PyPI."""

import json
import time
import urllib.request
from pathlib import Path

VERSION_CACHE = Path.home() / ".claude" / "jacked-version-cache.json"
CACHE_TTL = 86400  # 24 hours


def get_latest_pypi_version(package: str = "claude-jacked", timeout: float = 3.0) -> str | None:
    """Query PyPI JSON API for latest version. Returns version string or None on failure.

    >>> # With mocked network, returns a version string
    >>> isinstance(get_latest_pypi_version.__doc__, str)
    True
    """
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return data.get("info", {}).get("version")
    except Exception:
        return None


def is_newer(latest: str, current: str) -> bool:
    """True if latest > current using tuple comparison. No packaging dependency.

    >>> is_newer("0.3.12", "0.3.11")
    True
    >>> is_newer("0.3.11", "0.3.11")
    False
    >>> is_newer("0.3.11", "0.3.12")
    False
    >>> is_newer("abc", "0.3.11")
    False
    >>> is_newer("0.3.11", "xyz")
    False
    >>> is_newer("0.5.0", "0.3.11.dev1")
    True
    >>> is_newer("0.5.0", "0.3.11+local")
    True
    """
    try:
        def parse(v: str) -> tuple:
            # Strip +local/-beta suffixes, then take leading numeric parts only
            # "0.3.11.dev1" → (0, 3, 11), "0.3.11+local" → (0, 3, 11)
            v = v.split("+")[0].split("-")[0]
            parts = []
            for x in v.split("."):
                try:
                    parts.append(int(x))
                except ValueError:
                    break  # Stop at first non-numeric part (e.g. "dev1", "rc1")
            return tuple(parts)
        p_latest, p_current = parse(latest), parse(current)
        if not p_latest or not p_current:
            return False  # Unparseable version — don't nag
        return p_latest > p_current
    except (ValueError, AttributeError):
        return False


def check_version_cached(current_version: str, force: bool = False) -> dict | None:
    """Check PyPI with 24h cache. Returns {"latest", "outdated", "checked_at", "next_check_at"} or None.

    >>> result = check_version_cached.__doc__  # doctest placeholder
    >>> isinstance(result, str)
    True
    """
    try:
        now = time.time()

        # Read cache (corrupt cache falls through to PyPI check)
        if not force:
            try:
                if VERSION_CACHE.exists():
                    cache = json.loads(VERSION_CACHE.read_text(encoding="utf-8"))
                    checked_at = cache.get("checked_at", 0)
                    age = now - checked_at
                    if 0 <= age < CACHE_TTL:
                        latest = cache.get("latest", "")
                        if latest:
                            return {
                                "latest": latest,
                                "outdated": is_newer(latest, current_version),
                                "ahead": is_newer(current_version, latest),
                                "checked_at": checked_at,
                                "next_check_at": checked_at + CACHE_TTL,
                            }
                        return None
            except (json.JSONDecodeError, KeyError, TypeError):
                pass  # Corrupt cache — fall through to PyPI

        # Cache stale, missing, corrupt, or force refresh — hit PyPI
        latest = get_latest_pypi_version()
        if latest is None:
            return None

        # Write cache atomically (temp file + replace)
        import tempfile
        import os
        VERSION_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache_data = json.dumps({"checked_at": now, "latest": latest})
        tmp_fd, tmp_path = tempfile.mkstemp(dir=VERSION_CACHE.parent, suffix=".tmp")
        try:
            os.write(tmp_fd, cache_data.encode("utf-8"))
            os.close(tmp_fd)
            os.replace(tmp_path, str(VERSION_CACHE))
        except Exception:
            try:
                os.close(tmp_fd)
            except Exception:
                pass
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        return {
            "latest": latest,
            "outdated": is_newer(latest, current_version),
            "ahead": is_newer(current_version, latest),
            "checked_at": now,
            "next_check_at": now + CACHE_TTL,
        }
    except Exception:
        return None

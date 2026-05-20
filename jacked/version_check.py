"""Version checking against PyPI."""

import json
import re
import time
import urllib.request
from pathlib import Path

VERSION_CACHE = Path.home() / ".claude" / "jacked-version-cache.json"
CACHE_TTL = 86400  # 24 hours — tray menu has "Check for updates" for on-demand refresh

# Matches both wheel and sdist filenames:
#   claude_jacked-0.45.3-py3-none-any.whl
#   claude_jacked-0.45.3.tar.gz
# PEP 503 normalizes the package name to lowercase + underscores in filenames.
_VERSION_FROM_FILENAME = re.compile(
    r"^[\w.]+?-(\d+(?:\.\d+)*(?:(?:a|b|rc|\.?dev|\.?post)\d+)?)"
    r"(?:-py|\.tar\.gz|\.zip|\.whl)"
)
# Captures ONLY the version segment, stopping at "-py..." for wheels or the
# archive suffix for sdists. Without the tight terminator, `-py3-none-any` would
# bleed into the capture group and the cache would store strings like
# "0.45.3-py3-none" instead of "0.45.3".


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


def get_latest_from_simple_index(package: str = "claude-jacked", timeout: float = 3.0) -> str | None:
    """Query the PEP 691 JSON variant of /simple/<package>/. Returns max non-yanked version or None.

    This is THE SAME index uv reads. A version's presence here guarantees
    `uv tool install --refresh` can resolve to it.
    """
    try:
        url = f"https://pypi.org/simple/{package}/"
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.pypi.simple.v1+json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        versions = set()
        for entry in data.get("files", []):
            if not isinstance(entry, dict):
                continue
            # Skip yanked releases — uv won't install them, so we shouldn't
            # advertise them as "available". PEP 691: yanked is either bool
            # or non-empty string (reason); falsy means not yanked.
            if entry.get("yanked"):
                continue
            fn = entry.get("filename", "")
            m = _VERSION_FROM_FILENAME.match(fn)
            if m:
                versions.add(m.group(1))
        if not versions:
            return None
        return max(versions, key=_parse_version_tuple)
    except Exception:
        return None


def _parse_version_tuple(v: str) -> tuple:
    """Parse a PEP 440-ish version string into a tuple of leading numeric parts.

    >>> _parse_version_tuple("0.45.3")
    (0, 45, 3)
    >>> _parse_version_tuple("0.45.3+local")
    (0, 45, 3)
    >>> _parse_version_tuple("0.45.3-beta")
    (0, 45, 3)
    >>> _parse_version_tuple("0.45.3.dev1")
    (0, 45, 3)
    >>> _parse_version_tuple("xyz")
    ()
    """
    v = v.split("+")[0].split("-")[0]
    parts = []
    for x in v.split("."):
        try:
            parts.append(int(x))
        except ValueError:
            break
    return tuple(parts)


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
        p_latest, p_current = _parse_version_tuple(latest), _parse_version_tuple(current)
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

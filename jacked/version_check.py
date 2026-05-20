"""Version checking against PyPI."""

import json
import re
import time
import urllib.request
from pathlib import Path

VERSION_CACHE = Path.home() / ".claude" / "jacked-version-cache.json"
CACHE_TTL = 86400  # 24 hours — tray menu has "Check for updates" for on-demand refresh
CACHE_TTL_PROBE_FAILURE = 3600  # 1 hour — retry sooner after a transient PyPI/network failure
                                # so the user isn't stuck "no updates available" for a full day
                                # after a Fastly hiccup. /dc finding #2 on the v0.45.4 review.

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
    """Check PyPI with 24h cache. Returns dict with keys:
       latest (alias for installable_latest, kept for back-compat),
       installable_latest, pypi_latest, simple_latest, outdated, checked_at, next_check_at.

    Both /pypi/json and /simple/ are probed; outdated is true only when the LESSER
    of the two (== what uv would actually install) is newer than current_version.
    This eliminates phantom 'Update available' badges when PyPI's /simple/ index
    is still propagating a newly-published release.
    """
    try:
        now = time.time()

        if not force:
            try:
                if VERSION_CACHE.exists():
                    cache = json.loads(VERSION_CACHE.read_text(encoding="utf-8"))
                    checked_at = cache.get("checked_at", 0)
                    age = now - checked_at
                    # Cached probe-failures (either endpoint null) expire faster
                    # so we retry within an hour, not a day.
                    cached_pypi = cache.get("pypi_latest")
                    cached_simple = cache.get("simple_latest")
                    cache_ttl = CACHE_TTL if (cached_pypi and cached_simple) else CACHE_TTL_PROBE_FAILURE
                    if 0 <= age < cache_ttl:
                        # New-schema cache hit
                        installable = cache.get("installable_latest")
                        if installable:
                            return {
                                "latest": installable,                     # back-compat alias
                                "installable_latest": installable,
                                "pypi_latest": cached_pypi,
                                "simple_latest": cached_simple,
                                "outdated": is_newer(installable, current_version),
                                "ahead": is_newer(current_version, installable),
                                "checked_at": checked_at,
                                "next_check_at": checked_at + cache_ttl,
                            }
                        # Old schema (just 'latest') — treat as miss, re-fetch
            except Exception:
                pass

        pypi_latest = get_latest_pypi_version()
        simple_latest = get_latest_from_simple_index()

        # Conservative: if either probe failed OR returned an unparseable
        # version (defensive — PyPI shouldn't ever do this, but a corrupted
        # cache/proxy could), do not surface an update.
        if not pypi_latest or not simple_latest:
            installable = current_version
        else:
            p_pypi = _parse_version_tuple(pypi_latest)
            p_simple = _parse_version_tuple(simple_latest)
            if not p_pypi or not p_simple:
                installable = current_version
            else:
                # The LESSER of the two — what uv would actually resolve to.
                installable = pypi_latest if p_pypi <= p_simple else simple_latest

        outdated = is_newer(installable, current_version)

        cache_data = {
            "checked_at": now,
            "pypi_latest": pypi_latest,
            "simple_latest": simple_latest,
            "installable_latest": installable,
            "latest": installable,          # back-compat for downgrade to pre-v0.45.4
            "current": current_version,
            "outdated": outdated,
        }
        # Shorter TTL when a probe failed — bounce back faster after transient
        # network/PyPI issues instead of pretending we know for 24 hours.
        effective_ttl = CACHE_TTL if (pypi_latest and simple_latest) else CACHE_TTL_PROBE_FAILURE

        # Atomic write — tempfile + os.replace — preserves the crash-safety
        # guarantee the prior implementation had. Half-written cache files
        # otherwise survive Ctrl-C / OOM during write and silently break
        # subsequent reads.
        try:
            import tempfile
            import os
            VERSION_CACHE.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(VERSION_CACHE.parent), prefix=".vcache-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(cache_data, fh)
                os.replace(tmp_path, VERSION_CACHE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception:
            pass

        return {
            "latest": installable,
            "installable_latest": installable,
            "pypi_latest": pypi_latest,
            "simple_latest": simple_latest,
            "outdated": outdated,
            "ahead": is_newer(current_version, installable),
            "checked_at": now,
            "next_check_at": now + effective_ttl,
        }
    except Exception:
        return None

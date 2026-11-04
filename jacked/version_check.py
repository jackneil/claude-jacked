"""Version checking against PyPI."""

import json
import logging
import re
import time
import urllib.request
from pathlib import Path

from packaging.version import InvalidVersion, Version

from jacked.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

VERSION_CACHE = Path.home() / ".claude" / "jacked-version-cache.json"
CACHE_TTL = 43200  # 12 hours — surfaces the update badge within half a day of a
                   # release; the tray menu also has "Check for updates" on demand
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


def _log_probe_failure(probe: str, exc: Exception) -> None:
    """One DEBUG line for a probe that failed: no answer is not a failure.

    The line has the error class only. The message of a network error can
    hold the index URL.
    """
    logger.debug("PyPI probe of %s failed: %s", probe, type(exc).__name__)


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
    except Exception as exc:
        _log_probe_failure("the JSON API", exc)
        return None


def get_latest_from_simple_index(package: str = "claude-jacked", timeout: float = 3.0) -> str | None:
    """Query the PEP 691 JSON variant of /simple/<package>/. Returns the version uv installs, or None.

    This is THE SAME index uv reads, and the pick follows uv's default
    resolution: yanked files do not count, a pre-release (a, b, rc or dev)
    counts only when the index has no final release, and versions compare in
    PEP 440 order. A version's presence here guarantees
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
                # The pattern captures only valid PEP 440 versions.
                versions.add(Version(m.group(1)))
        # uv does not install a pre-release while a final release exists. A
        # leading-numbers tuple would rank 0.111.0rc1 above 0.110.2, and tie
        # 0.111.0 with 0.111.0.post1 (then the set order picks one).
        finals = {v for v in versions if not v.is_prerelease}
        candidates = finals or versions
        if not candidates:
            return None
        return str(max(candidates))
    except Exception as exc:
        _log_probe_failure("the simple index", exc)
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


def installable_version(simple_latest: str | None) -> str | None:
    """What uv installs now: the update badge offers this version.

    uv resolves through the simple index (PEP 691), so the simple index's
    answer is the installable version. The JSON API answer is never the
    result, because uv does not read the JSON API. For minutes after a
    release, the JSON API can name an older version (it lags) or a version
    that the simple index does not serve yet (it leads). When the simple
    index gives no parseable answer, the installable version is unknown and
    the result is None.

    This answer differs from newest_listed_version() on purpose. The badge
    must offer only a version that uv can install now. Thus a JSON API that
    leads the simple index cannot show a phantom update. In G3 (the
    2026-11-02 lag), the JSON API still named 0.110.1 while the simple index
    served 0.110.2: the badge offers 0.110.2, the version that the update
    installs.

    >>> installable_version("0.110.2")
    '0.110.2'
    >>> installable_version(None) is None  # the simple index gave no answer
    True
    >>> installable_version("garbage") is None
    True
    """
    if not simple_latest or not _parse_version_tuple(simple_latest):
        return None
    return simple_latest


def newest_listed_version(pypi_latest: str | None, simple_latest: str | None) -> str | None:
    """What ``jacked upgrade`` aims for: the newest release that a probe names.

    Versions compare in PEP 440 order. An answer that is not a valid version
    string counts as no answer. The result is None only when neither probe
    gives a valid answer. The result is the normalized version string.

    This answer differs from installable_version() on purpose. When the
    simple index lags the JSON API, uv installs the older build. The target
    must stay the newer JSON API answer. Then the upgrade reports "the
    package index does not serve vX yet" instead of "Upgrade complete"
    (#166i, commit 26227f8). When the JSON API lags the simple index, the
    simple index's newer answer is the target, and uv installs it. In G3
    (the 2026-11-02 lag), the target line said v0.110.1 while uv installed
    0.110.2.

    >>> newest_listed_version("0.110.1", "0.110.2")  # the JSON API lags (G3)
    '0.110.2'
    >>> newest_listed_version("0.111.0", "0.110.2")  # the simple index lags (#166i)
    '0.111.0'
    >>> newest_listed_version(None, "0.110.2"), newest_listed_version("0.110.2", None)
    ('0.110.2', '0.110.2')
    >>> newest_listed_version("garbage", None) is None
    True
    """
    answers = []
    for answer in (simple_latest, pypi_latest):
        # The JSON API answer is whatever the response holds: it can be any
        # JSON type, not only a string.
        if not isinstance(answer, str):
            continue
        try:
            answers.append(Version(answer))
        except InvalidVersion:
            continue
    return str(max(answers)) if answers else None


def _cache_ttl(pypi_latest, simple_latest, installable, current_version) -> int:
    """How long one check of the index stays fresh, in seconds.

    A failed probe retries within an hour. An "ahead" answer (the installed
    version is newer than the index) also retries within an hour: right after
    a release the index CDN can still serve the previous version for some
    minutes, and the installed version came from that index, so the answer is
    most likely stale. A real development build pays one check an hour.

    >>> _cache_ttl("1.0", "1.0", "1.0", "1.0") == CACHE_TTL
    True
    >>> _cache_ttl("1.0", "1.0", "1.0", "1.1") == CACHE_TTL_PROBE_FAILURE
    True
    >>> _cache_ttl(None, "1.0", "1.0", "1.0") == CACHE_TTL_PROBE_FAILURE
    True
    """
    if not (pypi_latest and simple_latest):
        return CACHE_TTL_PROBE_FAILURE
    if installable and is_newer(current_version, installable):
        return CACHE_TTL_PROBE_FAILURE
    return CACHE_TTL


def check_version_cached(current_version: str, force: bool = False) -> dict | None:
    """Check PyPI with a 12 h cache (see CACHE_TTL). Returns dict with keys:
       latest (alias for installable_latest, kept for back-compat),
       installable_latest, pypi_latest, simple_latest, outdated, checked_at, next_check_at.

    Both /pypi/json and /simple/ are probed. outdated is true only when both
    answer and the simple index's version (what uv installs, see
    installable_version) is newer than current_version. This prevents a
    phantom 'Update available' badge while /simple/ still propagates a new
    release, and the badge names the version that the update installs while
    the JSON API lags.
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
                    installable = cache.get("installable_latest")
                    cache_ttl = _cache_ttl(
                        cached_pypi, cached_simple, installable, current_version
                    )
                    # A check made for another installed version is stale: an
                    # upgrade (or a downgrade) replaced the version it compared,
                    # and an upgraded service would show "dev" until the TTL.
                    recorded = cache.get("current")
                    same_install = recorded is None or recorded == current_version
                    if same_install and 0 <= age < cache_ttl:
                        # New-schema cache hit
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

        # The badge's own conservative rule: when either probe failed or gave
        # an unparseable version (a corrupted cache or proxy could), installable
        # stays the running version and no update shows. When both answer, the
        # version is the simple index's answer (installable_version), which is
        # what uv installs, also when the JSON API leads or lags that index.
        installable = current_version
        if pypi_latest and _parse_version_tuple(pypi_latest):
            installable = installable_version(simple_latest) or current_version

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
        # Shorter TTL when a probe failed or the answer says "ahead" (see
        # _cache_ttl): bounce back faster instead of pretending we know.
        effective_ttl = _cache_ttl(pypi_latest, simple_latest, installable, current_version)

        # The shared atomic writer keeps the crash-safety guarantee: a
        # half-written cache file would survive Ctrl-C / OOM during the write
        # and silently break later reads. The cache is best effort, so a
        # failed write only means the next check asks PyPI again.
        try:
            atomic_write_text(VERSION_CACHE, json.dumps(cache_data))
        except OSError:
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

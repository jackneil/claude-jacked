"""Upstream freshness check for the agent-reach integration.

Answers "is the vetted pin behind upstream's default branch?" so ``status`` and
the dashboard can surface a staleness signal (the early warning whose absence
would otherwise let a user silently rot months behind upstream's platform fixes).

Mirrors ``jacked.version_check`` discipline: a cached GitHub API read (12h TTL,
1h retry after a transient failure) at ``~/.claude/jacked-reach-upstream-cache.json``,
keyed by the pinned SHA so a pin bump invalidates the cache. Best-effort: any
network/parse failure returns ``None`` (unknown), never raises into ``status``.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_PATH = Path.home() / ".claude" / "jacked-reach-upstream-cache.json"
CACHE_TTL = 43200  # 12h, matches version_check
CACHE_TTL_PROBE_FAILURE = 3600  # 1h retry after a transient failure


def _api_url(upstream: str, branch: str) -> Optional[str]:
    """Map ``https://github.com/OWNER/REPO`` to the branch-head commit API URL."""
    marker = "github.com/"
    if marker not in upstream:
        return None
    path = upstream.split(marker, 1)[1].strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    return f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"


def _fetch_head_sha(upstream: str, branch: str, timeout: float) -> Optional[str]:
    url = _api_url(upstream, branch)
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "claude-jacked",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        sha = data.get("sha")
        return sha if isinstance(sha, str) and len(sha) == 40 else None
    except Exception as e:  # best-effort: never raise into status()
        logger.debug("reach upstream check failed: %s", e)
        return None


def _read_cache(now: float) -> Optional[dict]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    checked = data.get("checked_at")
    if not isinstance(checked, (int, float)):
        return None
    ttl = CACHE_TTL if data.get("head_sha") else CACHE_TTL_PROBE_FAILURE
    if now - checked > ttl:
        return None
    return data


def _write_cache(payload: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError as e:
        logger.debug("could not write reach upstream cache: %s", e)


def check_upstream(
    pinned_sha: str,
    upstream: str,
    *,
    branch: str = "main",
    now: float,
    timeout: float = 3.0,
) -> Optional[dict]:
    """Return ``{head_sha, behind, checked_at}`` or ``None`` when unknown.

    ``behind`` is True when upstream's branch head differs from the pinned SHA.
    ``now`` is injected (the runtime forbids argless clocks in some contexts and
    it keeps this testable). Cached per pinned SHA; a differing cache key forces
    a refresh so a pin bump re-checks immediately.
    """
    cached = _read_cache(now)
    if cached and cached.get("pinned_sha") == pinned_sha:
        return {
            "head_sha": cached.get("head_sha"),
            "behind": cached.get("behind"),
            "checked_at": cached.get("checked_at"),
        }

    head = _fetch_head_sha(upstream, branch, timeout)
    payload = {
        "pinned_sha": pinned_sha,
        "head_sha": head,
        "behind": (head is not None and head != pinned_sha),
        "checked_at": now,
    }
    _write_cache(payload)
    if head is None:
        # Unknown upstream state — do not assert "up to date"; report None so the
        # UI can say "could not check" rather than a false "current".
        return None
    return {"head_sha": head, "behind": payload["behind"], "checked_at": now}

"""The service side of the issue queue (#96i): refresh the per-repo cache.

The SessionStart hook prints the open GitHub issues of the session repo
(``jacked.data.hooks.issue_queue``), but it must never call the network. So
the jacked service does the network work here, on its own schedule:

- ``issue_queue_refresh_loop`` sleeps ``REFRESH_INTERVAL_S`` (10 minutes)
  FIRST, then runs one pass, and repeats. There is no pass at service start;
  a cache from before a restart stays good for 24 hours.
- ``refresh_pass`` reads the repositories that jacked knows (the projects in
  ``Database.get_project_activity_summary``), keeps one path for each
  repository slug (a worktree and its main checkout share one), and runs
  ``refresh_repo`` for each in a worker thread, never on the event loop. At
  most ``MAX_CONCURRENT`` repositories refresh at the same time.
- ``refresh_repo`` reads the ``origin`` remote with git, and stops when it is
  not a GitHub remote. Then it asks gh for one page of the newest open issues,
  one page of open issues for each queue label (so a labeled issue older than
  the newest page still shows), their authors' association with the
  repository, and the total count (one GraphQL call), and writes the cache
  file through the shared atomic writer (``jacked.atomic_io``).

Who is kept: an issue stays in the cache only when it was opened by the
owner, a member or a collaborator of the repo, or when it has a queue label
(``kept_issue``, the one rule that the hook also uses). On a public repo,
anyone can open an issue, and the hook puts each title in the context of
every session. A label needs triage rights, so a labeled issue stays.
``open_count`` is the number of kept issues, plus each open issue that no
page holds (the refresher does not see the author of those issues, so it
counts them and the trim marker loses none). ``github_open_count`` is the
total from GitHub.

Each git or gh call has a hard timeout of ``CALL_TIMEOUT_S`` (10 seconds).
gh and git are found through the shared lookup (``jacked.findbin``).

Quiet failure: gh not found, gh not signed in (a non-zero exit), a timeout,
a remote that is not GitHub, no ``origin`` remote, or an answer that is not
usable gives a DEBUG line with the reason class, and the pass writes
nothing. The last good cache file stays, and the hook stops showing it after
24 hours. A log line never holds the remote URL, because a URL can hold a
credential. The cache holds a remote URL that the refresher builds from the
owner and the repository name only.

Labels: ``ensure_label`` creates one of the queue labels (``in-flight``,
``next``, ``punted``) when the repository does not have it. A skill calls it
through ``jacked issues ensure-label <name>`` just before it files the first
issue with that label. Nothing creates a label at session start or at
service start.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlsplit

from jacked.atomic_io import atomic_write_text
from jacked.data.hooks.issue_queue import STALE_AFTER, cache_path, kept_issue, repo_slug
from jacked.findbin import find_bin
from jacked.git_guard import GIT_CONFIG_OVERRIDE_ARGS
from jacked.issue_labels import QUEUE_LABEL_NAMES
from jacked.memory.vault import one_line
from jacked.winproc import NO_WINDOW

logger = logging.getLogger(__name__)

#: Seconds between two refresh passes.
REFRESH_INTERVAL_S = 600

#: The hard timeout of one git or gh call, in seconds.
CALL_TIMEOUT_S = 10

#: The most repositories that refresh at the same time.
MAX_CONCURRENT = 4

#: How many known projects one pass reads. The routes that list the known
#: projects (``routes/system.py``, ``routes/permissions.py``) read the same
#: number, so the queue covers the projects the dashboard shows.
PROJECT_LIMIT = 100

#: Issues per page. 100 is the GitHub GraphQL page limit. ``open_count`` also
#: counts each issue that no page holds, so the hook's trim marker loses no
#: issue.
ISSUE_PAGE = 100

#: The queue labels and how a new one looks: (color, description). The cache
#: keeps only these labels, because the hook shows no other label.
_LABEL_LOOKS = {
    "in-flight": ("1d76db", "Work in progress on a branch or a PR"),
    "next": ("0e8a16", "Queued as the next work to do"),
    "punted": ("cfd3d7", "Found and kept for later; not in the current work"),
}
LABELS = {name: _LABEL_LOOKS[name] for name in QUEUE_LABEL_NAMES}

_GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")



def _label_alias(label: str) -> str:
    """The GraphQL alias of a queue label's connection: an alias cannot hold "-".

    >>> _label_alias("in-flight")
    'in_flight'
    """
    return label.replace("-", "_")


_ISSUE_ARGS = f"states: OPEN, first: {ISSUE_PAGE}, orderBy: {{field: CREATED_AT, direction: DESC}}"
_ISSUE_NODES = "nodes { number title authorAssociation labels(first: 100) { nodes { name } } }"
_QUERY = (
    "query($owner: String!, $name: String!) {"
    " repository(owner: $owner, name: $name) {"
    f" issues({_ISSUE_ARGS}) {{ totalCount {_ISSUE_NODES} }}"
    + "".join(
        f' {_label_alias(label)}: issues(labels: ["{label}"], {_ISSUE_ARGS}) {{ {_ISSUE_NODES} }}'
        for label in LABELS
    )
    + " } }"
)


class _Skip(Exception):
    """One repository is skipped. The message is the DEBUG reason class."""


class LabelError(Exception):
    """gh could not create a label, and the label does not exist."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def github_repo(url: str) -> tuple[str, str] | None:
    """The (owner, name) of a GitHub remote URL, or None for any other remote.

    Accepts the https, ssh, git and scp-like ("git@github.com:o/r.git")
    forms. Credentials and ports in the URL are ignored.

    >>> github_repo("git@github.com:acme/widgets.git")
    ('acme', 'widgets')
    >>> github_repo("https://gitlab.com/acme/widgets") is None
    True
    """
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if parts.scheme not in ("https", "http", "ssh", "git"):
            return None
        path = parts.path
    else:
        match = _SCP_RE.match(url)
        if not match:
            return None
        host, path = match.group(1).lower(), match.group(2)
    if host not in _GITHUB_HOSTS:
        return None
    segments = path.strip("/").split("/")
    if len(segments) != 2:
        return None
    owner, name = segments
    if name.endswith(".git"):
        name = name[: -len(".git")]
    for part in (owner, name):
        if not _NAME_RE.match(part) or part in (".", ".."):
            return None
    return owner, name


def _run(argv: list[str], cwd: str | None = None, env: dict | None = None):
    """Run one bounded call. A timeout raises ``_Skip("timed out")``."""
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=CALL_TIMEOUT_S,
            creationflags=NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise _Skip(f"{os.path.basename(argv[0])} timed out after {CALL_TIMEOUT_S} s") from None


def _gh_env() -> dict:
    """The environment for a gh call: never a prompt, never an update check."""
    env = dict(os.environ)
    env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"})
    return env


def _origin_url(git: str, path: str) -> str:
    """The ``origin`` URL of the repo at ``path``.

    The call carries the shared ``-c`` overrides (``jacked.git_guard``) like
    every git call in a checkout that can be untrusted, and takes no
    optional lock.
    """
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    proc = _run([git, "-C", path, *GIT_CONFIG_OVERRIDE_ARGS, "remote", "get-url", "origin"], env=env)
    if proc.returncode != 0:
        raise _Skip("no origin remote")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _node_item(node: dict) -> dict:
    """One cache item from one GraphQL issue node. Raises on a wrong shape."""
    association = node["authorAssociation"]
    if not isinstance(association, str):
        raise TypeError("authorAssociation")
    return {
        "number": int(node["number"]),
        "title": str(node["title"]),
        "labels": [
            name for name in (str(label["name"]) for label in node["labels"]["nodes"])
            if name in LABELS
        ],
        "author_association": association,
    }


def _merged_page(repository: dict) -> list[dict]:
    """The cache items of every connection in the answer, one item per issue number.

    The newest page keeps its order. A labeled issue that it does not hold
    is older than the whole page, so these issues go after it, newest first.
    """
    page = {item["number"]: item for item in map(_node_item, repository["issues"]["nodes"])}
    older: dict[int, dict] = {}
    for label in LABELS:
        for node in repository[_label_alias(label)]["nodes"]:
            item = _node_item(node)
            if item["number"] not in page:
                older[item["number"]] = item
    return [*page.values(), *(older[number] for number in sorted(older, reverse=True))]


def _fetch(gh: str, owner: str, name: str) -> tuple[list[dict], int, int]:
    """The kept issues of all pages, ``open_count`` and GitHub's total count.

    One gh call. ``open_count`` counts the kept issues and each issue that
    no page holds. An issue that ``kept_issue`` refuses is not stored.
    """
    proc = _run(
        [gh, "api", "graphql", "-f", f"query={_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"],
        env=_gh_env(),
    )
    if proc.returncode != 0:
        raise _Skip(f"gh exited {proc.returncode}")
    try:
        answer = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        if answer.get("errors"):
            raise ValueError("graphql errors")
        repository = answer["data"]["repository"]
        total = repository["issues"]["totalCount"]
        if not isinstance(total, int) or isinstance(total, bool):
            raise ValueError("totalCount")
        page = _merged_page(repository)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _Skip(f"gh gave an unusable answer ({type(exc).__name__})") from None
    items = [item for item in page if kept_issue(item["author_association"], item["labels"])]
    unread = max(total - len(page), 0)
    return items, len(items) + unread, max(total, len(page))


# --------------------------------------------------------------------------- #
# One repository, one pass, the loop
# --------------------------------------------------------------------------- #

def refresh_repo(path: str, *, now: datetime | None = None) -> bool:
    """Refresh the cache of the repository at ``path``. True when it wrote it.

    Never raises: an expected skip is a DEBUG line, and any other error is a
    WARNING with the error class.
    """
    slug = None
    try:
        slug = repo_slug(path)
        if slug is None:
            raise _Skip("not a git repository")
        git = find_bin("git")
        if not git:
            raise _Skip("git not found")
        repo = github_repo(_origin_url(git, path))
        if repo is None:
            raise _Skip("not a GitHub remote")
        gh = find_bin("gh")
        if not gh:
            raise _Skip("gh not found")
        items, open_count, github_open_count = _fetch(gh, *repo)
        stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
        payload = {
            "remote": f"https://github.com/{repo[0]}/{repo[1]}",
            "refreshed_at": stamp,
            "open_count": open_count,
            "github_open_count": github_open_count,
            "items": items,
        }
        atomic_write_text(cache_path(slug), json.dumps(payload, indent=1) + "\n", mode=0o600)
        return True
    except _Skip as reason:
        logger.debug("issue queue refresh: skip %s (%s)", slug or "a project", reason)
        return False
    except Exception as exc:  # noqa: BLE001 -- one repository must not stop the pass
        logger.warning("issue queue refresh: %s failed (%s)", slug or "a project", type(exc).__name__)
        return False


def _recently_active(row: dict, now: datetime) -> bool:
    """True when the project had activity inside ``STALE_AFTER``.

    The hook drops a cache that age or older, so a refresh for a project idle
    longer than that spends GitHub API budget on a file no session reads. A
    timestamp that does not parse counts as not recent.

    >>> now = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    >>> _recently_active({"last_activity": "2026-11-04T01:00:00+00:00"}, now)
    True
    >>> _recently_active({"last_activity": "2026-10-01T01:00:00"}, now), _recently_active({}, now)
    (False, False)
    """
    try:
        seen = datetime.fromisoformat(str(row.get("last_activity") or ""))
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return now - seen < STALE_AFTER


def _known_repos(db, now: datetime | None = None) -> list[str]:
    """One path for each recently active repository slug, newest activity first."""
    rows = db.get_project_activity_summary(limit=PROJECT_LIMIT)
    now = now or datetime.now(timezone.utc)
    chosen: dict[str, str] = {}
    for row in rows:
        path = (row or {}).get("repo_path")
        if not path or not _recently_active(row, now):
            continue
        try:
            slug = repo_slug(path)
        except Exception as exc:  # noqa: BLE001 -- one repository must not stop the pass
            logger.debug("issue queue refresh: skip a known project (%s)", type(exc).__name__)
            continue
        if slug is not None and slug not in chosen:
            chosen[slug] = path
    return list(chosen.values())


async def refresh_pass(app) -> int:
    """Refresh every known repository once. Returns how many caches it wrote."""
    db = getattr(app.state, "db", None)
    if db is None:
        return 0
    if not await asyncio.to_thread(find_bin, "gh"):
        logger.debug("issue queue refresh: skip the pass (gh not found)")
        return 0
    paths = await asyncio.to_thread(_known_repos, db)
    gate = asyncio.Semaphore(MAX_CONCURRENT)  # local to this pass, so no reset_locks

    async def _one(path: str) -> bool:
        async with gate:
            return await asyncio.to_thread(refresh_repo, path)

    results = await asyncio.gather(*(_one(p) for p in paths))
    return sum(1 for written in results if written)


async def issue_queue_refresh_loop(app) -> None:
    """Sleep the interval first, then refresh, forever. Never crashes."""
    logger.info(
        "Started the issue queue refresh (every %ds, first pass after one interval)",
        REFRESH_INTERVAL_S,
    )
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_S)
        try:
            written = await refresh_pass(app)
            if written:
                logger.debug("issue queue refresh: wrote %d cache file(s)", written)
        except Exception as exc:  # noqa: BLE001 -- the loop must keep running
            logger.warning("issue queue refresh pass failed (%s)", type(exc).__name__)


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def ensure_label(name: str, *, cwd: str | None = None) -> str:
    """Create the queue label ``name`` in the repo at ``cwd`` when it is missing.

    Returns "created" or "exists". It never changes a label that exists (no
    ``--force``), so a color or a description that the user set stays.
    Raises ``ValueError`` for a name that is not a queue label, and
    ``LabelError`` when gh is not found, times out, or fails in another way.
    """
    if name not in LABELS:
        raise ValueError(f"not a queue label: use one of {', '.join(LABELS)}")
    gh = find_bin("gh")
    if not gh:
        raise LabelError("gh not found: install the GitHub CLI and run `gh auth login`")
    color, description = LABELS[name]
    try:
        proc = _run(
            [gh, "label", "create", name, "--color", color, "--description", description],
            cwd=cwd,
            env=_gh_env(),
        )
    except _Skip as reason:
        raise LabelError(str(reason)) from None
    if proc.returncode == 0:
        return "created"
    detail = one_line(proc.stderr.decode("utf-8", errors="replace"))
    if "already exists" in detail.lower():
        return "exists"
    raise LabelError(f"gh label create exited {proc.returncode}: {detail[:300]}")

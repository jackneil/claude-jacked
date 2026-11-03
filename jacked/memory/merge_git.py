"""The git reads of merge capture: the merge list, the changed files, the trailers.

Every call runs ``git -C <repo>`` with the shared ``-c`` overrides
(``jacked.git_guard``), because the repository config is not trusted. The
output is bytes, decoded as UTF-8 with no newline translation, and records
split at LF only, so a CR inside a commit subject or a trailer value stays
in its value. A launch failure or a timeout gives None (``git``), and each
reader then gives its empty result.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from jacked.findbin import require_bin
from jacked.git_guard import GIT_CONFIG_OVERRIDE_ARGS
from jacked.memory.trailers import TRAILER_KEYS, cap_trailers, parse_trailers

logger = logging.getLogger(__name__)

GIT_TIMEOUT = 15

# Flags on every ``git log``: a repository config cannot make the log run its
# signature verifier (``log.showSignature`` with a ``gpg.program``), and the
# commit text comes out as UTF-8 whatever ``i18n.logOutputEncoding`` says.
# The ``-c log.showSignature=false`` override in ``GIT_CONFIG_OVERRIDE_ARGS``
# does the same through the config. Each one alone closes the path.
LOG_FLAGS = ("--no-show-signature", "--encoding=UTF-8")


def changed_files(repo: Path, sha: str) -> list[str]:
    """Files changed by the merge (diff against its first parent)."""
    proc = git(repo, ["diff", "--name-only", f"{sha}^1", sha])
    if proc is None or proc.returncode != 0:
        return []
    return [line.strip() for line in records(proc.stdout) if line.strip()]


def commit_trailers(repo: Path, sha: str) -> list[str]:
    """Ledger trailer lines of the commits the merge brings in, oldest first."""
    keys = ",".join(f"key={k}" for k in TRAILER_KEYS)
    proc = git(repo, ["log", *LOG_FLAGS, "--reverse", f"--format=%(trailers:{keys},unfold)",
                      f"{sha}^1..{sha}"])
    if proc is None or proc.returncode != 0:
        logger.debug("memory merge-capture: trailer read failed for %s", sha[:8])
        return []
    return cap_trailers(parse_trailers(proc.stdout or ""))


def git(repo: Path, args: list[str]) -> subprocess.CompletedProcess | None:
    """Run ``git -C repo <args>``; None on launch failure/timeout.

    The output is read as bytes and decoded as UTF-8 here, with no newline
    translation. Text mode turns a CR inside a commit subject or a trailer
    value into LF, and the record split then cuts the value. Callers split
    records at LF only (``records``).
    """
    try:
        proc = subprocess.run(
            [require_bin("git"), "-C", str(repo), *GIT_CONFIG_OVERRIDE_ARGS, *args],
            capture_output=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        (proc.stdout or b"").decode("utf-8", errors="replace"),
        (proc.stderr or b"").decode("utf-8", errors="replace"),
    )


def records(out: str) -> list[str]:
    """The LF-ended records of git output. A CR or U+2028 stays in its record.

    >>> records("a\\rb\\nc\\u2028d\\n")
    ['a\\rb', 'c\\u2028d']
    """
    return [line for line in (out or "").split("\n") if line]


def is_git_repo(repo: Path) -> bool:
    """True when ``repo`` is inside a git work tree or a git folder."""
    proc = git(repo, ["rev-parse", "--git-dir"])
    return proc is not None and proc.returncode == 0


def orig_head(repo: Path) -> str | None:
    """The pre-merge tip sha, or None when ORIG_HEAD is unset."""
    proc = git(repo, ["rev-parse", "-q", "--verify", "ORIG_HEAD"])
    if proc is None or proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def merge_commits(repo: Path) -> list[tuple[str, str]]:
    """Merge commits in ORIG_HEAD..HEAD as [(sha, subject), ...], oldest first."""
    proc = git(repo, ["log", *LOG_FLAGS, "--merges", "--format=%H%x00%s", "ORIG_HEAD..HEAD"])
    if proc is None or proc.returncode != 0:
        return []
    out: list[tuple[str, str]] = []
    for line in records(proc.stdout):
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        sha = sha.strip()
        if sha:
            out.append((sha, subject.strip()))
    out.reverse()  # git log is newest-first; process chronologically
    return out

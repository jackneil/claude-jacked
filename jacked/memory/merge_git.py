"""The git reads of merge capture: the merge list, the changed files, the trailers.

Every call runs ``git -C <repo>`` with the shared ``-c`` overrides
(``jacked.git_guard``), because the repository config is not trusted. The
output is bytes, decoded as UTF-8 with no newline translation, and records
split at LF only, so a CR inside a commit subject or a trailer value stays
in its value. A launch failure, a timeout or a non-zero exit gives None
(``git``), and each reader then gives its empty result.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from jacked import git_guard
from jacked.findbin import require_bin
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
    out = git(repo, ["diff", "--name-only", f"{sha}^1", sha])
    if out is None:
        return []
    return [line.strip() for line in records(out) if line.strip()]


def commit_trailers(repo: Path, sha: str) -> list[str]:
    """Ledger trailer lines of the commits the merge brings in, oldest first."""
    keys = ",".join(f"key={k}" for k in TRAILER_KEYS)
    out = git(repo, ["log", *LOG_FLAGS, "--reverse", f"--format=%(trailers:{keys},unfold)",
                     f"{sha}^1..{sha}"])
    if out is None:
        logger.debug("memory merge-capture: trailer read failed for %s", sha[:8])
        return []
    return cap_trailers(parse_trailers(out))


def git(repo: Path, args: list[str]) -> str | None:
    """The stdout of ``git -C repo <args>``, or None when git cannot start,
    times out, or exits non-zero.

    The read goes through ``git_guard.run_git`` with a ``Capture``, so the
    output is never held whole: it stops at ``git_guard.OUTPUT_LIMIT`` bytes
    and git is ended. A cut output keeps its complete lines only, and the cut
    goes to the DEBUG log (the size, never the content). The bytes are decoded
    as UTF-8 here, with no newline translation: text mode turns a CR inside a
    commit subject or a trailer value into LF, and the record split then cuts
    the value. Callers split records at LF only (``records``).
    """
    out = git_guard.Capture()
    try:
        code = git_guard.run_git(
            [require_bin("git"), "-C", str(repo), *git_guard.GIT_CONFIG_OVERRIDE_ARGS, *args],
            name=args[0], env=os.environ, timeout=GIT_TIMEOUT, consumer=out,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if code != 0 and not out.truncated:
        return None
    text = bytes(out.data).decode("utf-8", errors="replace")
    if out.truncated:
        text = text.rpartition("\n")[0] + "\n" if "\n" in text else ""
        logger.debug("memory merge-capture: git %s output cut at %d bytes", args[0], out.limit)
    return text


def records(out: str) -> list[str]:
    """The LF-ended records of git output. A CR or U+2028 stays in its record.

    >>> records("a\\rb\\nc\\u2028d\\n")
    ['a\\rb', 'c\\u2028d']
    """
    return [line for line in (out or "").split("\n") if line]


def is_git_repo(repo: Path) -> bool:
    """True when ``repo`` is inside a git work tree or a git folder."""
    return git(repo, ["rev-parse", "--git-dir"]) is not None


def orig_head(repo: Path) -> str | None:
    """The pre-merge tip sha, or None when ORIG_HEAD is unset."""
    out = git(repo, ["rev-parse", "-q", "--verify", "ORIG_HEAD"])
    return (out or "").strip() or None


def merge_commits(repo: Path) -> list[tuple[str, str]]:
    """Merge commits in ORIG_HEAD..HEAD as [(sha, subject), ...], oldest first."""
    text = git(repo, ["log", *LOG_FLAGS, "--merges", "--format=%H%x00%s", "ORIG_HEAD..HEAD"])
    if text is None:
        return []
    out: list[tuple[str, str]] = []
    for line in records(text):
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        sha = sha.strip()
        if sha:
            out.append((sha, subject.strip()))
    out.reverse()  # git log is newest-first; process chronologically
    return out

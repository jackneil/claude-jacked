"""The data that the recorder reads from a JUnit report, and the record of a run."""

from __future__ import annotations

import re
from dataclasses import dataclass

STATES = ("passed", "failed", "error", "skipped", "flaky")
RUNNERS = ("pytest", "vitest", "jest", "playwright", "go", "nextest", "unknown")
#: The pattern of a label, a machine label and a runner name.
LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")
#: The pattern of a machine identity: 32 lowercase hex characters.
MACHINE_ID_RE = re.compile(r"[0-9a-f]{32}")
#: The text of a machine whose record holds no valid label.
NO_LABEL = "(no label)"


def label_text(label: str | None) -> str:
    """Return a machine label for output. A record with no valid label shows as ``NO_LABEL``.

    >>> label_text("ci"), label_text(None)
    ('ci', '(no label)')
    """
    return NO_LABEL if label is None else label


def one_line(value) -> str:
    """Return stored free text (a test ID, a file, a time) as one line. None is empty.

    A line break shows as ``\\n``, so a stored value cannot add a line to the output.

    >>> one_line("a\\nb")
    'a\\\\nb'
    >>> one_line(None)
    ''
    """
    if value is None:
        return ""
    return str(value).replace("\r", "\\r").replace("\n", "\\n")


def label_order(label: str | None) -> tuple[bool, str]:
    """A sort key for a machine label: each real label first, then the empty label.

    >>> sorted(["ci", None, "box"], key=label_order)
    ['box', 'ci', None]
    """
    return (label is None, label or "")


@dataclass(frozen=True)
class CaseResult:
    """The result of one test, with one normalized ID.

    ``file`` is a repository-relative POSIX path inside the root, or None.
    ``seconds`` is None when the report holds no time. ``failure_type`` is
    the ``type`` attribute of the failure element, never its message.
    """

    id: str
    file: str | None
    seconds: float | None
    state: str
    failure_type: str | None
    attempts: int


@dataclass(frozen=True)
class RunData:
    """All tests of one run, and the total time that the report gives."""

    runner: str
    tests: tuple[CaseResult, ...]
    seconds_reported: float | None


@dataclass(frozen=True)
class TreeId:
    """The identity of the work tree of a git checkout.

    ``commit`` is the HEAD commit. ``tree`` is the hash of the tree that a
    commit of the work tree would have, changes that are not committed
    included. ``clean`` is True when that tree is the tree of HEAD.
    """

    commit: str
    tree: str
    clean: bool


@dataclass(frozen=True)
class RunMeta:
    """The record of one run of a gate, in the history store.

    A value that the recorder did not get is None (or an empty tuple for
    ``failing`` and ``flaky``). It is never zero. ``source`` is "run" or
    "record". ``verdict`` is "passed", "failed", "conflict", "no-data" or
    "unknown". ``command_sha`` is the SHA-256 of the typed command, never
    the command text. ``machine`` is None when a stored record holds no
    valid label.
    """

    id: str
    started: str
    source: str
    wall_seconds: float | None
    exit_code: int | None
    verdict: str
    kind: str | None
    label: str | None
    runner: str | None
    command_sha: str | None
    inputs_sha: str | None
    machine: str | None
    machine_id: str | None
    tree: TreeId | None
    tree_changed: bool | None
    on_default_branch: bool | None
    counts: dict[str, int] | None
    seconds_reported: float | None
    load_start: float | None
    load_end: float | None
    child_cpu_seconds: float | None
    failing: tuple[str, ...]
    flaky: tuple[str, ...]
    failing_sig: str | None


class JUnitError(ValueError):
    """A JUnit file cannot be read."""


class ListingError(ValueError):
    """A test listing holds an error line."""

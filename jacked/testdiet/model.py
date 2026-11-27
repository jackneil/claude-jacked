"""The data that the recorder reads from a JUnit report, and the record of a run."""

from __future__ import annotations

import logging
import re
import sys
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

STATES = ("passed", "failed", "error", "skipped", "flaky")
#: The states of a test that make the run fail.
FAILING_STATES = ("failed", "error")
RUNNERS = ("pytest", "vitest", "jest", "playwright", "go", "nextest", "unknown")
#: The values of ``--kind``: a label for the views only.
KINDS = ("full", "targeted")
#: The verdicts of a run, in the order of the views.
VERDICTS = ("passed", "failed", "conflict", "no-data", "unknown")
#: The ref of the remote default branch. No command fetches it.
DEFAULT_BRANCH_REF = "refs/remotes/origin/HEAD"
#: The rows of each text table of ``profile`` and ``history``, and the rows of the repeat notice.
DEFAULT_TOP = 20
#: The name of a TypeScript or JavaScript file (ids and loss read the same set).
SCRIPT_FILE_RE = re.compile(r"\.[cm]?[jt]sx?$")
#: The pattern of a label, a machine label and a runner name.
LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")
#: The size of a machine identity in random bytes. ``MACHINE_ID_RE`` derives from it.
MACHINE_ID_BYTES = 16
#: The pattern of a machine identity: ``2 * MACHINE_ID_BYTES`` lowercase hex characters.
MACHINE_ID_RE = re.compile(rf"[0-9a-f]{{{2 * MACHINE_ID_BYTES}}}")
#: The version of the record format. ``seen`` proves a tree only with a record of this version.
SCHEMA = 1
#: The text of a machine whose record holds no valid label.
NO_LABEL = "(no label)"


def label_text(label: str | None) -> str:
    """Return a machine label for output. A record with no valid label shows as ``NO_LABEL``.

    >>> label_text("ci"), label_text(None)
    ('ci', '(no label)')
    """
    return NO_LABEL if label is None else label


# Each character that str.splitlines() reads as a line boundary, and each
# bidi control (the "Trojan Source" class). A stored value shows each one as a
# visible escape, so it cannot add a line or change how a line reads.
_LINE_BOUNDARIES = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_BIDI_CONTROLS = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
_ESCAPES = {ord(ch): ("\\n" if ch == "\n" else "\\r" if ch == "\r"
                      else f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
            for ch in _LINE_BOUNDARIES + _BIDI_CONTROLS}


def one_line(value) -> str:
    """Return stored free text (a test ID, a file, a time) as one line. None is empty.

    Each line boundary of ``str.splitlines`` and each bidi control shows as
    an escape (``\\n``, ``\\u2028``, ``\\u202e``), so a stored value cannot
    add a line to the output or turn the text of a line around. A lone
    surrogate (a file name that is not UTF-8) shows as ``\\udcff``.

    >>> one_line("a\\nb")
    'a\\\\nb'
    >>> one_line("a\\u2028b")
    'a\\\\u2028b'
    >>> one_line(None)
    ''
    """
    if value is None:
        return ""
    text = str(value).translate(_ESCAPES)
    # A file name that is not UTF-8 holds a lone surrogate (os.fsdecode); a
    # strict UTF-8 stream cannot write it, so it shows as an escape.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


#: The first text of each message that the recorder prints on standard error.
PREFIX = "jacked tests: "


def clean_text(text: str) -> str:
    """Remove each control character from ``text``, except the newline and the tab.

    The control characters are the Unicode category Cc: C0, DEL and C1 (C1
    holds CSI, U+009B, which a terminal reads as ESC ``[``).

    >>> clean_text("a\\x1b[31mb\\u009bc\\td")
    'a[31mbc\\td'
    """
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")


#: The texts of a value that the record does not hold. A view never prints an empty slot.
NO_START = "no start time recorded"
NO_VERDICT = "not recorded"
NO_SOURCE = "source not recorded"


def or_text(value, missing: str) -> str:
    """Return ``one_line(value)``, or ``missing`` when that is empty.

    >>> or_text(None, NO_START), or_text("2026-11-26", NO_START)
    ('no start time recorded', '2026-11-26')
    """
    return one_line(value) or missing


def error_text(exc: BaseException) -> str:
    """The class and the cause of ``exc`` for one output line.

    An ``OSError`` can hold a path, so it gives the class, the errno name and
    the system text of the errno, never the path. Another error gives its
    class and its message, escaped by ``one_line``.

    >>> error_text(PermissionError(13, "Permission denied", "/home/u/x"))
    'PermissionError(EACCES): Permission denied'
    >>> error_text(ValueError("a\\nb"))
    'ValueError: a\\\\nb'
    """
    from jacked.public_errors import describe, os_error_reason

    if isinstance(exc, OSError):
        return f"{describe(exc)}: {os_error_reason(exc)}"
    message = one_line(str(exc))
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def emit(message: str) -> None:
    """Print one message on standard error, as one line, with the prefix and no control characters.

    Each caller gives one line. A line boundary or a bidi control in the
    message (a file name, the text of an error) shows as an escape, as in
    ``one_line``. A standard error that cannot be written (closed, a broken
    pipe) is ignored, because a message must never stop or change the gate.
    """
    try:
        print(PREFIX + clean_text(one_line(message)), file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 -- a message must never stop or change the gate
        logger.debug("emit: standard error cannot be written", exc_info=True)


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
    """All tests of one run, and the total time that the report gives.

    ``suite_failures`` counts the failures and errors that a suite counts
    and that no test case of that suite holds (a failure at suite level).
    It is not stored in the tests file; the verdict of the record keeps it.
    """

    runner: str
    tests: tuple[CaseResult, ...]
    seconds_reported: float | None
    suite_failures: int = 0


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
    "unknown". ``command_sha`` is the SHA-256 of the gate folder and the
    typed command, never the command text. ``folder`` is the work-tree
    relative POSIX path of the folder that the gate ran in ("." at the top),
    or None for a record of ``record``. ``schema`` is the version of the
    record format (``SCHEMA``), or None for a record with no version.
    ``machine`` is None when a stored record holds no valid label.
    ``test_seconds`` is the sum of the seconds of the tests that have a
    time, or None when no test has one (or the run has no report).
    ``interrupted`` is True when the user stopped the run (Ctrl-C) while
    the gate ran, False when not, and None for a record of ``record``.
    ``inputs_changed`` is True when the ``--input`` fingerprint after the
    gate is not the one before it, and None with no ``--input``.
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
    folder: str | None = None
    schema: int | None = None
    test_seconds: float | None = None
    interrupted: bool | None = None
    inputs_changed: bool | None = None


class JUnitError(ValueError):
    """A JUnit file cannot be read."""


class ListingError(ValueError):
    """A test listing holds an error line."""


#: A reason in ``Declaration.unread``.
NOT_PARSED = "does not parse"
BODY_NOT_READ = "body not read"
COMPUTED_CASES = "computed cases"


@dataclass(frozen=True)
class Declaration:
    """One test of a file, as ``loss`` reads it.

    ``asserts``, ``skipped`` and ``cases`` are None when the source cannot
    give them. ``unread`` holds the reason of each None value. ``body`` is
    the ``body_print`` of the body, or None when the body is not read.
    """

    name: str
    asserts: int | None
    skipped: bool | None
    cases: int | None = 1
    unread: tuple[str, ...] = ()
    body: str | None = None


def body_print(text: str) -> str:
    """A short SHA-256 of a test body with no white space, so a new indent gives the same value.

    >>> body_print("assert 1") == body_print("  assert   1\\n")
    True
    """
    import hashlib

    return hashlib.sha256("".join(text.split()).encode("utf-8", "surrogatepass")).hexdigest()[:16]

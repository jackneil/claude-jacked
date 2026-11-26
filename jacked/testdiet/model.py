"""The data that the recorder reads from a JUnit report."""

from __future__ import annotations

from dataclasses import dataclass

STATES = ("passed", "failed", "error", "skipped", "flaky")
RUNNERS = ("pytest", "vitest", "jest", "playwright", "go", "nextest", "unknown")


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


class JUnitError(ValueError):
    """A JUnit file cannot be read."""


class ListingError(ValueError):
    """A test listing holds an error line."""

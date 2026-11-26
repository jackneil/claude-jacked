"""Read JUnit XML files of a test run into one ``RunData``.

The reader keeps IDs, files, times, states and the class name of a failure.
It never keeps the message of a failure or the text of an element: the
parser target ignores all text, and the reader reads no ``message``
attribute. A file with a DOCTYPE (and so with an entity declaration) raises
``JUnitError`` before a case is read. The reader has no size limit: it feeds
the file to the parser in blocks.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from jacked.regular_file import open_regular_fd
from jacked.testdiet.ids import TreeLookup, _normalize, detect_runner
from jacked.testdiet.model import CaseResult, JUnitError, RunData

_BLOCK = 1 << 16
# A failed attempt that the testcase also shows (the elements of Maven
# Surefire that rerun tools use). "flaky" ones end in a pass.
_FLAKY_ATTEMPTS = {"flakyFailure": "failed", "flakyError": "error"}
_RERUN_ATTEMPTS = {"rerunFailure": "failed", "rerunError": "error"}
# The worst state first.
_ORDER = ("error", "failed", "passed", "skipped")


def _seconds(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number >= 0 and number == number and number != float("inf") else None


class _Target:
    """The parser target. It reads attributes only. It keeps no text."""

    def __init__(self) -> None:
        self.root: dict[str, str] | None = None
        self.properties: dict[str, str] = {}
        self.cases: list[dict] = []
        self._case: dict | None = None
        self._depth = 0
        self._case_depth = 0
        self._suites = 0
        # The times of the outer suites, for a root element with no time.
        self.suite_times: list[float | None] = []

    def start(self, tag: str, attrib: dict[str, str]) -> None:
        self._depth += 1
        if self.root is None:
            self.root = {k: attrib[k] for k in ("name", "id", "time") if k in attrib}
        if tag == "testsuite":
            self._suites += 1
            if self._depth == 2:
                self.suite_times.append(_seconds(attrib.get("time")))
        if tag == "testcase" and self._case is None:
            self._case = {
                "classname": attrib.get("classname", ""),
                "name": attrib.get("name", ""),
                "file": attrib.get("file") or None,
                "seconds": _seconds(attrib.get("time")),
                "outcomes": [],
                "extra": [],
            }
            self._case_depth = self._depth
            return
        if self._case is not None and self._depth == self._case_depth + 1:
            failure_type = attrib.get("type") or None
            if tag in ("failure", "error", "skipped"):
                self._case["outcomes"].append((tag, failure_type))
            elif tag in _FLAKY_ATTEMPTS or tag in _RERUN_ATTEMPTS:
                self._case["extra"].append((tag, failure_type))
        elif (self._case is None and tag == "property" and self._suites == 1 and not self.cases
              and attrib.get("name") == "go.version"):
            self.properties["go.version"] = "1"

    def end(self, tag: str) -> None:
        if self._case is not None and tag == "testcase" and self._depth == self._case_depth:
            self.cases.append(self._case)
            self._case = None
        self._depth -= 1

    def data(self, data: str) -> None:
        pass

    def doctype(self, name, pubid, system) -> None:
        raise JUnitError("the file has a DOCTYPE; a JUnit file must not have one")

    def close(self) -> None:
        return None


def _parse(path: Path) -> _Target:
    target = _Target()
    parser = ET.XMLParser(target=target)
    try:
        fd = open_regular_fd(path)
    except OSError as exc:
        raise JUnitError(f"cannot open {Path(path).name}: {exc.strerror or exc}") from exc
    try:
        while block := os.read(fd, _BLOCK):
            parser.feed(block)
        parser.close()
    except ET.ParseError as exc:
        raise JUnitError(f"{Path(path).name} is not complete XML: {exc}") from exc
    finally:
        os.close(fd)
    if target.root is None:
        raise JUnitError(f"{Path(path).name} has no element")
    return target


def _attempts(case: dict) -> list[tuple[str, str | None]]:
    """Return each attempt of one testcase element as (state, failure type)."""
    kinds = {tag for tag, _ in case["outcomes"]}
    if "error" in kinds:
        final = ("error", next(t for tag, t in case["outcomes"] if tag == "error"))
    elif "failure" in kinds:
        final = ("failed", next(t for tag, t in case["outcomes"] if tag == "failure"))
    elif "skipped" in kinds:
        final = ("skipped", None)
    else:
        final = ("passed", None)
    earlier = [(_FLAKY_ATTEMPTS.get(tag) or _RERUN_ATTEMPTS[tag], t) for tag, t in case["extra"]]
    return [*earlier, final]


def _state(states: list[str]) -> str:
    if "passed" in states and ("failed" in states or "error" in states):
        return "flaky"
    return next(state for state in _ORDER if state in states)


def read_junit(paths: Sequence[Path], *, root: Path, report_root: str | None = None,
               runner: str | None = None) -> RunData:
    """Read the JUnit files ``paths`` of one run.

    ``root`` is the folder of the repository. ``report_root`` is the root
    folder of the machine that wrote the report, when that is not ``root``.
    ``runner`` is found from the files when it is None.

    One ID that is in the files more than one time gives one ``CaseResult``:
    ``attempts`` counts the attempts, the seconds are the sum, and the state
    is ``flaky`` when one attempt passed and one failed or had an error.
    """
    lookup = TreeLookup(root)
    runners: list[str] = []
    merged: dict[str, dict] = {}
    reported: list[float] = []
    for path in paths:
        target = _parse(path)
        suite = {**(target.root or {}), **target.properties}
        first = target.cases[0] if target.cases else {}
        file_runner = runner or detect_runner(suite, {"classname": first.get("classname", "")})
        runners.append(file_runner)
        seconds = _seconds((target.root or {}).get("time"))
        if seconds is None and target.suite_times and None not in target.suite_times:
            # pytest writes its time on the suite, not on the root element.
            seconds = sum(target.suite_times)
        if seconds is not None:
            reported.append(seconds)
        for case in target.cases:
            test_id, file = _normalize(file_runner, case["classname"], case["name"], case["file"],
                                       root, report_root, lookup)
            entry = merged.setdefault(test_id, {"file": None, "seconds": [], "attempts": []})
            entry["file"] = entry["file"] or file
            if case["seconds"] is not None:
                entry["seconds"].append(case["seconds"])
            entry["attempts"].extend(_attempts(case))
    tests = []
    for test_id, entry in merged.items():
        states = [state for state, _ in entry["attempts"]]
        failure_type = next((t for state, t in entry["attempts"] if state in ("failed", "error") and t), None)
        tests.append(CaseResult(
            id=test_id,
            file=entry["file"],
            seconds=sum(entry["seconds"]) if entry["seconds"] else None,
            state=_state(states),
            failure_type=failure_type,
            attempts=len(entry["attempts"]),
        ))
    distinct = set(runners)
    return RunData(
        runner=distinct.pop() if len(distinct) == 1 else "unknown",
        tests=tuple(tests),
        seconds_reported=sum(reported) if reported else None,
    )

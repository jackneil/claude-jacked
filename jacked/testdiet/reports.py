"""The report files of a wrapped run: the ``{junit}`` placeholder and the ``--junit`` globs.

``placeholder`` makes the private report folder for an argument that holds
``{junit}``. ``hash_fixed`` takes the SHA-256 of each ``--junit`` match
before the gate, and ``read_reports`` reads the reports after the gate.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from jacked.regular_file import open_regular_fd
from jacked.testdiet import globs, junit
from jacked.testdiet.model import RunData, emit

#: The text in a gate argument that ``run_gate`` replaces with the path of a private report file.
JUNIT_PLACEHOLDER = "{junit}"


def sha_of(path: Path) -> str | None:
    """The SHA-256 of a regular file, or None when it is not one (a FIFO is never opened for a read)."""
    try:
        fd = open_regular_fd(path)
    except OSError:
        return None
    digest = hashlib.sha256()
    try:
        while block := os.read(fd, 1 << 16):
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def matches(top: Path, patterns: Sequence[str]) -> list[Path]:
    """The paths that the globs match. A relative glob starts at ``top``. No path is listed two times."""
    found: dict[str, Path] = {}
    for pattern in patterns:
        for name in globs.matches(top, pattern):
            path = globs.full_path(top, name)
            found.setdefault(os.path.normpath(path), path)
    return list(found.values())


def hash_fixed(top: Path, patterns: Sequence[str]) -> dict[str, str | None]:
    """The SHA-256 of each file that the ``--junit`` globs match, keyed by its normalized path."""
    return {os.path.normpath(p): sha_of(p) for p in matches(top, patterns)}


def placeholder(argv: list[str], make_folder) -> tuple[Path | None, Path | None, list[str]]:
    """Make the private report folder when an argument holds ``{junit}``.

    Return the folder, the report path and the argv of the child.
    ``make_folder`` makes the folder, or returns None when it cannot. Then
    the child gets ``argv`` as it is.
    """
    if not any(JUNIT_PLACEHOLDER in arg for arg in argv):
        return None, None, argv
    folder = make_folder()
    if folder is None:
        return None, None, argv
    report = folder / "junit.xml"
    return folder, report, [arg.replace(JUNIT_PLACEHOLDER, str(report)) for arg in argv]


def make_report_folder() -> Path:
    """A new private folder for the ``{junit}`` report, outside the work tree."""
    return Path(tempfile.mkdtemp(prefix="jacked-junit-"))


def read_reports(top: Path, report: Path | None, patterns: Sequence[str],
                 fixed_before: dict[str, str | None] | None, runner: str | None) -> RunData | None:
    """Read the report at the placeholder path and each fixed report that is new or changed.

    The read is all or nothing: when one file cannot be read, the run has no
    data. A report that is left out could hold a failure. ``fixed_before``
    is None when the fixed reports were not hashed before the run: then no
    fixed report is read.
    """
    paths: list[Path] = []
    if report is not None:
        if os.path.lexists(report):
            paths.append(report)
        else:
            emit("the gate wrote no report at the {junit} path.")
    if fixed_before is None:
        emit("did not read the --junit reports: they were not hashed before the run.")
        patterns = ()
    for path in matches(top, patterns):
        key = os.path.normpath(path)
        now = sha_of(path)
        if now is None:
            emit(f"did not read {path}: it is not a regular file.")
        elif key in fixed_before and fixed_before[key] == now:
            emit(f"did not read {path}: its content did not change during the run.")
        else:
            paths.append(path)
    if patterns and not any(matches(top, [g]) for g in patterns):
        emit("no file matches the --junit pattern(s).")
    if not paths:
        return None
    try:
        return junit.read_junit(paths, root=top, runner=runner)
    except (junit.JUnitError, OSError) as exc:
        emit(f"cannot read the report, so the run has no test data: {exc}")
        return None

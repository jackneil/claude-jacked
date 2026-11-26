"""The local store of test runs.

Each run is two files in ``<store>/runs/``: ``<id>.tests.json.gz`` (the
tests) and then ``<id>.meta.json`` (the record). The meta file is the commit
marker: a run with no meta file does not exist. No file is shared between
runs, so two sessions that write at the same time cannot damage a record.
Each file is written atomically.

The store holds IDs, numbers, states and the class name of a failure. It
holds no failure message, no test output and no command text. Nothing is
deleted automatically: ``prune`` deletes on request.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import os
import re
import secrets
import sys
import zlib
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jacked.atomic_io import atomic_write_bytes
from jacked.regular_file import open_regular_fd
from jacked.testdiet.model import LABEL_RE, MACHINE_ID_RE, CaseResult, RunData, RunMeta, TreeId

RUNS = "runs"
META_SUFFIX = ".meta.json"
TESTS_SUFFIX = ".tests.json.gz"
#: A run ID: the UTC time to the microsecond, then 8 random hex characters.
RUN_ID_RE = re.compile(r"(\d{8})T\d{12}Z-[0-9a-f]{8}")

_FAILING_STATES = ("failed", "error")
_RAN_STATES = ("passed", "flaky")
#: A tests file with no meta file is a write that is in progress or that
#: stopped. A write takes milliseconds, so ``prune`` deletes such a file only
#: when its run ID is older than this margin. It is a margin for a live
#: writer, not a limit on data.
ORPHAN_AGE = timedelta(hours=24)
_HEX_ID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_META_FIELDS = tuple(f.name for f in dataclasses.fields(RunMeta))
_CASE_FIELDS = tuple(f.name for f in dataclasses.fields(CaseResult))
_TUPLE_FIELDS = ("failing", "flaky")
_INT_FIELDS = ("exit_code",)
_NUMBER_FIELDS = ("wall_seconds", "seconds_reported", "load_start", "load_end", "child_cpu_seconds")
_BOOL_FIELDS = ("tree_changed", "on_default_branch")


class DamagedRecordError(ValueError):
    """A file of the store cannot be read as a record."""


class PruneError(OSError):
    """``prune`` could not delete some files.

    ``deleted`` holds the run IDs that are gone. ``failed`` holds each file
    name that stays, with the class name of its error.
    """

    def __init__(self, deleted: list[str], failed: list[tuple[str, str]]):
        names = ", ".join(f"{name} ({error})" for name, error in failed)
        super().__init__(f"prune could not delete {len(failed)} file(s): {names}")
        self.deleted = deleted
        self.failed = failed


def _read(path: Path) -> bytes:
    """Read a store file. A path that is not a regular file raises ``NotRegularFileError``."""
    with os.fdopen(open_regular_fd(path), "rb") as handle:
        return handle.read()


def id_time(run_id: str) -> datetime:
    """Return the UTC time of a run ID: the clock of the machine that wrote the run.

    >>> id_time("20261126T090501000007Z-0a1b2c3d").isoformat()
    '2026-11-26T09:05:01.000007+00:00'
    """
    return datetime.strptime(run_id[:21], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)


def new_run_id(now: datetime | None = None) -> str:
    """Return a new run ID. IDs sort in the order of their UTC time.

    >>> new_run_id(datetime(2026, 11, 26, 9, 5, 1, 7, tzinfo=timezone.utc))[:23]
    '20261126T090501000007Z-'
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{moment:%Y%m%dT%H%M%S%f}Z-{secrets.token_hex(4)}"


def _check_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError("a run ID must be <UTC time>Z-<8 hex characters>")
    return run_id


def failing_signature(cases: Iterable[CaseResult]) -> str | None:
    """Return the SHA-256 of the sorted (id, failure_type) pairs of the failed and error tests.

    A flaky test is not in the failing set. The result is None when no test failed.
    """
    pairs = sorted({(c.id, c.failure_type or "") for c in cases if c.state in _FAILING_STATES})
    if not pairs:
        return None
    text = json.dumps(pairs, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def verdict_of(exit_code: int | None, data: RunData | None) -> str:
    """Return the verdict of a run.

    "unknown": no exit code. "failed": an exit code that is not 0.
    "no-data": exit code 0 and no report, or a report in which no test
    ran (no test, or only skipped tests). "conflict": exit code 0 and a
    failed or an error test. "passed": exit code 0, no failed and no error
    test, and at least one test that passed (a flaky test passed too).
    """
    if exit_code is None:
        return "unknown"
    if exit_code != 0:
        return "failed"
    if data is None or not data.tests:
        return "no-data"
    if any(case.state in _FAILING_STATES for case in data.tests):
        return "conflict"
    if not any(case.state in _RAN_STATES for case in data.tests):
        return "no-data"
    return "passed"


def write_run(store: Path, meta: RunMeta, data: RunData | None) -> None:
    """Write one run into ``store``: the tests file first, then the meta file.

    With no ``data``, only the meta file is written.
    """
    run_id = _check_id(meta.id)
    runs = Path(store) / RUNS
    if data is not None:
        payload = {"runner": data.runner, "seconds_reported": data.seconds_reported,
                   "tests": [dataclasses.asdict(case) for case in data.tests]}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        atomic_write_bytes(runs / f"{run_id}{TESTS_SUFFIX}", gzip.compress(raw, mtime=0))
    text = json.dumps(dataclasses.asdict(meta), ensure_ascii=False, indent=1, sort_keys=True)
    atomic_write_bytes(runs / f"{run_id}{META_SUFFIX}", (text + "\n").encode("utf-8"))


def _is(value, kinds) -> bool:
    # bool is an int in Python; a number field never takes a bool.
    return isinstance(value, kinds) and not isinstance(value, bool)


def _meta_from(run_id: str, raw) -> RunMeta:
    """Build a RunMeta from the JSON of a meta file.

    A field that this version does not know is ignored. A field that the
    file does not hold is None (an empty tuple for ``failing`` and
    ``flaky``). A machine label that does not match ``LABEL_RE`` and a
    machine identity that is not 32 lowercase hex characters read as None,
    and the record is still read. Another field of the wrong type makes the
    file damaged.
    """
    if not isinstance(raw, dict):
        raise DamagedRecordError("the meta file is not an object")
    values: dict = {}
    for name in _META_FIELDS:
        value = raw.get(name)
        if name == "id":
            value = run_id
        elif name in _TUPLE_FIELDS:
            value = () if value is None else value
            if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
                raise DamagedRecordError(f"{name} is not a list of IDs")
            value = tuple(value)
        elif name == "machine":
            # A label that is not valid reads as empty. The record is not damaged.
            value = value if isinstance(value, str) and LABEL_RE.fullmatch(value) else None
        elif name == "machine_id":
            value = value if isinstance(value, str) and MACHINE_ID_RE.fullmatch(value) else None
        elif value is None:
            pass
        elif name == "tree":
            value = _tree_from(value)
        elif name == "counts":
            if not isinstance(value, dict) or not all(_is(v, int) for v in value.values()):
                raise DamagedRecordError("counts is not a map of numbers")
        elif name in _INT_FIELDS:
            if not _is(value, int):
                raise DamagedRecordError(f"{name} is not an integer")
        elif name in _NUMBER_FIELDS:
            if not _is(value, (int, float)):
                raise DamagedRecordError(f"{name} is not a number")
        elif name in _BOOL_FIELDS:
            if not isinstance(value, bool):
                raise DamagedRecordError(f"{name} is not true or false")
        elif not isinstance(value, str):
            raise DamagedRecordError(f"{name} is not text")
        values[name] = value
    return RunMeta(**values)


def _tree_from(value) -> TreeId | None:
    """A tree identity, or None when a sub-field is missing or has the wrong type."""
    if not isinstance(value, dict):
        return None
    commit, tree_hash, clean = value.get("commit"), value.get("tree"), value.get("clean")
    if not (isinstance(commit, str) and _HEX_ID_RE.fullmatch(commit)
            and isinstance(tree_hash, str) and _HEX_ID_RE.fullmatch(tree_hash)
            and isinstance(clean, bool)):
        return None
    return TreeId(commit=commit, tree=tree_hash, clean=clean)


def _warn(name: str, exc: Exception) -> None:
    print(f"jacked tests: skipped the damaged record {name} ({type(exc).__name__})",
          file=sys.stderr)


def iter_runs(store: Path) -> Iterator[RunMeta]:
    """Yield each run of ``store``, newest first by the run ID.

    A run with no meta file does not exist. A damaged meta file is skipped,
    with one warning on standard error for each file.
    """
    runs = Path(store) / RUNS
    try:
        names = [p.name for p in runs.iterdir()]
    except FileNotFoundError:
        return
    ids = sorted((n[: -len(META_SUFFIX)] for n in names if n.endswith(META_SUFFIX)), reverse=True)
    for run_id in ids:
        if not RUN_ID_RE.fullmatch(run_id):
            continue
        name = f"{run_id}{META_SUFFIX}"
        try:
            meta = _meta_from(run_id, json.loads(_read(runs / name)))
        except FileNotFoundError:
            continue  # pruned while the list was read
        except (OSError, ValueError, TypeError) as exc:
            _warn(name, exc)
            continue
        yield meta


def load_tests(store: Path, run_id: str) -> RunData | None:
    """Return the tests of the run ``run_id``, or None when the run has no tests file.

    A damaged tests file gives None, with one warning on standard error.
    """
    name = f"{_check_id(run_id)}{TESTS_SUFFIX}"
    try:
        raw = json.loads(gzip.decompress(_read(Path(store) / RUNS / name)))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError, zlib.error) as exc:
        _warn(name, exc)
        return None
    try:
        tests = tuple(CaseResult(**{k: case.get(k) for k in _CASE_FIELDS}) for case in raw["tests"])
        return RunData(runner=raw["runner"], tests=tests, seconds_reported=raw.get("seconds_reported"))
    except (KeyError, TypeError, AttributeError) as exc:
        _warn(name, exc)
        return None


def prune(store: Path, before: date, *, now: datetime | None = None) -> list[str]:
    """Delete each run whose UTC date is before ``before``. Return the deleted run IDs, sorted.

    For a complete run, the meta file goes first, so the run is gone in one
    step, and then its tests file. A tests file with no meta file is a write
    that is in progress or that stopped: it goes only when its run ID is
    also more than 24 hours (``ORPHAN_AGE``) before ``now``. A file that is
    not a run file stays. ``now`` must be an aware datetime. When a file
    cannot be deleted, the other files still go, and then ``PruneError``
    names each file that stays and holds the deleted IDs.
    """
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be an aware datetime (UTC)")
    runs = Path(store) / RUNS
    try:
        names = set(os.listdir(runs))
    except FileNotFoundError:
        return []
    limit = f"{before:%Y%m%d}"
    orphan_limit = (now or datetime.now(timezone.utc)) - ORPHAN_AGE
    found: dict[str, set[str]] = {}
    for name in names:
        for suffix in (META_SUFFIX, TESTS_SUFFIX):
            run_id = name[: -len(suffix)]
            if name.endswith(suffix) and (match := RUN_ID_RE.fullmatch(run_id)):
                if match.group(1) < limit:
                    found.setdefault(run_id, set()).add(suffix)
    deleted: list[str] = []
    failed: list[tuple[str, str]] = []
    for run_id in sorted(found):
        suffixes = found[run_id]
        if META_SUFFIX not in suffixes and id_time(run_id) >= orphan_limit:
            continue  # a write can still be in progress
        gone = False
        for suffix in (META_SUFFIX, TESTS_SUFFIX):
            if suffix not in suffixes:
                continue
            try:
                (runs / f"{run_id}{suffix}").unlink(missing_ok=True)
            except OSError as exc:
                failed.append((f"{run_id}{suffix}", type(exc).__name__))
                if suffix == META_SUFFIX:
                    break  # the run still exists; keep its tests file
            else:
                gone = True
        if gone:
            deleted.append(run_id)
    if failed:
        raise PruneError(deleted, failed)
    return deleted

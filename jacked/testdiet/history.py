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
import re
import secrets
import sys
from collections.abc import Iterable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path

from jacked.atomic_io import atomic_write_bytes
from jacked.testdiet.model import CaseResult, RunData, RunMeta, TreeId

RUNS = "runs"
META_SUFFIX = ".meta.json"
TESTS_SUFFIX = ".tests.json.gz"
#: A run ID: the UTC time to the microsecond, then 8 random hex characters.
RUN_ID_RE = re.compile(r"(\d{8})T\d{12}Z-[0-9a-f]{8}")

_FAILING_STATES = ("failed", "error")
_META_FIELDS = tuple(f.name for f in dataclasses.fields(RunMeta))
_CASE_FIELDS = tuple(f.name for f in dataclasses.fields(CaseResult))
_TREE_FIELDS = tuple(f.name for f in dataclasses.fields(TreeId))
_TUPLE_FIELDS = ("failing", "flaky")
_INT_FIELDS = ("exit_code",)
_NUMBER_FIELDS = ("wall_seconds", "seconds_reported", "load_start", "load_end", "child_cpu_seconds")
_BOOL_FIELDS = ("tree_changed", "on_default_branch")


class DamagedRecordError(ValueError):
    """A file of the store cannot be read as a record."""


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
    "no-data": exit code 0 and no report, or a report with no test.
    "conflict": exit code 0 and a failed or an error test. "passed": all
    other runs.
    """
    if exit_code is None:
        return "unknown"
    if exit_code != 0:
        return "failed"
    if data is None or not data.tests:
        return "no-data"
    if any(case.state in _FAILING_STATES for case in data.tests):
        return "conflict"
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
    ``flaky``). A field of the wrong type makes the file damaged.
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
        elif value is None:
            pass
        elif name == "tree":
            if not isinstance(value, dict) or not all(k in value for k in _TREE_FIELDS):
                raise DamagedRecordError("tree is not a tree identity")
            value = TreeId(**{k: value[k] for k in _TREE_FIELDS})
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
            meta = _meta_from(run_id, json.loads((runs / name).read_bytes()))
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
        raw = json.loads(gzip.decompress((Path(store) / RUNS / name).read_bytes()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, EOFError) as exc:
        _warn(name, exc)
        return None
    try:
        tests = tuple(CaseResult(**{k: case.get(k) for k in _CASE_FIELDS}) for case in raw["tests"])
        return RunData(runner=raw["runner"], tests=tests, seconds_reported=raw.get("seconds_reported"))
    except (KeyError, TypeError, AttributeError) as exc:
        _warn(name, exc)
        return None


def prune(store: Path, before: date) -> list[str]:
    """Delete each run whose UTC date is before ``before``. Return the deleted run IDs, sorted.

    The meta file goes first, so a run never shows with no tests. A tests
    file with no meta file (a write that stopped) goes too. A file that is
    not a run file stays.
    """
    runs = Path(store) / RUNS
    try:
        names = [p.name for p in runs.iterdir()]
    except FileNotFoundError:
        return []
    limit = f"{before:%Y%m%d}"
    doomed: dict[str, list[str]] = {}
    for name in names:
        for suffix in (META_SUFFIX, TESTS_SUFFIX):
            if name.endswith(suffix):
                match = RUN_ID_RE.fullmatch(name[: -len(suffix)])
                if match and match.group(1) < limit:
                    doomed.setdefault(name[: -len(suffix)], []).append(suffix)
    for run_id, suffixes in doomed.items():
        for suffix in (META_SUFFIX, TESTS_SUFFIX):
            if suffix in suffixes:
                (runs / f"{run_id}{suffix}").unlink(missing_ok=True)
    return sorted(doomed)

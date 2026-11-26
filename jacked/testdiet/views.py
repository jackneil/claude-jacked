"""The numbers of ``jacked tests profile`` and ``jacked tests history``.

A machine is the pair of its label and its private identity. A view reads
the runs of one machine only, because the times of two machines are never
compared. With no machine label, a view uses the machine of the newest run
whose kind is "full", and it says which machine it used. No result and no
printed line holds a machine identity.

A value that a record does not hold stays None. It prints as empty, never
as zero. A view prints numbers. It does not decide what a number means.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jacked.testdiet import history as _history
from jacked.testdiet.model import CaseResult, RunMeta
from jacked.testdiet.record import clean_text

VERDICTS = ("passed", "failed", "conflict", "no-data", "unknown")
#: The text output shows this number of rows in each table when no --top is given.
DEFAULT_TOP = 20


class ViewError(ValueError):
    """A view cannot be made (no run, or no run of the given machine or ID)."""


def _key(meta: RunMeta) -> tuple[str, str | None]:
    return (meta.machine, meta.machine_id)


def _select(runs: list[RunMeta], machine: str | None) -> tuple[tuple[str, str | None], list[str]]:
    """Return the machine of a view and the lines that say why. ``runs`` is newest first."""
    if not runs:
        raise ViewError("the history has no run")
    candidates = runs if machine is None else [m for m in runs if m.machine == machine]
    if not candidates:
        raise ViewError(f"the history has no run of the machine {machine}")
    full = [m for m in candidates if m.kind == "full"]
    pick = full[0] if full else candidates[0]
    if machine is None:
        why = "the machine of the newest full run" if full else \
            "the machine of the newest run, because no run has the kind full"
    else:
        why = "the machine label that you gave"
    notes = [f"machine: {pick.machine} ({why})"]
    identities = {m.machine_id for m in runs if m.machine == pick.machine}
    if len(identities) > 1:
        which = "full run" if full else "run"
        notes.append(f"the label {pick.machine} has {len(identities)} machine identities. "
                     f"This view uses the identity of its newest {which}.")
    return _key(pick), notes


def _sum(values: Iterable[float | None]) -> float | None:
    """The sum of the values that are not None, or None when all are None."""
    known = [v for v in values if v is not None]
    return sum(known) if known else None


def _min_max(values: Iterable[float | None]) -> tuple[float | None, float | None]:
    known = [v for v in values if v is not None]
    return (min(known), max(known)) if known else (None, None)


def _seconds_order(seconds: float | None, name: str | None) -> tuple:
    """Sort the largest seconds first, and a row with no seconds last."""
    return (seconds is None, -(seconds or 0.0), name or "")


def _test_seconds(store: Path, run_id: str) -> float | None:
    data = _history.load_tests(store, run_id)
    return None if data is None else _sum(case.seconds for case in data.tests)


def _groups(runs: Iterable[RunMeta]) -> dict[tuple[str, str], list[RunMeta]]:
    """Group the runs that have a tree and a command SHA by (tree, command SHA). None is never a key."""
    found: dict[tuple[str, str], list[RunMeta]] = {}
    for meta in runs:
        if meta.tree is not None and meta.command_sha:
            found.setdefault((meta.tree.tree, meta.command_sha), []).append(meta)
    return found


def files_for_half(seconds: Iterable[float | None]) -> int | None:
    """Return the fewest files that hold half (or more) of the seconds of all files.

    A file with no seconds is not counted. The result is None when no file
    has seconds.

    >>> files_for_half([10, 5, 3, 1, 1])
    1
    >>> files_for_half([1, 1, 1, 1])
    2
    >>> files_for_half([None]) is None
    True
    """
    known = sorted((s for s in seconds if s is not None), reverse=True)
    if not known:
        return None
    half, total, count = sum(known) / 2, 0.0, 0
    while total < half:
        total += known[count]
        count += 1
    return count


def _file_rows(tests: tuple[CaseResult, ...]) -> list[dict]:
    by_file: dict[str | None, list[CaseResult]] = {}
    for case in tests:
        by_file.setdefault(case.file, []).append(case)
    rows = [{"file": name, "seconds": _sum(c.seconds for c in cases), "tests": len(cases)}
            for name, cases in by_file.items()]
    rows.sort(key=lambda r: _seconds_order(r["seconds"], r["file"]))
    return rows


def profile(store: Path, run_id: str | None = None, *, machine: str | None = None,
            top: int | None = DEFAULT_TOP) -> dict:
    """Return where the time of one run goes, and the spread of its repeat runs.

    With no ``run_id``, the run is the newest full run of the machine (the
    newest run when the machine has no full run). Each list holds every
    row. ``top`` only sets ``hidden_rows``: the number of rows of each table
    that the text output does not show. ``repeats`` compares runs of the
    same machine only: one group for each tree and command with two runs or
    more. ``setup_share`` is None, because a JUnit record holds no setup time.
    """
    store = Path(store)
    runs = list(_history.iter_runs(store))
    if run_id is not None:
        if not _history.RUN_ID_RE.fullmatch(run_id):
            raise ViewError("a run ID must be <UTC time>Z-<8 hex characters>")
        meta = next((m for m in runs if m.id == run_id), None)
        if meta is None:
            raise ViewError(f"the history has no run {run_id}")
        if machine is not None and meta.machine != machine:
            raise ViewError(f"the run {run_id} is a run of the machine {meta.machine}, not {machine}")
        key, notes = _key(meta), [f"machine: {meta.machine} (the machine of the run that you gave)"]
    else:
        key, notes = _select(runs, machine)
        mine = [m for m in runs if _key(m) == key]
        meta = next((m for m in mine if m.kind == "full"), mine[0])
    data = _history.load_tests(store, meta.id)
    tests = data.tests if data is not None else ()
    files = _file_rows(tests)
    test_rows = sorted(({"id": c.id, "seconds": c.seconds} for c in tests),
                       key=lambda r: _seconds_order(r["seconds"], r["id"]))
    shown = len(files) if top is None else top
    shown_tests = len(test_rows) if top is None else top
    repeats = []
    groups = _groups(m for m in runs if _key(m) == key)
    for (tree_hash, command), group in groups.items():
        if len(group) < 2:
            continue
        wall_min, wall_max = _min_max(m.wall_seconds for m in group)
        sec_min, sec_max = _min_max(_test_seconds(store, m.id) for m in group)
        repeats.append({"tree": tree_hash, "command_sha": command, "runs": len(group),
                        "wall_min": wall_min, "wall_max": wall_max,
                        "test_seconds_min": sec_min, "test_seconds_max": sec_max})
    repeats.sort(key=lambda r: (-r["runs"], r["tree"], r["command_sha"]))
    return {
        "run": meta.id, "started": meta.started, "verdict": meta.verdict, "kind": meta.kind,
        "machine": meta.machine, "notes": notes,
        "total_test_seconds": _sum(c.seconds for c in tests),
        "tests_without_seconds": sum(1 for c in tests if c.seconds is None),
        "files_for_half": files_for_half(r["seconds"] for r in files),
        "file_count": len(files), "files": files, "tests": test_rows,
        "hidden_rows": {"files": max(0, len(files) - shown), "tests": max(0, len(test_rows) - shown_tests)},
        "setup_share": None, "repeats": repeats,
    }


def _store_size(store: Path) -> int | None:
    """The bytes of the run files of the store, or None when the folder cannot be read."""
    total = 0
    try:
        with os.scandir(Path(store) / _history.RUNS) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    return total


def history(store: Path, *, machine: str | None = None, days: int | None = None,
            now: datetime | None = None) -> dict:
    """Return the runs of one machine: verdicts, failing sets, flaky tests and green repeats.

    ``days`` keeps the runs whose run ID (the clock of this machine when the
    run was recorded) is less than that number of days before ``now``.
    ``now`` must be an aware datetime. ``first`` and ``last`` of a failing
    set are the start times of its oldest and its newest run. A store with
    no run gives a result with no machine and no rows, which is not an
    error. A ``machine`` with no run raises ``ViewError``.
    """
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be an aware datetime (UTC)")
    if days is not None and days < 0:
        raise ValueError("days must be 0 or more")
    store = Path(store)
    runs = list(_history.iter_runs(store))
    if not runs and machine is None:
        return _empty_history(store, days)
    key, notes = _select(runs, machine)
    mine = [m for m in runs if _key(m) == key]
    if days is not None:
        limit = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        mine = [m for m in mine if _history._id_time(m.id) >= limit]
    verdicts = dict.fromkeys(VERDICTS, 0)
    for meta in mine:
        verdicts[meta.verdict if meta.verdict in verdicts else "unknown"] += 1
    green = _groups(m for m in mine if m.verdict == "passed")
    repeated = [{"tree": t, "command_sha": c, "runs": len(g)} for (t, c), g in green.items() if len(g) > 1]
    repeated.sort(key=lambda r: (-r["runs"], r["tree"], r["command_sha"]))
    by_sig: dict[str, list[RunMeta]] = {}
    for meta in mine:
        if meta.failing_sig:
            by_sig.setdefault(meta.failing_sig, []).append(meta)
    failing_sets = []
    for sig, group in by_sig.items():  # each group is newest first
        failing_sets.append({
            "sig": sig, "runs": len(group), "first": group[-1].started, "last": group[0].started,
            "trees": len({m.tree.tree for m in group if m.tree is not None}),
            "on_default_branch": sum(1 for m in group if m.on_default_branch is True),
            "ids": list(group[0].failing),
        })
    failing_sets.sort(key=lambda s: (-s["runs"], s["sig"]))
    flaky: dict[str, int] = {}
    for meta in mine:
        for test_id in meta.flaky:
            flaky[test_id] = flaky.get(test_id, 0) + 1
    others: list[str] = []
    for meta in runs:
        if _key(meta) != key:
            label = meta.machine if meta.machine != key[0] else f"{meta.machine} (another identity)"
            if label not in others:
                others.append(label)
    return {
        "machine": key[0], "notes": notes, "days": days, "runs": len(mine),
        "labelled_full_runs": sum(1 for m in mine if m.kind == "full"),
        "verdicts": verdicts,
        "failing_counts": [len(m.failing) for m in mine if m.counts is not None],
        "repeated_green": repeated, "failing_sets": failing_sets,
        "flaky": [{"id": i, "runs": n} for i, n in sorted(flaky.items(), key=lambda x: (-x[1], x[0]))],
        "other_machines": others, "size_bytes": _store_size(store),
    }


#: The one line of ``history`` on a store with no run. It is not an error.
NO_RUN = "the history has no run."


def _empty_history(store: Path, days: int | None) -> dict:
    """The result of ``history`` on a store with no run: no machine, no rows, zero runs."""
    return {
        "machine": None, "notes": [NO_RUN], "days": days, "runs": 0, "labelled_full_runs": 0,
        "verdicts": dict.fromkeys(VERDICTS, 0), "failing_counts": [], "repeated_green": [],
        "failing_sets": [], "flaky": [], "other_machines": [], "size_bytes": _store_size(store),
    }


def _num(value) -> str:
    """A number as text. None is empty, never zero."""
    if value is None:
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def _hidden(count: int, what: str) -> list[str]:
    return [f"  {count} more {what} are not shown. --json shows all rows."] if count else []


def render_profile(result: dict, top: int | None = DEFAULT_TOP) -> str:
    """The text output of ``profile``. ``top`` is the number of rows of each table."""
    lines = list(result["notes"])
    lines.append(f"run: {result['run']} started {result['started']}, kind {_num(result['kind'])}, "
                 f"verdict {result['verdict']}")
    lines.append(f"test seconds: {_num(result['total_test_seconds'])}")
    if result["tests_without_seconds"]:
        lines.append(f"tests with no seconds in the report: {result['tests_without_seconds']}")
    lines.append(f"files that hold half the seconds: {_num(result['files_for_half'])} "
                 f"of {result['file_count']}")
    if result["setup_share"] is not None:
        lines.append(f"setup share: {_num(result['setup_share'])}")
    lines.append("files (seconds, tests, file):")
    rows = result["files"] if top is None else result["files"][:top]
    lines += [f"  {_num(r['seconds']):>10} {r['tests']:>6} {r['file'] or '(no file)'}" for r in rows]
    lines += _hidden(result["hidden_rows"]["files"], "files")
    lines.append("tests (seconds, test ID):")
    rows = result["tests"] if top is None else result["tests"][:top]
    lines += [f"  {_num(r['seconds']):>10} {r['id']}" for r in rows]
    lines += _hidden(result["hidden_rows"]["tests"], "tests")
    if result["repeats"]:
        lines.append("repeat runs of one tree and one command on this machine "
                     "(runs, wall min-max, test seconds min-max, tree, command):")
        for r in result["repeats"]:
            lines.append(f"  {r['runs']} runs, wall {_num(r['wall_min'])}-{_num(r['wall_max'])}, "
                         f"test seconds {_num(r['test_seconds_min'])}-{_num(r['test_seconds_max'])}, "
                         f"{r['tree'][:12]} {r['command_sha'][:12]}")
    else:
        lines.append("repeat runs: no tree and command has two runs on this machine.")
    return clean_text("\n".join(lines))


def render_history(result: dict, *, size: bool = False) -> str:
    """The text output of ``history``. ``size`` adds the size of the store.

    A store with no run gives one line.
    """
    if result["machine"] is None:
        return NO_RUN
    lines = list(result["notes"])
    span = "all runs" if result["days"] is None else f"the runs of the last {result['days']} days"
    lines.append(f"{span}: {result['runs']}, with the kind full: {result['labelled_full_runs']}")
    lines.append("verdicts: " + ", ".join(f"{k} {v}" for k, v in result["verdicts"].items()))
    counts = result["failing_counts"]
    lines.append("failing tests in each run with a report, newest first: "
                 + (" ".join(str(n) for n in counts) or "no run with a report"))
    lines.append("trees that passed one command two times or more:")
    lines += [f"  {r['runs']} runs, tree {r['tree'][:12]}, command {r['command_sha'][:12]}"
              for r in result["repeated_green"]] or ["  none"]
    lines.append("failing sets:")
    for s in result["failing_sets"]:
        lines.append(f"  {s['runs']} runs, {s['trees']} trees, {s['on_default_branch']} on the default "
                     f"branch, first {s['first']}, last {s['last']}, set {s['sig'][:12]}:")
        lines += [f"    {test_id}" for test_id in s["ids"]]
    if not result["failing_sets"]:
        lines.append("  none")
    lines.append("flaky tests (runs, test ID):")
    lines += [f"  {r['runs']:>4} {r['id']}" for r in result["flaky"]] or ["  none"]
    if result["other_machines"]:
        lines.append("other machines in the history (use --machine LABEL): "
                     + ", ".join(result["other_machines"]))
    if size:
        lines.append(f"size of the history: {_num(result['size_bytes'])} bytes")
    return clean_text("\n".join(lines))


def to_json(result: dict) -> str:
    """The JSON output of a view: every row, no machine identity."""
    return json.dumps(result, indent=1, sort_keys=True)

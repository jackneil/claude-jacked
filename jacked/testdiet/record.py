"""Record a report that was not wrapped, ask if a tree passed, and the repeat-failure notice.

``jacked.testdiet.run`` runs a gate and records it (``run_gate``). This
module holds what ``run`` and the commands share: the label check, the
verdict counts, ``record_files``, ``seen`` and ``repeat_notice``.

Each message goes to standard error through ``jacked.testdiet.model.emit``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from jacked.public_errors import describe
from jacked.testdiet import history, junit, tree
from jacked.testdiet.views import recorded_count
from jacked.testdiet import machine as identity
from jacked.testdiet.model import (
    DEFAULT_TOP,
    NO_SOURCE,
    NO_START,
    KINDS,
    LABEL_RE,
    SCHEMA,
    RUNNERS,
    RunMeta,
    TreeId,
    clean_text,
    emit,
    label_order,
    label_text,
    one_line,
    or_text,
)

logger = logging.getLogger(__name__)


# LABEL_RE (the pattern of a label) is in jacked.testdiet.model and is read from here too.


#: The machine label of a run of ``run`` and of a ``record`` with no ``--machine``.
LOCAL = "local"


INPUTS_UNAVAILABLE = history.INPUTS_UNAVAILABLE


T = TypeVar("T")


def guard(step: str, func: Callable[[], T], default: T, then: str = "") -> T:
    """Run one recorder step. An error is logged and printed, and ``default`` is the result.

    The WARNING log line has no traceback; the traceback goes to DEBUG (``jacked -v``).
    ``then`` ends the printed line with what the failure costs, for example
    "The run is not recorded."
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 -- a recorder error never stops or changes the gate
        logger.warning("recorder step %s failed: %s", step, describe(exc))
        logger.debug("recorder step %s failed", step, exc_info=True)
        emit(f"the recorder step '{step}' failed ({type(exc).__name__}). The gate result is not changed."
             + (f" {then}" if then else ""))
        return default


def check_label(value: str | None, what: str = "label") -> str | None:
    """Return ``value`` when it is None or matches ``[A-Za-z0-9._-]+``. Else raise ``ValueError``."""
    if value is not None and not (isinstance(value, str) and LABEL_RE.fullmatch(value)):
        raise ValueError(f"the {what} must match [A-Za-z0-9._-]+")
    return value


def check_options(kind: str | None, label: str | None, runner: str | None) -> None:
    """Refuse a ``kind``, ``label`` or ``runner`` that is not valid with ``ValueError``."""
    if kind is not None and kind not in KINDS:
        raise ValueError("the kind must be full or targeted")
    check_label(label)
    if runner is not None and runner not in RUNNERS:
        raise ValueError("the runner must be one of " + ", ".join(RUNNERS))


def now_utc() -> str:
    """The time now in UTC, as ISO 8601 to the second."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_files(junit_paths: Sequence[Path], *, cwd: Path, kind: str | None, label: str | None,
                 runner: str | None, commit: str | None, exit_code: int | None,
                 wall_seconds: float | None, machine: str | None,
                 report_root: str | None, started: str | None = None) -> RunMeta:
    """Record the JUnit files of a run that was not wrapped, and return the record.

    The source is "record". With no ``commit``, the record has no tree. With
    a ``commit``, the tree is the tree of that commit, clean. ``machine`` is a
    label for a different machine: the record then has no machine identity.
    With no ``exit_code`` the verdict is "unknown". ``started`` is the start
    time of the gate as ISO 8601 with a time zone; when it is not given, the
    start time is the time of this call. A file that cannot be
    read raises ``JUnitError`` and nothing is recorded. Outside a git
    repository, ``TreeError``.
    """
    check_options(kind, label, runner)
    check_label(machine, "machine label")
    if wall_seconds is not None and not wall_seconds >= 0:
        raise ValueError("the wall time must be 0 or more seconds")
    started_text = parse_started(started) if started is not None else now_utc()
    cwd = Path(cwd)
    top = tree.work_tree_top(cwd)
    store = tree.store_dir(top)
    tree_value = _commit_tree(top, commit) if commit is not None else None
    data = junit.read_junit([Path(p) for p in junit_paths], root=top, report_root=report_root,
                            runner=runner)
    meta = RunMeta(
        id=history.new_run_id(), started=started_text, source="record", wall_seconds=wall_seconds,
        exit_code=exit_code, verdict=history.verdict_of(exit_code, data), kind=kind, label=label,
        runner=runner or data.runner, command_sha=None, inputs_sha=None,
        machine=machine or LOCAL, machine_id=None if machine else identity.machine_id(),
        tree=tree_value, tree_changed=None,
        on_default_branch=tree.on_default_branch(top, tree_value) if tree_value else None,
        counts=history.counts_of(data), seconds_reported=data.seconds_reported, load_start=None,
        load_end=None, child_cpu_seconds=None, failing=history.failing_of(data), flaky=history.flaky_of(data),
        failing_sig=history.failing_signature(data.tests), folder=None, schema=SCHEMA,
        test_seconds=history.sum_test_seconds(data),
    )
    history.write_run(store, meta, data)
    return meta


def parse_started(text: str) -> str:
    """Return the ISO 8601 time ``text`` in UTC, to the second. Raise ``ValueError`` when it is not valid.

    The time must have a time zone, because the times of two machines are
    never compared as local times.

    >>> parse_started("2026-11-26T09:25:01-05:00")
    '2026-11-26T14:25:01+00:00'
    """
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"the start time {one_line(text)!r} is not an ISO 8601 time. Give a time such "
                         "as 2026-11-26T09:00:00+00:00 or 2026-11-26T09:00:00Z") from None
    if moment.tzinfo is None:
        raise ValueError("the start time must have a time zone. Give a time such as "
                         "2026-11-26T09:00:00+00:00 or 2026-11-26T09:00:00Z")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _commit_tree(top: Path, commit: str) -> TreeId:
    """The clean tree identity of ``commit``. A name that is not a commit raises ``TreeError``."""
    found = [tree.rev(top, commit + suffix) for suffix in ("^{commit}", "^{tree}")]
    if None in found:
        raise tree.TreeError("the commit is not in this repository")
    return TreeId(commit=found[0], tree=found[1], clean=True)


@dataclass(frozen=True)
class SeenAnswer:
    """The answer of ``seen``: the run that proves the tree, or the reason that none does.

    ``decider`` is the newest run of the same identity (on this machine, or
    on each machine with ``any_machine``) when it did not prove the tree.
    ``why`` is one line for the user; it is empty for a hit.
    """

    hit: RunMeta | None
    decider: RunMeta | None
    why: str


def _same_identity(meta: RunMeta, tree_value: TreeId, command_sha: str, folder: str,
                   inputs_sha: str | None) -> bool:
    return (meta.source == "run" and meta.schema == SCHEMA and meta.tree is not None
            and meta.tree.tree == tree_value.tree and meta.command_sha == command_sha
            and meta.folder == folder and meta.inputs_sha == inputs_sha)


def _proves(meta: RunMeta) -> bool:
    # With --input, the files after the gate must be known to be the same (fail closed).
    return (meta.verdict == "passed" and meta.tree_changed is False and meta.inputs_changed is not True
            and (meta.inputs_sha is None or meta.inputs_changed is False))


def _cause(meta: RunMeta) -> str:
    """Why the run ``meta`` does not prove the tree."""
    if meta.verdict != "passed":
        return f"its verdict is {one_line(meta.verdict) or 'not recorded'}"
    if meta.tree_changed is True:
        return "the gate changed the tree"
    if meta.inputs_changed is True:
        return "the gate changed the --input files"
    if meta.inputs_sha is not None and meta.inputs_changed is None:
        return "the --input files after the gate are not known"
    return "the tree after the gate is not known"


def _not_proved(meta: RunMeta) -> str:
    return (f"the newest run of this tree and this command is {meta.id}, and it does not prove the tree: "
            f"{_cause(meta)}.")


def _not_proved_any(deciders: list[RunMeta]) -> str:
    """One line for an ``--any-machine`` miss: the newest run of each machine, at most ``DEFAULT_TOP``.

    The runs are in machine-label order, never in time order: the times of
    two machines are never compared.
    """
    ordered = sorted(deciders, key=lambda m: (*label_order(m.machine), m.id))
    shown = "; ".join(f"{m.id} ({_cause(m)})" for m in ordered[:DEFAULT_TOP])
    rest = len(ordered) - DEFAULT_TOP
    more = "" if rest <= 0 else f"; and {rest} more machine{'s' if rest > 1 else ''}"
    return f"no machine's newest run of this tree and this command proves the tree: {shown}{more}."


def seen_answer(store: Path, tree_value: TreeId, command_sha: str, *, folder: str,
                inputs_sha: str | None, machine_id: str | None, any_machine: bool = False) -> SeenAnswer:
    """Find the run that proves this tree passed this command on this machine.

    The identity of a run: its source is "run", its schema is ``SCHEMA``,
    and the tree hash, the command SHA, the gate folder and the inputs
    fingerprint are equal (a fingerprint that is "unavailable" never
    matches), on the same machine identity (not empty). The newest run of
    that identity decides: it proves the tree when its verdict is "passed"
    and ``tree_changed`` is False. A newer run that did not pass is never
    hidden by an older pass. ``any_machine`` lets the newest run of each
    machine identity decide for that machine, so the times of two machines
    are never compared; a miss then names the newest run of each machine
    (at most ``DEFAULT_TOP``). The commit is not compared, so a second commit
    with the same content is the same tree.
    """
    if tree_value is None or not command_sha or inputs_sha == INPUTS_UNAVAILABLE:
        return SeenAnswer(None, None, "")
    if not any_machine and not machine_id:
        return SeenAnswer(None, None, "")
    deciders: dict[str | None, RunMeta] = {}
    for meta in history.iter_runs(store):
        if not _same_identity(meta, tree_value, command_sha, folder, inputs_sha):
            continue
        if not any_machine and meta.machine_id != machine_id:
            continue
        if meta.machine_id in deciders:
            continue  # an older run of a machine that already has its newest run
        deciders[meta.machine_id] = meta
        if _proves(meta):
            return SeenAnswer(meta, None, "")
        if not any_machine:
            break
    decider = next(iter(deciders.values()), None)
    if decider is not None:
        why = _not_proved_any(list(deciders.values())) if any_machine else _not_proved(decider)
        return SeenAnswer(None, decider, why)
    return SeenAnswer(None, None, _nearest(store, tree_value, command_sha, folder, inputs_sha,
                                           machine_id, any_machine))


def _nearest(store: Path, tree_value: TreeId, command_sha: str, folder: str, inputs_sha: str | None,
             machine_id: str | None, any_machine: bool) -> str:
    """One line that names the nearest record when no run of the same identity exists.

    The order: the same identity on another machine, the same command with
    other ``--input`` files, other commands in this folder, a run in another
    folder, a record of ``record``. Each line ends with what to do.
    """
    same_tree = [m for m in history.iter_runs(store) if m.tree is not None and m.tree.tree == tree_value.tree]
    runs = [m for m in same_tree if m.source == "run" and m.schema == SCHEMA]
    same_command = [m for m in runs if m.command_sha == command_sha and m.folder == folder]
    if not any_machine and any(m.inputs_sha == inputs_sha and m.machine_id != machine_id for m in same_command):
        return ("a run of this tree and this command is on another machine. Give --any-machine to accept "
                "a run of any machine.")
    mine = [m for m in same_command if any_machine or m.machine_id == machine_id]
    if mine:
        return ("the runs of this tree and this command have other --input files. Run the gate with "
                "jacked tests run, with the same --input options.")
    if any(m.folder == folder for m in runs):
        return ("the runs of this tree in this folder have other commands. Run seen with the same "
                "command that jacked tests run ran, or run that command with jacked tests run.")
    other = sorted({one_line(m.folder) for m in runs if m.folder is not None and m.folder != folder})
    if other:
        return (f"the runs of this tree are in the folder {', '.join(other)}, not in {one_line(folder)}. "
                "Run seen in the folder of the gate.")
    if same_tree:
        return ("this tree has only records of jacked tests record, and seen accepts only a run of "
                "jacked tests run. Run the gate with jacked tests run.")
    return "no run of this tree is in the history. Run the gate with jacked tests run."


def seen(store: Path, tree_value: TreeId, command_sha: str, *, folder: str, inputs_sha: str | None,
         machine_id: str | None, any_machine: bool = False) -> RunMeta | None:
    """Return the run of ``seen_answer`` that proves the tree, or None."""
    return seen_answer(store, tree_value, command_sha, folder=folder, inputs_sha=inputs_sha,
                       machine_id=machine_id, any_machine=any_machine).hit


def repeat_notice(store: Path, meta: RunMeta) -> str | None:
    """Return the facts about earlier records with the failing set of ``meta``, or None.

    The text gives the number of earlier records with the same failing
    signature, the number of trees, and how many of the records are on the
    default branch. Then the failing IDs, and one line for each earlier
    record, with its own date, grouped by machine label. There is no date
    range over all records, because times of two machines are never
    compared. It does not say whose defect it is.
    The result is None for a run with no failing set, or with a failing set
    that no earlier record holds.
    """
    if not meta.failing_sig:
        return None
    earlier = [m for m in history.iter_runs(store)
               if m.id != meta.id and m.failing_sig == meta.failing_sig]
    if not earlier:
        return None
    # Times of two machines are never compared: no range over all records.
    # The records are grouped by machine label, and in run-ID order in a group.
    # A record with no label sorts after each real label.
    earlier.sort(key=lambda m: (*label_order(m.machine), m.id))
    found = history.failing_set_summary(earlier)
    records = "record" if len(earlier) == 1 else "records"
    trees = recorded_count(found["trees"], found["trees_unknown"], found["runs"])
    branch = recorded_count(found["on_default_branch"], found["on_default_branch_unknown"], found["runs"])
    summary = (f"the same failing set is in {len(earlier)} earlier {records}, trees: {trees}, "
               f"on the default branch: {branch}.")
    # At most DEFAULT_TOP rows of each list, then the count of the rest: the
    # notice goes to standard error on each red run. The record keeps each ID.
    lines = [summary, f"the failing set has {len(meta.failing)} test(s):"]
    lines += [f"  {one_line(test_id)}" for test_id in meta.failing[:DEFAULT_TOP]]
    lines += _more(len(meta.failing), "failing test", "jacked tests history --json gives each ID of the set.")
    lines.append("the earlier records, by machine:")
    for m in earlier[:DEFAULT_TOP]:
        place = m.tree.commit[:12] if m.tree is not None else "no tree"
        flag = ", default branch" if m.on_default_branch is True else ""
        lines.append(f"  {label_text(m.machine)} {m.id} {or_text(m.started, NO_START)} "
                     f"{or_text(m.source, NO_SOURCE)} "
                     f"{place}{flag}")
    lines += _more(len(earlier), "earlier record",
                   "jacked tests history --machine LABEL shows the failing sets of each machine.")
    lines.append('see "A red gate" in jacked-reference.md: a failure on the base branch never clears '
                 "the branch.")
    return "\n".join(lines)


def _more(count: int, what: str, pointer: str) -> list[str]:
    """The line for the rows after the first ``DEFAULT_TOP``. ``what`` is "failing test" or "earlier record"."""
    rest = count - DEFAULT_TOP
    if rest <= 0:
        return []
    return [f"  1 more {what} is not shown. {pointer}" if rest == 1 else
            f"  {rest} more {what}s are not shown. {pointer}"]


def seen_line(meta: RunMeta) -> str:
    """Return the one line that ``jacked tests seen`` prints for the run ``meta``.

    Each stored text value is on one line, so the record cannot add a line.
    """
    counts = ", ".join(f"{one_line(k)} {v}" for k, v in (meta.counts or {}).items()) or "no counts"
    wall = "unknown" if meta.wall_seconds is None else f"{meta.wall_seconds} s"
    return clean_text(f"passed before: {meta.id} started {or_text(meta.started, NO_START)}, "
                      f"folder {one_line(meta.folder)}, wall {wall}, {counts}")

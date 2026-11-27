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
    LABEL_RE,
    SCHEMA,
    RUNNERS,
    STATES,
    RunData,
    RunMeta,
    TreeId,
    clean_text,
    emit,
    label_order,
    label_text,
    one_line,
)

logger = logging.getLogger(__name__)


# LABEL_RE (the pattern of a label) is in jacked.testdiet.model and is read from here too.


KINDS = ("full", "targeted")


_LOCAL = "local"


INPUTS_UNAVAILABLE = history.INPUTS_UNAVAILABLE


T = TypeVar("T")


def _guard(step: str, func: Callable[[], T], default: T) -> T:
    """Run one recorder step. An error is logged and printed, and ``default`` is the result.

    The WARNING log line has no traceback; the traceback goes to DEBUG (``jacked -v``).
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 -- a recorder error never stops or changes the gate
        logger.warning("recorder step %s failed: %s", step, describe(exc))
        logger.debug("recorder step %s failed", step, exc_info=True)
        emit(f"the recorder step '{step}' failed ({type(exc).__name__}). The gate result is not changed.")
        return default


def check_label(value: str | None, what: str = "label") -> str | None:
    """Return ``value`` when it is None or matches ``[A-Za-z0-9._-]+``. Else raise ``ValueError``."""
    if value is not None and not (isinstance(value, str) and LABEL_RE.fullmatch(value)):
        raise ValueError(f"the {what} must match [A-Za-z0-9._-]+")
    return value


def _check_options(kind: str | None, label: str | None, runner: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise ValueError("the kind must be full or targeted")
    check_label(label)
    if runner is not None and runner not in RUNNERS:
        raise ValueError("the runner must be one of " + ", ".join(RUNNERS))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counts(data: RunData | None) -> dict[str, int] | None:
    if data is None:
        return None
    return {state: sum(1 for case in data.tests if case.state == state) for state in STATES}


def _failing(data: RunData | None) -> tuple[str, ...]:
    if data is None:
        return ()
    return tuple(sorted(case.id for case in data.tests if case.state in ("failed", "error")))


def _flaky(data: RunData | None) -> tuple[str, ...]:
    if data is None:
        return ()
    return tuple(sorted(case.id for case in data.tests if case.state == "flaky"))


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
    _check_options(kind, label, runner)
    check_label(machine, "machine label")
    if wall_seconds is not None and not wall_seconds >= 0:
        raise ValueError("the wall time must be 0 or more seconds")
    started_text = parse_started(started) if started is not None else _now()
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
        machine=machine or _LOCAL, machine_id=None if machine else identity.machine_id(),
        tree=tree_value, tree_changed=None,
        on_default_branch=tree.on_default_branch(top, tree_value) if tree_value else None,
        counts=_counts(data), seconds_reported=data.seconds_reported, load_start=None,
        load_end=None, child_cpu_seconds=None, failing=_failing(data), flaky=_flaky(data),
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
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise ValueError("the start time must have a time zone (for example +00:00 or Z)")
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _commit_tree(top: Path, commit: str) -> TreeId:
    """The clean tree identity of ``commit``. A name that is not a commit raises ``TreeError``."""
    found = []
    for suffix in ("^{commit}", "^{tree}"):
        code, out = tree.git(top, "rev-parse", "--verify", "-q", "--end-of-options", commit + suffix)
        lines = tree.output_lines(out)
        if code != 0 or len(lines) != 1:
            raise tree.TreeError("the commit is not in this repository")
        found += lines
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
    return meta.verdict == "passed" and meta.tree_changed is False


def _not_proved(meta: RunMeta) -> str:
    if meta.verdict != "passed":
        cause = f"its verdict is {one_line(meta.verdict) or 'not recorded'}"
    elif meta.tree_changed is True:
        cause = "the gate changed the tree"
    else:
        cause = "the tree after the gate is not known"
    return (f"the newest run of this tree and this command is {meta.id}, and it does not prove the tree: "
            f"{cause}.")


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
    are never compared. The commit is not compared, so a second commit
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
    return SeenAnswer(None, decider, _not_proved(decider) if decider is not None else "")


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
    lines = [summary, f"the failing set has {len(meta.failing)} test(s):"]
    lines += [f"  {one_line(test_id)}" for test_id in meta.failing]
    lines.append("the earlier records, by machine:")
    for m in earlier:
        place = m.tree.commit[:12] if m.tree is not None else "no tree"
        flag = ", default branch" if m.on_default_branch is True else ""
        lines.append(f"  {label_text(m.machine)} {m.id} {one_line(m.started)} {one_line(m.source)} "
                     f"{place}{flag}")
    return "\n".join(lines)


def seen_line(meta: RunMeta) -> str:
    """Return the one line that ``jacked tests seen`` prints for the run ``meta``.

    Each stored text value is on one line, so the record cannot add a line.
    """
    counts = ", ".join(f"{one_line(k)} {v}" for k, v in (meta.counts or {}).items()) or "no counts"
    wall = "unknown" if meta.wall_seconds is None else f"{meta.wall_seconds} s"
    return clean_text(f"{meta.id} started {one_line(meta.started)}, folder {one_line(meta.folder)}, "
                      f"wall {wall}, {counts}")

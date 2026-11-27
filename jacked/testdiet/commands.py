"""The ``jacked tests`` command group: run, record, seen, profile, history, prune and loss.

``jacked.cli`` adds this group to ``main``. Each command imports its module
inside the function, so ``jacked --help`` does not load the recorder.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from jacked.testdiet import model as _model


@click.group(name="tests")
def recorder_group():
    """Record the runs of a test gate and ask if a tree passed it.

    The history is in the common git directory of the clone. Git never
    commits it.
    """


_KINDS = _model.KINDS
#: The runner names that a user can give ("unknown" is only what the reader finds).
_RUNNERS = tuple(name for name in _model.RUNNERS if name != "unknown")


def _label_option(ctx, param, value):
    """Refuse a label that does not match [A-Za-z0-9._-]+ (usage error, exit 2)."""
    from jacked.testdiet import record as _record

    try:
        return _record.check_label(value, param.name.replace("_", " "))
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


def _started_option(ctx, param, value):
    """Refuse a start time that is not ISO 8601 with a time zone (usage error, exit 2)."""
    from jacked.testdiet import record as _record

    if value is None:
        return None
    try:
        return _record.parse_started(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc


@recorder_group.command(name="run", context_settings={"ignore_unknown_options": True,
                                                    "allow_interspersed_args": False})
@click.option("--junit", "junit", multiple=True, metavar="GLOB",
              help="A report at a fixed path. It is read only when its content changes.")
@click.option("--input", "inputs", multiple=True, metavar="GLOB",
              help="A file that the gate reads and git does not see. Its fingerprint is recorded.")
@click.option("--kind", type=click.Choice(_KINDS), default=None,
              help="A label for the views only.")
@click.option("--label", default=None, callback=_label_option,
              help="A free name for the run, for the views (history, profile). [A-Za-z0-9._-]+.")
@click.option("--runner", type=click.Choice(_RUNNERS), default=None,
              help="The runner of the report. Found from the report when not given.")
@click.option("--strict", is_flag=True, help="Exit 1 when the gate returns 0 and the report holds failures.")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run_command(ctx, junit, inputs, kind, label, runner, strict, command):
    """Run a gate command, record the run, and return the exit code of the gate.

    Usage: jacked tests run [OPTIONS] -- CMD... An argument that holds {junit}
    gets the path of a private report file.
    """
    import os as _os

    from jacked.testdiet import run as _run

    if not command:
        raise click.UsageError("give the gate command after --")
    code = _run.run_gate(list(command), cwd=Path(_os.getcwd()), junit=junit, inputs=inputs,
                            kind=kind, label=label, runner=runner, strict=strict)
    ctx.exit(code)


@recorder_group.command(name="record")
@click.option("--junit", "junit", multiple=True, required=True, metavar="PATH",
              type=click.Path(dir_okay=False, path_type=Path), help="A JUnit file of the run.")
@click.option("--kind", type=click.Choice(_KINDS), default=None, help="A label for the views only.")
@click.option("--label", default=None, callback=_label_option,
              help="A free name for the run, for the views (history, profile). [A-Za-z0-9._-]+.")
@click.option("--runner", type=click.Choice(_RUNNERS), default=None,
              help="The runner of the report. Found from the report when not given.")
@click.option("--exit-code", "exit_code", type=int, default=None,
              help="The exit code of the gate. With no value the verdict is unknown.")
@click.option("--wall", "wall", type=click.FloatRange(min=0), default=None, metavar="SECONDS",
              help="The wall time of the gate.")
@click.option("--commit", default=None, metavar="SHA", help="The commit that the gate ran on.")
@click.option("--machine", default=None, callback=_label_option, metavar="LABEL",
              help="The label of the machine that ran the gate.")
@click.option("--report-root", "report_root", default=None, metavar="PATH",
              help="The root folder of the machine that wrote the report.")
@click.option("--started", default=None, callback=_started_option, metavar="ISO",
              help="The start time of the gate, ISO 8601 with a time zone. "
                   "Without it, the start time is the time of the record.")
def record_command(junit, kind, label, runner, exit_code, wall, commit, machine, report_root, started):
    """Record the JUnit files of a gate run that was not wrapped."""
    import os as _os

    from jacked.testdiet import record as _record
    from jacked.testdiet.model import JUnitError
    from jacked.testdiet.tree import TreeError

    try:
        meta = _record.record_files(list(junit), cwd=Path(_os.getcwd()), kind=kind, label=label,
                                    runner=runner, commit=commit, exit_code=exit_code,
                                    wall_seconds=wall, machine=machine, report_root=report_root,
                                    started=started)
    except (JUnitError, TreeError, OSError, ValueError) as exc:
        _model.emit(f"nothing is recorded: {exc}")
        raise SystemExit(1)
    if machine is None:
        _model.emit("the record has no --machine, so it is filed under this machine (local). "
                    "Give --machine LABEL for a run of another machine.")
    click.echo(f"{meta.id} {meta.verdict}")


@recorder_group.command(name="seen", context_settings={"ignore_unknown_options": True,
                                                     "allow_interspersed_args": False})
@click.option("--input", "inputs", multiple=True, metavar="GLOB",
              help="The same --input globs that the run had.")
@click.option("--any-machine", "any_machine", is_flag=True, help="Accept a run of a different machine.")
@click.option("--json", "as_json", is_flag=True, help="Print the record as JSON (null when not seen).")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def seen_command(inputs, any_machine, as_json, command):
    """Exit 0 when this tree passed this gate command on this machine. Exit 1 when not.

    Usage: jacked tests seen [OPTIONS] -- CMD...
    """
    import dataclasses
    import json as _json

    if not command:
        raise click.UsageError("give the gate command after --")
    answer = _seen_answer(list(command), inputs, any_machine)
    found = answer.hit if answer is not None else None
    if as_json:
        data = None
        if found is not None:
            data = dataclasses.asdict(found)
            data.pop("machine_id", None)
        click.echo(_json.dumps(data, indent=1, sort_keys=True))
    elif found is not None:
        click.echo(_seen_line(found))
    elif answer is not None:
        _model.emit(answer.why or "no passed run of this tree, this command and this machine.")
    raise SystemExit(0 if found is not None else 1)


def _seen_line(meta) -> str:
    from jacked.testdiet import record as _record

    return _record.seen_line(meta)


def _seen_answer(command: list[str], inputs, any_machine: bool):
    """The answer of seen for the current folder, or None when a line already said why there is none."""
    import os as _os

    from jacked.testdiet import machine as _machine
    from jacked.testdiet import record as _record
    from jacked.testdiet import run as _run
    from jacked.testdiet import tree as _tree

    try:
        here = Path(_os.getcwd())
        top = _tree.work_tree_top(here)
        folder = _tree.work_folder(here)
        current, problem_tree = _tree.tree_state(top)
        machine_id, problem = _machine.identity()
        if machine_id is None and not any_machine:
            _model.emit(f"{problem} seen cannot match a run of this machine with no identity. "
                        "Give --any-machine to accept a run of any machine.")
            return None
        if current is None:
            _model.emit(f"{problem_tree}, so seen cannot prove a tree.")
            return None
        return _record.seen_answer(_tree.store_dir(top), current,
                                   _tree.command_sha(command, folder=folder), folder=folder,
                                   inputs_sha=_run.hash_inputs(top, inputs),
                                   machine_id=machine_id, any_machine=any_machine)
    except _tree.TreeError as exc:
        _model.emit(f"{exc}.")
    except Exception as exc:  # noqa: BLE001 -- an unknown answer is "not seen", never a traceback
        logging.getLogger(__name__).debug("seen: the check failed", exc_info=True)
        _model.emit(f"cannot check the history ({_model.error_text(exc)}). The answer is: not seen.")
    return None


def _store_or_exit() -> Path:
    """The history store of the work tree that holds the current folder. Outside one, exit 1."""
    import os as _os

    from jacked.testdiet import tree as _tree

    try:
        return _tree.store_dir(_tree.work_tree_top(Path(_os.getcwd())))
    except _tree.TreeError as exc:
        _model.emit(f"{exc}.")
        raise SystemExit(1)


@recorder_group.command(name="profile")
@click.option("--run", "run_id", default=None, metavar="ID", help="The run to show. Default: the newest full run.")
@click.option("--machine", default=None, callback=_label_option, metavar="LABEL",
              help="The machine label. Default: the machine of the newest full run.")
@click.option("--top", type=click.IntRange(min=1), default=_model.DEFAULT_TOP, show_default=True,
              help="The number of rows of each table in the text output.")
@click.option("--json", "as_json", is_flag=True, help="Print every row as JSON.")
def profile_command(run_id, machine, top, as_json):
    """Show where the time of a run goes, by file and by test."""
    from jacked.testdiet import views as _views

    store = _store_or_exit()
    try:
        result = _views.profile(store, run_id, machine=machine, top=None if as_json else top)
    except _views.ViewError as exc:
        _model.emit(str(exc))
        raise SystemExit(1)
    click.echo(_views.to_json(result) if as_json else _views.render_profile(result))


@recorder_group.command(name="history")
@click.option("--machine", default=None, callback=_label_option, metavar="LABEL",
              help="The machine label. Default: the machine of the newest full run.")
@click.option("--days", type=click.IntRange(min=0), default=None, metavar="N",
              help="Show only the runs of the last N days.")
@click.option("--json", "as_json", is_flag=True, help="Print every row as JSON.")
def history_command(machine, days, as_json):
    """Show the runs of one machine: verdicts, failing sets, flaky tests and green repeats."""
    from jacked.testdiet import views as _views

    store = _store_or_exit()
    try:
        result = _views.history(store, machine=machine, days=days)
    except _views.ViewError as exc:
        _model.emit(str(exc))
        raise SystemExit(1)
    click.echo(_views.to_json(result) if as_json else _views.render_history(result))


@recorder_group.command(name="prune")
@click.option("--before", required=True, type=click.DateTime(formats=["%Y-%m-%d"]), metavar="YYYY-MM-DD",
              help="Delete each run whose UTC date is before this date.")
@click.option("--dry-run", is_flag=True, help="Print the runs that prune deletes, and delete nothing.")
def prune_command(before, dry_run):
    """Delete the runs before a date. Print each run that is deleted, and the count that stays."""
    from jacked.testdiet import history as _history

    store = _store_or_exit()
    try:
        deleted = _history.prune(store, before.date(), dry_run=dry_run)
    except _history.PruneError as exc:
        for run_id in exc.deleted:
            click.echo(_model.clean_text(f"deleted {run_id}"))
        for name, error in exc.failed:
            _model.emit(f"cannot delete {name} ({error})")
        click.echo(_kept_line(len(_history.run_ids(store))))
        raise SystemExit(1)
    verb = "would delete" if dry_run else "deleted"
    for run_id in deleted:
        click.echo(_model.clean_text(f"{verb} {run_id}"))
    if not deleted:
        _model.emit(f"no run is before {before:%Y-%m-%d}. Nothing is deleted.")
    click.echo(_kept_line(len(_history.run_ids(store) - set(deleted))))


def _kept_line(count: int) -> str:
    return "1 run stays in the history." if count == 1 else f"{count} runs stay in the history."


@recorder_group.command(name="loss")
@click.option("--base", default=None, metavar="REF",
              help=f"Compare with the merge base of HEAD and REF. Default REF: {_model.DEFAULT_BRANCH_REF}.")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON.")
def loss_command(base, as_json):
    """Show the tests that the change removes or weakens.

    It gives exit 0 when it makes a report, also a report with a loss. It
    gives exit 1 when it cannot make a report.

    The change is the work tree against the base: commits, changes that are
    not committed and untracked files. A renamed file, a moved test and an
    edited parameter value give no loss. A test that moves prints a moved
    line.

    \b
    Counted cases: the literal lists of Python parametrize marks, the rows of
    a literal .each table (TypeScript and JavaScript) and the t.Run calls of
    a Go test. A t.Run inside a for-range loop over a slice literal (inline,
    or assigned in the same function and not assigned again) counts each
    element of that literal. A t.Run in a loop over any other value, a
    computed case list and a fixture parameter are not counted. A test on
    both sides whose cases or body the report cannot read prints a not read
    line. A file that is not read prints a not read file line.
    """
    import dataclasses
    import json as _json
    import os as _os

    from jacked.testdiet import loss as _loss
    from jacked.testdiet.model import one_line
    from jacked.testdiet.tree import TreeError

    try:
        report = _loss.loss_report(Path(_os.getcwd()), base)
    except TreeError as exc:
        _model.emit(f"{exc}.")
        raise SystemExit(1)
    except _loss.LossError as exc:
        _model.emit(f"no loss report: {one_line(str(exc))}")
        raise SystemExit(1)
    if as_json:
        click.echo(_json.dumps(dataclasses.asdict(report), indent=1, sort_keys=True))
    else:
        click.echo(_loss.render(report))

"""The ``jacked issues`` CLI group: the queue labels for discovered work (#96i).

``jacked.cli`` adds this group with ``main.add_command``. The group lives in
its own module so that ``jacked/cli.py`` does not grow. This module must not
import ``jacked.cli``, because ``jacked.cli`` imports it.
"""

from __future__ import annotations

import os

import click
from rich.console import Console
from rich.markup import escape

from jacked.issue_labels import QUEUE_LABEL_NAMES

console = Console()


@click.group(name="issues")
def issues_group():
    """GitHub issues for discovered work: the queue labels."""


@issues_group.command(name="ensure-label")
@click.argument("name", type=click.Choice(QUEUE_LABEL_NAMES))
def issues_ensure_label(name: str):
    """Create the queue label NAME in this repo if it does not exist.

    Run it just before you file the first issue with that label. A label
    that exists stays as it is.
    """
    from jacked.service import issue_queue_refresh as _iq

    try:
        outcome = _iq.ensure_label(name, cwd=os.getcwd())
    except _iq.LabelError as exc:
        console.print(f"[red][FAIL][/red] {escape(str(exc))}")
        raise SystemExit(1)
    if outcome == "created":
        console.print(f"Label '{name}' created.")
    else:
        console.print(f"Label '{name}' already exists.")

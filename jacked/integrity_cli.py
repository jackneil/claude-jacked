"""The ``jacked integrity`` CLI group and the doctor line for the tripwire (#99i).

``jacked.cli`` adds this group with ``main.add_command``. The group lives in
its own module so that ``jacked/cli.py`` does not grow. This module must not
import ``jacked.cli``, because ``jacked.cli`` imports it.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.markup import escape

console = Console()


def _settings_file():
    from jacked.memory import claude_paths, vault

    # The file that Claude Code loads, the same one that the SessionStart check reads.
    return claude_paths.config_dir(vault.jacked_home()) / "settings.json"


def print_doctor_lines(out: Console | None = None) -> None:
    """Print the ``Hook/settings integrity`` lines of ``jacked doctor``.

    Reads only. A failure prints one line that names the error class.
    """
    from jacked import integrity

    out = out or console
    try:
        lines = integrity.doctor_lines(_settings_file())
    except Exception as exc:  # noqa: BLE001 -- doctor must finish its other checks
        out.print(f"[bold]Hook/settings integrity:[/bold] [yellow]not checked ({type(exc).__name__})[/yellow]")
        return
    head, _, rest = lines[0].partition(": ")
    color = "green" if rest.startswith("OK") else "yellow"
    out.print(f"[bold]{escape(head)}:[/bold] [{color}]{escape(rest)}[/{color}]", soft_wrap=True)
    for line in lines[1:]:
        out.print(escape(line), soft_wrap=True)


@click.group(name="integrity")
def integrity_group():
    """The hook and settings integrity tripwire: a baseline of settings.json and hook scripts."""


@integrity_group.command(name="accept")
def integrity_accept():
    """Accept the current settings.json and hook scripts as the new baseline.

    Run it only after you review each change that jacked doctor or the session
    start reported. The log in the baseline file records the accept.
    """
    from jacked import integrity
    from jacked.memory.settings_io import SettingsUnreadableError

    try:
        integrity.accept(_settings_file())
    except SettingsUnreadableError as exc:
        console.print(f"[red][FAIL][/red] {escape(str(exc))}")
        raise SystemExit(1)
    console.print("Hook/settings integrity: rebaselined (user accept)")

"""The body and the output of ``jacked service preflight``.

``jacked.cli`` defines the command and calls :func:`run_preflight`. This
module must not import ``jacked.cli``, because ``jacked.cli`` imports it.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from typing import Any

import click
from rich.console import Console
from rich.markup import escape

from jacked.service import preflight_probe
from jacked.service.preflight_probe import ProbeOptions, ProbeResult, ServiceProbeError

console = Console()


def preflight_budget(timeout_seconds: float | None) -> float:
    """Return the total time budget of the preflight command, in seconds.

    The default is ``PREFLIGHT_DEFAULT_BUDGET_SECONDS``: 20 seconds less
    than the timeout of an older upgrade helper that passes no ``--timeout``.

    >>> preflight_budget(5.0)
    5.0
    """
    if timeout_seconds is not None:
        return timeout_seconds
    return float(preflight_probe.PREFLIGHT_DEFAULT_BUDGET_SECONDS)


def run_preflight(
    as_json: bool,
    timeout_seconds: float | None,
    *,
    provision: Callable[..., tuple[Any, dict[str, str]]],
    exit_hard: Callable[[int], None],
) -> None:
    """Provision the contract, then run the boot probe in the time that is left.

    ``provision`` is ``jacked.cli._provision_with_timeout``. ``exit_hard``
    ends the process when a provisioning thread is wedged.
    """
    from jacked.service.lifecycle import default_service_paths, provision_service_contract

    budget = preflight_budget(timeout_seconds)
    started = time.monotonic()
    paths = default_service_paths()
    try:
        spec, environment = provision(provision_service_contract, paths, budget)
    except (OSError, ValueError, TimeoutError) as exc:
        print_preflight_failure(exc, as_json)
        if isinstance(exc, TimeoutError):
            # The provisioning thread is still wedged; a normal exit would
            # wait for it. The batches depend on this command returning.
            exit_hard(1)
        sys.exit(1)
    remaining = budget - (time.monotonic() - started)
    if remaining <= 0:
        probe = ProbeResult(False, "interpreter_start", "no time left in the preflight budget")
    else:
        # Through the module, so that one patch point serves the tests.
        probe = preflight_probe.run_service_probe(
            spec, environment, paths.root, ProbeOptions(timeout=remaining)
        )
    if not probe.ok:
        print_preflight_failure(
            ServiceProbeError(probe), as_json, spec=spec, probe_step=probe.step
        )
        sys.exit(1)
    print_preflight_ok(spec, as_json, note=probe.note)


def print_preflight_failure(
    exc: BaseException,
    as_json: bool,
    *,
    spec: Any = None,
    probe_step: str | None = None,
) -> None:
    """Print a refusal. A probe failure keeps the provisioned contract fields."""
    error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, ServiceProbeError) else (
        f"{type(exc).__name__} {exc}"
    )
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "runtime_path": getattr(spec, "runtime_path", None),
                    "runtime_target_path": getattr(spec, "runtime_target_path", None),
                    "supervisor": _supervisor(spec) if spec is not None else None,
                    "generation": getattr(spec, "generation", None),
                    "probe_step": probe_step,
                    "note": None,
                    "error": error,
                }
            )
        )
        return
    # soft_wrap: the upgrade helper keeps only the first line of this output.
    console.print(f"[red][FAIL][/red] {escape(error)}", soft_wrap=True)
    console.print("[dim]Run 'jacked service status' for details.[/dim]")


def _supervisor(spec: Any) -> str:
    return getattr(spec.supervisor, "value", str(spec.supervisor))


def print_preflight_ok(spec: Any, as_json: bool, *, note: str | None = None) -> None:
    """Print a pass with the provisioned contract fields.

    ``note`` is a fact about this host that does not refuse the build (for
    example, the tray cannot load here, as it could not before the upgrade).
    """
    supervisor = _supervisor(spec)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "runtime_path": spec.runtime_path,
                    "runtime_target_path": spec.runtime_target_path,
                    "supervisor": supervisor,
                    "generation": spec.generation,
                    "probe_step": None,
                    "note": note,
                    "error": None,
                }
            )
        )
        return
    console.print("[green][OK][/green] Service contract OK (boot probe passed)")
    console.print(f"[dim]runtime:[/dim] {spec.runtime_path}")
    console.print(f"[dim]target:[/dim] {spec.runtime_target_path}")
    console.print(f"[dim]supervisor:[/dim] {supervisor}")
    console.print(f"[dim]generation:[/dim] {spec.generation[:12]}")
    if note:
        console.print(f"[yellow]Note:[/yellow] {escape(note)}", soft_wrap=True)

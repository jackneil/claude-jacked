"""Output and the boot probe step of ``jacked service preflight``.

This module holds the preflight helpers that ``jacked.cli`` re-exports. It
must not import ``jacked.cli``, because ``jacked.cli`` imports it.
"""

from __future__ import annotations

import click
from rich.console import Console

from jacked.service import preflight_probe
from jacked.service.preflight_probe import (
    PROBE_TIMEOUT_SECONDS,
    ProbeResult,
    ServiceProbeError,
)

console = Console()


def run_boot_probe(spec, environment, paths, timeout_seconds) -> ProbeResult:
    """Run the boot probe for ``spec`` with its exact service environment.

    The probe stops at ``PROBE_TIMEOUT_SECONDS``, or at ``timeout_seconds``
    when the caller gives a smaller limit.
    """
    timeout = PROBE_TIMEOUT_SECONDS
    if timeout_seconds is not None:
        timeout = min(timeout, timeout_seconds)
    # Through the module, so that one patch point serves the tests.
    return preflight_probe.run_service_probe(
        runtime=spec.runtime_path,
        environment=environment,
        generation=spec.generation,
        cwd=paths.root,
        timeout=timeout,
    )


def print_probe_failure(probe: ProbeResult, as_json: bool) -> None:
    """Print a failed boot probe. The text names the step, never a value."""
    print_preflight_failure(ServiceProbeError(probe), as_json, probe_step=probe.step)


def print_preflight_failure(
    exc: BaseException, as_json: bool, *, probe_step: str | None = None
) -> None:
    import json as _json

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "ok": False,
                    "runtime_path": None,
                    "runtime_target_path": None,
                    "supervisor": None,
                    "generation": None,
                    "probe_step": probe_step,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        return
    console.print(f"[red][FAIL][/red] {type(exc).__name__}: {exc}")
    console.print("[dim]Run 'jacked service status' for details.[/dim]")


def print_preflight_ok(spec, as_json: bool) -> None:
    import json as _json

    supervisor = getattr(spec.supervisor, "value", str(spec.supervisor))
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "ok": True,
                    "runtime_path": spec.runtime_path,
                    "runtime_target_path": spec.runtime_target_path,
                    "supervisor": supervisor,
                    "generation": spec.generation,
                    "probe_step": None,
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

"""Probes that compare the autostart definition with the running service.

The probes moved here from ``jacked.service.platform``, which still re-exports
each public name. ``supervision_drift`` reads ``inspect_autostart`` from that
module at call time, so a patch on ``jacked.service.platform.inspect_autostart``
still reaches it. The log lines keep the logger name of the old module.
"""

import logging
import os
import subprocess
import sys

from jacked.service import LAUNCHD_LABEL

logger = logging.getLogger("jacked.service.platform")


# The reason that supervision_drift gives when launchd does not have the
# owned job loaded. It is the only reason that needs no live service.
LAUNCHD_JOB_NOT_LOADED = "launchd job not loaded"


class _NotGiven:
    """Marks a ``probe=`` argument that the caller did not give."""

    def __repr__(self) -> str:
        return "<not given>"


_PROBE_NOT_GIVEN = _NotGiven()


def launchd_job_probe(run=None, uid: "int | None" = None):
    """Ask launchd for the jacked job in the GUI domain of the user.

    Returns the completed ``launchctl print`` call, or None when launchd
    cannot be asked: there is no ``os.getuid`` (Windows), launchctl does not
    start, or it times out. A launchctl call that cannot run logs one
    WARNING with the exception class name, because the caller then skips a
    check. Exit 113 ("Could not find service") is an answer: launchd does
    not have the job loaded. Any other non-zero exit is no answer.

    ``supervision_drift`` and ``launchd_job_never_ran`` ask the same
    question. A caller that runs both (``jacked service status``, ``jacked
    doctor``) calls this once and gives the result to both as ``probe=``,
    so launchctl runs once and a failure logs one WARNING. ``run`` replaces
    ``subprocess.run`` and is resolved at call time, so the test tripwire on
    ``subprocess.run`` still applies.
    """
    if uid is None:
        getuid = getattr(os, "getuid", None)  # absent on Windows
        if getuid is None:
            return None
        uid = getuid()
    runner = subprocess.run if run is None else run
    try:
        return runner(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The class name only: the text of an OSError can carry a path.
        logger.warning(
            "launchctl could not be asked about the jacked job (%s); "
            "autostart supervision was not checked",
            type(exc).__name__,
        )
        return None


def launchd_job_never_ran(
    run=None, uid: "int | None" = None, *, probe=_PROBE_NOT_GIVEN
) -> bool:
    """True when the launchd job is loaded but launchd never spawned it.

    Seen 2026-10-22 after an interrupted upgrade: `launchctl print` showed
    ``state = not running`` and ``runs = 0`` for a job loaded with RunAtLoad,
    while `jacked service status` said "Autostart: enabled" and nothing was
    going to start it (#178i). Only the job's top-level fields count (one tab
    deep; nested dictionaries carry their own ``state``). Never raises; any
    other platform or a probe failure is False. ``probe`` is a result of
    ``launchd_job_probe`` that the caller already has (None when launchctl
    could not be asked); when it is given, this asks launchctl nothing.

    >>> class _R:
    ...     returncode = 0
    ...     stdout = "gui/501/x = {\\n\\tstate = not running\\n\\truns = 0\\n\\t\\tstate = active\\n}"
    >>> launchd_job_never_ran(run=lambda *a, **k: _R(), uid=501) if sys.platform == "darwin" else True
    True
    """
    if sys.platform != "darwin":
        return False
    import re

    if probe is _PROBE_NOT_GIVEN:
        probe = launchd_job_probe(run, uid)
    if probe is None or probe.returncode != 0:
        return False
    fields = dict(re.findall(r"^\t(state|runs) = (.+)$", probe.stdout or "", re.MULTILINE))
    return fields.get("runs", "").strip() == "0" and fields.get("state", "").strip() != "running"


def supervision_drift(
    run=None,
    uid: "int | None" = None,
    paths=None,
    *,
    inspection=None,
    probe=_PROBE_NOT_GIVEN,
) -> "str | None":
    """Return why the enabled autostart does not supervise the running service.

    `jacked service status` said "Autostart: enabled" when an owned
    definition existed. After one upgrade, launchd did not have the job
    loaded, the plist named the previous generation, and a manual service
    ran in its place. Nothing restarted that service after a crash, and at
    the next login launchd would start the previous generation (#128i).
    This probe compares the definition with the running
    service and returns the first mismatch as a short reason. It checks in
    this order:

    1. The definition must be owned and enabled. If it is not, there is
       nothing to supervise, and the result is None.
    2. macOS only: launchd does not have the owned job loaded
       (``launchctl print`` exits 113), reason ``LAUNCHD_JOB_NOT_LOADED``.
       This check needs no running service: until launchd loads the plist
       again at the next login, nothing starts the service and nothing
       restarts it after a crash. Any other non-zero exit is no answer and
       gives one DEBUG line. A launchctl call that cannot run gives one
       WARNING. In both cases the probe continues with the checks below.
    3. A manual supervisor started the running service.
    4. The definition on disk records a different generation.

    Checks 3 and 4 need a live service. A manifest that is missing,
    unreadable or names a dead process gives None there. The result is
    also None when the definition supervises the running service.
    ``paths`` is the ``ServicePaths`` of the running service (default:
    ``default_service_paths()``). ``inspection`` is the
    ``inspect_autostart()`` result of the caller, if it has one: on Windows
    each query of the native manager is a PowerShell COM call. ``probe`` is
    a ``launchd_job_probe`` result that the caller already has (None when
    launchctl could not be asked); when it is given, this asks launchctl
    nothing. ``run`` replaces ``subprocess.run`` and is resolved at call
    time.
    """
    from jacked.service.autostart import AutostartState
    from jacked.service.instance import process_is_stale, read_manifest
    from jacked.service.platform import inspect_autostart
    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors.launchd import LAUNCHCTL_NOT_FOUND

    if inspection is None:
        inspection = inspect_autostart(run=run)
    if inspection.state is not AutostartState.OWNED_ENABLED:
        return None
    if sys.platform == "darwin":
        if probe is _PROBE_NOT_GIVEN:
            probe = launchd_job_probe(run, uid)
        if probe is not None and probe.returncode == LAUNCHCTL_NOT_FOUND:
            return LAUNCHD_JOB_NOT_LOADED
        if probe is not None and probe.returncode != 0:
            logger.debug(
                "launchctl print exited %d; the loaded state of the jacked "
                "job is unknown",
                probe.returncode,
            )
    if paths is None:
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
    try:
        running = read_manifest(paths.manifest)
    except (OSError, ValueError):
        return None
    # The shared staleness rule (manifest_is_proven_stale), applied to the
    # manifest that was just read instead of to a second read of the file.
    if process_is_stale(running.process):
        return None
    if running.supervisor == SupervisorKind.MANUAL.value:
        return "the running service is under a manual supervisor"
    on_disk = inspection.generation
    if on_disk is not None and on_disk != running.generation:
        return f"generation {on_disk[:12]} on disk, {running.generation[:12]} running"
    return None


def drift_consequence(drift: str, *, service_live: bool) -> str:
    """Return the words that lead a drift reason in status and doctor.

    ``service_live`` is the verdict that the caller printed: True when it
    found a live service. Only ``LAUNCHD_JOB_NOT_LOADED`` can describe a
    stopped service. The other reasons come from a live manifest, so they
    always read as "not supervising the running service", even when the
    caller's own check missed that service (doctor probes port 8321 only).

    >>> drift_consequence(LAUNCHD_JOB_NOT_LOADED, service_live=True)
    'not supervising the running service'
    >>> drift_consequence(LAUNCHD_JOB_NOT_LOADED, service_live=False)
    'will not start the service until the next login'
    >>> drift_consequence(
    ...     "the running service is under a manual supervisor", service_live=False)
    'not supervising the running service'
    """
    if service_live or drift != LAUNCHD_JOB_NOT_LOADED:
        return "not supervising the running service"
    return "will not start the service until the next login"

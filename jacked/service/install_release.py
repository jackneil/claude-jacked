"""Release the installed package before a Windows package step.

Windows locks each file that a running process uses. The jacked service runs
from the tool environment, so ``uv tool install --force`` cannot remove that
environment while the service runs. uv can then delete part of the
environment, stop with "Access is denied", and leave a broken install.

The two Windows update batches call :func:`release_install` (through the
hidden ``jacked _release_install`` command) with the OLD build, before the
package step. The function stops the owned service through its authenticated
control channel. Then it waits until the service process exits and no jacked
service holds the service port. The function never signals a process by PID
alone.

When the service port is in use, the function asks the listener for its
``/api/health`` answer. A jacked answer, or no clear answer, holds the
install. A listener that is clearly a different program does not hold it:
that program does not run from the tool environment, so it does not lock
the files that the package step replaces.

When no manifest exists, the function does not assume that the install is
free. It waits until no jacked service holds the port. The tray batch also
gives the creation identity of the tray process, because the tray deletes its
manifest before its interpreter exits. The function then waits until that
process exits. A new process that uses the same PID has a different creation
identity, so it cannot look like the tray.

Exit codes for the batch:

* ``EXIT_RELEASED`` (0): no jacked service holds the install.
* ``EXIT_NOT_STOPPED`` (1): this command did not stop the service. The batch
  must not install, and it must not restart a service that it did not stop.
* ``EXIT_STOPPED_NOT_RELEASED`` (3): the service accepted the stop, but the
  install is not free in the time limit. The batch must not install, and it
  must start the previous service again.

>>> EXIT_RELEASED, EXIT_NOT_STOPPED, EXIT_STOPPED_NOT_RELEASED
(0, 1, 3)
>>> ReleaseResult(EXIT_RELEASED, "free").released
True
>>> parse_wait_process("4242:windows-filetime:133")
(4242, 'windows-filetime:133')
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from functools import partial
from typing import Callable

from jacked.service import DEFAULT_PORT
from jacked.service.instance import (
    process_identity,
    process_is_stale,
    read_manifest,
)
from jacked.service.instance_models import InstanceManifest, ServicePaths
from jacked.service.ipc import ControlAction, send_native_control
from jacked.service.legacy import resolve_active_legacy_service
from jacked.service.process import is_port_available

EXIT_RELEASED = 0
EXIT_NOT_STOPPED = 1
EXIT_STOPPED_NOT_RELEASED = 3

# Default time limit, in seconds, for the service to exit. A graceful stop can
# take about 9 s; the batches use this value in their messages too.
RELEASE_TIMEOUT_SECONDS = 60

# The tray's own pre-bind guard probes loopback, and every bind plan except a
# pinned specific IP includes or covers loopback.
_PROBE_HOST = "127.0.0.1"

# A manifest that cannot be read or parsed. ``from_dict`` can also raise
# KeyError or TypeError on a document with the wrong shape.
_MANIFEST_ERRORS = (OSError, ValueError, KeyError, TypeError)

# Errors that show that the control channel is closed or closing: the owner
# is already shutting down. The Windows codes are ERROR_BROKEN_PIPE (109),
# ERROR_NO_DATA ("the pipe is being closed", 232) and ERROR_PIPE_NOT_CONNECTED
# (233).
_CHANNEL_CLOSING_ERRORS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
_CHANNEL_CLOSING_WINERRORS = frozenset({109, 232, 233})

# ERROR_FILE_NOT_FOUND. A missing pipe or a missing manifest raises
# FileNotFoundError. It shows a stop in progress only when the owner also
# removed its manifest: a live owner whose pipe never existed is not stopping.
_WINERROR_FILE_NOT_FOUND = 2

# The text of a creation identity that a batch line can carry. Windows writes
# ``windows-filetime:<ticks>``. Spaces, quotes and cmd.exe metacharacters are
# not permitted.
_CREATION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")

# What :func:`_send_stop` returns when the stop request was not refused.
_STOP_ACCEPTED = "accepted"
_STOP_IN_PROGRESS = "in_progress"


def parse_wait_process(text: str) -> tuple[int, str]:
    """Parse ``<pid>:<creation_id>`` into a PID and a creation identity.

    The creation identity can contain colons, so the function splits at the
    first colon only. Raise ValueError when the text is not valid.

    >>> parse_wait_process("12:linux-boot-ticks:99")
    (12, 'linux-boot-ticks:99')
    >>> parse_wait_process("0:windows-filetime:1")
    Traceback (most recent call last):
    ...
    ValueError: the process identity is not valid
    """
    pid_text, sep, creation_id = text.partition(":")
    if (
        not sep
        or not pid_text.isdigit()
        or int(pid_text) <= 0
        or not _CREATION_ID_RE.fullmatch(creation_id)
    ):
        raise ValueError("the process identity is not valid")
    return int(pid_text), creation_id


def format_wait_process(pid: int, creation_id: str) -> str:
    """Return the ``--wait-process`` value for a process identity.

    Raise ValueError when the identity cannot go safely into a batch line.

    >>> format_wait_process(4242, "windows-filetime:133")
    '4242:windows-filetime:133'
    >>> format_wait_process(4242, "darwin-lstart:Sat Oct 11")
    Traceback (most recent call last):
    ...
    ValueError: the process identity is not valid
    """
    text = f"{pid}:{creation_id}"
    parse_wait_process(text)
    return text


def _waited_process_exited(wait_process: tuple[int, str] | None) -> bool:
    """Return True when the named process is proven gone.

    The check uses the same proof as ``process_is_stale``, but it compares
    only the PID and the creation identity. A process that reuses the PID has
    a different creation identity, so it counts as gone. A probe that times
    out proves nothing and returns False.
    """
    if wait_process is None:
        return True
    pid, creation_id = wait_process
    try:
        observed = process_identity(pid)
    except subprocess.SubprocessError:
        return False
    except (OSError, ProcessLookupError, ValueError, IndexError):
        return True
    return observed.creation_id != creation_id


def _channel_is_closing(exc: BaseException, paths: ServicePaths) -> bool:
    """Return True when a control error shows that the owner is stopping.

    A "file not found" error counts only when the manifest no longer exists.
    The owner removes its manifest when it stops. When the manifest is still
    there, the pipe is missing for a different reason, and the error is a
    refusal.
    """
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, FileNotFoundError) or winerror == _WINERROR_FILE_NOT_FOUND:
        return not paths.manifest.exists()
    if isinstance(exc, _CHANNEL_CLOSING_ERRORS):
        return True
    return winerror in _CHANNEL_CLOSING_WINERRORS


@dataclass(frozen=True)
class ReleaseResult:
    """The outcome of one release attempt.

    ``message`` is a short sentence for the update log. It contains no PID,
    path, or token.
    """

    exit_code: int
    message: str

    @property
    def released(self) -> bool:
        return self.exit_code == EXIT_RELEASED


def _read_owner(paths: ServicePaths) -> InstanceManifest | None:
    """Return the manifest of the owned instance, or None when there is none.

    Raise one of ``_MANIFEST_ERRORS`` when a manifest exists but is not valid.
    """
    if not paths.manifest.exists():
        return None
    try:
        return read_manifest(paths.manifest)
    except FileNotFoundError:
        # The owner removed its manifest between the two calls.
        return None


_WAITED_PROCESS_REASON = "the process that started the update did not exit"

# The values-free sentence for a service port that a different program uses.
FOREIGN_PORT_NOTE = (
    "A different program uses the service port; it does not lock the install."
)


def _identify_listener(port: int) -> str:
    """Return the kind of program that listens on the service port.

    The value is ``LISTENER_JACKED``, ``LISTENER_FOREIGN`` or
    ``LISTENER_UNKNOWN`` from :mod:`jacked.service.updater`. The import is
    local because the updater imports this module.
    """
    from jacked.service.updater import (
        IDENTIFY_TIMEOUT_SECONDS,
        classify_jacked_listener,
    )

    verdict, _reason = classify_jacked_listener(port, IDENTIFY_TIMEOUT_SECONDS)
    return verdict


class _PortCheck:
    """Decide whether the program on the service port holds the install.

    A jacked service locks the install. A program that cannot be identified
    also counts as a holder, so the check fails closed. A program that is
    clearly not jacked does not lock the install. Then ``foreign`` becomes
    True, so that the caller can tell the operator.
    """

    def __init__(self) -> None:
        self.foreign = False

    def __call__(self, port: int) -> str | None:
        """Return why the port blocks the install, or None when it does not."""
        if is_port_available(_PROBE_HOST, port):
            self.foreign = False
            return None
        from jacked.service.updater import LISTENER_FOREIGN, LISTENER_JACKED

        verdict = _identify_listener(port)
        if verdict == LISTENER_FOREIGN:
            self.foreign = True
            return None
        self.foreign = False
        if verdict == LISTENER_JACKED:
            return "the service port is still in use by a jacked service"
        return "the service port is still in use"


def _holder(
    paths: ServicePaths,
    old: InstanceManifest,
    wait_process: tuple[int, str] | None = None,
    port_check: Callable[[int], str | None] | None = None,
) -> str | None:
    """Return why the install is not free yet, or None when it is free.

    The install is free when all of these conditions are true:
    the stopped process is gone, the waited process is gone, no other live
    owner has a manifest, and no jacked service holds the service port.
    """
    port_check = port_check or _PortCheck()
    if not process_is_stale(old.process):
        return "the service process did not exit"
    if not _waited_process_exited(wait_process):
        return _WAITED_PROCESS_REASON
    try:
        current = _read_owner(paths)
    except _MANIFEST_ERRORS:
        return "the service manifest cannot be read"
    if (
        current is not None
        and current.instance_id != old.instance_id
        and not process_is_stale(current.process)
    ):
        return "a different jacked service started from this install"
    return port_check(old.bind.port)


def _ownerless_holder(
    paths: ServicePaths,
    port: int,
    wait_process: tuple[int, str] | None,
    port_check: Callable[[int], str | None] | None = None,
) -> str | None:
    """Return why the install is not free when no manifest named an owner.

    A missing manifest does not prove that the install is free. The tray
    deletes its manifest before its interpreter exits, and a service with no
    manifest can still hold the port. Return None only when the waited
    process is gone, no live owner has started, and no jacked service holds
    the port. The port check identifies the listener through /api/health, so
    a jacked service with no manifest still holds the install.
    """
    port_check = port_check or _PortCheck()
    if not _waited_process_exited(wait_process):
        return _WAITED_PROCESS_REASON
    try:
        current = _read_owner(paths)
    except _MANIFEST_ERRORS:
        return "the service manifest cannot be read"
    if current is not None and not process_is_stale(current.process):
        return "a jacked service started from this install"
    return port_check(port)


def _send_stop(
    paths: ServicePaths, old: InstanceManifest
) -> ReleaseResult | str:
    """Send the authenticated stop request.

    Return ``_STOP_ACCEPTED`` when the service accepted the stop, or when it
    exited before the request arrived. Return ``_STOP_IN_PROGRESS`` when the
    control channel is already closed or closing: the owner is stopping, so
    the caller must wait and must count the stop as its own. Return a refusal
    result in all other cases.
    """
    try:
        response = send_native_control(paths.manifest, ControlAction.SHUTDOWN)
    except (OSError, ValueError) as exc:
        if process_is_stale(old.process):
            return _STOP_ACCEPTED
        if _channel_is_closing(exc, paths):
            return _STOP_IN_PROGRESS
        return ReleaseResult(
            EXIT_NOT_STOPPED,
            "Could not authenticate the service control channel "
            f"({type(exc).__name__}). No process was signalled.",
        )
    if not response.get("ok"):
        return ReleaseResult(
            EXIT_NOT_STOPPED,
            "The service rejected the authenticated stop request. "
            "No process was signalled.",
        )
    return _STOP_ACCEPTED


def _owner_or_refusal(
    paths: ServicePaths,
) -> tuple[InstanceManifest | None, ReleaseResult | None]:
    """Find the owned instance before any change.

    Return ``(manifest, None)`` when there is an owner to release,
    ``(None, None)`` when nothing holds the install, and ``(None, result)``
    when the owner cannot be proven.
    """
    try:
        old = _read_owner(paths)
    except _MANIFEST_ERRORS as exc:
        return None, ReleaseResult(
            EXIT_NOT_STOPPED,
            f"The service manifest is not valid ({type(exc).__name__}), so the "
            "owner of the install is unknown. No process was signalled. Run "
            "`jacked service recover`, then update again.",
        )
    if old is not None:
        return old, None
    if resolve_active_legacy_service(paths.legacy_pid) is not None:
        return None, ReleaseResult(
            EXIT_NOT_STOPPED,
            "A legacy jacked service is running, and its process identity is "
            "not proven. No process was signalled. Quit it from its tray menu, "
            "then update again.",
        )
    return None, None


def release_install(
    paths: ServicePaths | None = None,
    *,
    timeout: float = RELEASE_TIMEOUT_SECONDS,
    stop: bool = True,
    port: int = DEFAULT_PORT,
    wait_process: tuple[int, str] | None = None,
    poll_interval: float = 0.25,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ReleaseResult:
    """Stop the owned service and wait until it releases the install.

    Use ``stop=False`` to check only: the function then refuses when a live
    service holds the install, and it sends no stop request.

    Use ``port`` for the service port that applies when no manifest exists.
    When a manifest exists, the function uses the port in the manifest.

    Use ``wait_process`` to give a ``(pid, creation_id)`` process that must
    exit before the install is free. The tray batch gives the tray process.

    The function waits at most ``timeout`` seconds. It returns a
    :class:`ReleaseResult`; it does not raise for an expected refusal.
    """
    if paths is None:
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
    old, refusal = _owner_or_refusal(paths)
    if refusal is not None:
        return refusal

    stopped = False
    stop_note = "The service accepted the stop request"
    if old is not None and not process_is_stale(old.process):
        if not stop:
            return ReleaseResult(
                EXIT_NOT_STOPPED,
                "The jacked service is running from this install. No process "
                "was signalled. Stop the service first.",
            )
        outcome = _send_stop(paths, old)
        if isinstance(outcome, ReleaseResult):
            return outcome
        if outcome == _STOP_IN_PROGRESS:
            stop_note = "The service was already stopping"
        stopped = True

    port_check = _PortCheck()
    check: Callable[[], str | None]
    if old is None:
        check = partial(_ownerless_holder, paths, port, wait_process, port_check)
        success = "No jacked service is running. The install is free."
    else:
        check = partial(_holder, paths, old, wait_process, port_check)
        success = (
            "The jacked service exited and its port is free. "
            "The install is free."
        )

    failure_code = EXIT_STOPPED_NOT_RELEASED if stopped else EXIT_NOT_STOPPED
    deadline = clock() + max(0.0, timeout)
    reason: str | None = "the service did not exit"
    try:
        while True:
            reason = check()
            if reason is None:
                if port_check.foreign:
                    success = f"{success} {FOREIGN_PORT_NOTE}"
                return ReleaseResult(EXIT_RELEASED, success)
            if clock() >= deadline:
                break
            sleep(poll_interval)
    except Exception as exc:  # noqa: BLE001 - the batch needs an exit code
        reason = f"the wait failed ({type(exc).__name__})"
    # A check-only call uses a time limit of 0. "within 0 seconds" says
    # nothing useful, so the clause is left out.
    within = f" within {timeout:g} seconds" if timeout > 0 else ""
    if stopped:
        message = f"{stop_note}, but {reason}{within}. Nothing was installed."
    else:
        message = (
            f"No live service was stopped, but {reason}{within}. "
            "Nothing was installed."
        )
    return ReleaseResult(failure_code, message)

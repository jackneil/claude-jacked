"""Atomic reader/writer for the update-status JSON file.

Used by the detached POSIX updater, the Windows cmd.exe batch (via the
`jacked _update_status` CLI shim), and the `/api/update/status` endpoint.

Lifecycle contract:
  1. `init_status()` — clobbers any prior file unless another updater is
     actively in-flight (raises LockBusy). Stale `in_progress` files
     (older than STALE_IN_PROGRESS_SECONDS) are considered abandoned.
     `init_or_adopt_status()` wraps this to tolerate the tray's own
     pre-init file (metadata written, no phases opened yet).
  2. `begin_phase` / `end_phase` throughout the update.
  3. `mark_succeeded()` OR end_phase with status="failed" finalizes.
  4. `read_status()` reports as missing (None): a `succeeded` file older
     than STALE_SUCCEEDED_SECONDS, AND an `in_progress` file older than
     STALE_IN_PROGRESS_SECONDS (an aborted updater that never finalized),
     so the UI doesn't resurrect old banners or a zombie "updating" state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from packaging.version import InvalidVersion, Version

from jacked.atomic_io import atomic_write_text
from jacked.process_identity import creation_id as _creation_id
from jacked.service import CLAUDE_DIR
from jacked.service.update_noop import is_unclaimed_preinit

logger = logging.getLogger(__name__)

UPDATE_STATUS_FILE: Path = CLAUDE_DIR / "jacked-update-status.json"

STALE_SUCCEEDED_SECONDS: int = 600  # 10 minutes
STALE_IN_PROGRESS_SECONDS: int = 300  # 5 minutes
# Passed as `updater_pid` by an updater that cannot be probed (a Windows batch
# whose Python shim exits at once): the record then carries no pid and the
# abandoned-update check uses the mtime rule alone.
NO_UPDATER_PID: int = 0
# How long one Windows batch step waits for the update lock before it writes
# the status record without the lock (``with_update_lock``). Each holder of
# the lock in a Windows update (a batch step, the abandoned-update close)
# keeps it for milliseconds, so a longer hold is a stuck holder.
BATCH_STEP_LOCK_WAIT_SECONDS: float = 3.0
# How often ``with_update_lock`` asks for the lock again while it waits.
_LOCK_POLL_SECONDS: float = 0.1


class LockBusy(Exception):
    """Another updater is actively in-flight — refuse to clobber its state."""


def acquire_update_lock(path: Path):
    """Take the exclusive update lock, or return None when one is held.

    `init_or_adopt_status` inspects the status file and then writes it. Two
    upgrades that start inside the same second both read "no in-flight
    updater" and both proceed, so the mtime rule alone cannot serialize a
    package install against a service restart. This is a real OS lock on a
    sidecar file (`<status>.lock`), taken before that read-check-write and
    held for the whole update.

    Returns a handle whose ``close()`` releases the lock (it is also a
    context manager), or None when another process already holds it. A lock
    file that cannot be opened also gives None.

    Scope: the lock serializes updaters that run as Python processes (the
    POSIX CLI upgrade and the tray updater), and it holds them for the whole
    update. A Windows batch cannot hold an OS lock across its steps, so the
    Windows helpers rely on the status record's in-flight check (``jacked
    _update_status_init``, exit 2) to refuse a second updater. But each batch
    step (``jacked _update_status_init``, ``_update_status``,
    ``_update_status_succeed``, ``_update_status_fail``) is its own short
    process, and it takes this lock for its own read-modify-write through
    ``with_update_lock``. So a batch step does not write between the check
    and the write of a close that holds the lock (``_close_under_lock``),
    unless the lock stays held past the wait of ``with_update_lock``.
    """
    return _try_update_lock(path)[0]


def _try_update_lock(path: Path):
    """Return ``(handle, None)`` with the update lock, else ``(None, refusal)``.

    ``refusal`` is the reason of the lease (``jacked.credentials.lease``):
    REFUSAL_HELD when another holder has the lock, or REFUSAL_UNUSABLE when
    the lock file cannot be opened at all.
    """
    from contextlib import ExitStack

    from jacked.credentials.lease import FileSwitchLease

    lease = FileSwitchLease(_lock_path(path))
    stack = ExitStack()
    try:
        if not stack.enter_context(lease.acquire()):
            stack.close()
            return None, lease.refusal
    except Exception:
        stack.close()
        raise
    return stack, None


def with_update_lock(path: Path, action, *, wait_seconds: Optional[float] = None):
    """Run ``action()`` under the update lock and return its result.

    A Windows batch step uses this for its read-modify-write of the status
    record (see ``acquire_update_lock``). The helper asks for the lock every
    0.1 s, for up to ``wait_seconds`` (BATCH_STEP_LOCK_WAIT_SECONDS, 3 s,
    when it is None). With the lock, it runs ``action()`` and releases the
    lock when ``action()`` returns or raises. An exception from ``action()``
    goes to the caller unchanged.

    The status record is observability, not the update itself, so a batch
    step never fails because of the lock. The fallback is the earlier
    behavior, a write without the lock:

    * the lock stays held past the wait: one WARNING, then ``action()``;
    * the lock file cannot be opened, so nobody can hold the lock and a
      wait cannot help: one WARNING at once, then ``action()``.

    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     with_update_lock(p, lambda: acquire_update_lock(p) is None)
    True
    """
    from jacked.credentials.lease import REFUSAL_UNUSABLE

    limit = BATCH_STEP_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    start = time.monotonic()
    while True:
        lock, refusal = _try_update_lock(path)
        if lock is not None:
            break
        if refusal == REFUSAL_UNUSABLE:
            logger.warning(
                "the update lock file cannot be used; writing the status without it"
            )
            return action()
        waited = time.monotonic() - start
        if waited >= limit:
            logger.warning(
                "the update lock stayed held for %.1f s; writing the status without it",
                waited,
            )
            return action()
        time.sleep(min(_LOCK_POLL_SECONDS, limit - waited))
    try:
        return action()
    finally:
        lock.close()


def _lock_path(path: Path) -> Path:
    """Return the advisory lock file that guards one status file."""
    return path.with_name(path.name + ".lock")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: dict) -> None:
    atomic_write_text(path, json.dumps(data, indent=2))


def _read_raw(path: Path) -> Optional[dict]:
    """Return the status record, or None when the file holds no JSON object.

    Every reader calls ``.get`` on the result. Valid JSON that is not an
    object (a ``[]`` or a string) reads as missing, so a damaged file
    cannot raise AttributeError in a reader.
    """
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_status(path: Path) -> Optional[dict]:
    """Public read. Returns None on missing, corrupt, stale-succeeded, or
    stale-in_progress."""
    data = _read_raw(path)
    if data is None:
        return None
    overall = data.get("overall")
    if overall == "succeeded":
        try:
            age = time.time() - path.stat().st_mtime
            if age > STALE_SUCCEEDED_SECONDS:
                return None
        except OSError:
            return None
    elif overall == "in_progress":
        # An aborted updater leaves in_progress forever; without this the
        # dashboard would show a zombie "updating..." banner indefinitely.
        # The same rule as abandoned_status: old AND not proven alive, so a
        # long phase (a 15-minute pack refresh) stays visible while it runs.
        try:
            if in_progress_is_stale(path, data):
                return None
        except OSError:
            return None
    return data


def read_record(path: Path) -> Optional[dict]:
    """Return the status record as stored, or None when there is none.

    ``read_status`` hides a ``succeeded`` record after
    STALE_SUCCEEDED_SECONDS and a stale ``in_progress`` one, so that the
    dashboard shows no old banner. A boot check that compares the record
    with other evidence (the recovery file, the running build) needs the
    record whatever its age.
    """
    return _read_raw(path)


def in_progress_is_stale(path: Path, data: dict) -> bool:
    """True when an ``in_progress`` record is older than the stale window AND
    its updater is not proven alive. One phase can run longer than the window
    without writing (the skill-pack refresh may take 15 minutes), so age alone
    must never end an update whose updater still runs. Raises OSError when the
    file cannot be read."""
    age = time.time() - path.stat().st_mtime
    return age > STALE_IN_PROGRESS_SECONDS and updater_liveness(data) is not True


def _parse_version(value: object) -> Optional[Version]:
    """Return ``value`` as a PEP 440 version, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return Version(value)
    except InvalidVersion:
        return None


def _parse_instant(value: object) -> Optional[datetime]:
    """Return an ISO 8601 timestamp as an aware datetime, or None.

    A timestamp without a UTC offset is not an instant (its zone is not
    known), so it also gives None.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def install_completed_after(
    data: dict, last_install: Optional[dict], running_version: str
) -> bool:
    """True when an install completed after the update in ``data`` started.

    The status record can stay ``in_progress``, or close as failed, after
    the install itself is done: the batch or the updater stopped after the
    new package was in place. The last-install record and the running
    version are independent proof. All four facts must be true:

    * the last install (``last_install["at"]``) is later than the update's
      ``started_at``;
    * it installed ``running_version`` or a newer version;
    * it installed a version newer than the update's ``from_version``;
    * the running build is newer than the update's ``from_version``. While
      the build that the update started from still runs, the install did
      not reach this process.

    Versions compare by PEP 440 (a dev build sorts before its release).
    Timestamps compare as instants, so the UTC offset does not matter. A
    missing or malformed version or timestamp proves nothing: the result is
    False, with one DEBUG line that names the unusable fields. No
    last-install record also gives False.

    >>> start = {"started_at": "2026-11-02T10:00:00+00:00",
    ...          "from_version": "0.110.1"}
    >>> done = {"at": "2026-11-02T10:00:25+00:00", "to_version": "0.110.2"}
    >>> install_completed_after(start, done, "0.110.2")
    True
    >>> install_completed_after(
    ...     start, {**done, "at": "2026-11-02T09:59:00+00:00"}, "0.110.2")
    False
    >>> install_completed_after(
    ...     start, {**done, "to_version": "0.110.2.dev1"}, "0.110.2")
    False
    >>> install_completed_after(start, done, "0.110.1")
    False
    >>> install_completed_after(start, None, "0.110.2")
    False
    """
    started = (
        _parse_instant(data.get("started_at")) if isinstance(data, dict) else None
    )
    return _install_completed_since(
        data, last_install, running_version, "started_at", started
    )


def _install_completed_since(
    data: dict,
    last_install: Optional[dict],
    running_version: str,
    since_name: str,
    since: Optional[datetime],
) -> bool:
    """The rule of ``install_completed_after``, with the instant to beat.

    ``since`` is the instant that the last install must be later than, and
    ``since_name`` names the field it came from for the DEBUG line.
    """
    if last_install is None:
        return False
    if not isinstance(data, dict) or not isinstance(last_install, dict):
        logger.debug(
            "Cannot prove the install completed: a record is not a JSON object"
        )
        return False
    finished = _parse_instant(last_install.get("at"))
    before = _parse_version(data.get("from_version"))
    installed = _parse_version(last_install.get("to_version"))
    running = _parse_version(running_version)
    unusable = [
        name
        for name, value in (
            (since_name, since),
            ("at", finished),
            ("from_version", before),
            ("to_version", installed),
            ("running_version", running),
        )
        if value is None
    ]
    if unusable:
        logger.debug(
            "Cannot prove the install completed: unusable fields %s",
            ", ".join(unusable),
        )
        return False
    return (
        finished > since
        and installed >= running
        and installed > before
        and running > before
    )


def failed_update_superseded(
    data: dict, last_install: Optional[dict], running_version: str
) -> bool:
    """True when a failed update no longer applies to the running build.

    Three facts must be true: the record closed as failed, the running
    build is newer than the version that update started from, and an
    install completed after the update FAILED (the rule of
    ``install_completed_after``, measured from the failure instant). Then a
    later install already replaced the build that failed to update, and the
    recovery steps of the record are stale. While the build that failed to
    update still runs, the failure still applies.

    The failed attempt writes its own last-install record (its settings
    step runs ``jacked install``), so the start of the attempt is the wrong
    instant: an update that installed the new build and then failed its
    verification read as repaired. The failure instant comes from the
    record, in this order (``_failure_instant``):

    * ``failed_at``: ``mark_failed`` and ``end_phase`` with status
      ``failed`` write it each time they close the record. The last close
      wins, so the installs of the failed attempt itself (its settings
      step, a rollback) all come before it.
    * A record written before ``failed_at`` existed: the latest
      ``finished_at`` of its phases.
    * A record with neither (no phases, as the Windows ``jacked upgrade``
      batch writes): its ``started_at``.

    A malformed version or instant proves nothing: the result is False.

    >>> failed = {"overall": "failed", "from_version": "0.103.0",
    ...           "started_at": "2026-10-01T10:00:00+00:00"}
    >>> later = {"at": "2026-11-02T10:00:00+00:00", "to_version": "0.110.2"}
    >>> failed_update_superseded(failed, later, "0.110.2")
    True
    >>> failed_update_superseded(
    ...     {**failed, "from_version": "0.110.2"}, later, "0.110.2")
    False
    >>> failed_update_superseded(
    ...     {**failed, "failed_at": "2026-11-02T11:00:00+00:00"}, later, "0.110.2")
    False
    """
    if not isinstance(data, dict) or data.get("overall") != "failed":
        return False
    before = _parse_version(data.get("from_version"))
    running = _parse_version(running_version)
    if before is None or running is None:
        logger.debug(
            "Cannot compare the failed update with the running build: "
            "a version is not usable"
        )
        return False
    if not before < running:
        return False
    since_name, since = _failure_instant(data)
    return _install_completed_since(
        data, last_install, running_version, since_name, since
    )


def _failure_instant(data: dict) -> tuple[str, Optional[datetime]]:
    """Return the field that dates the failure of ``data``, and its instant.

    The instant is None when the field is present but is not an instant.
    See ``failed_update_superseded`` for the order of the sources.

    >>> _failure_instant({"failed_at": "2026-11-02T10:02:00+00:00"})[0]
    'failed_at'
    >>> name, when = _failure_instant({"phases": [
    ...     {"finished_at": "2026-11-02T10:01:00+00:00"}, {"finished_at": None}]})
    >>> name, when.isoformat()
    ('phases.finished_at', '2026-11-02T10:01:00+00:00')
    >>> _failure_instant({"started_at": "2026-11-02T10:00:00+00:00"})[0]
    'started_at'
    """
    if "failed_at" in data:
        return "failed_at", _parse_instant(data.get("failed_at"))
    phases = data.get("phases")
    finishes = [
        instant
        for instant in (
            _parse_instant(entry.get("finished_at"))
            for entry in (phases if isinstance(phases, list) else [])
            if isinstance(entry, dict)
        )
        if instant is not None
    ]
    if finishes:
        return "phases.finished_at", max(finishes)
    return "started_at", _parse_instant(data.get("started_at"))


def recovery_predates_completed_update(
    data: dict, running_version: str, recovery_mtime: float
) -> bool:
    """True when a completed update started after the recovery file was written.

    A recovery file describes an earlier failure. The updater removes it
    when its own update succeeds, but an update that ``abandoned_status``
    closes as succeeded (from a completed install) removes nothing, so the
    old file toasted on every start. Three facts must be true:

    * the record closed as succeeded;
    * the running build is newer than the update's ``from_version``;
    * the recovery file was last written before the update started (its
      mtime, ``recovery_mtime``, is earlier than ``started_at``).

    Then a complete update ran after the failure that the file describes,
    and its recovery steps are stale. A file written during or after the
    update is never stale by this rule. A missing or malformed version or
    timestamp proves nothing: the result is False, with one DEBUG line.

    >>> done = {"overall": "succeeded", "from_version": "0.110.1",
    ...         "started_at": "2026-11-02T10:00:00+00:00"}
    >>> earlier = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
    >>> recovery_predates_completed_update(done, "0.110.2", earlier)
    True
    >>> recovery_predates_completed_update(done, "0.110.1", earlier)
    False
    >>> later = datetime(2026, 11, 2, 11, tzinfo=timezone.utc).timestamp()
    >>> recovery_predates_completed_update(done, "0.110.2", later)
    False
    """
    if not isinstance(data, dict) or data.get("overall") != "succeeded":
        return False
    started = _parse_instant(data.get("started_at"))
    before = _parse_version(data.get("from_version"))
    running = _parse_version(running_version)
    unusable = [
        name
        for name, value in (
            ("started_at", started),
            ("from_version", before),
            ("running_version", running),
        )
        if value is None
    ]
    if unusable:
        logger.debug(
            "Cannot compare the recovery file with the completed update: "
            "unusable fields %s",
            ", ".join(unusable),
        )
        return False
    return running > before and recovery_mtime < started.timestamp()


def abandoned_status(
    path: Path,
    *,
    last_install: Optional[dict] = None,
    running_version: Optional[str] = None,
) -> Optional[dict]:
    """Return the raw record of an update that was interrupted, else None.

    `read_status` hides an `in_progress` file older than
    STALE_IN_PROGRESS_SECONDS so the dashboard shows no zombie banner. Hiding
    it is not the same as telling the user: the machine may be running a build
    whose settings never migrated. The tray reads this at boot, closes the
    record as failed with ``close_abandoned_update``, and writes a recovery
    file when that close happened.

    The mtime rule alone is not enough. One phase can legitimately run longer
    than STALE_IN_PROGRESS_SECONDS (a slow package install on a slow link),
    and a supervisor that relaunches the tray mid-update would then read a
    LIVE update as abandoned, mark it failed and write a false recovery file.
    So a record that names its updater's pid is abandoned only when that
    process is gone. A record with no pid keeps the mtime rule.

    Age is also not proof that the install failed. The batch or the updater
    can stop after the new package is in place and before it closes the
    record. When the caller gives the last-install record and the running
    version, and they prove that the install completed after the update
    started (``install_completed_after``), this closes the record as
    succeeded and returns None. Without that evidence, the age rule alone
    decides.

    The close takes the update lock (``acquire_update_lock``), because a
    new updater can start between the first read and the write. A held
    lock means that an updater runs now, and a live updater is never
    abandoned: nothing is written and the result is None. With the lock,
    the record is read again and closed only when it is still the same
    attempt (the same ``started_at`` and ``from_version``), still
    ``in_progress`` and still stale. A Windows batch cannot hold the lock
    across its steps, but each step takes it for its own write
    (``with_update_lock``), so no step writes between the second read and
    the write. A step that wrote between the first read and the lock fails
    the identity check or the second mtime check. ``close_abandoned_update``
    uses the same rule.

    >>> import json, os, tempfile, time
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     _ = p.write_text(json.dumps({"overall": "in_progress",
    ...                                  "current_phase": "migrating_settings"}))
    ...     old = time.time() - STALE_IN_PROGRESS_SECONDS - 60
    ...     os.utime(p, (old, old))
    ...     abandoned_status(p)["current_phase"]
    'migrating_settings'

    The same stale record, after an install that completed 25 s after the
    update started, at the running version:

    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     _ = p.write_text(json.dumps({"overall": "in_progress",
    ...                                  "started_at": "2026-11-02T10:00:00+00:00",
    ...                                  "from_version": "0.110.1"}))
    ...     old = time.time() - STALE_IN_PROGRESS_SECONDS - 60
    ...     os.utime(p, (old, old))
    ...     done = {"at": "2026-11-02T10:00:25+00:00", "to_version": "0.110.2"}
    ...     verdict = abandoned_status(p, last_install=done,
    ...                                running_version="0.110.2")
    ...     verdict, json.loads(p.read_text())["overall"]
    (None, 'succeeded')
    """
    data = _read_raw(path)
    if data is None or data.get("overall") != "in_progress":
        return None
    try:
        stale = in_progress_is_stale(path, data)
    except OSError:
        return None
    # A proven-live updater is never abandoned. Proven-dead or indeterminate
    # (no pid, probe failed) falls back to the mtime rule.
    if not stale:
        return None
    if running_version is not None and install_completed_after(
        data, last_install, running_version
    ):
        # The install finished; the batch or the updater stopped after it.
        # The verdict holds whether or not the close is written: no recovery
        # file is due, and the next start reads the same evidence again.
        _close_under_lock(path, data, _set_succeeded, "completed")
        return None
    return data


def close_abandoned_update(
    path: Path, seen: dict, *, error: str, recovery: Optional[str] = None
) -> Optional[dict]:
    """Close the abandoned attempt in ``seen`` as failed, under the update lock.

    ``seen`` is the record that ``abandoned_status`` returned. The close
    follows the rule of the completed close in ``abandoned_status``: take
    the update lock, read the record again, and write only when it is still
    the same stale attempt. Returns the record as written, or None when
    nothing was written:

    * an updater holds the lock, so an update runs now;
    * the record is no longer the attempt that was read (another updater
      started or closed one);
    * the attempt wrote again, so it still runs (a Windows batch step can
      write between the first read and the lock);
    * the write failed (one WARNING with the exception class name).

    The caller writes the recovery file only when this returns a record.
    """

    def close(data: dict) -> None:
        _set_failed(data, error, recovery)

    return _close_under_lock(path, seen, close, "abandoned")


def _close_under_lock(path: Path, seen: dict, close, what: str) -> Optional[dict]:
    """Close the attempt in ``seen`` with ``close``, under the update lock.

    ``close`` changes the record in place. ``what`` names the record in the
    log lines. Returns the record as written, or None when nothing was
    written (see ``close_abandoned_update`` for the cases).
    """
    try:
        lock = acquire_update_lock(path)
    except OSError as exc:
        logger.warning(
            "Could not close the %s update record: %s", what, type(exc).__name__
        )
        return None
    if lock is None:
        logger.info(
            "The update lock is held, so an update runs now; "
            "the %s update record stays open",
            what,
        )
        return None
    try:
        current = _read_raw(path)
        if not _same_stale_attempt(path, current, seen):
            logger.info(
                "The update record changed before the %s update could be "
                "closed; it is left alone",
                what,
            )
            return None
        close(current)
        _atomic_write(path, current)
        return current
    except OSError as exc:
        logger.warning(
            "Could not close the %s update record: %s", what, type(exc).__name__
        )
        return None
    finally:
        lock.close()


def _same_stale_attempt(path: Path, current: Optional[dict], seen: dict) -> bool:
    """True when ``current`` is still the stale attempt that ``seen`` read.

    The same ``started_at`` and ``from_version``, still ``in_progress``, and
    still stale (``in_progress_is_stale``): an attempt that wrote again after
    the first read still runs. Raises OSError when the file cannot be read.
    """
    return (
        current is not None
        and current.get("overall") == "in_progress"
        and current.get("started_at") == seen.get("started_at")
        and current.get("from_version") == seen.get("from_version")
        and in_progress_is_stale(path, current)
    )


def _resolve_updater_pid(updater_pid: Optional[int]) -> Optional[int]:
    """None means "this process"; NO_UPDATER_PID means "record no pid"."""
    if updater_pid is None:
        return os.getpid()
    if updater_pid == NO_UPDATER_PID:
        return None
    return updater_pid


def updater_liveness(data: dict) -> Optional[bool]:
    """True when the recording process runs, False when it is gone, None when
    the record names no usable pid or the probe is indeterminate.

    A pid alone can be reused by an unrelated process (after the updater
    exits, or after a reboot), which would keep an abandoned record alive for
    good. A record that carries ``updater_creation_id`` counts as alive only
    while that same process (same creation identity) holds the pid.
    """
    pid = data.get("updater_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    from jacked.service.process import process_liveness

    try:
        alive = process_liveness(pid)
    except Exception:  # noqa: BLE001 - a probe failure is indeterminate, not proof
        return None
    recorded = data.get("updater_creation_id")
    if alive is not True or not isinstance(recorded, str):
        return alive  # dead, unknown, or a record written before the field
    current = _creation_id(pid)
    if current is None:
        return None
    return current == recorded


def read_status_with_mtime(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Returns (data, mtime_iso). Used by /api/update/status."""
    data = read_status(path)
    if data is None:
        return None, None
    try:
        mtime = path.stat().st_mtime
        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        mtime_iso = None
    return data, mtime_iso


def init_status(
    path: Path,
    from_version: str,
    to_version: str,
    method: str,
    log_path: Optional[str] = None,
    updater_pid: Optional[int] = None,
    preinit: bool = False,
) -> None:
    """Create a fresh status file. Raises LockBusy if another updater is active.

    ``preinit`` marks the record the TRAY writes moments before it spawns the
    updater, so the dashboard has something to show at once. Only a record
    carrying this marker may be adopted by the updater that follows; a fresh
    record from another updater is a busy updater, phases or not.

    `updater_pid` names the process that drives this update, so a reader can
    tell a slow phase from an abandoned one. It defaults to this process.

    Clobbers:
      - no prior file
      - prior succeeded/failed file
      - prior in_progress file older than STALE_IN_PROGRESS_SECONDS
      - prior in_progress file whose recorded updater pid is proven dead
    Refuses:
      - prior in_progress file fresher than that whose updater is alive or
        cannot be probed
    """
    prior = _read_raw(path)
    if prior is not None and prior.get("overall") == "in_progress":
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        # The write side agrees with the read side: a record whose updater is
        # proven dead is clobbered at once, not after the mtime window. The
        # tray's pre-init is exempt: the tray that wrote it is expected to be
        # gone by the time the updater arrives, and adoption consumes it.
        if age <= STALE_IN_PROGRESS_SECONDS and (
            prior.get("preinit") is True or updater_liveness(prior) is not False
        ):
            raise LockBusy(
                "Another updater is in-flight (started "
                f"{prior.get('started_at', '?')}, last updated "
                f"{int(age)}s ago)"
            )

    data = {
        "started_at": _now_iso(),
        "from_version": from_version,
        "to_version": to_version,
        "method": method,
        "current_phase": None,
        "phases": [],
        "overall": "in_progress",
        "error": None,
        "recovery": None,
        "log_path": log_path,
        "updater_pid": _resolve_updater_pid(updater_pid),
        "updater_creation_id": _creation_id(_resolve_updater_pid(updater_pid)),
    }
    if preinit:
        data["preinit"] = True
    _atomic_write(path, data)


def init_or_adopt_status(
    path: Path,
    from_version: str,
    to_version: str,
    method: str,
    log_path: Optional[str] = None,
    updater_pid: Optional[int] = None,
) -> str:
    """Init a fresh status file, or adopt the tray's pre-init one.

    The tray pre-creates the status file (metadata, no phases) the instant
    the user clicks update so the dashboard has something from t=0. The
    detached updater — POSIX Python or the Windows cmd.exe shim — then races
    to init again moments later and hits LockBusy on the tray's own
    seconds-old file. That's not a real collision: a file the tray wrote with
    ``preinit=True`` and no phases is the tray's pre-init, so adopt it without
    rewriting (its metadata is already correct) and consume the marker. Any
    other in-flight file, phases or not, IS a real concurrent updater: re-raise
    LockBusy so callers refuse.

    Returns ``"initialized"`` (wrote a fresh file) or ``"adopted"`` (reused
    the tray's pre-init metadata).

    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     init_status(p, "1.0.0", "1.1.0", "uv", preinit=True)
    ...     init_or_adopt_status(p, "1.0.0", "1.1.0", "uv")
    ...     try:
    ...         init_or_adopt_status(p, "1.0.0", "1.1.0", "uv")
    ...     except LockBusy:
    ...         print("busy")
    'adopted'
    busy
    """
    try:
        init_status(
            path,
            from_version,
            to_version,
            method,
            log_path=log_path,
            updater_pid=updater_pid,
        )
        return "initialized"
    except LockBusy:
        prior = _read_raw(path) or {}
        if is_unclaimed_preinit(prior):
            # Claim the record for THIS process. The tray that pre-created it
            # is about to exit, and a record naming a dead pid would read as
            # abandoned the moment this update runs longer than the mtime rule.
            # The marker is consumed here: a second updater arriving later sees
            # a claimed record and is refused like any other busy updater.
            prior.pop("preinit", None)
            claimed = _resolve_updater_pid(updater_pid)
            if claimed is None:
                # An updater that cannot be probed (a Windows batch) must
                # not leave the dead tray's pid on the record.
                prior.pop("updater_pid", None)
                prior.pop("updater_creation_id", None)
            else:
                prior["updater_pid"] = claimed
                prior["updater_creation_id"] = _creation_id(claimed)
            # A claim that cannot be written leaves the tray's dead pid on
            # the record; the update would then read as abandoned mid-run.
            # Surface it as the LockBusy the caller already handles.
            try:
                _atomic_write(path, prior)
            except OSError as exc:
                raise LockBusy(
                    f"could not claim the update status record: {exc}"
                ) from exc
            return "adopted"
        raise


def begin_phase(path: Path, phase: str) -> None:
    data = _read_raw(path) or {}
    phases = data.get("phases", [])
    phases.append(
        {
            "name": phase,
            "started_at": _now_iso(),
            "finished_at": None,
            "status": "in_progress",
        }
    )
    data["phases"] = phases
    data["current_phase"] = phase
    _atomic_write(path, data)


def end_phase(
    path: Path,
    phase: str,
    status: str,
    error: Optional[str] = None,
    recovery: Optional[str] = None,
) -> None:
    """Close the most recent in-progress phase with the given name.

    Raises ValueError if no such open phase exists — loud failure > silent drift.
    """
    data = _read_raw(path) or {}
    phases = data.get("phases", [])
    finished = _now_iso()
    matched = False
    for entry in reversed(phases):
        if entry["name"] == phase and entry["status"] == "in_progress":
            entry["status"] = status
            entry["finished_at"] = finished
            matched = True
            break
    if not matched:
        raise ValueError(
            f"end_phase({phase!r}) called but no in_progress entry with that name"
        )
    data["phases"] = phases
    if status == "failed":
        data["overall"] = "failed"
        # The instant the record closed as failed (failed_update_superseded).
        data["failed_at"] = finished
        if error:
            data["error"] = error
        if recovery:
            data["recovery"] = recovery
    _atomic_write(path, data)


def mark_succeeded(path: Path) -> None:
    data = _read_raw(path) or {}
    _set_succeeded(data)
    _atomic_write(path, data)


def _set_succeeded(data: dict) -> None:
    """Close the record in ``data`` as succeeded (in place, not written)."""
    data["overall"] = "succeeded"
    data["current_phase"] = None


def _set_failed(data: dict, error: str, recovery: Optional[str]) -> None:
    """Close the record in ``data`` as failed (in place, not written).

    Writes ``failed_at``, the instant the record closed as failed, on every
    close, so the last close wins (``failed_update_superseded``).
    """
    data["overall"] = "failed"
    data["failed_at"] = _now_iso()
    data["error"] = error
    if recovery:
        data["recovery"] = recovery


def mark_failed(
    path: Path,
    error: str,
    recovery: Optional[str] = None,
) -> None:
    """Mark overall=failed with an explicit error/recovery.

    Use when a failure happens OUTSIDE any active phase (pre-flight failure,
    post-phase exception). No-op if the status file doesn't exist. Each call
    writes ``failed_at``, the instant the record closed as failed, so the
    last close wins (``failed_update_superseded``).
    """
    data = _read_raw(path)
    if data is None:
        return
    _set_failed(data, error, recovery)
    _atomic_write(path, data)


def clear_status(path: Path) -> None:
    """Delete the status file. Used by the tray's refusal path."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass

"""Refuse a tray update before its transaction starts, and give the service back.

The tray runs the service in its own process. When the user clicks Update,
the tray spawns ``python -m jacked.service.updater`` and then stops, so the
service is down until the updater restarts it. The supervisor does not
restart a clean exit (launchd ``KeepAlive`` has ``SuccessfulExit = false``).
An updater that refuses before its transaction and only returns therefore
leaves jacked down until the user restarts it by hand.

:func:`refuse_and_restore` is the one exit for a refusal before the
transaction: it logs the reason and writes the recovery file. Then it takes
the update lock without waiting. With the lock, it closes the tray's status
record as failed, waits for the old service to exit, restarts it, and
releases the lock. A no-op refusal (the index serves only an older version,
#295i) is not a failure: it writes no recovery file, and it closes the
tray's pre-init record as done at the installed version. Without the lock (another updater holds it, or the lock
cannot be taken), another updater can own the record and the restart, so it
changes neither. Each step is best effort, because the reason for the
refusal (a package that cannot import its own modules, for example) can
break the later steps too.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# How long the refusal waits for the old service to exit before it restarts
# it. The transaction's own wait uses the same limit.
PARENT_EXIT_TIMEOUT_SECONDS = 15.0


def refuse_and_restore(
    reason: str,
    recovery_text: str,
    *,
    parent_pid: int,
    log,
    write_recovery,
    wait_for_exit,
    start_fallback=None,
    noop_version=None,
) -> bool:
    """Refuse the update and restart the service the tray stopped.

    ``reason`` goes into the update log after ``REFUSED:`` and into the
    status record. ``recovery_text`` is the whole recovery file.
    ``start_fallback`` starts the service without its supervisor; it runs
    when the supervisor restart fails. With ``noop_version`` (the installed
    version), the refusal is a no-op: no recovery file, and the tray's
    pre-init record closes as done at that version, with ``reason`` as its
    note. Returns True when a restart ran and reported success.
    """
    log(f"REFUSED: {reason}")
    if noop_version is None:
        write_recovery(recovery_text)
    try:
        from jacked.service import update_status

        lock = update_status.acquire_update_lock(update_status.UPDATE_STATUS_FILE)
    except Exception as exc:
        logger.exception("Could not take the update lock after a refused update")
        log(f"Refusal: the update lock could not be taken ({type(exc).__name__}); status and service left as they are")
        return False
    if lock is None:
        log("Refusal: another updater holds the update lock; status and service left to it")
        return False
    try:
        try:
            if noop_version is not None:
                from jacked.service.update_noop import close_preinit_as_noop

                close_preinit_as_noop(update_status.UPDATE_STATUS_FILE, noop_version, reason)
            else:
                update_status.mark_failed(
                    update_status.UPDATE_STATUS_FILE,
                    error=f"update refused: {reason}",
                    recovery="see ~/.claude/jacked-update-failed.txt",
                )
        except Exception:
            logger.exception("Could not close the status record of a refused update")
        if not wait_for_exit(parent_pid, timeout=PARENT_EXIT_TIMEOUT_SECONDS):
            log("Refusal: the old service did not exit; nothing restarted")
            return False
        return _restart(log, start_fallback)
    finally:
        try:
            lock.close()
        except Exception:
            logger.exception("Could not release the update lock after a refused update")


def _restart(log, start_fallback) -> bool:
    try:
        from jacked.service.platform import native_restart

        ok, detail = native_restart()
    except Exception as exc:
        logger.exception("Could not restart the service after a refused update")
        ok, detail = False, f"raised {type(exc).__name__}"
    log(f"Refusal: service restart {'OK' if ok else 'FAILED'} ({detail})")
    if ok or start_fallback is None:
        return bool(ok)
    try:
        start_fallback()
    except Exception as exc:
        logger.exception("Could not start the service after a refused update")
        log(f"Refusal: fallback start raised {type(exc).__name__}")
        return False
    log("Refusal: fallback start spawned")
    return False


def refuser(parent_pid: int, log, write_recovery, wait_for_exit, *, close, start_fallback=None):
    """Bind :func:`refuse_and_restore` to one updater run.

    The returned function takes ``reason``, ``recovery_text`` and
    ``noop_version`` (see :func:`refuse_and_restore`), refuses, and then
    calls ``close`` (the update log).
    """

    def refuse(reason: str, recovery_text: "str | None", *, noop_version=None) -> bool:
        try:
            return refuse_and_restore(
                reason,
                recovery_text,
                parent_pid=parent_pid,
                log=log,
                write_recovery=write_recovery,
                wait_for_exit=wait_for_exit,
                start_fallback=start_fallback,
                noop_version=noop_version,
            )
        finally:
            close()

    return refuse


def record_tray_refusal(reason: str) -> None:
    """Record a refusal that the tray makes before it spawns an updater. Never raises.

    The install cannot upgrade itself (``can_auto_upgrade``), so the tray
    writes ``reason`` to the recovery file and removes a stale status record.
    The tray keeps running, so there is no service to restart.
    """
    try:
        from jacked.service.updater import RECOVERY_FILE

        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(reason + "\n")
    except Exception:
        logger.exception("Could not write recovery file on refusal")
    try:
        from jacked.service import update_status

        update_status.clear_status(update_status.UPDATE_STATUS_FILE)
    except Exception:
        logger.exception("Could not clear stale status file on refusal")

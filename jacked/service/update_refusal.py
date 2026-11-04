"""Refuse a tray update before its transaction starts, and give the service back.

The tray runs the service in its own process. When the user clicks Update,
the tray spawns ``python -m jacked.service.updater`` and then stops, so the
service is down until the updater restarts it. The supervisor does not
restart a clean exit (launchd ``KeepAlive`` has ``SuccessfulExit = false``).
An updater that refuses before its transaction and only returns therefore
leaves jacked down until the user restarts it by hand.

:func:`refuse_and_restore` is the one exit for a refusal that this updater
owns: it logs the reason, writes the recovery file, closes the tray's status
record as failed, waits for the old service to exit, and restarts it. Each
step is best effort, because the reason for the refusal (a package that
cannot import its own modules, for example) can break the later steps too.
A refusal because ANOTHER updater holds the update lock does not use it:
that updater owns the status record and the restart.
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
    mark_status: bool = True,
) -> bool:
    """Refuse the update and restart the service the tray stopped.

    ``reason`` goes into the update log after ``REFUSED:`` and into the
    status record. ``recovery_text`` is the whole recovery file. With
    ``mark_status`` False, the status record is not changed. Returns True
    when the service restart reported success.
    """
    log(f"REFUSED: {reason}")
    write_recovery(recovery_text)
    if mark_status:
        try:
            from jacked.service import update_status

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
    try:
        from jacked.service.platform import native_restart

        ok, detail = native_restart()
    except Exception as exc:
        logger.exception("Could not restart the service after a refused update")
        log(f"Refusal: service restart raised {type(exc).__name__}")
        return False
    log(f"Refusal: service restart {'OK' if ok else 'FAILED'} ({detail})")
    return bool(ok)


def refuser(parent_pid: int, log, write_recovery, wait_for_exit, *, close):
    """Bind :func:`refuse_and_restore` to one updater run.

    The returned function takes ``reason``, ``recovery_text`` and
    ``mark_status``, refuses, and then calls ``close`` (the update log).
    """

    def refuse(reason: str, recovery_text: str, *, mark_status: bool = True) -> bool:
        try:
            return refuse_and_restore(
                reason,
                recovery_text,
                parent_pid=parent_pid,
                log=log,
                write_recovery=write_recovery,
                wait_for_exit=wait_for_exit,
                mark_status=mark_status,
            )
        finally:
            close()

    return refuse

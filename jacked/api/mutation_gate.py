"""The one Request-level gate for remote mutations.

Credential switches, credential-operation status and the service upgrade all
decide "may this client mutate the host" the same way: loopback always may;
a remote client may only when the persisted remote-access setting is on AND
its address is inside the enabled scope. The policy itself lives in
``jacked.api.remote_access``; this module resolves the inputs off the
``Request`` and records the outcome, so no route carries its own copy.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from fastapi import Request, status
from fastapi.responses import JSONResponse

from jacked.api.remote_access import (
    credential_mutation_decision,
    credential_policy_editable,
    read_enabled_scope,
)

logger = logging.getLogger(__name__)

# A remote peer that is refused keeps being refused; one audit line per host
# and reason per minute keeps a denial loop from evicting the dashboard's
# 2,000-line log ring buffer, and the next line carries how many were folded.
DENIAL_LOG_INTERVAL_SECONDS = 60.0
_denial_log_state: dict[tuple[str, str], tuple[float, int]] = {}


def reset_denial_throttle() -> None:
    """Forget the denial throttle (tests, and the lifespan's lock reset)."""
    _denial_log_state.clear()


def _log_denial(audit: str, host: str, reason: str, enabled: bool, scope: str) -> None:
    now = time.monotonic()
    key = (host, reason)
    last_at, folded = _denial_log_state.get(key, (None, 0))
    if last_at is not None and now - last_at < DENIAL_LOG_INTERVAL_SECONDS:
        _denial_log_state[key] = (last_at, folded + 1)
        return
    suffix = f" ({folded} more in the last {int(DENIAL_LOG_INTERVAL_SECONDS)} s)" if folded else ""
    logger.info(
        "Remote credential mutation DENIED (%s) for client %s: %s "
        "(enabled=%s, scope=%s)%s",
        audit,
        host,
        reason,
        enabled,
        scope,
        suffix,
    )
    _denial_log_state[key] = (now, 0)


def mutation_decision(
    request: Request, *, audit: str | None = None
) -> tuple[bool, str | None, str | None]:
    """Resolve ``(allowed, reason, client_host)`` for this request.

    Loopback always may. A remote client may only when the persisted
    remote-access setting is on AND its address is inside the enabled scope,
    so the Settings toggle is the one visible switch that hands out mutation
    control. No DB, or a settings DB that will not read, means loopback only.

    Pass ``audit`` (a short action name) from the routes that mutate or expose
    a mutation: every non-loopback decision then leaves one INFO line naming
    the client and the reason, so a remote action is attributable after the
    fact. Loopback is deliberately silent, since the local dashboard acts all
    day. Only addresses and setting values are logged, never tokens or
    account secrets.
    """
    client = request.client
    host = client.host if client and client.host else None
    enabled, scope = read_enabled_scope(getattr(request.app.state, "db", None))
    allowed, reason = credential_mutation_decision(host, enabled, scope)
    if audit and not credential_policy_editable(host):
        if allowed:
            logger.info(
                "Remote credential mutation ALLOWED (%s) for client %s "
                "(remote access on, scope=%s)",
                audit,
                host,
                scope,
            )
        else:
            _log_denial(audit, str(host), str(reason), enabled, scope)
    return allowed, reason, host


def denied_response(
    reason: str, messages: Mapping[str, str], *, code: str
) -> JSONResponse:
    """The 403 a refused mutation returns: reason-keyed copy, a stable code.

    ``messages`` maps each policy reason to the user-facing sentence for the
    action that was refused; ``code`` is the machine-readable error code the
    dashboard keys on. The response is never cached.
    """
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"error": {"message": messages[reason], "code": code}},
        headers={"Cache-Control": "no-store"},
    )

"""The one gate that every write route of the dashboard app runs.

``jacked.api.main`` attaches :func:`require_mutation_access` to the whole
app as a dependency, so each POST, PUT, PATCH and DELETE route runs
``mutation_decision`` before its handler (and before its body is checked).
Loopback always may. A remote client may only when remote access is on and
its address is inside the enabled scope, which is the same rule as for an
account switch. A refused client gets the 403 of ``denied_response``.

Some write routes are not refused here. Each one is in :data:`GATE_SKIPS`
with the reason. tests/unit/api/test_write_gate.py walks the route table
and fails on a write route that is not gated and not in this table.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.requests import HTTPConnection

from jacked.api.mutation_gate import denied_response, mutation_decision
from jacked.api.remote_access import REASON_OUTSIDE_SCOPE, REASON_REMOTE_ACCESS_OFF

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The copy of the app-wide refusal. A route that runs the gate itself keeps
# copy for its own action. No em-dash: the text is user-facing.
MUTATION_DENIED_MESSAGES = {
    REASON_REMOTE_ACCESS_OFF: (
        "Changes from this browser are off. Turn on remote access under "
        "Settings > Advanced on the host machine."
    ),
    REASON_OUTSIDE_SCOPE: (
        "This browser's address is outside the remote access scope. Widen the "
        "scope under Settings > Advanced on the host machine."
    ),
}

_OWN_GATE = "The route runs mutation_decision itself, with copy for its own action."
_OAUTH_STORE_ONLY = (
    "Starts an OAuth flow that only stores the login that the user completes "
    "in the browser. The gate decides whether the flow may activate it."
)
_NO_RENEWAL = (
    "Reads usage or the profile. For a refused caller the route sets "
    "allow_refresh=False: a 401 is reported, and no token is renewed or imported."
)

# (method, full path) -> why the app-wide gate does not refuse the route.
GATE_SKIPS: dict[tuple[str, str], str] = {
    ("PUT", "/api/settings/swap-settings"): _OWN_GATE,
    ("POST", "/api/settings/swap-pause"): _OWN_GATE,
    ("POST", "/api/settings/swap-resume"): _OWN_GATE,
    ("POST", "/api/upgrade"): _OWN_GATE,
    ("PUT", "/api/settings/{key}"): _OWN_GATE,
    ("DELETE", "/api/settings/{key}"): _OWN_GATE,
    ("POST", "/api/auth/accounts/{account_id}/use"): _OWN_GATE + " It also meters remote switches.",
    ("PUT", "/api/settings/remote-access"): (
        "Loopback only, which is stricter than the gate: this setting is the grant itself."
    ),
    ("POST", "/api/settings/remote-access/restart"): (
        "Loopback only, which is stricter than the gate: it applies the grant and restarts the service."
    ),
    ("POST", "/api/auth/accounts/add"): (
        "The Codex import runs the gate in the route. " + _OAUTH_STORE_ONLY
    ),
    ("POST", "/api/auth/accounts/{account_id}/reauth"): _OAUTH_STORE_ONLY,
    ("POST", "/api/auth/accounts/{account_id}/authorize-cc"): _OAUTH_STORE_ONLY,
    ("POST", "/api/auth/flow/{flow_id}/code"): (
        "Completes a flow whose start already decided activation."
    ),
    ("POST", "/api/auth/flow/{flow_id}/open"): (
        "Opens the browser of a flow that already exists. It writes nothing."
    ),
    ("POST", "/api/auth/accounts/{account_id}/refresh-usage"): _NO_RENEWAL,
    ("POST", "/api/auth/accounts/refresh-all-usage"): _NO_RENEWAL,
    ("POST", "/api/auth/accounts/{account_id}/validate"): _NO_RENEWAL,
}


class MutationRefused(Exception):
    """The app-wide gate refused this write. ``reason`` is a policy reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def require_mutation_access(conn: HTTPConnection) -> None:
    """Refuse a write by a client that may not change this host.

    A read, a WebSocket handshake and a route in :data:`GATE_SKIPS` pass.
    FastAPI runs this plain function in a worker thread, so the settings
    read stays off the event loop.
    """
    method = conn.scope.get("method")
    if conn.scope.get("type") != "http" or method not in UNSAFE_METHODS:
        return
    route = conn.scope.get("route")
    path = getattr(route, "path", None) or conn.url.path
    if (method, path) in GATE_SKIPS:
        return
    allowed, reason, _host = mutation_decision(conn, audit=f"{method} {path}")
    if not allowed:
        raise MutationRefused(str(reason))


async def mutation_refused_handler(request: Request, exc: MutationRefused) -> JSONResponse:
    """The 403 of a refused write: reason-keyed copy, a stable code."""
    return denied_response(exc.reason, MUTATION_DENIED_MESSAGES, code=exc.reason.upper())


def mutation_refusal(request: Request, audit: str) -> Optional[JSONResponse]:
    """The 403 for a route that runs the gate itself, else None.

    Call it in a worker thread: it reads the settings DB.
    """
    allowed, reason, _host = mutation_decision(request, audit=audit)
    if allowed:
        return None
    return denied_response(str(reason), MUTATION_DENIED_MESSAGES, code=str(reason).upper())

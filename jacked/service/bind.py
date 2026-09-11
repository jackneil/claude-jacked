"""Bind resolution and pre-bound socket construction for the dashboard server.

The dashboard has no authentication layer, so *where it listens* is a security
decision, not a convenience knob. This module is the single place that decides
which addresses the server binds, using one fixed precedence:

1. **Explicit CLI ``--host``** — a one-shot override. When the operator typed a
   host, we honor exactly that and nothing else: no settings read, no Tailscale
   detection. This preserves today's CLI behavior verbatim, including
   ``--host 0.0.0.0``. It is never persisted.
2. **DB settings** — ``remote_access_enabled`` (the bare string ``'true'`` /
   ``'false'``, matching the settings_swap.py convention) and
   ``remote_access_scope`` (``'tailscale'`` | ``'all'``). This is the GUI's
   channel and the durable source of truth across reboots and upgrades.
3. **Loopback default** — ``127.0.0.1`` when nothing above applies.

Fail-safe rules (a bind decision must never take the dashboard down):

- The socket sets are **mutually exclusive** so two sockets never race for the
  same ``addr:port`` (EADDRINUSE): loopback → ``(127.0.0.1,)``; tailscale →
  ``(127.0.0.1, 100.x)``; all → ``(0.0.0.0,)`` *alone*.
- ``remote_access_scope`` other than the two documented values (missing, typo,
  garbage) falls back to the documented default ``'tailscale'`` and logs a
  warning naming the offending value.
- When Tailscale is requested but **no tailnet IP is detected**, we bind
  loopback only, record a human-readable ``fallback_reason``, and log loudly.
  We never silently widen to ``0.0.0.0`` on a detection miss, and detection
  failures never raise out of :func:`resolve_bind`.
- A DB read error (corrupt/locked settings DB) is caught and degraded to the
  loopback default with a warning — a broken DB must not stop the server from
  coming up on loopback.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import subprocess
import sys
from dataclasses import dataclass

from jacked.winproc import NO_WINDOW

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_ALL_INTERFACES = "0.0.0.0"

# Tailscale hands every node an address in the CGNAT range 100.64.0.0/10, and
# routes 100.100.100.100 to its local service endpoint (used as the UDP
# route-trick target). ipaddress does the membership test.
#
# Public because it is also the authority for "is this HTTP client on the
# tailnet?" in jacked/api/remote_access.py. One definition, so the bind plan
# and the credential-mutation gate can never disagree about what a tailnet
# address is.
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_SERVICE_IP = "100.100.100.100"


@dataclass(frozen=True)
class BindPlan:
    """The resolved decision about where the server should listen.

    ``addresses`` are IPv4 literals to bind, in order; ``primary_host`` is the
    address that feeds ``JACKED_HOST`` (CORS/CSRF/lifespan) and any user-facing
    "listening on" line. ``tailscale_ip`` and ``fallback_reason`` are populated
    only on the tailscale path.
    """

    mode: str  # 'loopback' | 'tailscale' | 'all' | 'cli'
    addresses: tuple[str, ...]
    port: int
    primary_host: str
    tailscale_ip: str | None = None
    fallback_reason: str | None = None

    @property
    def probe_host(self) -> str:
        """The address to health-probe / bind-check this server on locally.

        For ``loopback``, ``tailscale``, and ``all`` the loopback address is
        always reachable from the same machine (``127.0.0.1`` is bound
        directly, or ``0.0.0.0`` covers it), so we probe loopback. Only a
        ``cli`` plan binds a single caller-specified address and nothing else,
        so it must be probed on exactly that address (this also preserves
        today's ``--host 0.0.0.0`` probe behavior verbatim).
        """
        if self.mode in ("loopback", "tailscale", "all"):
            return _LOOPBACK
        return self.primary_host

    def as_effective(self) -> dict:
        """The live-state view the settings API exposes to the GUI.

        ``addresses`` is a plain list so the payload is JSON-serializable.
        """
        return {
            "mode": self.mode,
            "addresses": list(self.addresses),
            "tailscale_ip": self.tailscale_ip,
            "fallback_reason": self.fallback_reason,
        }


# Module-level active-plan registry. The serving paths (tray
# ``ServiceRunner._start_uvicorn`` and the ``webux`` non-reload path) publish
# the BindPlan they ACTUALLY bound here, so the settings API can report live
# effective state instead of guessing. Plain module global with no lock: there
# is only ever one writer (whichever serving path is currently up), and the
# reader (the settings route) just needs the last-published value.
_active_plan: BindPlan | None = None


def set_active_plan(plan: BindPlan | None) -> None:
    """Publish the BindPlan the server actually bound (``None`` clears it)."""
    global _active_plan
    _active_plan = plan


def get_active_plan() -> BindPlan | None:
    """The BindPlan the server last bound, or ``None`` if nothing is serving."""
    return _active_plan


def _loopback_plan(port: int) -> BindPlan:
    return BindPlan(
        mode="loopback",
        addresses=(_LOOPBACK,),
        port=port,
        primary_host=_LOOPBACK,
    )


def describe_remote_exposure(plan: "BindPlan", enabled: bool, scope: str) -> str | None:
    """One startup line when reachability goes past loopback, else ``None``.

    Worth saying out loud because the two decisions are independent: the bind
    says who can REACH the dashboard, the persisted setting says who may
    SWITCH CREDENTIALS through it. ``--host``/``JACKED_HOST`` short-circuits
    the settings read entirely (``mode="cli"``), so a one-shot wide bind can
    meet a stale ``remote_access_enabled=true`` row and re-grant remote
    credential control with no fresh confirmation. This names both halves.

    >>> loopback = BindPlan('loopback', ('127.0.0.1',), 8321, '127.0.0.1')
    >>> describe_remote_exposure(loopback, True, 'all') is None
    True
    >>> wide = BindPlan('cli', ('0.0.0.0',), 8321, '0.0.0.0')
    >>> describe_remote_exposure(wide, False, 'tailscale')
    ... # doctest: +ELLIPSIS
    'Listening beyond loopback on 0.0.0.0:8321, but remote access is off: ...'
    >>> 'any peer that can reach the port' in describe_remote_exposure(wide, True, 'all')
    True
    >>> 'wider than the saved scope' in describe_remote_exposure(wide, True, 'tailscale')
    True
    """
    reachable = [a for a in plan.addresses if a not in (_LOOPBACK, "::1")]
    if not reachable:
        return None
    where = ", ".join(f"{a}:{plan.port}" for a in reachable)

    if not enabled:
        return (
            f"Listening beyond loopback on {where}, but remote access is off: "
            "the setting, not the bind, decides credential switching, so every "
            "remote peer is denied account switching and upgrades."
        )

    audience = (
        "any peer that can reach the port"
        if scope == "all"
        else "peers on your tailnet (100.64.0.0/10)"
    )
    message = (
        f"Listening beyond loopback on {where} with remote access on "
        f"(scope {scope}): {audience} can switch accounts and trigger upgrades."
    )
    if plan.mode == "cli" and scope != "all":
        message += (
            " This bind came from --host or JACKED_HOST and is wider than the "
            "saved scope; the setting, not the bind, decides credential "
            "switching, so peers outside the scope are denied."
        )
    return message


def log_remote_exposure(plan: BindPlan, db=None) -> str | None:
    """Emit :func:`describe_remote_exposure` once at service start.

    Never raises: a startup log line must not be able to stop the service
    coming up, so an unreadable settings DB degrades to the fail-closed
    ``(False, default)`` reading rather than propagating.
    """
    try:
        from jacked.api.remote_access import read_enabled_scope

        if db is None:
            from jacked.web.database import Database

            db = Database()
        enabled, scope = read_enabled_scope(db)
    except Exception as exc:
        logger.warning(
            "Could not read remote-access settings for the startup exposure "
            "check (%s: %s); assuming off.",
            type(exc).__name__,
            exc,
        )
        enabled, scope = False, "tailscale"

    message = describe_remote_exposure(plan, enabled, scope)
    if message:
        logger.warning("%s", message)
    return message


def resolve_bind(cli_host: str | None, port: int, db=None) -> BindPlan:
    """Resolve the bind plan. Read-only: this never persists anything.

    Precedence: explicit ``cli_host`` > DB settings > loopback default. See the
    module docstring for the full fail-safe contract.
    """
    # 1. Explicit CLI override — honored verbatim, no DB, no detection.
    if cli_host is not None:
        return BindPlan(
            mode="cli",
            addresses=(cli_host,),
            port=port,
            primary_host=cli_host,
        )

    # 2. DB settings. Construct the default Database lazily so importing this
    #    module stays cheap, and wrap the whole read so a broken DB can't stop
    #    the server from coming up on loopback.
    #    Function-level import: jacked.api.remote_access imports TAILSCALE_CGNAT
    #    from this module, so a module-level import here would be a cycle.
    from jacked.api.remote_access import VALID_SCOPES, read_enabled_scope

    try:
        if db is None:
            from jacked.web.database import Database

            db = Database()
        # Read the raw scope first, inside the guard: read_enabled_scope()
        # normalizes and never raises, so this is what still surfaces a
        # locked/corrupt DB as the loopback fallback below, and it is also
        # what lets us warn about an unrecognized value before it is
        # silently defaulted.
        raw_scope = db.get_setting("remote_access_scope")
        enabled, scope = read_enabled_scope(db)
    except Exception as exc:  # sqlite errors, corrupt DB, unreadable path ...
        logger.warning(
            "Could not read remote-access settings (%s: %s); binding loopback only.",
            type(exc).__name__,
            exc,
        )
        return _loopback_plan(port)

    # 3. Anything other than an explicit 'true' means remote access is off.
    if not enabled:
        return _loopback_plan(port)

    if scope == "all":
        return BindPlan(
            mode="all",
            addresses=(_ALL_INTERFACES,),
            port=port,
            primary_host=_ALL_INTERFACES,
        )

    # scope == 'tailscale', or a missing/invalid value that defaulted to it.
    if raw_scope not in VALID_SCOPES:
        logger.warning(
            "Unknown remote_access_scope %r; defaulting to 'tailscale'.", raw_scope
        )

    ts_ip = detect_tailscale_ip()
    if ts_ip is not None:
        return BindPlan(
            mode="tailscale",
            addresses=(_LOOPBACK, ts_ip),
            port=port,
            primary_host=ts_ip,
            tailscale_ip=ts_ip,
        )

    reason = (
        "Remote access is on, but no Tailscale IP was detected at startup; "
        "listening on loopback only."
    )
    logger.warning("%s Re-detection happens on every restart.", reason)
    return BindPlan(
        mode="tailscale",
        addresses=(_LOOPBACK,),
        port=port,
        primary_host=_LOOPBACK,
        tailscale_ip=None,
        fallback_reason=reason,
    )


def _in_cgnat_range(ip_str: str) -> bool:
    """True only if ``ip_str`` is a valid IP inside Tailscale's 100.64.0.0/10."""
    try:
        return ipaddress.ip_address(ip_str) in TAILSCALE_CGNAT
    except (ValueError, TypeError):
        return False


def _detect_via_udp() -> str | None:
    """Primary detection: the UDP route trick.

    Connecting a UDP socket sends no packets; it just makes the kernel pick the
    source address it *would* use to reach the Tailscale service IP. If the
    tailnet is up, that source is the node's 100.x address.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((_TAILSCALE_SERVICE_IP, 80))
        local_ip = sock.getsockname()[0]
    except OSError as exc:
        # Diagnostic breadcrumb: distinguish "no route to the tailnet" (daemon
        # down / not on the tailnet) from the other miss modes below, so an
        # on-call can tell WHY the bind fell back to loopback, not just THAT it
        # did. debug-level: normal on a machine that never runs Tailscale.
        logger.debug("Tailscale UDP route detection failed (%s)", exc)
        return None
    finally:
        sock.close()
    if _in_cgnat_range(local_ip):
        return local_ip
    logger.debug(
        "Tailscale UDP route detection returned %s, outside 100.64.0.0/10; "
        "not a tailnet address",
        local_ip,
    )
    return None


def _detect_via_cli() -> str | None:
    """Fallback detection: shell out to ``tailscale ip -4``.

    Tolerates a missing binary, a hung/slow CLI (short timeout), a non-zero
    exit, and garbage output. Parses the first line and validates it against
    the same CGNAT range.
    """
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=NO_WINDOW,  # hidden console on Windows, no-op on POSIX
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # FileNotFoundError (⊂ OSError), TimeoutExpired (⊂ SubprocessError), ...
        # debug-level so an on-call can distinguish "binary missing" from "CLI
        # hung/timed out" when triaging a loopback fallback.
        logger.debug(
            "`tailscale ip -4` could not run (%s: %s)", type(exc).__name__, exc
        )
        return None
    if result.returncode != 0:
        logger.debug(
            "`tailscale ip -4` exit %d: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return None
    lines = (result.stdout or "").splitlines()
    first_line = lines[0].strip() if lines else ""
    if _in_cgnat_range(first_line):
        return first_line
    logger.debug(
        "`tailscale ip -4` returned %r, not a 100.64.0.0/10 address", first_line
    )
    return None


def detect_tailscale_ip() -> str | None:
    """Best-effort Tailscale node IP, or ``None``. Never raises."""
    return _detect_via_udp() or _detect_via_cli()


def _apply_reuse_option(sock: socket.socket) -> None:
    """Set the correct address-reuse option for the platform.

    POSIX: ``SO_REUSEADDR`` lets a fresh bind reclaim a port still in TIME_WAIT
    from the just-stopped server (needed across a restart); it does NOT let a
    second process steal an actively-listening socket.

    Windows: ``SO_REUSEADDR`` means something DIFFERENT and dangerous - it lets
    a second same-user socket bind over an active listener with nondeterministic
    delivery. The server-correct option there is ``SO_EXCLUSIVEADDRUSE``, which
    claims the port exclusively. The tray's ``_wait_for_port_free`` already polls
    until the old bind is released, so we do not need TIME_WAIT reuse on Windows.
    """
    if sys.platform == "win32":
        # SO_EXCLUSIVEADDRUSE exists only on Windows.
        opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if opt is not None:
            sock.setsockopt(socket.SOL_SOCKET, opt, 1)
            return
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def create_sockets(plan: BindPlan) -> list[socket.socket]:
    """Create and bind (but do NOT listen on) one socket per plan address.

    uvicorn calls ``listen()`` itself, so we hand it bound-but-unlistened
    sockets. Sockets are created fresh on every call — a restart can't reuse
    closed sockets. On any bind failure we close everything opened so far and
    raise ``OSError`` naming the address:port that failed.
    """
    sockets: list[socket.socket] = []
    for addr in plan.addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _apply_reuse_option(sock)
            sock.bind((addr, plan.port))
        except OSError as exc:
            sock.close()
            for opened in sockets:
                opened.close()
            raise OSError(f"Failed to bind {addr}:{plan.port} ({exc})") from exc
        sockets.append(sock)
    return sockets

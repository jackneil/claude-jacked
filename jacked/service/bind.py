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
from dataclasses import dataclass

from jacked.winproc import NO_WINDOW

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_ALL_INTERFACES = "0.0.0.0"

# Tailscale hands every node an address in the CGNAT range 100.64.0.0/10, and
# routes 100.100.100.100 to its local service endpoint (used as the UDP
# route-trick target). ipaddress does the membership test.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
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


def _loopback_plan(port: int) -> BindPlan:
    return BindPlan(
        mode="loopback",
        addresses=(_LOOPBACK,),
        port=port,
        primary_host=_LOOPBACK,
    )


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
    try:
        if db is None:
            from jacked.web.database import Database

            db = Database()
        enabled = db.get_setting("remote_access_enabled")
        scope = db.get_setting("remote_access_scope")
    except Exception as exc:  # sqlite errors, corrupt DB, unreadable path ...
        logger.warning(
            "Could not read remote-access settings (%s: %s); "
            "binding loopback only.",
            type(exc).__name__,
            exc,
        )
        return _loopback_plan(port)

    # 3. Anything other than an explicit 'true' means remote access is off.
    if enabled != "true":
        return _loopback_plan(port)

    if scope == "all":
        return BindPlan(
            mode="all",
            addresses=(_ALL_INTERFACES,),
            port=port,
            primary_host=_ALL_INTERFACES,
        )

    # scope == 'tailscale', or a missing/invalid value that defaults to it.
    if scope != "tailscale":
        logger.warning(
            "Unknown remote_access_scope %r; defaulting to 'tailscale'.", scope
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
        return ipaddress.ip_address(ip_str) in _TAILSCALE_CGNAT
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
    except OSError:
        return None
    finally:
        sock.close()
    return local_ip if _in_cgnat_range(local_ip) else None


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
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError (⊂ OSError), TimeoutExpired (⊂ SubprocessError), ...
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or "").splitlines()
    first_line = lines[0].strip() if lines else ""
    return first_line if _in_cgnat_range(first_line) else None


def detect_tailscale_ip() -> str | None:
    """Best-effort Tailscale node IP, or ``None``. Never raises."""
    return _detect_via_udp() or _detect_via_cli()


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
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((addr, plan.port))
        except OSError as exc:
            sock.close()
            for opened in sockets:
                opened.close()
            raise OSError(
                f"Failed to bind {addr}:{plan.port} ({exc})"
            ) from exc
        sockets.append(sock)
    return sockets

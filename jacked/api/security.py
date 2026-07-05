"""Network-exposure hardening for the dashboard API.

The dashboard has no authentication layer: it is designed to be reached
either from the machine it runs on, or over a private overlay network
(Tailscale) where the tailnet ACL is the access control. What this module
guarantees is that *network reachability is the only way in*: a browser
on an allowed machine must not become a confused deputy:

- **No CORS wildcard, ever.** The frontend builds every API and WebSocket
  URL from ``location.host``, so legitimate remote use is always
  same-origin and needs no CORS allowance at all. Binding ``0.0.0.0`` used
  to flip CORS to ``*``, which let any website open in a browser on an
  allowed machine read and mutate the API. Cross-origin allowance is now
  loopback-only plus an explicit opt-in list (``JACKED_ALLOWED_ORIGINS``).

- **WebSocket handshakes enforce origin.** Browsers do not apply CORS to
  WebSockets; the server must check ``Origin`` itself. The handshake is
  accepted only when the origin is same-host with the request or in the
  explicit allowlist.

- **Host-header validation blocks DNS rebinding.** A rebinding attack
  serves a page from ``attacker.example`` and re-points that name at this
  machine, making the browser's requests *same-origin*, so CORS and origin
  checks cannot see it. The tell is the ``Host`` header, which keeps the
  attacker's domain. We reject any Host that is not an IP literal, a
  single-label name (MagicDNS short names, mDNS), ``localhost``,
  ``*.ts.net`` (Tailscale MagicDNS FQDNs), ``*.local`` (mDNS), or an
  explicit entry in ``JACKED_ALLOWED_HOSTS``. Public rebinding requires a
  registrable multi-label domain the attacker controls, which can never
  take any of those forms.

Environment knobs (both comma-separated, read at request time):

- ``JACKED_ALLOWED_ORIGINS``: extra exact origins (``scheme://host[:port]``)
  granted CORS and WebSocket access, for genuinely cross-origin consumers.
- ``JACKED_ALLOWED_HOSTS``: extra hostnames accepted in the ``Host``
  header, for custom DNS names pointing at this machine.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}

# Suffixes an attacker cannot serve a rebinding page from: .ts.net names
# are issued only by Tailscale's DNS, .local is reserved for mDNS, and
# .localhost always resolves to loopback per RFC 6761.
_TRUSTED_SUFFIXES = (".ts.net", ".local", ".localhost")

_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _csv_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def extra_allowed_origins() -> list[str]:
    """Exact extra origins from JACKED_ALLOWED_ORIGINS, normalized."""
    return [o.rstrip("/").lower() for o in _csv_env("JACKED_ALLOWED_ORIGINS")]


def extra_allowed_hosts() -> list[str]:
    """Extra hostnames from JACKED_ALLOWED_HOSTS, lowercased."""
    return [h.lower() for h in _csv_env("JACKED_ALLOWED_HOSTS")]


def build_allowed_origins(host: str, port: int) -> list[str]:
    """Build the CORS / WebSocket cross-origin allowlist.

    Never returns ``"*"``: the dashboard frontend is same-origin by
    construction, so a network bind needs no blanket cross-origin grant.

    >>> build_allowed_origins("127.0.0.1", 8321)
    ['http://127.0.0.1:8321', 'http://localhost:8321']
    >>> build_allowed_origins("0.0.0.0", 8321)
    ['http://127.0.0.1:8321', 'http://localhost:8321']
    """
    origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    for extra in extra_allowed_origins():
        if extra not in origins:
            origins.append(extra)
    return origins


def _split_hostport(netloc: str) -> tuple[str, int | None]:
    """Split a Host header / netloc into (hostname, port).

    Handles bracketed IPv6 (``[::1]:8321``). Returns port ``None`` when
    absent or unparseable.
    """
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        # Bracketed IPv6: [::1] or [::1]:8321
        end = netloc.find("]")
        if end == -1:
            # Unterminated bracket is malformed; fail closed rather than
            # letting "[::1" fall into the single-label allowance.
            return "", None
        host = netloc[1:end]
        rest = netloc[end + 1 :]
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return host, None
        return host, None
    host, sep, port_s = netloc.rpartition(":")
    if not sep:
        return netloc, None
    # Unbracketed IPv6 (multiple colons) has no port component.
    if ":" in host:
        return netloc, None
    try:
        return host, int(port_s)
    except ValueError:
        return netloc, None


def _is_ip_literal(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        return False


def host_header_allowed(host_header: str) -> bool:
    """True if the request's Host header cannot be a DNS-rebinding vector.

    Allowed forms: IP literals (loopback, LAN, tailnet), ``localhost``,
    single-label names (MagicDNS short names, mDNS shorthand; these are
    not registrable on public DNS, so a rebinding page cannot be served
    from one), ``*.ts.net`` / ``*.local`` / ``*.localhost``, and explicit
    ``JACKED_ALLOWED_HOSTS`` entries.
    """
    if not host_header:
        return False
    hostname, _port = _split_hostport(host_header)
    # A trailing dot is the absolute-FQDN form of the same name; strip it
    # so "studio.tailnet.ts.net." and "studio." match their dotless twins.
    hostname = hostname.rstrip(".")
    if not hostname:
        return False
    if hostname in _LOOPBACK_NAMES:
        return True
    if _is_ip_literal(hostname):
        return True
    if hostname.endswith(_TRUSTED_SUFFIXES):
        return True
    if "." not in hostname:
        return True
    if hostname in extra_allowed_hosts():
        return True
    return False


def origin_allowed(
    origin: str,
    host_header: str,
    allowed_origins: list[str],
) -> bool:
    """True if a browser Origin may use the API / WebSocket.

    Accepts exact allowlist matches, and any origin that points at the
    same host the request was addressed to (same-origin use through
    whatever name the client browsed to: loopback, MagicDNS, LAN IP).
    Ports must agree when both sides specify one (scheme defaults count
    as specified).
    """
    if not origin or origin == "null":
        return False
    normalized = origin.rstrip("/").lower()
    if normalized in allowed_origins:
        return True

    parsed = urlsplit(normalized)
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.netloc:
        return False
    origin_host, origin_port = _split_hostport(parsed.netloc)
    if origin_port is None:
        origin_port = _DEFAULT_PORTS[parsed.scheme]

    request_host, request_port = _split_hostport(host_header or "")
    if not request_host or origin_host != request_host:
        return False
    # Same hostname. If the request carries an explicit port, it must
    # match; a portless Host means a default-port frontend proxy
    # (e.g. `tailscale serve`), where the origin port is the proxy's.
    return request_port is None or origin_port == request_port


_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class HostValidationMiddleware:
    """Pure-ASGI middleware for Host validation and cross-site writes.

    Two rejections:

    - Untrusted ``Host`` header (DNS-rebinding guard): 421 / handshake
      close. Pure ASGI (not ``BaseHTTPMiddleware``) so it covers
      *WebSocket* handshakes too: Starlette's http middleware never sees
      those, and a rebound page's WebSocket is same-origin and passes
      origin checks, leaving the Host header as the only tell.
    - Unsafe-method request carrying a foreign ``Origin`` (CSRF guard):
      403. Cross-site form posts never preflight, so CORS alone cannot
      stop a blind state-changing request; browsers do send ``Origin``
      on every cross-origin POST, which is the reliable tell. Requests
      without an Origin header (curl, scripts, same-origin GETs) are
      untouched.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        host_header = ""
        host_count = 0
        origin_header = ""
        for name, value in scope.get("headers", []):
            if name == b"host":
                host_count += 1
                host_header = value.decode("latin-1")
            elif name == b"origin":
                origin_header = value.decode("latin-1")

        # Starlette's request.headers.get("host") returns the FIRST value
        # while a naive scan keeps the LAST; refusing duplicates outright
        # removes the desync (and the smuggling shape it becomes behind a
        # reverse proxy). Legitimate clients never send two Host headers.
        if host_count > 1 or not host_header_allowed(host_header):
            logger.warning(
                "Rejected %s request: %s (possible DNS rebinding; set "
                "JACKED_ALLOWED_HOSTS to allow)",
                scope["type"],
                f"{host_count} duplicate Host headers"
                if host_count > 1
                else f"untrusted Host header {host_header!r}",
            )
            if scope["type"] == "websocket":
                # Close before accept; the server surfaces this as a
                # 403 handshake rejection.
                await send({"type": "websocket.close", "code": 4003})
                return
            await self._reject(send, 421, "Untrusted Host header", "UNTRUSTED_HOST")
            return

        if (
            scope["type"] == "http"
            and scope.get("method", "").upper() in _UNSAFE_METHODS
            and origin_header
            and not origin_allowed(
                origin_header,
                host_header,
                build_allowed_origins(
                    os.environ.get("JACKED_HOST", "127.0.0.1"),
                    int(os.environ.get("JACKED_PORT", "8321")),
                ),
            )
        ):
            logger.warning(
                "Rejected cross-site %s from origin %r "
                "(set JACKED_ALLOWED_ORIGINS to allow)",
                scope.get("method"),
                origin_header,
            )
            await self._reject(send, 403, "Cross-site request blocked", "CSRF_ORIGIN")
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send, status: int, message: str, code: str):
        body = json.dumps({"error": {"message": message, "code": code}}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

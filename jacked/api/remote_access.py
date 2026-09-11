"""Who may mutate credentials over the network.

The dashboard has no login, so "which client is talking to us?" is the only
authentication the credential-switching routes get. This module holds that
decision and nothing else: pure functions over a client address plus the
persisted remote-access setting, with no ``Request``, no DB handle inside the
policy itself, and no I/O. The routes in ``jacked/api/routes/auth.py`` only
have to prove they wire the right three values in.

The policy, in one sentence: loopback may always switch accounts, and a remote
client may switch accounts exactly when remote access is turned on AND its
address falls inside the scope that was turned on. That makes the Settings
toggle the single, visible switch that grants credential control, which is
what both remote-access confirmation dialogs already promise ("anyone on your
tailnet ... will have full control, including switching accounts").

Everything fails closed: an address that will not parse, a missing client, an
absent DB, or a scope string nobody recognizes all land on loopback-only.
"""

import ipaddress
import logging
import threading
import time
from collections import deque

from jacked.service.bind import TAILSCALE_CGNAT

logger = logging.getLogger(__name__)

# Tailscale's IPv6 half. A tailnet node gets both a CGNAT v4 address and an
# address inside this ULA prefix. The tailscale-scope bind is IPv4-only today
# (resolve_bind binds the detected 100.x address), so no live client arrives
# from this prefix; the branch is defensive, for a future v6 bind or a v6
# forwarded address, and costs nothing. It must never be read as a range we
# actively serve.
TAILSCALE_ULA = ipaddress.ip_network("fd7a:115c:a1e0::/48")

# Hosts that mean "this machine". "testclient" is Starlette's TestClient
# default, local by definition. Kept as literals rather than IP objects
# because "localhost"/"testclient" are names, not addresses.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")

# The two scope values the settings UI can persist. Anything else (a corrupted
# row, a hand-edited DB, an older build) is read as the narrow one.
DEFAULT_SCOPE = "tailscale"
VALID_SCOPES = ("tailscale", "all")

# Machine-readable reasons a mutation was refused. Published on
# GET /api/auth/active-credential and used to pick the 403 copy, because the
# two denials have different fixes: flip a toggle, or widen a scope.
REASON_REMOTE_ACCESS_OFF = "remote_access_off"
REASON_OUTSIDE_SCOPE = "outside_scope"


def read_enabled_scope(db) -> tuple[bool, str]:
    """Read ``(enabled, scope)`` from the settings DB.

    Uses the bare-string convention (``'true'``/``'false'``) the rest of the
    settings table uses; absent keys, an unrecognized scope, or no DB at all
    mean off plus the narrow default. Shared by the remote-access GET/PUT
    response body, the restart broadcast payload, and the credential-mutation
    gate, so all three read the setting the same way.

    >>> read_enabled_scope(None)
    (False, 'tailscale')
    >>> class _DB:
    ...     def __init__(self, **kv):
    ...         self._kv = kv
    ...     def get_setting(self, key):
    ...         return self._kv.get(key)
    >>> read_enabled_scope(_DB())
    (False, 'tailscale')
    >>> read_enabled_scope(_DB(remote_access_enabled='true', remote_access_scope='all'))
    (True, 'all')
    >>> read_enabled_scope(_DB(remote_access_enabled='true', remote_access_scope='lan'))
    (True, 'tailscale')
    >>> read_enabled_scope(_DB(remote_access_enabled='yes'))
    (False, 'tailscale')

    A settings DB that cannot be read denies rather than raises:

    >>> class _BrokenDB:
    ...     def get_setting(self, key):
    ...         raise RuntimeError('database is locked')
    >>> read_enabled_scope(_BrokenDB())
    (False, 'tailscale')
    """
    if db is None:
        return False, DEFAULT_SCOPE
    try:
        enabled = db.get_setting("remote_access_enabled") == "true"
        stored_scope = db.get_setting("remote_access_scope")
    except Exception as exc:  # locked/corrupt sqlite, unreadable path ...
        # Fail closed, and do NOT propagate: this read now runs on /use,
        # /credential-operations/* and the polled /active-credential, so a
        # raising DB would turn a locked settings file into a 500 storm.
        # Only the exception type and message are logged; no setting values,
        # no tokens.
        logger.warning(
            "Could not read remote-access settings (%s: %s); "
            "treating remote credential mutation as off.",
            type(exc).__name__,
            exc,
        )
        return False, DEFAULT_SCOPE
    scope = stored_scope if stored_scope in VALID_SCOPES else DEFAULT_SCOPE
    return enabled, scope


def client_in_scope(host: str | None, scope: str) -> bool:
    """Whether ``host`` is inside the network ``scope`` remote access was
    enabled for.

    ``'all'`` means every address, because that scope binds every interface.
    Any other scope (including the documented ``'tailscale'``) means the
    tailnet only: IPv4 in 100.64.0.0/10 or IPv6 in ``fd7a:115c:a1e0::/48``.
    Unknown scopes collapse to tailnet-only to match ``read_enabled_scope``'s
    own fallback, so a corrupted settings row narrows access instead of
    widening it.

    A host that is not a parsable IP literal is never in scope.
    ``request.client.host`` is a socket peer address, or a forwarded token
    substituted by a trusted loopback proxy (see the ``forwarded_allow_ips``
    pinning in cli.py/tray.py), so a name here means an exotic proxy or a
    stub, and guessing is not a thing a credential gate should do.

    >>> client_in_scope('100.116.47.72', 'tailscale')
    True
    >>> client_in_scope('192.168.42.5', 'tailscale')
    False
    >>> client_in_scope('192.168.42.5', 'all')
    True
    >>> client_in_scope('fd7a:115c:a1e0::1', 'tailscale')
    True
    >>> client_in_scope('2001:db8::1', 'tailscale')
    False
    >>> client_in_scope('hank-llm', 'all')
    False
    >>> client_in_scope(None, 'all')
    False
    """
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host.strip().lower())
    except (ValueError, TypeError):
        return False
    if scope == "all":
        return True
    return address in TAILSCALE_CGNAT or address in TAILSCALE_ULA


def credential_policy_editable(host: str | None) -> bool:
    """Whether a client at ``host`` may CHANGE the remote-access setting.

    Loopback only, always, regardless of what the setting currently says.
    The setting is a live authorization bit: it decides who may switch
    credentials on the very next request. If a remote peer could flip it,
    reaching the port once would be enough to grant itself credential
    control, and "turn remote access off" would not be a revocation (the
    socket stays bound wide until the service restarts). So the switch that
    grants remote credential control is only ever flipped at the host.

    >>> credential_policy_editable('127.0.0.1')
    True
    >>> credential_policy_editable('localhost')
    True
    >>> credential_policy_editable('100.116.47.72')
    False
    >>> credential_policy_editable(None)
    False
    """
    return (host.strip().lower() if host else "") in LOOPBACK_HOSTS


def credential_mutation_decision(
    host: str | None, enabled: bool, scope: str
) -> tuple[bool, str | None]:
    """``(allowed, reason)`` for a credential mutation by a client at ``host``.

    ``enabled``/``scope`` are the persisted remote-access setting (see
    :func:`read_enabled_scope`). Loopback is always allowed, since that is
    someone sitting at the machine. Everyone else needs remote access turned
    on *and* an address inside the enabled scope.

    ``reason`` is ``None`` when allowed, otherwise the machine-readable
    ``'remote_access_off'`` (the feature is off entirely) or
    ``'outside_scope'`` (it is on, but not for this address). The dashboard
    turns those two into different sentences, because the fix differs: one is
    a toggle, the other is a scope.

    >>> credential_mutation_decision('127.0.0.1', False, 'tailscale')
    (True, None)
    >>> credential_mutation_decision('100.116.47.72', False, 'tailscale')
    (False, 'remote_access_off')
    >>> credential_mutation_decision('100.116.47.72', True, 'tailscale')
    (True, None)
    >>> credential_mutation_decision('192.168.42.5', True, 'tailscale')
    (False, 'outside_scope')
    >>> credential_mutation_decision('192.168.42.5', True, 'all')
    (True, None)
    >>> credential_mutation_decision(None, True, 'all')
    (False, 'outside_scope')
    """
    normalized = host.strip().lower() if host else ""
    if normalized in LOOPBACK_HOSTS:
        return True, None
    if not enabled:
        return False, REASON_REMOTE_ACCESS_OFF
    if client_in_scope(normalized, scope):
        return True, None
    return False, REASON_OUTSIDE_SCOPE


def credential_mutation_allowed(
    host: str | None, enabled: bool, scope: str
) -> bool:
    """Boolean half of :func:`credential_mutation_decision`.

    >>> credential_mutation_allowed('127.0.0.1', False, 'tailscale')
    True
    >>> credential_mutation_allowed('100.116.47.72', False, 'tailscale')
    False
    >>> credential_mutation_allowed('100.116.47.72', True, 'tailscale')
    True
    >>> credential_mutation_allowed('192.168.42.5', True, 'tailscale')
    False
    >>> credential_mutation_allowed('192.168.42.5', True, 'all')
    True
    >>> credential_mutation_allowed(None, True, 'all')
    False
    """
    return credential_mutation_decision(host, enabled, scope)[0]


class SwitchRateLimiter:
    """Per-client-host rolling-window cap on credential switches.

    Only non-loopback peers are metered (the route decides that); someone at
    the machine is not a threat model. An in-scope remote peer is, though:
    every accepted switch drives a Keychain write plus settings writes on the
    host, and a fresh action id per attempt defeats the idempotency replay, so
    without a cap a single tailnet peer can thrash the live credential store.

    Deliberately in-memory and per-process: this bounds abuse from a reachable
    peer, it is not an auth boundary (the scope gate is), and a restart
    clearing it is fine. ``clock`` is injectable so the window is testable
    without sleeping.

    >>> clock = iter([0.0, 1.0, 2.0, 3.0])
    >>> limiter = SwitchRateLimiter(limit=2, window=60.0, clock=lambda: next(clock))
    >>> limiter.check('100.64.0.9')[0], limiter.check('100.64.0.9')[0]
    (True, True)
    >>> allowed, retry_after = limiter.check('100.64.0.9')
    >>> allowed, round(retry_after)
    (False, 58)
    >>> limiter.check('100.64.0.10')[0]
    True
    """

    def __init__(self, limit: int = 6, window: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, host: str) -> tuple[bool, float]:
        """Record an attempt by ``host``; return ``(allowed, retry_after)``.

        ``retry_after`` is ``0.0`` when allowed, else whole seconds (at least
        1) until the oldest hit in the window expires. A blocked attempt does
        NOT extend the window: the caller is rate limited, not punished.
        """
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            # Drop hosts whose whole window has expired so a peer rotating
            # source addresses cannot grow this map without bound.
            for stale in [
                h
                for h, hits in self._hits.items()
                if not hits or hits[-1] <= cutoff
            ]:
                del self._hits[stale]

            hits = self._hits.setdefault(host, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return False, max(1.0, round(hits[0] + self.window - now, 3))
            hits.append(now)
            return True, 0.0

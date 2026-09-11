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

from jacked.service.bind import TAILSCALE_CGNAT

# Tailscale's IPv6 half. A tailnet node gets both a CGNAT v4 address and an
# address inside this ULA prefix, and a browser on a dual-stack tailnet may
# connect over either, so the v4 range alone would deny half of a legitimately
# enabled tailnet.
TAILSCALE_ULA = ipaddress.ip_network("fd7a:115c:a1e0::/48")

# Hosts that mean "this machine". "testclient" is Starlette's TestClient
# default, local by definition. Kept as literals rather than IP objects
# because "localhost"/"testclient" are names, not addresses.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")

# The two scope values the settings UI can persist. Anything else (a corrupted
# row, a hand-edited DB, an older build) is read as the narrow one.
DEFAULT_SCOPE = "tailscale"
VALID_SCOPES = ("tailscale", "all")


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
    """
    enabled = False
    scope = DEFAULT_SCOPE
    if db is not None:
        enabled = db.get_setting("remote_access_enabled") == "true"
        stored_scope = db.get_setting("remote_access_scope")
        if stored_scope in VALID_SCOPES:
            scope = stored_scope
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

    A host that is not a parsable IP literal is never in scope. Uvicorn always
    reports ``request.client.host`` as an address, so a name here means an
    exotic proxy or a stub, and guessing is not a thing a credential gate
    should do.

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


def credential_mutation_allowed(
    host: str | None, enabled: bool, scope: str
) -> bool:
    """Whether a client at ``host`` may switch credentials.

    ``enabled``/``scope`` are the persisted remote-access setting (see
    :func:`read_enabled_scope`). Loopback is always allowed, since that is
    someone sitting at the machine. Everyone else needs remote access turned
    on *and* an address inside the enabled scope.

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
    normalized = host.strip().lower() if host else ""
    if normalized in LOOPBACK_HOSTS:
        return True
    if not enabled:
        return False
    return client_in_scope(normalized, scope)

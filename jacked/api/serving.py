"""Settings that every jacked uvicorn server shares.

Three places start a server: the tray service (``jacked/service/tray.py``),
the foreground ``jacked webux`` command, and its ``--reload`` dev mode
(``jacked/cli.py``). They spread ``SERVER_SETTINGS`` so that a setting cannot
drift between them; ``tests/unit/api/test_server_settings.py`` scans for any
server that does not.
"""

from __future__ import annotations

from types import MappingProxyType

SERVER_SETTINGS = MappingProxyType(
    {
        # Proxy-header trust is pinned, never inherited: uvicorn would
        # otherwise read FORWARDED_ALLOW_IPS from the environment, and
        # request.client.host is what the credential gate authorizes on.
        # proxy_headers stays TRUE on purpose: `tailscale serve` proxies
        # from loopback and rewrites X-Forwarded-For to the real tailnet
        # source, so honoring a loopback proxy's forwarding is what keeps
        # a serve viewer from inheriting loopback's permissions.
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
        # The dashboard socket (/api/ws) runs on the websockets Sans-I/O
        # protocol. "auto" picks uvicorn's legacy implementation before 0.50,
        # and websockets deprecated that API in 14.0 and plans to remove it.
        "ws": "websockets-sansio",
    }
)

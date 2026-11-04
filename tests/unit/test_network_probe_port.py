"""The heal sweep's reachability probe keeps the token endpoint's port.

The probe sends a HEAD to the token endpoint's origin. Built from
``url.host`` alone it dropped an explicit port, so an endpoint on a
non-default port (a test endpoint) was probed on port 80 and read as
unreachable, and the sweep deferred every account forever.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import httpx

from jacked.web import auth


def _probe_with(token_url: str) -> str:
    seen: list[str] = []

    async def head(self, url, *args, **kwargs):
        seen.append(str(url))
        return httpx.Response(405)

    with (
        mock.patch.object(auth, "TOKEN_URL", token_url),
        mock.patch.object(httpx.AsyncClient, "head", head),
    ):
        assert asyncio.run(auth._network_reachable()) is True
    return seen[0]


def test_the_probe_keeps_an_explicit_port():
    assert _probe_with("http://127.0.0.1:8398/v1/oauth/token") == "http://127.0.0.1:8398/"


def test_the_probe_targets_the_origin_of_the_production_endpoint():
    assert _probe_with("https://platform.claude.com/v1/oauth/token") == "https://platform.claude.com/"


def test_a_refused_connection_is_unreachable():
    async def head(self, url, *args, **kwargs):
        raise httpx.ConnectError("refused")

    with mock.patch.object(httpx.AsyncClient, "head", head):
        assert asyncio.run(auth._network_reachable()) is False

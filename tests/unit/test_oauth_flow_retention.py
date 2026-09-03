"""A finished OAuth flow must stay pollable after its verdict lands.

The dashboard learns a flow's result by polling GET /api/auth/flow/{id} once a
second. The sign-in window takes the foreground, so the dashboard tab is hidden
for the whole sign-in, and a hidden tab's timers are throttled (Chrome runs them
once a minute after five minutes hidden, or freezes the tab outright). The
server used to forget a flow 30 seconds after the callback, so a late poll came
back not_found: the tokens were stored, the banner said "expired", and the
account-action guard stayed up until the page was reloaded.
"""

import asyncio
from unittest.mock import MagicMock

from jacked.web import oauth as oauth_mod
from jacked.web.oauth import FLOW_RETENTION_SECONDS, OAuthFlow, _active_flows


class _Runner:
    async def cleanup(self):
        pass


def test_retention_outlasts_browser_timer_throttling():
    # Five minutes hidden is where Chrome's intensive throttling starts, so a
    # verdict has to survive at least that long to be read by a throttled tab.
    assert FLOW_RETENTION_SECONDS >= 300


def test_completed_flow_stays_registered_until_retention_elapses(monkeypatch):
    monkeypatch.setattr(oauth_mod, "FLOW_RETENTION_SECONDS", 0.05)

    async def _body():
        flow = OAuthFlow(MagicMock())
        _active_flows[flow.flow_id] = flow
        task = asyncio.create_task(flow._wait_for_callback(_Runner()))
        flow._status = "completed"
        flow._event.set()
        await asyncio.sleep(0.01)
        still_registered = flow.flow_id in _active_flows
        await task
        gone_after = flow.flow_id not in _active_flows
        return still_registered, gone_after

    still_registered, gone_after = asyncio.run(_body())
    assert still_registered, "a completed flow must remain pollable during retention"
    assert gone_after, "and be dropped once retention elapses"


def test_manual_flow_uses_the_same_retention(monkeypatch):
    monkeypatch.setattr(oauth_mod, "FLOW_RETENTION_SECONDS", 0.05)
    monkeypatch.setattr(oauth_mod, "MANUAL_TIMEOUT_SECONDS", 60)

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        _active_flows[flow.flow_id] = flow
        task = asyncio.create_task(flow._expire_manual_flow())
        flow._status = "completed"
        flow._event.set()
        await asyncio.sleep(0.01)
        still_registered = flow.flow_id in _active_flows
        await task
        return still_registered, flow.flow_id not in _active_flows

    still_registered, gone_after = asyncio.run(_body())
    assert still_registered and gone_after

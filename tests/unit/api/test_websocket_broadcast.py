"""The WebSocket broadcaster never sends a frame that a browser cannot parse.

Python's ``json.dumps`` writes the bare tokens ``NaN`` and ``Infinity`` by
default. ``JSON.parse`` refuses them, so the dashboard drops the whole message,
not only the bad value (#231i). The broadcaster refuses such a payload and
logs the topic, and the usage parsers keep non-finite values out of the
payloads in the first place.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from jacked.api.routes.auth import _account_to_response
from jacked.api.websocket import WebSocketRegistry
from jacked.service.menubar_summary import binding_model_compact

_LOGGER = "jacked.api.websocket"


def _socket():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def _strict_loads(text):
    """``json.loads`` that fails on NaN, Infinity and -Infinity, like a browser."""

    def _refuse(token):
        raise AssertionError(f"the frame holds the token {token}")

    return json.loads(text, parse_constant=_refuse)


def _broadcast_all(messages):
    """Connect one client, broadcast each (topic, payload), return the client."""
    registry = WebSocketRegistry()
    ws = _socket()

    async def _go():
        await registry.connect(ws)
        for topic, payload in messages:
            await registry.broadcast(topic, payload)

    asyncio.run(_go())
    return registry, ws


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_payload_is_dropped_with_a_warning(value, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        registry, ws = _broadcast_all([("x", {"v": value})])
    ws.send_text.assert_not_called()
    assert registry.client_count == 1  # the client stays connected
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER and r.levelno == logging.WARNING
    ]
    assert warnings == ["WebSocket broadcast dropped: topic=x carries a non-finite number"]


def test_a_finite_payload_still_sends():
    _, ws = _broadcast_all([("x", {"v": 1.5})])
    ws.send_text.assert_called_once()
    frame = _strict_loads(ws.send_text.call_args[0][0])
    assert (frame["type"], frame["payload"]) == ("x", {"v": 1.5})


def _usage_row():
    """An account whose payload holds a model percent of 1e999, a severity
    of 1e999 and a model reset of 1e999, beside finite 5h and 7d windows."""
    raw = (
        '{"five_hour": {"utilization": 12.0, "resets_at": "2026-11-02T22:00:00+00:00"}, '
        '"seven_day": {"utilization": 34.0, "resets_at": "2026-11-08T00:00:00+00:00"}, '
        '"limits": [{"kind": "session", "percent": 12}, {"kind": "weekly_all", "percent": 34}, '
        '{"kind": "weekly_scoped", "percent": 1e999, "severity": 1e999, "is_active": true, '
        '"scope": {"model": {"display_name": "Fable"}}}, '
        '{"kind": "weekly_scoped", "percent": 20, "resets_at": 1e999, "is_active": false, '
        '"scope": {"model": {"display_name": "Opus"}}}]}'
    )
    return {
        "id": 7, "email": "u@x.com", "expires_at": 9999999999, "provider": "claude",
        "subscription_type": "max", "rate_limit_tier": "default_claude_max_5x",
        "cached_usage_5h": 12.0, "cached_usage_7d": 34.0, "cached_usage_raw": raw,
    }


def test_usage_frames_hold_no_nan_or_infinity_token():
    row = _usage_row()
    raw = json.loads(row["cached_usage_raw"])
    _, ws = _broadcast_all([
        # The bulk refresh sends the full account response.
        ("usage_refresh_progress",
         {"account_id": 7, "account_data": _account_to_response(row).model_dump()}),
        # The usage poll sends the compact binding model.
        ("usage_poll_updated",
         {"account_id": 7, "account_data": {"_binding_model": binding_model_compact(raw)}}),
    ])
    frames = [call.args[0] for call in ws.send_text.call_args_list]
    assert len(frames) == 2  # neither message was dropped
    for text in frames:
        assert "NaN" not in text and "Infinity" not in text
    progress, poll = (_strict_loads(text) for text in frames)
    usage = progress["payload"]["account_data"]["usage"]
    assert (usage["five_hour"], usage["seven_day"]) == (12.0, 34.0)
    assert set(usage["per_model"]) == {"opus"}
    assert usage["per_model"]["opus"]["resets_at"] is None
    assert poll["payload"]["account_data"]["_binding_model"]["label"] == "Opus"

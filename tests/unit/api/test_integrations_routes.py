"""Route tests for jacked/api/routes/integrations.py (agent-reach).

Fully offline: the runner class is patched, so no uv/agent-reach/npm/git
process is ever spawned. Each test builds a minimal FastAPI app with only the
router under test, wired to a fake DB, so it is isolated from the full app's
lifespan and background loops.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes import integrations, system
from jacked.integrations.agent_reach import (
    SETTING_OVERRIDE_ACK,
    SETTING_OVERRIDE_AT,
    SETTING_OVERRIDE_SHA,
)

REACH_BASE = "/api/integrations/agent-reach"

_STATUS_FIXTURE = {
    "installed": True,
    "installed_sha": "e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
    "pin_sha": "e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
    "authoritative_sha": "e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
    "sha_matches": True,
    "drift": [],
    "override": {"active": False, "sha": None, "ack": False, "at": None},
    "doctor": {"channels": [{"channel": "twitter", "active_backend": "twitter-cli", "ok": True}]},
    "doctor_error": None,
    "pin": {"version_label": "1.5.0", "vetted_at": "2026-07-11", "short_sha": "e825f6740d24"},
}


@pytest.fixture(autouse=True)
def _rebind_lock():
    """Rebind the module lock to each test's fresh TestClient event loop."""
    integrations.reset_locks()
    yield


def _reach_client(runner_mock: MagicMock | None = None) -> tuple[TestClient, MagicMock]:
    """Minimal app with just the integrations router + a fake DB and runner."""
    app = FastAPI()
    app.include_router(integrations.router, prefix="/api/integrations")
    app.state.db = MagicMock()  # non-None; runner is mocked so DB is never hit
    runner = runner_mock if runner_mock is not None else MagicMock()
    return TestClient(app), runner


def _system_client() -> TestClient:
    """Minimal app with the system router (for the generic /settings endpoint)."""
    app = FastAPI()
    app.include_router(system.router, prefix="/api")
    app.state.db = MagicMock()
    return TestClient(app)


def test_get_returns_runner_status_shape():
    client, runner = _reach_client()
    runner.status.return_value = dict(_STATUS_FIXTURE)
    with patch.object(integrations, "AgentReachRunner", return_value=runner):
        resp = client.get(REACH_BASE)
    assert resp.status_code == 200
    body = resp.json()
    # Runner status passes through verbatim...
    assert body["installed"] is True
    assert body["pin"]["version_label"] == "1.5.0"
    assert body["override"]["active"] is False
    # ...plus the update-availability stub field.
    assert body["upstream_check"] is None


def test_install_runs_off_thread():
    client, runner = _reach_client()
    main_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def _install():
        seen["thread"] = threading.get_ident()
        return {"installed": True, "installed_sha": "a" * 40}

    runner.install.side_effect = _install
    with patch.object(integrations, "AgentReachRunner", return_value=runner):
        resp = client.post(f"{REACH_BASE}/install")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "op": "install", "result": {"installed": True, "installed_sha": "a" * 40}}
    runner.install.assert_called_once()
    # Proof it did not run inline on the request-issuing thread.
    assert seen["thread"] != main_thread


def test_unknown_channel_returns_422():
    client, runner = _reach_client()
    runner.enable_channel.side_effect = RuntimeError(
        "unknown channel 'bogus'; valid channels: reddit, search, twitter"
    )
    with patch.object(integrations, "AgentReachRunner", return_value=runner):
        resp = client.post(f"{REACH_BASE}/channels/bogus")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "REACH_CHANNEL_ERROR"
    assert "unknown channel" in body["error"]["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"ref": "main"},                          # confirm_unvetted missing
        {"ref": "main", "confirm_unvetted": False},  # explicit false
        {"ref": "main", "confirm_unvetted": "true"}, # string, not a real bool
    ],
)
def test_put_override_rejects_without_confirm_unvetted_true(payload):
    client, runner = _reach_client()
    with patch.object(integrations, "AgentReachRunner", return_value=runner):
        resp = client.put(f"{REACH_BASE}/override", json=payload)
    assert resp.status_code == 422
    # The runner must never be asked to install unvetted code.
    runner.set_override.assert_not_called()


def test_put_override_accepts_real_true():
    client, runner = _reach_client()
    runner.set_override.return_value = {"override_sha": "c" * 40, "constraints": "/tmp/c.txt"}
    with patch.object(integrations, "AgentReachRunner", return_value=runner):
        resp = client.put(f"{REACH_BASE}/override", json={"ref": "main", "confirm_unvetted": True})
    assert resp.status_code == 200
    assert resp.json()["override"]["override_sha"] == "c" * 40
    runner.set_override.assert_called_once_with("main", ack=True)


def test_delete_override_clears():
    client, runner = _reach_client()
    with patch.object(integrations, "AgentReachRunner", return_value=runner):
        resp = client.delete(f"{REACH_BASE}/override")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "cleared": True}
    runner.clear_override.assert_called_once()


@pytest.mark.parametrize(
    "key", [SETTING_OVERRIDE_SHA, SETTING_OVERRIDE_ACK, SETTING_OVERRIDE_AT]
)
def test_generic_settings_endpoint_rejects_override_keys(key):
    client = _system_client()
    resp = client.put(f"/api/settings/{key}", json={"value": "forged"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"

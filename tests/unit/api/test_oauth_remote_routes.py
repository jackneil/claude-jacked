"""Tests for the remote/manual OAuth surface of jacked/api/routes/auth.py.

Covers the manual-mode decision (_manual_oauth), the ``remote`` query param
on the three flow-starting routes, the flow-status payload, and the new
POST /flow/{id}/code paste endpoint.

Routes are driven through the real FastAPI app with a plain ``TestClient(app)``
that never enters the context manager, so the lifespan (background sweeps,
real home dir) never runs. The only app.state a request path needs — ``db`` —
is set by the fixture and cleaned up. No test starts a real OAuth flow: the
OAuthFlow class is stubbed so nothing binds a port or calls Anthropic.
"""

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jacked.api.main import app
from jacked.api.routes import auth as routes_auth
from jacked.web import oauth as oauth_mod
from jacked.web.database import Database


@pytest.fixture
def db(tmp_path):
    """A throwaway file-backed DB wired onto app.state for the test.

    File-backed, not ``:memory:``: TestClient serves handlers on a worker
    thread and Database keeps a thread-local connection, so an in-memory DB
    would look schema-less to the handler thread."""
    database = Database(str(tmp_path / "jacked.db"))
    prev = getattr(app.state, "db", None)
    app.state.db = database
    yield database
    if prev is not None:
        app.state.db = prev
    else:
        try:
            del app.state.db
        except AttributeError:
            pass
    database.close()


@pytest.fixture
def client(db):
    return TestClient(app)


# ---------------------------------------------------------------------------
# _manual_oauth: who gets the paste flow
# ---------------------------------------------------------------------------


def _request(host):
    """Minimal stand-in — _manual_oauth only reads request.client.host."""
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(client=client)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "testclient"])
def test_manual_oauth_false_for_loopback_clients(host):
    """A local dashboard can use the localhost callback and the local
    browser, so it keeps the automatic flow."""
    assert routes_auth._manual_oauth(_request(host), remote=False) is False


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.20", "100.64.0.3"])
def test_manual_oauth_true_for_non_loopback_clients(host):
    """The server's browser and its localhost:451xx callback are both
    useless to a user sitting on another machine."""
    assert routes_auth._manual_oauth(_request(host), remote=False) is True


def test_manual_oauth_remote_flag_overrides_a_loopback_client():
    """An explicit ``remote=true`` wins — e.g. a tunnel or reverse proxy
    that makes a remote user look local."""
    assert routes_auth._manual_oauth(_request("127.0.0.1"), remote=True) is True


def test_manual_oauth_defaults_to_manual_without_a_client():
    """No client address (ASGI scope without ``client``) fails safe toward
    the flow that works from anywhere."""
    assert routes_auth._manual_oauth(_request(None), remote=False) is True


# ---------------------------------------------------------------------------
# remote= on the flow-starting routes
# ---------------------------------------------------------------------------


@pytest.fixture
def started_flows(monkeypatch):
    """Replace OAuthFlow everywhere the routes reach it; return the list of
    stub instances the routes constructed.

    Two patch targets on purpose: /accounts/add and /reauth use the
    module-level import in routes/auth.py, while /authorize-cc re-imports
    OAuthFlow inside the function body, which only a patch on
    jacked.web.oauth can reach.
    """
    created = []

    class _StubFlow:
        def __init__(self, db, purpose="primary", target_account_id=None, manual=False):
            self.purpose = purpose
            self.target_account_id = target_account_id
            self.manual = manual
            self.flow_id = "stub-flow-id"
            created.append(self)

        async def start(self):
            return {
                "flow_id": self.flow_id,
                "auth_url": "https://claude.com/cai/oauth/authorize?stub=1",
                "mode": "manual" if self.manual else "browser",
            }

    monkeypatch.setattr(routes_auth, "OAuthFlow", _StubFlow)
    monkeypatch.setattr(oauth_mod, "OAuthFlow", _StubFlow)
    return created


def _make_account(db, email="jack@example.com"):
    return db.create_account(
        email=email,
        access_token="at",
        expires_at=int(time.time()) + 3600,
    )


def test_add_account_with_remote_flag_starts_a_manual_flow(client, started_flows):
    resp = client.post("/api/auth/accounts/add?remote=true")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "manual"
    assert started_flows[0].manual is True


def test_add_account_from_the_test_client_stays_automatic(client, started_flows):
    """Without the flag, a loopback caller (TestClient reports host
    "testclient") keeps the browser flow."""
    resp = client.post("/api/auth/accounts/add")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "browser"
    assert started_flows[0].manual is False


def test_reauth_with_remote_flag_starts_a_manual_flow(client, db, started_flows):
    account = _make_account(db)

    resp = client.post(f"/api/auth/accounts/{account['id']}/reauth?remote=true")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "manual"
    assert started_flows[0].manual is True
    assert started_flows[0].target_account_id == account["id"]


def test_reauth_without_remote_flag_stays_automatic(client, db, started_flows):
    account = _make_account(db)

    resp = client.post(f"/api/auth/accounts/{account['id']}/reauth")

    assert resp.status_code == 200
    assert started_flows[0].manual is False


def test_authorize_cc_with_remote_flag_starts_a_manual_flow(client, db, started_flows):
    """The CC route re-imports OAuthFlow inside the handler, so it is the
    one most likely to drift out of manual mode unnoticed."""
    account = _make_account(db)

    resp = client.post(f"/api/auth/accounts/{account['id']}/authorize-cc?remote=true")

    assert resp.status_code == 200
    assert resp.json()["mode"] == "manual"
    assert started_flows[0].manual is True
    assert started_flows[0].purpose == "claude_code"


def test_authorize_cc_without_remote_flag_stays_automatic(client, db, started_flows):
    account = _make_account(db)

    resp = client.post(f"/api/auth/accounts/{account['id']}/authorize-cc")

    assert resp.status_code == 200
    assert started_flows[0].manual is False


# ---------------------------------------------------------------------------
# GET /flow/{id}: the response model must not drop fields
# ---------------------------------------------------------------------------


class _StubStatusFlow:
    """A flow whose get_status()/submit_code() return canned payloads."""

    def __init__(self, status=None, submit_result=None):
        self._status = status or {}
        self._submit_result = submit_result or {}
        self.submitted = None

    def get_status(self):
        return dict(self._status)

    async def submit_code(self, pasted):
        self.submitted = pasted
        return dict(self._submit_result)


def test_flow_status_returns_every_field_the_flow_reports(client, monkeypatch):
    """Regression guard: the response model used to omit organization_name,
    redirected_from_account_id, auth_url and mode, so the poller never saw
    them no matter what the flow reported."""
    flow = _StubStatusFlow(
        status={
            "status": "completed",
            "flow_id": "flow-1",
            "mode": "manual",
            "auth_url": "https://claude.com/cai/oauth/authorize?stub=1",
            "account_id": 7,
            "email": "jack@example.com",
            "organization_name": "Acme",
            "redirected_from_account_id": 3,
            "cc_flow_id": "cc-flow-1",
        }
    )
    monkeypatch.setattr(routes_auth, "get_flow", lambda flow_id: flow)

    body = client.get("/api/auth/flow/flow-1").json()

    assert body["status"] == "completed"
    assert body["flow_id"] == "flow-1"
    assert body["mode"] == "manual"
    assert body["auth_url"] == "https://claude.com/cai/oauth/authorize?stub=1"
    assert body["account_id"] == 7
    assert body["email"] == "jack@example.com"
    assert body["organization_name"] == "Acme"
    assert body["redirected_from_account_id"] == 3
    assert body["cc_flow_id"] == "cc-flow-1"


def test_flow_status_for_an_unknown_flow_is_not_found(client, monkeypatch):
    monkeypatch.setattr(routes_auth, "get_flow", lambda flow_id: None)

    body = client.get("/api/auth/flow/nope").json()

    assert body["status"] == "not_found"
    assert body["flow_id"] == "nope"


# ---------------------------------------------------------------------------
# POST /flow/{id}/code: pasting the authorization code
# ---------------------------------------------------------------------------


def test_submit_code_unknown_flow_returns_404(client, monkeypatch):
    """An expired flow must say so plainly — the user has to restart, not
    keep pasting."""
    monkeypatch.setattr(routes_auth, "get_flow", lambda flow_id: None)

    resp = client.post("/api/auth/flow/gone/code", json={"code": "auth-code-abc"})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
    assert "not found" in resp.json()["error"]["message"].lower()


def test_submit_code_surfaces_a_recoverable_paste_error(client, monkeypatch):
    """A bad paste comes back 200 with submit_error and a still-pending
    flow, so the UI can ask for another paste instead of restarting."""
    flow = _StubStatusFlow(
        submit_result={
            "status": "pending",
            "flow_id": "flow-1",
            "mode": "manual",
            "submit_error": "bad paste",
        }
    )
    monkeypatch.setattr(routes_auth, "get_flow", lambda flow_id: flow)

    resp = client.post("/api/auth/flow/flow-1/code", json={"code": "garbage"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["submit_error"] == "bad paste"
    assert body["mode"] == "manual"
    assert flow.submitted == "garbage"


def test_submit_code_success_returns_the_completed_account(client, monkeypatch):
    flow = _StubStatusFlow(
        submit_result={
            "status": "completed",
            "flow_id": "flow-1",
            "mode": "manual",
            "account_id": 7,
            "email": "jack@example.com",
            "cc_flow_id": "cc-flow-1",
        }
    )
    monkeypatch.setattr(routes_auth, "get_flow", lambda flow_id: flow)

    resp = client.post(
        "/api/auth/flow/flow-1/code", json={"code": "auth-code-abc#state-xyz"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["account_id"] == 7
    assert body["email"] == "jack@example.com"
    assert body["cc_flow_id"] == "cc-flow-1"
    assert body["submit_error"] is None
    assert flow.submitted == "auth-code-abc#state-xyz"


def test_submit_code_requires_a_code_field(client, monkeypatch):
    """The body model is required — a malformed request must not reach the
    flow at all."""
    flow = _StubStatusFlow(submit_result={"status": "pending", "flow_id": "flow-1"})
    monkeypatch.setattr(routes_auth, "get_flow", lambda flow_id: flow)

    resp = client.post("/api/auth/flow/flow-1/code", json={})

    assert resp.status_code == 422
    assert flow.submitted is None

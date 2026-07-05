"""Tests for jacked.api.security, the network-exposure hardening layer.

Covers the four pure helpers (build_allowed_origins, host_header_allowed,
origin_allowed) plus the HostValidationMiddleware and the /api/ws origin
gate driven through the real FastAPI app.

The app is exercised with a plain TestClient(app) that never enters the
context manager, so the lifespan (which starts background sweep loops and
touches the real home dir) never runs. The two bits of app.state the
request paths need (allowed_origins for the WS gate, ws_registry for the
endpoint) are set by the client fixture and cleaned up after.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jacked.api.main import app
from jacked.api.security import (
    build_allowed_origins,
    host_header_allowed,
    origin_allowed,
)
from jacked.api.websocket import WebSocketRegistry


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Env-reading helpers read os.environ at call time. Strip every knob
    that could bleed in from the ambient shell so each test starts from a
    known-empty baseline (tests that need a knob set it explicitly)."""
    for name in (
        "JACKED_ALLOWED_ORIGINS",
        "JACKED_ALLOWED_HOSTS",
        "JACKED_HOST",
        "JACKED_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


# ---------------------------------------------------------------------------
# build_allowed_origins
# ---------------------------------------------------------------------------


def test_build_allowed_origins_loopback_returns_two_origins():
    assert build_allowed_origins("127.0.0.1", 8321) == [
        "http://127.0.0.1:8321",
        "http://localhost:8321",
    ]


def test_build_allowed_origins_wildcard_bind_never_returns_star():
    """Regression guard: binding 0.0.0.0 used to flip CORS to '*'. The host
    argument is ignored and the loopback list is returned unchanged."""
    result = build_allowed_origins("0.0.0.0", 8321)
    assert result == ["http://127.0.0.1:8321", "http://localhost:8321"]
    assert "*" not in result


def test_build_allowed_origins_appends_env_extras_normalized_and_deduped(monkeypatch):
    """JACKED_ALLOWED_ORIGINS entries are appended lowercased, and a repeat
    of an already-present origin is not duplicated."""
    monkeypatch.setenv(
        "JACKED_ALLOWED_ORIGINS",
        "https://FOO.tail1234.ts.net, http://bar:9000, https://foo.tail1234.ts.net",
    )
    result = build_allowed_origins("127.0.0.1", 8321)
    assert result == [
        "http://127.0.0.1:8321",
        "http://localhost:8321",
        "https://foo.tail1234.ts.net",
        "http://bar:9000",
    ]
    assert "*" not in result
    assert result.count("https://foo.tail1234.ts.net") == 1


# ---------------------------------------------------------------------------
# host_header_allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host_header",
    [
        "127.0.0.1:8321",
        "localhost:8321",
        "LOCALHOST",
        "[::1]:8321",
        "::1",
        "192.168.1.50:8321",
        "100.101.102.103",
        "studio",
        "studio:8321",
        "studio.tail1234.ts.net:8321",
        "mymac.local",
        "anything.localhost",
    ],
)
def test_host_header_allowed_accepts_trusted_forms(host_header):
    assert host_header_allowed(host_header) is True


def test_host_header_allowed_accepts_env_extra(monkeypatch):
    monkeypatch.setenv("JACKED_ALLOWED_HOSTS", "home.example.com")
    assert host_header_allowed("home.example.com") is True


@pytest.mark.parametrize(
    "host_header",
    [
        "",
        "evil.example.com",
        "evil.example.com:8321",
        "sub.evil.io",
    ],
)
def test_host_header_allowed_rejects_registrable_domains(host_header):
    assert host_header_allowed(host_header) is False


def test_host_header_allowed_rejects_env_host_when_var_unset():
    """home.example.com is only allowed when JACKED_ALLOWED_HOSTS names it;
    with the var unset (autouse fixture) it must be rejected."""
    assert host_header_allowed("home.example.com") is False


# ---------------------------------------------------------------------------
# origin_allowed
# ---------------------------------------------------------------------------


def test_origin_allowed_exact_allowlist_match():
    """An origin in the explicit allowlist passes even when it is not
    same-origin with the request host."""
    assert (
        origin_allowed(
            "http://cross.example.com",
            "studio:8321",
            ["http://cross.example.com"],
        )
        is True
    )


@pytest.mark.parametrize(
    "origin, host_header",
    [
        ("http://studio:8321", "studio:8321"),
        ("https://machine.tail1.ts.net", "machine.tail1.ts.net"),
    ],
)
def test_origin_allowed_same_origin(origin, host_header):
    assert origin_allowed(origin, host_header, []) is True


@pytest.mark.parametrize(
    "origin, host_header",
    [
        # Portless Host (default-port proxy such as `tailscale serve`) with a
        # default-port origin.
        ("https://machine.tail1.ts.net", "machine.tail1.ts.net"),
        # Portless Host with an explicit-port origin on the same hostname.
        ("http://studio:8321", "studio"),
    ],
)
def test_origin_allowed_portless_host_accepts_any_port(origin, host_header):
    assert origin_allowed(origin, host_header, []) is True


def test_origin_allowed_port_mismatch_fails():
    assert origin_allowed("http://studio:9999", "studio:8321", []) is False


def test_origin_allowed_foreign_host_fails():
    assert origin_allowed("http://evil.example.com", "studio:8321", []) is False


@pytest.mark.parametrize("origin", ["", "null"])
def test_origin_allowed_empty_and_null_fail(origin):
    assert origin_allowed(origin, "studio:8321", []) is False


def test_origin_allowed_non_http_scheme_fails():
    assert origin_allowed("ftp://studio:8321", "studio:8321", []) is False


# ---------------------------------------------------------------------------
# HostValidationMiddleware + /api/ws, driven through the real app
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """Plain TestClient with the request-path app.state set by hand.

    Never entered as a context manager, so lifespan does not run. Any
    pre-existing state on the shared app singleton is saved and restored.
    """
    # Keep the version route (hit by one test) off the network and off the
    # real home dir: return no update info so it renders a bare response.
    monkeypatch.setattr(
        "jacked.version_check.check_version_cached",
        lambda *args, **kwargs: None,
    )

    sentinel = object()
    prev_origins = getattr(app.state, "allowed_origins", sentinel)
    prev_registry = getattr(app.state, "ws_registry", sentinel)

    app.state.allowed_origins = build_allowed_origins("127.0.0.1", 8321)
    app.state.ws_registry = WebSocketRegistry()

    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        test_client.close()
        if prev_origins is sentinel:
            app.state._state.pop("allowed_origins", None)
        else:
            app.state.allowed_origins = prev_origins
        if prev_registry is sentinel:
            app.state._state.pop("ws_registry", None)
        else:
            app.state.ws_registry = prev_registry


def test_get_with_default_host_is_not_421(client):
    resp = client.get("/api/version")
    assert resp.status_code != 421


def test_get_with_untrusted_host_is_421(client):
    resp = client.get("/api/version", headers={"host": "evil.example.com"})
    assert resp.status_code == 421
    assert resp.json()["error"]["code"] == "UNTRUSTED_HOST"


def test_post_with_foreign_origin_is_403_csrf(client):
    resp = client.post(
        "/api/anything", headers={"origin": "http://evil.example.com"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_post_with_same_origin_is_not_403(client):
    """Same-origin (Origin host matches the default `testserver` Host) must
    pass the CSRF gate; a route-level 404/405 is fine."""
    resp = client.post("/api/anything", headers={"origin": "http://testserver"})
    assert resp.status_code != 403


def test_post_with_no_origin_is_not_403(client):
    resp = client.post("/api/anything")
    assert resp.status_code != 403


@pytest.mark.parametrize("method", ["DELETE", "PUT"])
def test_unsafe_methods_with_foreign_origin_are_403(client, method):
    resp = client.request(
        method, "/api/anything", headers={"origin": "http://evil.example.com"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_get_with_foreign_origin_is_not_blocked(client):
    """Reads are protected by CORS non-issuance, not by the middleware. A
    GET carrying a foreign Origin must not be turned into a 403."""
    resp = client.get("/api/anything", headers={"origin": "http://evil.example.com"})
    assert resp.status_code != 403


# ---- /api/ws origin gate ----


def test_ws_same_origin_connects(client):
    with client.websocket_connect(
        "/api/ws", headers={"origin": "http://testserver"}
    ) as ws:
        assert ws is not None


def test_ws_foreign_origin_is_rejected(client):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/ws", headers={"origin": "http://evil.example.com"}
        ):
            pass
    assert exc_info.value.code == 4003


def test_ws_missing_origin_is_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws"):
            pass


def test_ws_allowlisted_cross_origin_connects(client):
    """An origin present in app.state.allowed_origins connects even though it
    is not same-origin with the request host."""
    app.state.allowed_origins.append("http://cross.example.com")
    with client.websocket_connect(
        "/api/ws", headers={"origin": "http://cross.example.com"}
    ) as ws:
        assert ws is not None


# ---------------------------------------------------------------------------
# Security-review regression guards
# ---------------------------------------------------------------------------


def _run_middleware_http(headers: list[tuple[bytes, bytes]], method: str = "GET"):
    """Drive HostValidationMiddleware directly with a crafted ASGI scope and
    return (reached_app, sent_messages). Lets us send header combinations an
    HTTP client library would normalize away (duplicate Host)."""
    import asyncio

    from jacked.api.security import HostValidationMiddleware

    reached = []

    async def inner_app(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    middleware = HostValidationMiddleware(inner_app)
    scope = {"type": "http", "method": method, "headers": headers, "path": "/"}
    asyncio.run(middleware(scope, receive, send))
    return bool(reached), sent


@pytest.mark.parametrize(
    "headers",
    [
        # Good-then-evil and evil-then-good: BOTH orderings must be refused,
        # closing the first-vs-last desync with Starlette's Headers.get.
        [(b"host", b"127.0.0.1:8321"), (b"host", b"evil.example.com")],
        [(b"host", b"evil.example.com"), (b"host", b"127.0.0.1:8321")],
        # Even two trusted values: duplicates are malformed, period.
        [(b"host", b"127.0.0.1:8321"), (b"host", b"localhost:8321")],
    ],
)
def test_duplicate_host_headers_rejected_both_orderings(headers):
    reached, sent = _run_middleware_http(headers)
    assert reached is False
    assert sent[0]["status"] == 421


def test_single_trusted_host_reaches_app():
    reached, sent = _run_middleware_http([(b"host", b"127.0.0.1:8321")])
    assert reached is True
    assert sent[0]["status"] == 200


def test_unterminated_bracketed_ipv6_host_rejected():
    """"[::1" (no closing bracket) is malformed and must fail closed, not
    slip into the single-label allowance."""
    assert host_header_allowed("[::1") is False


@pytest.mark.parametrize(
    "host_header",
    [
        "studio.tail1234.ts.net.",
        "box.local.",
        "studio.",
    ],
)
def test_trailing_dot_absolute_fqdn_treated_as_dotless_twin(host_header):
    assert host_header_allowed(host_header) is True


def test_trailing_dot_does_not_rescue_registrable_domains():
    assert host_header_allowed("evil.example.com.") is False


def test_cors_middleware_honors_in_place_refresh(client):
    """The lifespan sync (main.py) refreshes the module-level _cors_origins
    list IN PLACE, relying on CORSMiddleware holding the same list object and
    doing per-request membership checks. If a future Starlette copies the
    list at init, this test fails and the sync strategy must be revisited."""
    from jacked.api.main import _cors_origins

    probe = "http://cors-probe.example.com"
    _cors_origins.append(probe)
    try:
        resp = client.get("/api/version", headers={"origin": probe})
        assert resp.headers.get("access-control-allow-origin") == probe
    finally:
        _cors_origins.remove(probe)
    # And after removal the same origin gets no CORS grant.
    resp = client.get("/api/version", headers={"origin": probe})
    assert "access-control-allow-origin" not in resp.headers

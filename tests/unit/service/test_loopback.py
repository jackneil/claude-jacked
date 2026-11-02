"""Loopback JSON requests from the tray agents (jacked.service.loopback).

Each test runs a real HTTP server on 127.0.0.1 inside this process. Nothing
leaves the machine and no jacked service starts.
"""

import http.server
import json
import threading
import urllib.error

import pytest

from jacked.service import loopback


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def _answer(self):
        status, headers, body = self.routes[self.path]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer  # noqa: N815 - http.server API
    do_POST = _answer  # noqa: N815 - http.server API
    do_PUT = _answer  # noqa: N815 - http.server API

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.routes = {}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_port}", _Handler.routes
    finally:
        srv.shutdown()
        srv.server_close()


def _json(obj):
    return 200, {"Content-Type": "application/json"}, json.dumps(obj).encode("utf-8")


def test_get_and_send_read_a_json_object(server):
    _, base, routes = server
    routes["/a"] = _json({"active": None})
    routes["/b"] = _json({"ok": True})
    assert loopback.get_json(base + "/a") == {"active": None}
    assert loopback.send_json(base + "/b", "POST", {}) == {"ok": True}


def test_a_body_that_is_not_an_object_reads_as_empty(server):
    _, base, routes = server
    routes["/list"] = _json(["x"])
    routes["/empty"] = (200, {}, b"")
    assert loopback.get_json(base + "/list") == {}
    assert loopback.get_json(base + "/empty") == {}


def test_a_proxy_setting_never_captures_the_request(server, monkeypatch):
    """The opener is built per request, so this proves the shipped opener,
    not one built before the proxy was set."""
    _, base, routes = server
    routes["/a"] = _json({"active": None})
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    import urllib.request

    # The premise: a default opener built now sends this request to the proxy.
    with pytest.raises(urllib.error.URLError):
        urllib.request.build_opener().open(base + "/a", timeout=2)
    assert loopback.get_json(base + "/a") == {"active": None}


def test_a_redirect_is_refused(server):
    _, base, routes = server
    routes["/moved"] = (302, {"Location": "http://example.invalid/"}, b"")
    with pytest.raises(urllib.error.HTTPError) as err:
        loopback.get_json(base + "/moved")
    assert err.value.code == 302


def test_a_body_over_the_limit_is_refused(server, monkeypatch):
    _, base, routes = server
    monkeypatch.setattr(loopback, "MAX_BODY_BYTES", 64)
    routes["/big"] = (200, {}, b'{"x": "' + b"y" * 100 + b'"}')
    with pytest.raises(ValueError, match="larger than 64 bytes"):
        loopback.get_json(base + "/big")


def test_the_macos_pill_reads_through_the_same_opener(server, monkeypatch):
    """menubar_mac's helpers used urllib.request.urlopen (proxy settings
    applied); they now use jacked.service.loopback."""
    from jacked.service import menubar_mac

    _, base, routes = server
    routes["/s"] = _json({"active": None})
    routes["/p"] = _json({"saved": True})
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert menubar_mac._http_get_json(base + "/s") == {"active": None}
    assert menubar_mac._http_send_json(base + "/p", "PUT", {"x": 1}) == {"saved": True}

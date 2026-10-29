"""Every dashboard page carries a same-origin CSP and, except the update
progress page the tray's local file frames, refuses to be framed."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from jacked.api.main import app

    return TestClient(app)


@pytest.mark.parametrize("path", ["/", "/panel", "/accounts", "/index.html"])
def test_a_dashboard_page_cannot_be_framed(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    for directive in ("default-src 'self'", "connect-src 'self'", "object-src 'none'", "base-uri 'none'"):
        assert directive in csp
    assert "unsafe-eval" not in csp


def test_the_update_page_stays_frameable_by_the_local_bootstrap(client):
    response = client.get("/update.html")
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors" not in response.headers["content-security-policy"]
    assert "x-frame-options" not in response.headers


def test_non_html_responses_carry_no_document_policy(client):
    assert "content-security-policy" not in client.get("/api/health").headers
    assert "content-security-policy" not in client.get("/js/app.js").headers

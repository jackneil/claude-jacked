"""Tests for the GET /panel route (compact usage page for the macOS popover
+ side panel).

Uses TestClient WITHOUT the lifespan context manager (the established pattern in
test_account_label.py) so background tasks / DB init don't run — /panel only
serves a static file. Asserts the route returns 200 with the panel scaffold and
loads the component scripts it depends on, and that the panel.html on disk wires
the reused bar component + grouping util.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from jacked.api.main import app

WEB_DIR = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"


client = TestClient(app, raise_server_exceptions=False)


def test_panel_route_returns_200_and_scaffold():
    resp = client.get("/panel")
    assert resp.status_code == 200
    body = resp.text
    # The render target the panel JS mounts into
    assert 'id="panel-root"' in body
    # A real loading state (verify checklist requires it)
    assert "Loading usage" in body


def test_panel_route_loads_required_scripts():
    body = client.get("/panel").text
    # Must pull the reused bar component + the shared grouping util + the renderer
    assert "/js/components/usage.js" in body
    assert "/js/util/account-grouping.js" in body
    assert "/js/components/panel.js" in body
    assert "/js/utils.js" in body


def test_panel_route_is_no_cache():
    resp = client.get("/panel")
    assert "no-cache" in resp.headers.get("cache-control", "").lower()


def test_panel_html_ships_bar_styles():
    """panel.html must carry the .usage-bar / .elapsed-marker styles so the
    reused bars render identically without the full dashboard stylesheet."""
    html = (WEB_DIR / "panel.html").read_text(encoding="utf-8")
    assert ".usage-bar" in html
    assert ".elapsed-marker" in html
    assert ".fill.red" in html and ".fill.yellow" in html and ".fill.green" in html


def test_component_files_exist_on_disk():
    assert (WEB_DIR / "panel.html").is_file()
    assert (WEB_DIR / "js" / "components" / "panel.js").is_file()
    assert (WEB_DIR / "js" / "util" / "account-grouping.js").is_file()

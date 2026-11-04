"""The SPA catch-all route serves files only from the web folder.

``GET /{full_path:path}`` joins the URL path to ``jacked/data/web``. The
route resolves the result and refuses it unless it stays in the web folder
(``jacked/api/main.py``, ``serve_spa``). These tests send the escapes that a
remote client can send: encoded ``..`` segments, an encoded absolute path,
and a backslash form. None of them may return a file from outside the web
folder. CodeQL does not model ``Path.resolve`` with ``relative_to`` as a
guard, so these tests are the proof that the guard holds.

Uses TestClient without the lifespan, as test_panel_route.py does.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jacked.api.main import app

REPO = Path(__file__).resolve().parents[2]
MARKER = "[project]"  # the first table in pyproject.toml, outside the web folder

client = TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "url",
    [
        "/%2e%2e/%2e%2e/%2e%2e/pyproject.toml",
        "/css/%2e%2e/%2e%2e/%2e%2e/%2e%2e/pyproject.toml",
        "/%2F" + str(REPO / "pyproject.toml").lstrip("/").replace("/", "%2F"),
        "/..%5c..%5c..%5cpyproject.toml",
    ],
    ids=["dot-dot", "dot-dot-under-a-mount", "absolute", "backslash"],
)
def test_an_escape_from_the_web_folder_returns_no_outside_file(url):
    assert MARKER in (REPO / "pyproject.toml").read_text(encoding="utf-8")

    resp = client.get(url)

    assert MARKER not in resp.text
    assert resp.status_code in (400, 404) or "<html" in resp.text.lower()


def test_a_dot_dot_escape_gets_the_invalid_path_answer():
    resp = client.get("/%2e%2e/%2e%2e/%2e%2e/pyproject.toml")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"


def test_a_file_in_the_web_folder_is_served():
    resp = client.get("/panel.html")

    assert resp.status_code == 200
    assert 'id="panel-root"' in resp.text

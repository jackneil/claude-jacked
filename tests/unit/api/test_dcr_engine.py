"""Tests for the DCR review-engine routes — GET/PUT /api/dcr-engine.

``JACKED_HOME`` is redirected to tmp_path in every test (the routes resolve home
through ``dcr_settings.jacked_home()``), so the real ~/.claude is never touched,
and ``codex_preflight`` is patched, so no test spawns the real codex CLI.

The load-bearing cases: a corrupt jacked-dcr.json makes GET degrade to Claude
with a reason (never a 500) while PUT REFUSES with a 503 rather than clobbering a
config file it could not parse.
"""
import json
import os
import typing

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked import dcr_settings
from jacked.api.routes.features import DcrEngineRequest, router
from tests._platform import requires_posix_dir_permissions

RESOLVE_KEYS = {
    "engine", "model", "effort", "keep_on_claude", "usable", "reason",
    "codex_installed", "codex_logged_in", "codex_path", "codex_version", "schema_path",
}

READY = {
    "codex_installed": True, "codex_logged_in": True,
    "codex_path": "/usr/bin/codex", "reason": None,
}
NOT_SIGNED_IN = {
    "codex_installed": True,
    "codex_logged_in": False,
    "codex_path": "/usr/bin/codex",
    "reason": "Codex CLI is not signed in. Run: codex login",
}

# chmod-based refusals are meaningless as root.
skip_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores chmod"
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as c:
        yield c


def _corrupt(home):
    path = dcr_settings.config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# GET
# --------------------------------------------------------------------------- #

def test_get_returns_the_contract_on_a_fresh_home(client, home):
    r = client.get("/api/dcr-engine")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == RESOLVE_KEYS
    assert body["engine"] == "claude"
    assert body["model"] == "gpt-6-astra"
    assert body["effort"] == "xhigh"
    assert body["keep_on_claude"] == ["Security", "Frontend Design"]
    assert body["usable"] is True
    assert body["reason"] is None
    assert body["schema_path"] == str(dcr_settings.schema_path())


def test_get_reports_codex_preflight(client, home):
    dcr_settings.write_config(home, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight", return_value=NOT_SIGNED_IN):
        body = client.get("/api/dcr-engine").json()
    assert body["engine"] == "codex"
    assert body["usable"] is False
    assert body["reason"] == NOT_SIGNED_IN["reason"]
    assert body["codex_installed"] is True
    assert body["codex_logged_in"] is False


def test_get_on_a_corrupt_config_degrades_to_claude(client, home):
    path = _corrupt(home)
    r = client.get("/api/dcr-engine")
    assert r.status_code == 200, "a corrupt config must not 500 the dashboard"
    body = r.json()
    assert body["engine"] == "claude"
    assert body["usable"] is True
    assert "is not valid JSON" in body["reason"]
    assert path.read_text(encoding="utf-8") == "{ not valid json"


@pytest.mark.parametrize("stored, field, expected", [
    # A hand-edited or foreign-written file must never 500 the dashboard, and a
    # non-list keep_on_claude used to raise TypeError inside resolve().
    ({"keep_on_claude": 5}, "keep_on_claude", ["Security", "Frontend Design"]),
    ({"keep_on_claude": "Security, Frontend Design"}, "keep_on_claude",
     ["Security", "Frontend Design"]),
    ({"model": 'gpt"; touch /tmp/x; "'}, "model", "gpt-6-astra"),
    ({"effort": "turbo"}, "effort", "xhigh"),
    ({"engine": "gemini"}, "engine", "claude"),
])
def test_get_sanitizes_a_hand_edited_config_instead_of_500ing(
    client, home, stored, field, expected,
):
    path = dcr_settings.config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**dcr_settings.DEFAULTS, **stored}), encoding="utf-8")

    r = client.get("/api/dcr-engine")

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == RESOLVE_KEYS
    assert body[field] == expected
    assert field in body["reason"]


# --------------------------------------------------------------------------- #
# PUT
# --------------------------------------------------------------------------- #

def test_put_writes_and_returns_fresh_state(client, home):
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        r = client.put("/api/dcr-engine", json={
            "engine": "codex", "model": "gpt-6-astra", "effort": "high",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == RESOLVE_KEYS
    assert body["engine"] == "codex"
    assert body["effort"] == "high"
    assert body["usable"] is True
    on_disk = json.loads(dcr_settings.config_path(home).read_text(encoding="utf-8"))
    assert on_disk["engine"] == "codex"
    assert on_disk["effort"] == "high"
    assert on_disk["version"] == 1


def test_put_merges_and_never_touches_keep_on_claude(client, home):
    """keep_on_claude is CLI-only: a PUT must return it but never change it."""
    dcr_settings.write_config(home, {
        **dcr_settings.DEFAULTS, "keep_on_claude": ["Security"], "effort": "low",
    })
    with patch("jacked.dcr_settings.codex_preflight", return_value=READY):
        body = client.put("/api/dcr-engine", json={"engine": "codex"}).json()
    assert body["keep_on_claude"] == ["Security"]
    assert body["effort"] == "low", "an omitted field must keep its stored value"
    assert dcr_settings.read_config(home)["keep_on_claude"] == ["Security"]


def test_put_back_to_claude_runs_no_preflight(client, home):
    dcr_settings.write_config(home, {**dcr_settings.DEFAULTS, "engine": "codex"})
    with patch("jacked.dcr_settings.codex_preflight") as preflight:
        body = client.put("/api/dcr-engine", json={"engine": "claude"}).json()
    preflight.assert_not_called()
    assert body["engine"] == "claude"
    assert body["usable"] is True
    assert body["codex_installed"] is None


def test_put_saves_codex_even_when_it_is_not_usable(client, home):
    with patch("jacked.dcr_settings.codex_preflight", return_value=NOT_SIGNED_IN):
        r = client.put("/api/dcr-engine", json={"engine": "codex"})
    assert r.status_code == 200
    assert r.json()["usable"] is False
    assert dcr_settings.read_config(home)["engine"] == "codex"


def test_put_invalid_effort_is_422(client, home):
    r = client.put("/api/dcr-engine", json={"engine": "codex", "effort": "turbo"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_VALUE"
    assert "turbo" in r.json()["error"]["message"]
    assert not dcr_settings.config_path(home).exists()


def test_put_invalid_engine_is_422(client, home):
    """The Literal on the body rejects an unknown engine before we touch disk."""
    r = client.put("/api/dcr-engine", json={"engine": "gemini"})
    assert r.status_code == 422
    assert not dcr_settings.config_path(home).exists()


def test_put_empty_model_is_422(client, home):
    r = client.put("/api/dcr-engine", json={"engine": "codex", "model": "   "})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_VALUE"
    assert not dcr_settings.config_path(home).exists()


def test_put_on_a_corrupt_config_is_503_and_leaves_the_file_untouched(client, home):
    path = _corrupt(home)
    r = client.put("/api/dcr-engine", json={"engine": "codex"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "DCR_SETTINGS_UNREADABLE"
    assert path.read_text(encoding="utf-8") == "{ not valid json"


def test_put_switches_engine_even_with_a_stale_invalid_stored_effort(client, home):
    """The lockout regression: a hand-edited effort typo must not block the
    dashboard from switching the engine (or from switching back to Claude)."""
    path = dcr_settings.config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**dcr_settings.DEFAULTS, "engine": "codex", "effort": "turbo"}),
        encoding="utf-8",
    )

    r = client.put("/api/dcr-engine", json={"engine": "claude"})

    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "claude"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["engine"] == "claude"
    assert on_disk["effort"] == dcr_settings.DEFAULTS["effort"], "the typo self-heals"


@skip_as_root
@requires_posix_dir_permissions
def test_put_on_an_unwritable_home_is_503_unwritable(client, home):
    """A filesystem refusal is a 503 the dashboard can explain, never a 500 with
    a PermissionError traceback."""
    home.chmod(0o500)
    try:
        r = client.put("/api/dcr-engine", json={"engine": "claude"})
    finally:
        home.chmod(0o700)

    assert r.status_code == 503, r.text
    error = r.json()["error"]
    assert error["code"] == "DCR_SETTINGS_UNWRITABLE"
    assert str(dcr_settings.config_path(home)) in error["message"]
    assert "permissions" in error["message"]


def test_put_uses_the_shared_update_config(client, home):
    """Both surfaces go through one read-modify-write, so they cannot drift on
    merge semantics (and both get the cross-process lock)."""
    with patch("jacked.dcr_settings.update_config",
               wraps=dcr_settings.update_config) as update:
        r = client.put("/api/dcr-engine", json={"engine": "claude", "effort": "low"})
    assert r.status_code == 200, r.text
    assert update.call_count == 1
    assert update.call_args.kwargs == {
        "engine": "claude", "model": None, "effort": "low",
    }


def test_dcr_lock_is_separate_from_the_settings_lock():
    """A DCR write must not serialize behind (or race) a settings.json write."""
    from jacked.api.routes import features

    assert features._dcr_lock is not features._settings_lock


# --------------------------------------------------------------------------- #
# Cross-language parity: the request Literal is a third copy of the engine list
# --------------------------------------------------------------------------- #

def test_the_request_literal_matches_the_engine_choices():
    """A drifted Literal either rejects an engine the CLI accepts or accepts one
    the writer then refuses with a 422."""
    engine_field = DcrEngineRequest.model_fields["engine"].annotation
    assert typing.get_args(engine_field) == dcr_settings.ENGINE_CHOICES

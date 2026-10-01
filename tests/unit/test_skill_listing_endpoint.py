"""Tests for GET/PUT /api/skill-listing.

``JACKED_HOME`` points every request at a throwaway home under ``tmp_path``
(the route resolves home per request through ``packs.jacked_home``), so the
real ``~/.claude/settings.json`` is never read or written. Behavior tests use a
minimal app with only this router; the registration and CSRF tests use the
real app without entering its lifespan (same pattern as
tests/unit/api/test_network_security.py).
"""
import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked import skill_listing as sl
from jacked.api.routes.skill_listing import router


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    monkeypatch.delenv(sl.ENV_BUDGET_VAR, raising=False)
    return tmp_path


@pytest.fixture
def client(home):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    with TestClient(app) as c:
        yield c


def _skill(home, name, description):
    d = home / ".claude" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {json.dumps(description)}\n---\nBody\n",
        encoding="utf-8",
    )


def _over_budget(home, n=80, desc_len=600):
    for i in range(n):
        _skill(home, f"skill-{i:03d}", "d" * desc_len)


def _settings(home):
    path = home / ".claude" / "settings.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_get_report_shape(client, home):
    _over_budget(home)
    resp = client.get("/api/skill-listing")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("summary", "budget_chars", "full_chars", "dropped_count", "dropped_groups",
                "recommendation", "strict_yaml_warnings", "windows", "settings"):
        assert key in body
    assert body["context_window"] == 1_000_000
    assert body["dropped_count"] > 0
    assert body["recommendation"]["max_desc_chars"] >= 150


def test_get_window_parameter(client, home):
    _skill(home, "a", "Short.")
    assert client.get("/api/skill-listing?window=200000").json()["context_window"] == 200_000
    assert client.get("/api/skill-listing?window=5").status_code == 422


def test_get_does_not_write_settings(client, home):
    _over_budget(home)
    client.get("/api/skill-listing")
    assert _settings(home) is None


def test_put_recommended_writes_and_returns_fresh_report(client, home):
    _over_budget(home)
    (home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}))
    rec = client.get("/api/skill-listing").json()["recommendation"]
    resp = client.put("/api/skill-listing", json={"action": "recommended"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == {
        "skillListingMaxDescChars": rec["max_desc_chars"],
        "skillListingBudgetFraction": rec["budget_fraction"],
    }
    assert body["fits"] is True and body["dropped_count"] == 0
    assert body["message"].startswith("Saved.")
    assert _settings(home) == {"model": "opus", **body["applied"]}


def test_put_recommended_when_it_already_fits(client, home):
    _skill(home, "a", "Short.")
    body = client.put("/api/skill-listing", json={"action": "recommended"}).json()
    assert body["applied"] is None
    assert "Nothing changed" in body["message"]
    assert _settings(home) is None


def test_put_explicit(client, home):
    resp = client.put("/api/skill-listing",
                      json={"action": "explicit", "max_desc_chars": 400, "budget_fraction": 0.015})
    assert resp.status_code == 200
    assert _settings(home) == {"skillListingMaxDescChars": 400, "skillListingBudgetFraction": 0.015}
    assert resp.json()["settings"]["max_desc_chars"] == 400


@pytest.mark.parametrize("payload", [
    {"action": "explicit", "max_desc_chars": 50, "budget_fraction": 0.02},
    {"action": "explicit", "max_desc_chars": 250, "budget_fraction": 1.5},
    {"action": "explicit", "max_desc_chars": 250},
    {"action": "recommended", "context_window": 10},
])
def test_put_invalid_values_are_422_and_write_nothing(client, home, payload):
    resp = client.put("/api/skill-listing", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_VALUE"
    assert _settings(home) is None


def test_put_unknown_action_is_rejected(client, home):
    assert client.put("/api/skill-listing", json={"action": "nuke"}).status_code == 422


def test_put_reset_removes_keys(client, home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"skillListingMaxDescChars": 250,
                                "skillListingBudgetFraction": 0.02, "env": {"A": "1"}}))
    body = client.put("/api/skill-listing", json={"action": "reset"}).json()
    assert body["applied"] is None and "defaults" in body["message"]
    assert _settings(home) == {"env": {"A": "1"}}
    assert body["settings"]["max_desc_chars_set"] is False


@pytest.mark.parametrize("payload", [
    {"action": "recommended"},
    {"action": "explicit", "max_desc_chars": 250, "budget_fraction": 0.02},
    {"action": "reset"},
])
def test_put_refuses_unreadable_settings(client, home, payload):
    _over_budget(home)
    path = home / ".claude" / "settings.json"
    path.write_text("{corrupt", encoding="utf-8")
    resp = client.put("/api/skill-listing", json=payload)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "SETTINGS_UNREADABLE"
    assert path.read_text() == "{corrupt"


def test_get_with_unreadable_settings_still_reports(client, home):
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{corrupt", encoding="utf-8")
    body = client.get("/api/skill-listing").json()
    assert body["settings"]["settings_unreadable"] is True


def test_route_is_registered_on_real_app_and_csrf_guarded(home, monkeypatch):
    """The real app serves the route, and a foreign Origin on PUT is refused
    by the process-wide HostValidationMiddleware before the handler runs."""
    from jacked.api.main import app
    from jacked.api.security import build_allowed_origins

    sentinel = object()
    prev = getattr(app.state, "allowed_origins", sentinel)
    app.state.allowed_origins = build_allowed_origins("127.0.0.1", 8321)
    try:
        c = TestClient(app)
        assert c.get("/api/skill-listing").status_code == 200
        resp = c.put("/api/skill-listing", json={"action": "reset"},
                     headers={"origin": "http://evil.example.com"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "CSRF_ORIGIN"
        c.close()
    finally:
        if prev is sentinel:
            app.state._state.pop("allowed_origins", None)
        else:
            app.state.allowed_origins = prev


def test_get_and_put_follow_the_configured_model_window(client, home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": "claude-sonnet-4-6"}))
    body = client.get("/api/skill-listing").json()
    assert body["context_window"] == 200_000 and body["derived_window"] == 200_000
    assert body["window_source"] == "model"
    body = client.put("/api/skill-listing", json={"action": "reset"}).json()
    assert body["context_window"] == 200_000
    body = client.put("/api/skill-listing", json={"action": "reset", "context_window": 1_000_000}).json()
    assert body["context_window"] == 1_000_000 and body["window_source"] == "selected"

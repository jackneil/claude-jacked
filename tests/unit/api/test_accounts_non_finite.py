"""GET /api/auth/accounts with a model limit that is not a finite number (#231i).

The cached payload holds the provider's own text, ``1e999`` included (Python
reads it as infinity). The account list answers 200 in strict JSON. It omits a
model whose percent is not a finite number, shows no reset for a model whose
reset is not a timestamp, and keeps the 5-hour and weekly windows.
"""

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes import auth as routes_auth
from jacked.web.database import Database

GOOD_RESET = '"2026-11-08T00:00:00+00:00"'


def _raw_text(fable_percent: str, fable_reset: str) -> str:
    return (
        '{"five_hour": {"utilization": 12.0, "resets_at": "2026-11-02T22:00:00+00:00"}, '
        '"seven_day": {"utilization": 34.0, "resets_at": "2026-11-08T00:00:00+00:00"}, '
        '"limits": [{"kind": "session", "percent": 12}, {"kind": "weekly_all", "percent": 34}, '
        '{"kind": "weekly_scoped", "percent": ' + fable_percent + ', "is_active": true, '
        '"resets_at": ' + fable_reset + ', "scope": {"model": {"display_name": "Fable"}}}, '
        '{"kind": "weekly_scoped", "percent": 20, "is_active": false, '
        '"resets_at": ' + GOOD_RESET + ', "scope": {"model": {"display_name": "Opus"}}}]}'
    )


def _strict_loads(text):
    """``json.loads`` that fails on NaN, Infinity and -Infinity, like a browser."""

    def _refuse(token):
        raise AssertionError(f"the response holds the token {token}")

    return json.loads(text, parse_constant=_refuse)


@pytest.fixture
def list_usage(tmp_path, monkeypatch):
    """Seed one Max 5x account with *raw_text*; return its ``usage`` object
    from GET /api/auth/accounts."""
    db = Database(str(tmp_path / "jacked.db"))
    # No active account: the route must not reconcile credentials.
    monkeypatch.setattr(routes_auth, "_get_active_account_id_cached", lambda: None)
    app = FastAPI()
    app.include_router(routes_auth.router, prefix="/api/auth")
    app.state.db = db

    def _list(raw_text: str) -> dict:
        acct = db.create_account(
            "u@x.com", "tok", int(time.time()) + 3600,
            subscription_type="max", rate_limit_tier="default_claude_max_5x",
        )
        db.update_account_usage_cache(
            acct["id"], five_hour=12.0, seven_day=34.0,
            five_hour_resets_at="2026-11-02T22:00:00+00:00",
            seven_day_resets_at="2026-11-08T00:00:00+00:00",
        )
        # The provider's text as sent: json.dumps cannot write 1e999.
        with db._writer() as conn:
            conn.execute(
                "UPDATE accounts SET cached_usage_raw = ? WHERE id = ?",
                (raw_text, acct["id"]),
            )
        resp = TestClient(app).get("/api/auth/accounts")
        assert resp.status_code == 200
        [account] = _strict_loads(resp.text)
        usage = account["usage"]
        assert (usage["five_hour"], usage["seven_day"]) == (12.0, 34.0)
        return usage

    yield _list
    db.close()


def test_a_non_finite_model_percent_is_omitted(list_usage):
    usage = list_usage(_raw_text("1e999", GOOD_RESET))
    assert set(usage["per_model"]) == {"opus"}
    assert usage["per_model"]["opus"]["utilization"] == 20.0
    assert usage["binding_model"]["label"] == "Opus"


def test_a_non_finite_model_reset_is_no_reset(list_usage):
    usage = list_usage(_raw_text("50", "1e999"))
    assert usage["per_model"]["fable"]["utilization"] == 50.0
    assert usage["per_model"]["fable"]["resets_at"] is None
    assert usage["binding_model"]["label"] == "Fable"

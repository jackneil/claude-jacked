"""Raw exception text never reaches a client (#289i, CodeQL py/stack-trace-exposure).

An API response, a WebSocket event, the OAuth callback page and a stored
``last_error`` can reach a browser on another machine when remote access is
on. The rule under test (``jacked.public_errors``):

- an exception whose text is written for the user is a ``PublicError``, and
  its text reaches the client as it is;
- every other exception reaches the client only as a fixed message, and the
  server log names its type (and errno name), never its text.

Each test raises an exception whose text carries a marker path, then proves
the marker is absent from what the client receives.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.requests import Request

from jacked.public_errors import (
    PublicError,
    describe,
    os_error_reason,
    public_message,
)

MARKER = "/Users/someone/.secret-marker"


def _raw() -> RuntimeError:
    return RuntimeError(f"internal detail at {MARKER}")


# ---------------------------------------------------------------------------
# The shared helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_public_message_passes_only_public_text(self):
        assert public_message(PublicError("Run codex login."), "Failed.") == "Run codex login."
        assert public_message(_raw(), "Failed.") == "Failed."

    def test_describe_names_the_type_and_errno_only(self):
        exc = PermissionError(errno.EACCES, "Permission denied", MARKER)
        assert describe(exc) == "PermissionError(EACCES)"
        assert describe(_raw()) == "RuntimeError"
        assert MARKER not in describe(exc)

    def test_os_error_reason_has_no_path(self):
        exc = PermissionError(errno.EACCES, "Permission denied", MARKER)
        assert os_error_reason(exc) == "Permission denied"
        assert os_error_reason(OSError("no errno here")) == "a file system error"

    @pytest.mark.parametrize(
        "cls_path",
        [
            "jacked.codex.accounts.CodexImportError",
            "jacked.codex.swap_guards.CodexSwapError",
            "jacked.codex.usage.CodexUsageError",
            "jacked.dcr_settings.DcrSettingsAccessError",
            "jacked.dcr_settings.DcrSettingsValueError",
            "jacked.skill_listing.ListingSettingsError",
            "jacked.web.oauth.OAuthIdentityError",
            "jacked.api.routes.system.UpgradeStepError",
        ],
    )
    def test_every_authored_refusal_is_a_public_error(self, cls_path):
        import importlib

        module, _, name = cls_path.rpartition(".")
        cls = getattr(importlib.import_module(module), name)
        assert issubclass(cls, PublicError)

    def test_value_error_subclasses_stay_value_errors(self):
        """Callers that catch ValueError keep catching them."""
        from jacked.dcr_settings import DcrSettingsValueError
        from jacked.skill_listing import ListingSettingsError
        from jacked.web.oauth import OAuthIdentityError

        for cls in (DcrSettingsValueError, ListingSettingsError, OAuthIdentityError):
            assert issubclass(cls, ValueError)


# ---------------------------------------------------------------------------
# DCR settings: the write refusal names the file, not the OSError text
# ---------------------------------------------------------------------------


def test_dcr_access_error_has_no_raw_oserror_text(tmp_path, monkeypatch):
    from jacked import dcr_settings

    def refuse(path, text):
        raise PermissionError(errno.EACCES, "Permission denied", MARKER)

    monkeypatch.setattr(dcr_settings, "atomic_write_text", refuse)
    with pytest.raises(dcr_settings.DcrSettingsAccessError) as info:
        dcr_settings.write_config(tmp_path, dict(dcr_settings.DEFAULTS))

    message = info.value.public_message
    assert MARKER not in message
    assert "[Errno" not in message
    assert "Permission denied" in message
    assert str(dcr_settings.config_path(tmp_path)) in message
    assert "permissions" in message


def test_dcr_validation_error_is_public(tmp_path):
    from jacked import dcr_settings

    bad = dict(dcr_settings.DEFAULTS, engine="nope")
    with pytest.raises(dcr_settings.DcrSettingsValueError) as info:
        dcr_settings.write_config(tmp_path, bad)
    assert "engine must be one of" in info.value.public_message


def test_dcr_route_hides_a_non_public_value_error(tmp_path, monkeypatch):
    """A ValueError that is not a DcrSettingsValueError gets the fixed text."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from jacked.api.routes import features

    monkeypatch.setenv("JACKED_HOME", str(tmp_path))

    def boom(*args, **kwargs):
        raise ValueError(f"unexpected {MARKER}")

    monkeypatch.setattr(features, "_apply_dcr_engine", boom)
    app = FastAPI()
    app.include_router(features.router, prefix="/api")
    resp = TestClient(app).put("/api/dcr-engine", json={"engine": "claude"})

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "INVALID_VALUE"
    assert MARKER not in error["message"]


# ---------------------------------------------------------------------------
# Bulk usage refresh: an unexpected error in one account
# ---------------------------------------------------------------------------


def test_bulk_refresh_internal_error_is_fixed_text(monkeypatch, tmp_path, caplog):
    from pathlib import Path

    from jacked.api.routes import auth as routes_auth

    routes_auth.reset_locks()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    inner = MagicMock()
    inner.list_accounts.return_value = [{"id": 1, "email": "a@x"}]
    inner.get_account.return_value = {
        "id": 1, "email": "a@x", "expires_at": int(time.time()) + 3600,
    }
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: inner)
    monkeypatch.setattr(routes_auth, "_get_active_account_id_cached", lambda: None)

    request = MagicMock()
    request.app.state.ws_registry = None

    async def explode(*args, **kwargs):
        raise _raw()

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=explode):
            return await routes_auth.refresh_all_usage(request)

    try:
        with caplog.at_level(logging.ERROR):
            resp = asyncio.run(_run())
    finally:
        routes_auth.reset_locks()

    row = resp.results[0]
    assert row["success"] is False
    assert MARKER not in row["error"]
    assert "jacked log" in row["error"]


# ---------------------------------------------------------------------------
# The app-wide ValueError / FileNotFoundError handlers
# ---------------------------------------------------------------------------


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/x", "headers": []})


def test_global_value_error_handler_hides_text(caplog):
    from jacked.api import main as api_main

    with caplog.at_level(logging.WARNING, logger="jacked.api"):
        resp = asyncio.run(api_main.value_error_handler(_request(), ValueError(MARKER)))
    body = resp.body.decode()
    assert resp.status_code == 400
    assert "VALIDATION_ERROR" in body
    assert MARKER not in body
    assert MARKER not in caplog.text
    assert "ValueError" in caplog.text


def test_global_value_error_handler_keeps_public_text():
    from jacked.api import main as api_main

    class _Refusal(PublicError, ValueError):
        pass

    resp = asyncio.run(
        api_main.value_error_handler(_request(), _Refusal("Pick a smaller value."))
    )
    assert "Pick a smaller value." in resp.body.decode()


def test_global_not_found_handler_hides_the_path(caplog):
    from jacked.api import main as api_main

    exc = FileNotFoundError(errno.ENOENT, "No such file or directory", MARKER)
    with caplog.at_level(logging.WARNING, logger="jacked.api"):
        resp = asyncio.run(api_main.not_found_handler(_request(), exc))
    body = resp.body.decode()
    assert resp.status_code == 404
    assert "NOT_FOUND" in body
    assert MARKER not in body
    assert MARKER not in caplog.text
    assert "FileNotFoundError(ENOENT)" in caplog.text


# ---------------------------------------------------------------------------
# OAuth flow: the callback page and the flow status
# ---------------------------------------------------------------------------


def _flow(tmp_path):
    from jacked.web.database import Database
    from jacked.web.oauth import OAuthFlow

    db = Database(str(tmp_path / "oauth.db"))
    flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)
    flow._state = "test-state"
    return flow


def _callback(flow):
    req = MagicMock()
    req.query = {"code": "valid-code", "state": "test-state"}
    return asyncio.run(flow._handle_callback(req))


class TestOAuthFlowErrorText:
    def test_callback_hides_an_unexpected_exception(self, tmp_path):
        flow = _flow(tmp_path)
        flow._complete_auth = AsyncMock(side_effect=_raw())
        resp = _callback(flow)
        assert "Authorization Failed" in resp.text
        assert MARKER not in resp.text
        assert MARKER not in (flow.get_status().get("error") or "")
        assert "jacked log" in flow.get_status()["error"]

    def test_callback_shows_an_identity_refusal(self, tmp_path):
        from jacked.web.oauth import OAuthIdentityError

        flow = _flow(tmp_path)
        flow._complete_auth = AsyncMock(
            side_effect=OAuthIdentityError("CC auth email (b@x) does not match target account (a@x)")
        )
        resp = _callback(flow)
        assert "does not match target account" in resp.text

    def test_callback_names_the_http_status_only(self, tmp_path):
        flow = _flow(tmp_path)
        request = httpx.Request("POST", "https://platform.claude.com/v1/oauth/token")
        response = httpx.Response(400, request=request, text=MARKER)
        exc = httpx.HTTPStatusError(MARKER, request=request, response=response)
        flow._complete_auth = AsyncMock(side_effect=exc)
        resp = _callback(flow)
        assert "HTTP 400" in resp.text
        assert MARKER not in resp.text

    def test_callback_reports_a_network_failure_plainly(self, tmp_path):
        flow = _flow(tmp_path)
        flow._complete_auth = AsyncMock(side_effect=httpx.ConnectError(MARKER))
        resp = _callback(flow)
        assert "could not reach the sign-in server" in resp.text
        assert MARKER not in resp.text

    def test_submitted_code_hides_an_unexpected_exception(self, tmp_path):
        flow = _flow(tmp_path)
        flow._complete_auth = AsyncMock(side_effect=_raw())
        status = asyncio.run(flow.submit_code("abc#test-state"))
        assert status["status"] == "error"
        assert MARKER not in status["error"]

    def test_store_cc_tokens_raises_identity_errors(self, tmp_path):
        """The re-auth identity refusals are written for the user."""
        from jacked.web.oauth import OAuthIdentityError

        flow = _flow(tmp_path)
        with pytest.raises(OAuthIdentityError):
            flow._store_cc_tokens({"access_token": "x", "email": "a@x"}, {})


# ---------------------------------------------------------------------------
# Account validation and usage fetch: the stored last_error
# ---------------------------------------------------------------------------


def _client_raising(exc):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=exc)
    client.post = AsyncMock(side_effect=exc)
    return client


@pytest.fixture
def _auth_isolated():
    from jacked.web import auth

    auth.reset_locks()
    auth._account_usage_state.clear()
    with patch("jacked.web.auth._network_reachable", AsyncMock(return_value=True)), \
         patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store",
               return_value=False):
        yield auth
    auth._account_usage_state.clear()
    auth.reset_locks()


def _account(tmp_path):
    from jacked.web.database import Database

    db = Database(str(tmp_path / "acct.db"))
    acct = db.create_account("owner@test.com", "at-1", int(time.time()) + 3600,
                             refresh_token="rt-1")
    return db, acct["id"]


def test_validate_account_hides_an_unexpected_exception(tmp_path, _auth_isolated):
    auth = _auth_isolated
    db, account_id = _account(tmp_path)
    with patch("jacked.web.auth.httpx.AsyncClient", return_value=_client_raising(_raw())):
        verdict = asyncio.run(auth.validate_account(account_id, db))
    assert verdict["valid"] is False
    assert MARKER not in verdict["error"]
    assert MARKER not in (db.get_account(account_id).get("last_error") or "")


def test_fetch_usage_hides_an_unexpected_exception(tmp_path, _auth_isolated):
    auth = _auth_isolated
    db, account_id = _account(tmp_path)
    with patch("jacked.web.auth.httpx.AsyncClient", return_value=_client_raising(_raw())):
        result = asyncio.run(auth.fetch_usage(account_id, db, manual=True))
    assert result is None
    last_error = db.get_account(account_id).get("last_error") or ""
    assert last_error.startswith("Usage fetch error")
    assert MARKER not in last_error


# ---------------------------------------------------------------------------
# Codex usage poll: the app-server start failure
# ---------------------------------------------------------------------------


def test_codex_usage_start_failure_has_no_path(tmp_path, monkeypatch):
    from jacked.codex import usage

    async def refuse(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", MARKER)

    monkeypatch.setattr(usage.asyncio, "create_subprocess_exec", refuse)
    with pytest.raises(usage.CodexUsageError) as info:
        asyncio.run(usage.read_codex_rate_limits(home=tmp_path, env={}, codex_bin="/bin/codex"))
    assert MARKER not in info.value.public_message
    assert "PermissionError(EACCES)" in info.value.public_message


# ---------------------------------------------------------------------------
# Dashboard upgrade: the upgrade_failed WebSocket event
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self):
        self.events = []

    async def broadcast(self, event, payload):
        self.events.append((event, payload))


def _upgrade(monkeypatch, *, run_cmd):
    from jacked.api.routes import system

    monkeypatch.setattr(system, "find_bin", lambda name, *a, **k: f"/stub/bin/{name}")
    monkeypatch.setattr(system, "_upgrade_run_cmd", run_cmd)
    registry = _Registry()
    asyncio.run(system._run_upgrade_steps(registry))
    return registry.events[-1]


def test_upgrade_failure_hides_an_unexpected_exception(monkeypatch):
    async def explode(cmd):
        raise _raw()

    event, payload = _upgrade(monkeypatch, run_cmd=explode)
    assert event == "upgrade_failed"
    assert MARKER not in payload["error"]
    assert "upgrade step" in payload["error"]


def test_upgrade_failure_shows_the_step_error_text(monkeypatch):
    from jacked.api.routes import system

    async def installer_fails(cmd):
        raise system.UpgradeStepError("uv not found on PATH. Cannot upgrade.")

    event, payload = _upgrade(monkeypatch, run_cmd=installer_fails)
    assert event == "upgrade_failed"
    assert payload["error"] == "uv not found on PATH. Cannot upgrade."


# The installer is a real child that prints a marker path and exits 3.
_INSTALLER = f"import sys; print({MARKER!r}); sys.exit(3)"


def test_a_failed_installer_step_gives_fixed_text(caplog):
    """The upgrade_failed event reaches every WebSocket client, remote ones
    too, so the installer's output stays in the host's log file."""
    import sys
    from pathlib import Path

    from jacked.api.log_capture import _format_entry
    from jacked.api.upgrade_steps import UpgradeStepError, _upgrade_run_cmd

    with caplog.at_level(logging.WARNING, logger="jacked.api.upgrade_steps"), \
            pytest.raises(UpgradeStepError) as raised:
        asyncio.run(_upgrade_run_cmd([sys.executable, "-c", _INSTALLER]))

    name = Path(sys.executable).name
    assert raised.value.public_message == (
        f"The {name} step failed with exit code 3. See ~/.claude/jacked-server.log."
    )
    assert MARKER not in str(raised.value)
    records = [r for r in caplog.records if r.name == "jacked.api.upgrade_steps"]
    assert len(records) == 1
    assert MARKER in records[0].getMessage()  # the host's log file keeps it
    assert MARKER not in _format_entry(records[0])["msg"]  # the log viewer does not


def test_the_upgrade_failed_event_has_no_installer_output(monkeypatch):
    import sys

    from jacked.api.upgrade_steps import _upgrade_run_cmd

    async def installer(cmd):
        await _upgrade_run_cmd([sys.executable, "-c", _INSTALLER])

    event, payload = _upgrade(monkeypatch, run_cmd=installer)
    assert event == "upgrade_failed"
    assert MARKER not in payload["error"]
    assert "exit code 3" in payload["error"]
    assert "jacked-server.log" in payload["error"]
    assert "\u2014" not in payload["error"]


# ---------------------------------------------------------------------------
# The dashboard log viewer: a logger.exception record
# ---------------------------------------------------------------------------


def test_log_viewer_entry_has_no_traceback():
    """The log viewer reaches every dashboard client (WebSocket and /api/logs).

    A ``logger.exception`` record carries a traceback, and the traceback
    carries the exception text. The viewer entry keeps the message and names
    the exception type; the full traceback stays in the host's log file.
    """
    import sys

    from jacked.api.log_capture import _format_entry

    try:
        raise _raw()
    except RuntimeError:
        record = logging.LogRecord(
            "jacked.test", logging.ERROR, __file__, 1,
            "Bulk refresh: unexpected error for account %d", (7,), sys.exc_info(),
        )
    entry = _format_entry(record)
    assert entry["msg"] == "Bulk refresh: unexpected error for account 7 [RuntimeError]"
    assert MARKER not in entry["msg"]
    assert "Traceback" not in entry["msg"]

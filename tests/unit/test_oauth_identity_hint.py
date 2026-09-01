"""Re-auth must tell claude.ai which account it is re-authing.

Without a hint, the authorize page shows whoever the browser is already signed
in as, so a user re-authing account B gets account A's consent screen and has
to log out, remember which account the card was for, and start over. The
authorize endpoint accepts ``login_hint`` (pre-fills the email box and forces
account selection) and ``orgUUID`` (picks the org when one email belongs to
several), so every flow with a known target sends both.

The same identity travels back to the dashboard in the start/status payloads,
which is how the waiting banner can say whose account it is waiting on.

Manual flows throughout: manual=True binds no socket and opens no browser, so
these tests touch neither. Background tasks are drained the same way
tests/unit/test_oauth_manual_flow.py does it.
"""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from jacked.web import browser_launch
from jacked.web import oauth as oauth_mod
from jacked.web.database import Database
from jacked.web.oauth import OAuthFlow, identity_hint_params


@pytest.fixture(autouse=True)
def _clear_active_flows():
    oauth_mod.reset_locks()
    yield
    oauth_mod.reset_locks()


async def _drain_background_tasks():
    """Cancel the tasks start() spawned, including their 30s cleanup sleep."""
    current = asyncio.current_task()
    for _ in range(10):
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.sleep(0)


def run_async(body):
    async def _main():
        try:
            return await body()
        finally:
            await _drain_background_tasks()

    return asyncio.run(_main())


def _make_db(email="jack@example.com", org_uuid="org-abc", org_name="Acme Inc"):
    db = Database(":memory:")
    account = db.create_account(
        email=email,
        access_token="sk-ant-old",
        expires_at=9999999999,
        organization_uuid=org_uuid,
        organization_name=org_name,
    )
    return db, account


def _start_flow(db, target_account_id):
    async def _body():
        flow = OAuthFlow(
            db, purpose="primary", target_account_id=target_account_id, manual=True
        )
        return flow, await flow.start()

    return run_async(_body)


def _query_of(auth_url):
    return parse_qs(urlparse(auth_url).query)


# ---------------------------------------------------------------------------
# identity_hint_params
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "account,expected",
    [
        (None, {}),
        ({}, {}),
        ({"email": "a@b.com"}, {"login_hint": "a@b.com"}),
        ({"email": "a@b.com", "organization_uuid": ""}, {"login_hint": "a@b.com"}),
        ({"email": "a@b.com", "organization_uuid": None}, {"login_hint": "a@b.com"}),
        (
            {"email": "a@b.com", "organization_uuid": "org-1"},
            {"login_hint": "a@b.com", "orgUUID": "org-1"},
        ),
        ({"email": "", "organization_uuid": "org-1"}, {"orgUUID": "org-1"}),
    ],
)
def test_identity_hint_params(account, expected):
    assert identity_hint_params(account) == expected


# ---------------------------------------------------------------------------
# The authorize URL a re-auth flow builds
# ---------------------------------------------------------------------------


def test_reauth_url_carries_the_login_hint_and_org():
    db, account = _make_db()
    flow, result = _start_flow(db, account["id"])

    query = _query_of(result["auth_url"])
    assert query["login_hint"] == ["jack@example.com"]
    assert query["orgUUID"] == ["org-abc"]
    # The hints are additions, not replacements.
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [flow._state]


def test_reauth_start_and_status_report_who_is_being_authorized():
    db, account = _make_db()
    flow, result = _start_flow(db, account["id"])

    for payload in (result, flow.get_status()):
        assert payload["purpose"] == "primary"
        assert payload["target_account_id"] == account["id"]
        assert payload["target_email"] == "jack@example.com"
        assert payload["target_org_name"] == "Acme Inc"

    # Nothing the frontend already relies on was dropped.
    status = flow.get_status()
    assert status["flow_id"] == flow.flow_id
    assert status["mode"] == "manual"
    assert status["status"] == "pending"
    assert status["auth_url"] == result["auth_url"]


def test_a_flow_with_no_target_sends_no_hints():
    """Add Account has no identity yet; a stale hint would pre-fill the wrong
    email and force the user to clear it."""
    db = Database(":memory:")

    async def _body():
        flow = OAuthFlow(db, purpose="primary", manual=True)
        return flow, await flow.start()

    flow, result = run_async(_body)

    query = _query_of(result["auth_url"])
    assert "login_hint" not in query
    assert "orgUUID" not in query
    assert "target_email" not in result
    assert "target_account_id" not in result
    assert "target_email" not in flow.get_status()
    assert result["purpose"] == "primary"


def test_a_personal_account_sends_only_the_login_hint():
    """organization_uuid is "" for personal/legacy accounts, and an empty
    orgUUID param would select nothing."""
    db, account = _make_db(org_uuid="", org_name=None)
    flow, result = _start_flow(db, account["id"])

    query = _query_of(result["auth_url"])
    assert query["login_hint"] == ["jack@example.com"]
    assert "orgUUID" not in query
    assert flow.get_status()["target_org_name"] is None


def test_a_missing_target_account_does_not_break_the_flow():
    """The account could be deleted between the click and the start."""
    db = Database(":memory:")

    async def _body():
        flow = OAuthFlow(db, purpose="primary", target_account_id=999, manual=True)
        return flow, await flow.start()

    flow, result = run_async(_body)

    query = _query_of(result["auth_url"])
    assert "login_hint" not in query
    assert result["flow_id"] == flow.flow_id
    assert flow.get_status()["status"] == "pending"


def test_cc_flows_hint_the_same_account():
    """A CC flow re-authorizes an account jacked already knows, so it gets the
    same treatment as a re-auth."""
    db, account = _make_db()

    async def _body():
        flow = OAuthFlow(
            db,
            purpose="claude_code",
            target_account_id=account["id"],
            manual=True,
        )
        return flow, await flow.start()

    flow, result = run_async(_body)

    query = _query_of(result["auth_url"])
    assert query["login_hint"] == ["jack@example.com"]
    assert result["purpose"] == "claude_code"
    assert result["target_email"] == "jack@example.com"


# ---------------------------------------------------------------------------
# reopen_browser: getting the user back to the window jacked opened
# ---------------------------------------------------------------------------


def _pending_browser_flow(db, target_account_id=None):
    """A browser-mode flow parked where start() would leave it.

    start() is skipped deliberately: it binds a real socket in 45100-45199 and
    spawns a background task. reopen_browser only reads _auth_url and _status.
    """
    flow = OAuthFlow(
        db, purpose="primary", target_account_id=target_account_id, manual=False
    )
    flow._auth_url = "https://claude.com/cai/oauth/authorize?state=abc"
    flow._status = "pending"
    return flow


def test_reopen_relaunches_the_browser_and_reports_which_one(monkeypatch):
    db, account = _make_db()
    flow = _pending_browser_flow(db, account["id"])
    calls = []

    def _fake_open(url, acct, database):
        calls.append((url, acct, database))
        return browser_launch.LaunchResult("profile", "Chrome")

    monkeypatch.setattr(browser_launch, "open_auth_url", _fake_open)

    status = run_async(flow.reopen_browser)

    assert len(calls) == 1
    assert calls[0][0] == flow._auth_url
    assert calls[0][1]["email"] == "jack@example.com"
    assert "reopen_error" not in status
    assert status["status"] == "pending"
    assert status["browser_mode"] == "profile"
    assert status["browser_name"] == "Chrome"
    # The identity fields the banner already renders survive the reopen.
    assert status["target_email"] == "jack@example.com"


def test_reopen_reports_the_fallback_mode_it_actually_got(monkeypatch):
    """No installed browser means the system default opened, and the banner
    must stop claiming a dedicated window exists."""
    db, account = _make_db()
    flow = _pending_browser_flow(db, account["id"])
    flow._browser_mode = "profile"
    flow._browser_name = "Chrome"
    monkeypatch.setattr(
        browser_launch,
        "open_auth_url",
        lambda *a: browser_launch.LaunchResult("default", None),
    )

    status = run_async(flow.reopen_browser)

    assert status["browser_mode"] == "default"
    assert "browser_name" not in status


def test_reopen_refuses_a_manual_flow(monkeypatch):
    """A manual flow runs on someone else's machine; there is no window here
    to raise and nothing useful to launch."""
    db, account = _make_db()
    flow = OAuthFlow(db, target_account_id=account["id"], manual=True)
    flow._auth_url = "https://claude.com/cai/oauth/authorize?state=abc"
    calls = []
    monkeypatch.setattr(
        browser_launch, "open_auth_url", lambda *a: calls.append(a)
    )

    status = run_async(flow.reopen_browser)

    assert calls == []
    assert status["reopen_error"]
    assert status["status"] == "pending"


@pytest.mark.parametrize("state", ["completed", "error", "not_found"])
def test_reopen_refuses_a_finished_flow(monkeypatch, state):
    db, account = _make_db()
    flow = _pending_browser_flow(db, account["id"])
    flow._status = state
    calls = []
    monkeypatch.setattr(
        browser_launch, "open_auth_url", lambda *a: calls.append(a)
    )

    status = run_async(flow.reopen_browser)

    assert calls == []
    assert state in status["reopen_error"]


def test_reopen_refuses_a_flow_with_no_auth_url_yet(monkeypatch):
    db, account = _make_db()
    flow = _pending_browser_flow(db, account["id"])
    flow._auth_url = None
    calls = []
    monkeypatch.setattr(
        browser_launch, "open_auth_url", lambda *a: calls.append(a)
    )

    status = run_async(flow.reopen_browser)

    assert calls == []
    assert status["reopen_error"]


def test_reopen_survives_a_launcher_that_blows_up(monkeypatch):
    """A broken browser install must not turn a recoverable flow into an
    error the user has to restart from."""
    db, account = _make_db()
    flow = _pending_browser_flow(db, account["id"])

    def _boom(*a):
        raise OSError("chrome.exe is mid-upgrade")

    monkeypatch.setattr(browser_launch, "open_auth_url", _boom)

    status = run_async(flow.reopen_browser)

    assert status["reopen_error"]
    assert status["status"] == "pending"


def test_reopen_never_leaks_the_authorize_url_into_the_logs(monkeypatch, caplog):
    db, account = _make_db()
    flow = _pending_browser_flow(db, account["id"])
    monkeypatch.setattr(
        browser_launch,
        "open_auth_url",
        lambda *a: browser_launch.LaunchResult("profile", "Chrome"),
    )
    caplog.set_level("DEBUG")

    run_async(flow.reopen_browser)

    assert flow._auth_url not in caplog.text
    assert "state=abc" not in caplog.text

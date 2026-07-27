"""Tests for manual (remote-dashboard) OAuth code entry in jacked/web/oauth.py.

A dashboard opened from another machine can't use the localhost callback or
the server's own browser, so OAuthFlow(manual=True) binds no port, opens no
browser, points redirect_uri at Anthropic's code-display page, and waits for
the user to paste the code back through submit_code().

Uses asyncio.run() wrappers (project convention — no pytest-asyncio).
Two hazards shape the scaffolding here:

* start() spawns a background task per flow (_expire_manual_flow /
  _wait_for_callback). Both end in a 30s registry-cleanup sleep, so a test
  that just lets asyncio.run() tear down would block for 30 seconds on it —
  _drain_background_tasks() cancels through that sleep instead.
* Browser-mode start() binds a real socket in 45100-45199. The FakeWeb stub
  replaces aiohttp's ``web`` module so no test touches the network, and
  doubles as the assertion surface for "a manual flow binds nothing".
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from jacked.web import oauth as oauth_mod
from jacked.web.oauth import (
    AUTH_URL,
    BROWSER_TIMEOUT_SECONDS,
    MANUAL_REDIRECT_URI,
    MANUAL_TIMEOUT_SECONDS,
    OAuthFlow,
)


@pytest.fixture(autouse=True)
def _clear_active_flows():
    """Drop flows registered by a previous test (reset_locks is the module's
    own helper for exactly this)."""
    oauth_mod.reset_locks()
    yield
    oauth_mod.reset_locks()


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


class _FakeRouter:
    def __init__(self):
        self.routes = []

    def add_get(self, path, handler):
        self.routes.append((path, handler))


class _FakeApplication:
    def __init__(self):
        self.router = _FakeRouter()


class _FakeRunner:
    def __init__(self, app):
        self.app = app
        self.cleaned_up = False

    async def setup(self):
        pass

    async def cleanup(self):
        self.cleaned_up = True


class _FakeSite:
    def __init__(self, runner, host, port):
        self.runner = runner
        self.host = host
        self.port = port

    async def start(self):
        pass


class FakeWeb:
    """Stand-in for aiohttp's ``web`` module that records what start() built.

    Empty lists after a manual start() prove no callback server was created.
    """

    def __init__(self):
        self.applications = []
        self.runners = []
        self.sites = []

    def Application(self):
        app = _FakeApplication()
        self.applications.append(app)
        return app

    def AppRunner(self, app):
        runner = _FakeRunner(app)
        self.runners.append(runner)
        return runner

    def TCPSite(self, runner, host, port):
        site = _FakeSite(runner, host, port)
        self.sites.append(site)
        return site


@pytest.fixture
def fake_web(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(oauth_mod, "web", fake)
    return fake


async def _drain_background_tasks():
    """Cancel the tasks start() spawned, including their cleanup sleep.

    The first cancel unblocks the callback/expiry wait; the coroutine then
    lands in its ``finally`` and awaits a 30s sleep, which the next round
    cancels. Two rounds is the norm; the cap only bounds a pathological case.
    """
    current = asyncio.current_task()
    for _ in range(10):
        pending = [
            t for t in asyncio.all_tasks() if t is not current and not t.done()
        ]
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.sleep(0)


def run_async(body):
    """Run an async test body, then drain whatever start() left running."""

    async def _main():
        try:
            return await body()
        finally:
            await _drain_background_tasks()

    return asyncio.run(_main())


def _background_task_names():
    """Qualnames of the tasks start() spawned on this loop."""
    return [
        getattr(t.get_coro(), "__qualname__", "")
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
    ]


def _pending_flow(manual=True, state="flow-state-token"):
    """A flow in the state start() would leave it in, without running start().

    submit_code only needs ``_state``; skipping start() keeps the paste tests
    free of background tasks.
    """
    flow = OAuthFlow(MagicMock(), manual=manual)
    flow._state = state
    return flow


def _redirect_uri_of(auth_url):
    return parse_qs(urlparse(auth_url).query)["redirect_uri"][0]


# ---------------------------------------------------------------------------
# Manual start(): no port, no browser, code-display redirect
# ---------------------------------------------------------------------------


def test_manual_start_returns_manual_mode_and_code_redirect(fake_web):
    """The auth URL must carry Anthropic's code-display redirect — the only
    redirect that renders a copyable code instead of hitting localhost."""

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        return flow, await flow.start()

    flow, result = run_async(_body)

    assert result["mode"] == "manual"
    assert result["flow_id"] == flow.flow_id
    assert result["auth_url"].startswith(f"{AUTH_URL}?")
    assert _redirect_uri_of(result["auth_url"]) == MANUAL_REDIRECT_URI
    # Encoded exactly once — a raw "://" in the query would break the redirect.
    assert urlencode({"redirect_uri": MANUAL_REDIRECT_URI}) in result["auth_url"]
    assert flow._redirect_uri == MANUAL_REDIRECT_URI


def test_manual_start_registers_the_flow_for_polling(fake_web):
    """The dashboard polls by flow_id, so start() must register the flow."""

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        return flow, await flow.start()

    flow, result = run_async(_body)

    assert oauth_mod.get_flow(result["flow_id"]) is flow
    assert oauth_mod._active_flows[flow.flow_id] is flow


def test_manual_start_binds_no_callback_port(fake_web):
    """A remote user's browser can't reach our localhost:451xx, so a manual
    flow must not build a callback server at all."""

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        await flow.start()

    run_async(_body)

    assert fake_web.applications == []
    assert fake_web.runners == []
    assert fake_web.sites == []


def test_manual_start_does_not_open_a_browser(fake_web):
    """Opening a browser on the server is useless (and on a headless box,
    noise) when the user is on another machine."""

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        await flow.start()

    with patch("webbrowser.open") as opened:
        run_async(_body)

    opened.assert_not_called()


def test_manual_start_arms_the_expiry_task(fake_web):
    """No callback server means nothing else would ever expire the flow."""

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        await flow.start()
        return _background_task_names()

    names = run_async(_body)

    assert "OAuthFlow._expire_manual_flow" in names


def test_manual_flow_expiry_task_marks_the_flow_not_found(fake_web, monkeypatch):
    """The expiry task, not just a get_status() poll, must retire an
    abandoned flow — a flow nobody polls would otherwise sit pending
    forever."""
    monkeypatch.setattr(oauth_mod, "MANUAL_TIMEOUT_SECONDS", 0.01)

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        await flow.start()
        await asyncio.sleep(0.05)  # let the expiry task's wait_for lapse
        return flow

    flow = run_async(_body)

    # _status directly, not get_status(): only the task can have set it.
    assert flow._status == "not_found"
    assert "timed out" in flow._error


# ---------------------------------------------------------------------------
# Browser start(): a failed webbrowser.open must not kill the flow
# ---------------------------------------------------------------------------


def test_browser_start_opens_the_local_browser(fake_web):
    """Control for the manual case: browser mode still auto-opens."""

    async def _body():
        flow = OAuthFlow(MagicMock())
        return await flow.start()

    with patch("webbrowser.open") as opened:
        result = run_async(_body)

    opened.assert_called_once_with(result["auth_url"])
    assert result["mode"] == "browser"
    assert _redirect_uri_of(result["auth_url"]).startswith("http://localhost:")


def test_browser_start_survives_a_webbrowser_failure(fake_web):
    """On a headless box webbrowser.open raises. The frontend renders the
    link either way, so the flow must come back intact and still pending."""

    async def _body():
        flow = OAuthFlow(MagicMock())
        return flow, await flow.start(), _background_task_names()

    with patch("webbrowser.open", side_effect=RuntimeError("no display")):
        flow, result, task_names = run_async(_body)

    assert result["flow_id"] == flow.flow_id
    assert result["mode"] == "browser"
    assert result["auth_url"].startswith(f"{AUTH_URL}?")
    assert "error" not in result
    assert flow.get_status()["status"] == "pending"
    # The callback the user is about to complete still has a waiter.
    assert "OAuthFlow._wait_for_callback" in task_names


# ---------------------------------------------------------------------------
# get_status(): mode, auth_url, and the two timeout windows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("manual,expected_mode", [(True, "manual"), (False, "browser")])
def test_get_status_reports_mode_and_auth_url(fake_web, manual, expected_mode):
    """The frontend needs both to render the manual paste UI."""

    async def _body():
        flow = OAuthFlow(MagicMock(), manual=manual)
        return flow, await flow.start()

    flow, result = run_async(_body)
    status = flow.get_status()

    assert status["mode"] == expected_mode
    assert status["auth_url"] == result["auth_url"]
    assert status["status"] == "pending"


def test_get_status_expires_a_manual_flow_after_ten_minutes(fake_web):
    async def _body():
        flow = OAuthFlow(MagicMock(), manual=True)
        await flow.start()
        return flow

    flow = run_async(_body)
    flow._created_at -= MANUAL_TIMEOUT_SECONDS + 1

    assert flow.get_status()["status"] == "not_found"


def test_get_status_expires_a_browser_flow_after_two_minutes(fake_web):
    async def _body():
        flow = OAuthFlow(MagicMock())
        await flow.start()
        return flow

    flow = run_async(_body)
    flow._created_at -= BROWSER_TIMEOUT_SECONDS + 1

    assert flow.get_status()["status"] == "not_found"


def test_manual_window_outlives_the_browser_window(fake_web):
    """At 150s — past the browser limit, inside the manual one — a person is
    still plausibly copying a code between machines."""

    async def _body():
        manual = OAuthFlow(MagicMock(), manual=True)
        browser = OAuthFlow(MagicMock())
        await manual.start()
        await browser.start()
        return manual, browser

    manual, browser = run_async(_body)
    manual._created_at -= 150
    browser._created_at -= 150

    assert manual.get_status()["status"] == "pending"
    assert browser.get_status()["status"] == "not_found"


# ---------------------------------------------------------------------------
# parse_pasted_code(): every shape a user can plausibly paste
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pasted,expected",
    [
        # Bare code — the user copied only the code half.
        ("auth-code-abc", ("auth-code-abc", None)),
        # What Anthropic's code page actually shows.
        ("auth-code-abc#state-xyz", ("auth-code-abc", "state-xyz")),
        # Full callback URL, copied out of the address bar.
        (
            "https://platform.claude.com/oauth/code/callback"
            "?code=auth-code-abc&state=state-xyz",
            ("auth-code-abc", "state-xyz"),
        ),
        ("http://localhost:45100/callback?code=auth-code-abc", ("auth-code-abc", None)),
        # Bare query string — the "?" got lost on the way over.
        ("code=auth-code-abc&state=state-xyz", ("auth-code-abc", "state-xyz")),
        # Nothing usable.
        ("", (None, None)),
        ("   ", (None, None)),
        ("\n\t ", (None, None)),
        # Separator with an empty half on either side.
        ("#", (None, None)),
        ("auth-code-abc#", ("auth-code-abc", None)),
        ("#state-xyz", (None, "state-xyz")),
        # Surrounding whitespace from a sloppy copy.
        ("  auth-code-abc#state-xyz  ", ("auth-code-abc", "state-xyz")),
    ],
)
def test_parse_pasted_code(pasted, expected):
    assert OAuthFlow.parse_pasted_code(pasted) == expected


def test_parse_pasted_code_handles_none():
    """The route model requires a string, but the parser is a staticmethod
    other callers can reach — it must not raise on None."""
    assert OAuthFlow.parse_pasted_code(None) == (None, None)


# ---------------------------------------------------------------------------
# submit_code(): recoverable paste problems keep the flow alive
# ---------------------------------------------------------------------------


def test_submit_code_rejects_when_flow_is_not_pending():
    """A completed flow has already written credentials; a late paste must
    not re-run the exchange."""

    async def _body():
        flow = _pending_flow()
        flow._status = "completed"
        flow._complete_auth = AsyncMock()
        return flow, await flow.submit_code("auth-code-abc")

    flow, result = run_async(_body)

    assert "completed" in result["submit_error"]
    assert result["status"] == "completed"
    assert flow._status == "completed"
    flow._complete_auth.assert_not_awaited()


def test_submit_code_rejects_an_empty_paste_and_stays_pending():
    """The user can paste again, so an unparseable paste must not brick the
    flow the way a callback error does."""

    async def _body():
        flow = _pending_flow()
        flow._complete_auth = AsyncMock()
        return flow, await flow.submit_code("   ")

    flow, result = run_async(_body)

    assert "No authorization code" in result["submit_error"]
    assert result["status"] == "pending"
    assert flow._status == "pending"
    flow._complete_auth.assert_not_awaited()


def test_submit_code_rejects_a_state_mismatch_without_exchanging():
    """Same CSRF posture as the callback path: a code from a different
    authorization attempt never reaches the token exchange."""

    async def _body():
        flow = _pending_flow(state="the-right-state")
        flow._complete_auth = AsyncMock()
        return flow, await flow.submit_code("auth-code-abc#the-wrong-state")

    flow, result = run_async(_body)

    assert "different authorization" in result["submit_error"]
    assert result["status"] == "pending"
    assert flow._status == "pending"
    flow._complete_auth.assert_not_awaited()


def test_submit_code_completes_the_flow_on_a_valid_paste():
    """The happy path: only the code half is exchanged, and the poller sees
    a completed flow with the account it landed on."""

    async def _body():
        flow = _pending_flow(state="the-right-state")
        flow._complete_auth = AsyncMock(
            return_value={"account_id": 7, "email": "jack@example.com"}
        )
        return flow, await flow.submit_code("auth-code-abc#the-right-state")

    flow, result = run_async(_body)

    assert result["status"] == "completed"
    assert "submit_error" not in result
    assert result["account_id"] == 7
    assert result["email"] == "jack@example.com"
    assert flow._complete_auth.await_args.args == ("auth-code-abc",)
    assert flow._event.is_set()

    status = flow.get_status()
    assert status["status"] == "completed"
    assert status["account_id"] == 7
    assert status["email"] == "jack@example.com"


def test_submit_code_marks_the_flow_errored_when_the_exchange_fails():
    """A rejected code is not a paste mistake — it matches the callback
    path and errors the flow with the reason attached."""

    async def _body():
        flow = _pending_flow(state="the-right-state")
        flow._complete_auth = AsyncMock(
            side_effect=RuntimeError("token exchange HTTP 400")
        )
        return flow, await flow.submit_code("auth-code-abc")

    flow, result = run_async(_body)

    assert result["status"] == "error"
    assert "token exchange HTTP 400" in result["error"]
    assert flow._status == "error"
    assert flow._event.is_set()


def test_submit_code_rejects_a_concurrent_submission():
    """Two rapid submits must not both reach the exchange — the second is
    told to wait rather than queued behind the lock."""

    async def _body():
        flow = _pending_flow()
        flow._complete_auth = AsyncMock(
            return_value={"account_id": 1, "email": "jack@example.com"}
        )
        await flow._submit_lock.acquire()
        try:
            result = await flow.submit_code("auth-code-abc")
        finally:
            flow._submit_lock.release()
        return flow, result

    flow, result = run_async(_body)

    assert "already in progress" in result["submit_error"]
    assert result["status"] == "pending"
    flow._complete_auth.assert_not_awaited()


def test_submit_code_works_on_a_browser_flow_as_a_fallback():
    """When the localhost redirect fails, the user can still paste the code
    from the browser's address bar into the same flow."""

    async def _body():
        flow = _pending_flow(manual=False, state="the-right-state")
        flow._complete_auth = AsyncMock(
            return_value={"account_id": 3, "email": "jack@example.com"}
        )
        pasted = (
            "http://localhost:45100/callback"
            "?code=auth-code-abc&state=the-right-state"
        )
        return flow, await flow.submit_code(pasted)

    flow, result = run_async(_body)

    assert result["status"] == "completed"
    assert result["mode"] == "browser"
    assert flow._complete_auth.await_args.args == ("auth-code-abc",)


def test_submit_code_locks_out_after_too_many_attempts():
    """The attempt bound: every submission past MAX_SUBMIT_ATTEMPTS kills the
    flow instead of buying another outbound token exchange."""

    async def _body():
        from jacked.web.oauth import MAX_SUBMIT_ATTEMPTS

        flow = _pending_flow(state="the-right-state")
        flow._complete_auth = AsyncMock()
        for _ in range(MAX_SUBMIT_ATTEMPTS):
            result = await flow.submit_code("")  # bad pastes burn attempts
            assert result["status"] == "pending"
        final = await flow.submit_code("real-code#the-right-state")
        return flow, final

    flow, final = run_async(_body)

    assert final["status"] == "error"
    assert "Too many code submissions" in final["error"]
    flow._complete_auth.assert_not_awaited()

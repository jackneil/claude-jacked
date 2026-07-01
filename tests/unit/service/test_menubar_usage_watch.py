"""Tests for the mac menubar's event-driven usage watcher.

The pill must re-render within one USAGE_WATCH_INTERVAL tick of a
usage_events bump (dashboard refresh, account switch) and reload the
/panel webviews — whose own JS timers are suspended while offscreen.
The methods are exercised unbound with a stub ``self`` so no
NSApplication / status item is required.
"""

import pytest

from jacked import usage_events
from jacked.service import menubar_mac

if not menubar_mac.RUMPS_AVAILABLE:  # pragma: no cover
    pytest.skip("rumps/pyobjc unavailable", allow_module_level=True)


class _StubApp:
    """Just the attributes _watch_usage_version / _reload_panel_webviews touch."""

    def __init__(self):
        self._usage_version = usage_events.version()
        self.pill_refreshes = 0
        self.panel_reloads = 0
        self._popover_web = None
        self._panel_web = None

    def _refresh_pill(self, _timer):
        self.pill_refreshes += 1

    def _reload_panel_webviews(self):
        self.panel_reloads += 1


class _RecordingWebView:
    def __init__(self):
        self.scripts = []

    def evaluateJavaScript_completionHandler_(self, js, handler):
        self.scripts.append(js)


def _tick(app):
    menubar_mac.MacMenuBarApp._watch_usage_version(app, None)


class TestWatchUsageVersion:
    def test_no_bump_no_refresh(self):
        app = _StubApp()
        _tick(app)
        assert app.pill_refreshes == 0
        assert app.panel_reloads == 0

    def test_bump_triggers_refresh_and_reload(self):
        app = _StubApp()
        usage_events.bump()
        _tick(app)
        assert app.pill_refreshes == 1
        assert app.panel_reloads == 1

    def test_burst_of_bumps_coalesces_to_one_refresh(self):
        app = _StubApp()
        for _ in range(7):  # e.g. refresh-all-usage bumping once per account
            usage_events.bump()
        _tick(app)
        _tick(app)  # next tick with no new bumps
        assert app.pill_refreshes == 1
        assert app.panel_reloads == 1


class TestReloadPanelWebviews:
    def test_nudges_every_live_webview(self):
        app = _StubApp()
        app._popover_web = _RecordingWebView()
        app._panel_web = _RecordingWebView()
        menubar_mac.MacMenuBarApp._reload_panel_webviews(app)
        for web in (app._popover_web, app._panel_web):
            assert web.scripts == ["window.__panelReload && window.__panelReload()"]

    def test_missing_webviews_are_skipped(self):
        app = _StubApp()  # both webviews None
        menubar_mac.MacMenuBarApp._reload_panel_webviews(app)  # must not raise

    def test_webview_failure_is_swallowed(self):
        app = _StubApp()

        class _Boom:
            def evaluateJavaScript_completionHandler_(self, js, handler):
                raise RuntimeError("dead webview")

        app._popover_web = _Boom()
        app._panel_web = _RecordingWebView()
        menubar_mac.MacMenuBarApp._reload_panel_webviews(app)  # must not raise
        assert app._panel_web.scripts  # the healthy one still got nudged

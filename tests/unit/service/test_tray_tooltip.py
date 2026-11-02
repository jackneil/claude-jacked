"""The Windows/Linux tray tooltip: the active account's usage on hover.

The tray icon color shows the state of the service, so the usage lives in the
tooltip (jacked.service.usage_tooltip). These tests use a fake icon and a
patched loopback getter: nothing reaches a real server or a real tray.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from jacked.service.pill_text import TRAY_TOOLTIP_UNAVAILABLE
from jacked.service.usage_tooltip import UsageTooltip, loopback_get_json

_ULTRA = {"active": {"provider": "claude", "five_hour": None, "seven_day": 74.0,
                     "model_cap": {"label": "Fable", "tag": "F", "utilization": 100.0},
                     "blocked": False}}


def _tooltip(icon=None, stop=None):
    icon = SimpleNamespace(title="Jacked") if icon is None else icon
    stop = threading.Event() if stop is None else stop
    return UsageTooltip(9999, stop, lambda: icon), icon, stop


def test_tooltip_reads_the_active_summary_on_loopback():
    tip, icon, _ = _tooltip()
    with patch("jacked.service.usage_tooltip.loopback_get_json", return_value=_ULTRA) as get:
        tip.refresh()
    get.assert_called_once_with("http://127.0.0.1:9999/api/menubar-summary")
    assert icon.title == "Jacked · Claude · 7d 74% · Fable 100%"


def test_no_active_account_shows_the_plain_name():
    tip, icon, _ = _tooltip()
    with patch("jacked.service.usage_tooltip.loopback_get_json", return_value={"active": None}):
        tip.refresh()
    assert icon.title == "Jacked"


def test_a_failed_read_never_keeps_old_numbers():
    tip, icon, _ = _tooltip()
    icon.title = "Jacked · Claude · 7d 10%"
    with patch("jacked.service.usage_tooltip.loopback_get_json", side_effect=OSError("refused")):
        tip.refresh()
    assert icon.title == TRAY_TOOLTIP_UNAVAILABLE


def test_a_title_the_backend_refuses_is_contained():
    class _Icon:
        @property
        def title(self):
            return "Jacked"

        @title.setter
        def title(self, value):
            raise ValueError("string too long")

    tip, _, _ = _tooltip(icon=_Icon())
    with patch("jacked.service.usage_tooltip.loopback_get_json", return_value=_ULTRA):
        tip.refresh()  # must not raise into the tray thread


def test_no_icon_yet_is_a_no_op():
    tip = UsageTooltip(9999, threading.Event(), lambda: None)
    with patch("jacked.service.usage_tooltip.loopback_get_json") as get:
        tip.refresh()
    get.assert_not_called()


def _run_until(tip, stop, refreshes, on_refresh):
    """Run the loop on a thread with a fast tick; *on_refresh* sees each call."""
    calls = []

    def _refresh():
        calls.append(1)
        on_refresh(len(calls))
        if len(calls) >= refreshes:
            stop.set()

    with patch("jacked.service.usage_tooltip.TICK_S", 0.01), \
         patch.object(tip, "refresh", side_effect=_refresh):
        t = threading.Thread(target=tip.run, daemon=True)
        t.start()
        t.join(timeout=5)
    assert not t.is_alive()
    return calls


def test_the_loop_refreshes_on_a_usage_change_and_stops_on_stop():
    from jacked import usage_events

    tip, _, stop = _tooltip()
    seen = []

    def _on_refresh(n):
        seen.append(usage_events.version())
        if n == 1:
            usage_events.bump()  # a usage write lands: the next tick refreshes

    calls = _run_until(tip, stop, 2, _on_refresh)
    assert len(calls) == 2 and seen[1] == seen[0] + 1


def test_the_loop_refreshes_at_the_heartbeat_without_a_change():
    tip, _, stop = _tooltip()
    with patch("jacked.service.usage_tooltip.HEARTBEAT_S", 0.02):
        calls = _run_until(tip, stop, 2, lambda n: None)
    assert len(calls) == 2


def test_the_loop_does_not_run_after_stop():
    tip, _, stop = _tooltip()
    stop.set()
    with patch.object(tip, "refresh") as refresh:
        tip.run()
    refresh.assert_not_called()


def test_loopback_reads_never_use_a_proxy(monkeypatch):
    """A proxy in the environment must not capture the tray's loopback read."""
    import http.server
    import json
    import urllib.error
    import urllib.request

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            body = json.dumps({"active": None}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/menubar-summary"
        for name in ("HTTP_PROXY", "http_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
        # The premise: a default opener built now sends this request to the
        # proxy. (Not urlopen(): it caches a global opener that an earlier
        # test may have built before the proxy was set.)
        with pytest.raises(urllib.error.URLError):
            urllib.request.build_opener().open(url, timeout=2)
        assert loopback_get_json(url) == {"active": None}
    finally:
        server.shutdown()
        server.server_close()


def test_the_running_tray_starts_the_tooltip_loop():
    """ServiceRunner._setup starts one tooltip thread once the service is up."""
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except Exception:
        pytest.skip("tray dependencies unavailable")
    from jacked.service import tray

    runner = tray.ServiceRunner(host="127.0.0.1", port=9999)
    runner._started_at = time.time()
    started = []

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            self.target, self.name = target, name

        def start(self):
            started.append(self)

    with patch.object(tray.threading, "Thread", _Thread), \
         patch.object(runner, "_record_abandoned_update"), \
         patch.object(runner, "_apply_icon"):
        runner._setup(MagicMock())
    tooltip = [t for t in started if t.name == "jacked-usage-tooltip"]
    assert len(tooltip) == 1
    assert isinstance(tooltip[0].target.__self__, UsageTooltip)

"""The Windows/Linux tray: the active account's usage in the tooltip and menu.

The tray icon color shows the state of the service, so the usage lives in the
tooltip (Windows hover) and in a menu line (every backend, Linux included):
jacked.service.usage_tooltip. These tests use a fake icon and patch the
loopback read: nothing reaches a real server or a real tray.
"""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from jacked.service.pill_text import (
    TRAY_TOOLTIP_UNAVAILABLE,
    USAGE_LINE_EMPTY,
    USAGE_LINE_UNAVAILABLE,
)
from jacked.service.usage_tooltip import UsageTooltip

_GET = "jacked.service.loopback.get_json"
_ULTRA = {"active": {"provider": "claude", "five_hour": None, "seven_day": 74.0,
                     "model_cap": {"label": "Fable", "utilization": 100.0},
                     "blocked": False}}


class _Icon:
    def __init__(self):
        self.title = "Jacked"
        self.menu_updates = 0

    def update_menu(self):
        self.menu_updates += 1


def _tooltip(base="http://127.0.0.1:9999", icon=None, stop=None):
    icon = _Icon() if icon is None else icon
    stop = threading.Event() if stop is None else stop
    return UsageTooltip(lambda: base, stop, lambda: icon), icon, stop


def test_reads_the_own_service_and_sets_tooltip_and_menu_line():
    tip, icon, _ = _tooltip()
    with patch(_GET, return_value=_ULTRA) as get:
        tip.refresh()
    get.assert_called_once_with("http://127.0.0.1:9999/api/menubar-summary")
    assert icon.title == "Jacked · Claude · 7d 74% · Fable 100%"
    assert tip.menu_text() == "Claude · 7d 74% · Fable 100%"
    assert icon.menu_updates == 1  # GTK/AppIndicator rebuild the menu on change
    with patch(_GET, return_value=_ULTRA):
        tip.refresh()
    assert icon.menu_updates == 1  # no change, no rebuild


def test_no_active_account_shows_the_plain_name():
    tip, icon, _ = _tooltip()
    with patch(_GET, return_value={"active": None}):
        tip.refresh()
    assert (icon.title, tip.menu_text()) == ("Jacked", USAGE_LINE_EMPTY)


def test_a_service_that_is_not_ready_is_not_read():
    tip, icon, _ = _tooltip(base=None)
    with patch(_GET) as get:
        tip.refresh()
    get.assert_not_called()
    assert (icon.title, tip.menu_text()) == (TRAY_TOOLTIP_UNAVAILABLE, USAGE_LINE_UNAVAILABLE)


def test_a_failed_read_never_keeps_old_numbers_and_logs_once(caplog):
    tip, icon, _ = _tooltip()
    with patch(_GET, return_value=_ULTRA):
        tip.refresh()
    with caplog.at_level(logging.INFO, logger="jacked.service.usage_tooltip"):
        with patch(_GET, side_effect=OSError("refused")):
            tip.refresh()
            tip.refresh()
        assert (icon.title, tip.menu_text()) == (TRAY_TOOLTIP_UNAVAILABLE, USAGE_LINE_UNAVAILABLE)
        with patch(_GET, return_value=_ULTRA):
            tip.refresh()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and "OSError" in warnings[0]
    assert infos == ["Tray usage: the usage summary reads again"]


@pytest.mark.parametrize("payload", [
    {"active": ["x"]},
    {"active": {"seven_day": 10, "model_cap": {"label": "Fable"}}},
    {"active": {"seven_day": "10", "model_cap": {"utilization": "50"}}},
    {"active": "x"},
])
def test_a_malformed_answer_never_raises(payload):
    tip, icon, _ = _tooltip()
    with patch(_GET, return_value=payload):
        tip.refresh()
    assert isinstance(icon.title, str) and isinstance(tip.menu_text(), str)


def test_a_title_the_backend_refuses_is_contained(caplog):
    class _Refusing(_Icon):
        def __setattr__(self, name, value):
            if name == "title" and hasattr(self, "title"):
                raise ValueError("string too long")
            super().__setattr__(name, value)

    tip, _, _ = _tooltip(icon=_Refusing())
    with caplog.at_level(logging.WARNING, logger="jacked.service.usage_tooltip"):
        with patch(_GET, return_value=_ULTRA):
            tip.refresh()  # must not raise into the tray thread
    assert any("refused the update (ValueError)" in r.getMessage() for r in caplog.records)


def test_no_icon_yet_is_a_no_op():
    tip = UsageTooltip(lambda: "http://127.0.0.1:9999", threading.Event(), lambda: None)
    with patch(_GET) as get:
        tip.refresh()
    get.assert_not_called()


def _run_until(tip, stop, refreshes, refresh_effect):
    """Run the loop on a thread with a fast tick until *refreshes* calls."""
    calls = []

    def _refresh():
        calls.append(1)
        try:
            refresh_effect(len(calls))
        finally:
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

    def _effect(n):
        seen.append(usage_events.version())
        if n == 1:
            usage_events.bump()  # a usage write lands: the next tick refreshes

    calls = _run_until(tip, stop, 2, _effect)
    assert len(calls) == 2 and seen[1] == seen[0] + 1


def test_the_loop_refreshes_at_the_heartbeat_without_a_change():
    tip, _, stop = _tooltip()
    with patch("jacked.service.usage_tooltip.HEARTBEAT_S", 0.02):
        calls = _run_until(tip, stop, 2, lambda n: None)
    assert len(calls) == 2


def test_the_loop_survives_a_refresh_that_raises():
    tip, _, stop = _tooltip()

    def _effect(n):
        if n == 1:
            raise RuntimeError("boom")

    with patch("jacked.service.usage_tooltip.HEARTBEAT_S", 0.02):
        calls = _run_until(tip, stop, 2, _effect)
    assert len(calls) == 2  # the first failure did not end the thread


def test_the_loop_does_not_run_after_stop():
    tip, _, stop = _tooltip()
    stop.set()
    with patch.object(tip, "refresh") as refresh:
        tip.run()
    refresh.assert_not_called()


# ---------------------------------------------------------------------------
# ServiceRunner wiring (needs pystray importable)
# ---------------------------------------------------------------------------


def _runner():
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except Exception:
        pytest.skip("tray dependencies unavailable")
    from jacked.service import tray

    return tray, tray.ServiceRunner(host="127.0.0.1", port=9999)


@pytest.mark.parametrize("mode,addresses,primary,expected", [
    ("loopback", ("127.0.0.1",), "127.0.0.1", "http://127.0.0.1:9999"),
    ("all", ("0.0.0.0",), "0.0.0.0", "http://127.0.0.1:9999"),
    ("tailscale", ("127.0.0.1", "100.64.0.7"), "100.64.0.7", "http://127.0.0.1:9999"),
    ("cli", ("192.0.2.10",), "192.0.2.10", "http://192.0.2.10:9999"),
    ("cli", ("0.0.0.0",), "0.0.0.0", "http://127.0.0.1:9999"),
])
def test_the_read_follows_the_live_bind_plan(mode, addresses, primary, expected):
    from jacked.service.bind import BindPlan

    _, runner = _runner()
    runner.bind_plan = BindPlan(mode=mode, addresses=addresses, port=9999, primary_host=primary)
    runner._server_ready = True
    assert runner._usage_base_url() == expected
    runner._server_ready = False
    assert runner._usage_base_url() is None


def test_setup_starts_the_loop_even_when_the_first_start_failed():
    """A later Restart can make the server ready, so the loop always starts."""
    tray, runner = _runner()
    runner._started_at = None  # the first start failed
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
    loops = [t for t in started if t.name == "jacked-usage-tooltip"]
    assert len(loops) == 1 and loops[0].target == runner._usage_tooltip.run


def test_the_menu_shows_the_usage_line_under_the_header():
    tray, runner = _runner()
    runner._usage_tooltip._menu_text = "Claude · 7d 74% · Fable 100%"
    menu = tray.build_menu(
        port=9999, version="0.0.0", autostart_check=lambda: False,
        on_open_dashboard=lambda: None, on_restart=lambda: None, on_stop=lambda: None,
        on_toggle_autostart=lambda: None, usage_text_fn=runner._usage_tooltip.menu_text,
    )
    texts = [item.text for item in menu.items if not getattr(item, "_separator", False)]
    assert texts[:2] == ["JACKED", "Claude · 7d 74% · Fable 100%"]


def test_a_menu_without_usage_keeps_its_old_shape():
    tray, _ = _runner()
    menu = tray.build_menu(
        port=9999, version="0.0.0", autostart_check=lambda: False,
        on_open_dashboard=lambda: None, on_restart=lambda: None, on_stop=lambda: None,
        on_toggle_autostart=lambda: None,
    )
    assert [item.text for item in menu.items][:2] == ["JACKED", "Running on :9999"]


def test_time_is_monotonic_in_the_loop():
    # The heartbeat uses time.monotonic, so a wall-clock jump cannot stall it.
    import inspect

    from jacked.service import usage_tooltip

    assert "time.monotonic()" in inspect.getsource(usage_tooltip.UsageTooltip.run)
    assert time.monotonic() > 0

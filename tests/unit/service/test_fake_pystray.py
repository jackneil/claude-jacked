"""Contract tests for the fake pystray in ``tests/unit/service/_fake_pystray.py``.

The ``tray_module`` fixture uses the fake only on a host where pystray cannot
load (a Linux runner with no display). On macOS and Windows the real pystray
loads, so the fake is never imported there. These tests import the fake on
every platform. Where the real pystray loads, the menu tests also run on it,
so the fake cannot drift from the API that it copies.
"""

import pytest

from tests.unit.service import _fake_pystray


def _menu_apis():
    """The menu APIs under test: the fake always, the real pystray where it loads.

    pystray selects its GUI backend at import, and on a Linux runner with no
    display that raises an error that is not ImportError. That host is the
    reason the fake exists, so there the fake is tested alone.
    """
    apis = [("fake", _fake_pystray)]
    try:
        import pystray
    except Exception:  # noqa: BLE001 - pystray raises more than ImportError
        return apis
    return apis + [("pystray", pystray)]


def _reader(calls, label, value):
    """A property callable that records the item it gets and returns *value*."""

    def read(item):
        calls.append((label, item))
        return value

    return read


class TestMenuItem:
    def test_callable_properties_are_evaluated_with_the_item(self):
        for name, api in _menu_apis():
            calls = []
            item = api.MenuItem(
                _reader(calls, "text", "Open Dashboard"),
                None,
                checked=_reader(calls, "checked", True),
                default=_reader(calls, "default", True),
                enabled=_reader(calls, "enabled", False),
            )

            values = (item.text, item.checked, item.default, item.enabled)

            assert values == ("Open Dashboard", True, True, False), name
            assert calls == [
                ("text", item),
                ("checked", item),
                ("default", item),
                ("enabled", item),
            ], name
            assert str(item) == "Open Dashboard", name

    def test_plain_values_are_returned_as_they_are(self):
        for name, api in _menu_apis():
            item = api.MenuItem("Quit", None, default=False, enabled=True)

            assert (item.text, item.default, item.enabled, item.visible) == (
                "Quit", False, True, True,
            ), name
            assert item.checked is None, name  # the item cannot be checked
            assert item.radio is False, name

    def test_checked_must_be_none_or_a_callable(self):
        for name, api in _menu_apis():
            with pytest.raises(ValueError):
                api.MenuItem("Auto-swap", None, checked=True)


class TestMenu:
    def test_iteration_drops_invisible_items_and_stray_separators(self):
        for name, api in _menu_apis():
            separator = api.Menu.SEPARATOR
            first = api.MenuItem("Open Dashboard", None)
            hidden = api.MenuItem("Hidden", None, visible=False)
            hidden_by_callable = api.MenuItem("Hidden too", None, visible=lambda _: False)
            last = api.MenuItem("Quit", None)
            menu = api.Menu(
                separator, first, separator, hidden, separator,
                last, hidden_by_callable, separator,
            )

            # The head and tail separators go, and the separator that follows
            # a separator (after the hidden item is removed) goes too.
            assert list(menu) == [first, separator, last], name
            assert not api.Menu(separator, hidden, separator), name

    def test_default_item_activation_runs_the_default_action_once(self):
        for name, api in _menu_apis():
            runs = []

            class _Runner:
                def open_dashboard(self):  # the tray's shape: a bound method, no arguments
                    runs.append("open")

            icon = object()
            menu = api.Menu(
                api.MenuItem("Usage", lambda: runs.append("usage")),
                api.MenuItem("Open Dashboard", _Runner().open_dashboard, default=True),
                api.MenuItem("Later default", lambda i, item: runs.append("later"), default=True),
            )

            menu(icon)  # pystray's Icon.__call__ runs this on a left click

            assert runs == ["open"], name

    def test_a_menu_with_no_default_item_runs_nothing(self):
        for name, api in _menu_apis():
            runs = []
            menu = api.Menu(api.MenuItem("Usage", lambda: runs.append("usage")))

            assert menu(object()) is None, name
            assert runs == [], name

    def test_an_action_gets_the_arguments_that_it_takes(self):
        for name, api in _menu_apis():
            received = []
            icon = object()
            both = api.MenuItem("Both", lambda i, item: received.append((i, item)))

            api.MenuItem("None", lambda: received.append(()))(icon)
            api.MenuItem("Icon", lambda i: received.append((i,)))(icon)
            both(icon)

            assert received == [(), (icon,), (icon, both)], name
            with pytest.raises(ValueError):
                api.MenuItem("Three", lambda a, b, c: None)


class TestFakeIcon:
    """The fake Icon's record of calls. The real Icon draws instead, so
    these checks run on the fake only."""

    def test_the_icon_counts_menu_updates_records_notices_and_stops(self):
        icon = _fake_pystray.Icon("jacked", title="jacked", menu=_fake_pystray.Menu())
        assert (icon.name, icon.title, icon.visible) == ("jacked", "jacked", False)
        assert (icon.menu_updates, icon.notifications, icon.stopped) == (0, [], False)

        icon.update_menu()
        icon.update_menu()
        icon.notify("Jacked failed to start", "Jacked Service")
        icon.notify("Update ready")
        icon.stop()

        assert icon.menu_updates == 2
        assert icon.notifications == [
            ("Jacked failed to start", "Jacked Service"),
            ("Update ready", None),
        ]
        assert icon.stopped is True

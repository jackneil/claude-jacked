"""Usage in the tooltip of the Windows and Linux tray icon.

The pystray tray icon color shows the state of the service, so the usage of
the active account goes in the tooltip. :class:`UsageTooltip` reads
``/api/menubar-summary`` from the service on loopback and sets the tooltip
text from :func:`jacked.service.pill_text.tray_tooltip`. The macOS menu bar
does not use this module: it shows the usage as the pill text.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from typing import Callable

from jacked.service.pill_text import TRAY_TOOLTIP_UNAVAILABLE, tray_tooltip

logger = logging.getLogger(__name__)

# The loop reads the in-process change counter (jacked.usage_events) every
# TICK and reads the summary only when the counter moved, or at the
# HEARTBEAT for writers in other processes. The macOS pill uses the same
# 1-second watch and 30-second heartbeat.
TICK_S = 1.0
HEARTBEAT_S = 30.0

# A direct opener: the tray reads its own server on loopback, and a proxy
# setting must never send that request to a proxy.
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def loopback_get_json(url: str, timeout: float = 2.0) -> dict:
    """GET a JSON object from the service on loopback, never through a proxy."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with _LOOPBACK_OPENER.open(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


class UsageTooltip:
    """Keep the tray tooltip equal to the usage of the active account.

    *get_icon* returns the pystray icon, or None before it exists. The loop
    in :meth:`run` stops when *stop_event* is set.
    """

    def __init__(
        self, port: int, stop_event: threading.Event, get_icon: Callable[[], object]
    ) -> None:
        self._url = f"http://127.0.0.1:{port}/api/menubar-summary"
        self._stop_event = stop_event
        self._get_icon = get_icon

    def text(self) -> str:
        """The tooltip text. A failed read gives the "usage unavailable" text,
        never old numbers."""
        try:
            data = loopback_get_json(self._url)
        except Exception:  # noqa: BLE001 - a tooltip read must never stop the loop
            logger.debug("Tray tooltip: could not read the usage summary", exc_info=True)
            return TRAY_TOOLTIP_UNAVAILABLE
        return tray_tooltip(data.get("active"))

    def refresh(self) -> None:
        """Set the tooltip of the tray icon to :meth:`text`."""
        icon = self._get_icon()
        if icon is None:
            return
        text = self.text()
        try:
            icon.title = text  # pystray sends the OS update only on a change
        except Exception:  # noqa: BLE001 - a backend refusal must not stop the loop
            logger.debug("Tray tooltip: could not set the title", exc_info=True)

    def run(self) -> None:
        """Refresh on each usage change and at the heartbeat, until the stop event."""
        from jacked import usage_events

        seen = None
        last = float("-inf")
        while not self._stop_event.is_set():
            version = usage_events.version()
            now = time.monotonic()
            if version != seen or now - last >= HEARTBEAT_S:
                seen, last = version, now
                self.refresh()
            self._stop_event.wait(TICK_S)

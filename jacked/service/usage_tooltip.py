"""Usage of the active account on the Windows and Linux tray icon.

The pystray tray icon color shows the state of the service, so the usage goes
in two other places: the icon tooltip (Windows shows it on hover) and a line
at the top of the tray menu (every backend shows it; the Linux backends have
no hover tooltip). :class:`UsageTooltip` reads ``/api/menubar-summary`` from
its own service and sets both. The macOS menu bar does not use this module:
it shows the usage as the pill text.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from jacked.service import loopback
from jacked.service.pill_text import (
    TRAY_TOOLTIP_UNAVAILABLE,
    USAGE_LINE_EMPTY,
    USAGE_LINE_UNAVAILABLE,
    tray_tooltip,
    usage_line,
)

logger = logging.getLogger(__name__)

# The loop reads the in-process change counter (jacked.usage_events) every
# TICK and reads the summary only when the counter moved, or at the
# HEARTBEAT for writers in other processes. The macOS pill uses the same
# 1-second watch and 30-second heartbeat.
TICK_S = 1.0
HEARTBEAT_S = 30.0


class UsageTooltip:
    """Keep the tray tooltip and the menu usage line current.

    *get_base_url* returns the base URL of the service this tray owns (from
    its live bind plan), or None while that service is not ready; then the
    loop shows "usage unavailable" and reads nothing. *get_icon* returns the
    pystray icon, or None before it exists. :meth:`run` stops when
    *stop_event* is set.
    """

    def __init__(
        self,
        get_base_url: Callable[[], Optional[str]],
        stop_event: threading.Event,
        get_icon: Callable[[], object],
    ) -> None:
        self._get_base_url = get_base_url
        self._stop_event = stop_event
        self._get_icon = get_icon
        self._menu_text = USAGE_LINE_EMPTY
        self._failing: Optional[str] = None  # the failure being reported
        self._no_menu_noted = False

    def menu_text(self) -> str:
        """The usage line for the tray menu (a pystray menu text callable)."""
        return self._menu_text

    def _note_failure(self, what: str) -> None:
        """WARNING when a failure starts or changes; DEBUG while it repeats."""
        if self._failing != what:
            logger.warning(
                "Tray usage: %s; the tray shows \"usage unavailable\" until it "
                "reads again",
                what,
            )
            self._failing = what
        logger.debug("Tray usage failure: %s", what, exc_info=True)

    def _note_recovery(self) -> None:
        if self._failing is not None:
            logger.info("Tray usage: the usage summary reads again")
            self._failing = None

    def read(self) -> tuple[str, str]:
        """(tooltip, menu line) for the usage of the active account now."""
        base = self._get_base_url()
        if base is None:
            return TRAY_TOOLTIP_UNAVAILABLE, USAGE_LINE_UNAVAILABLE
        try:
            active = loopback.get_json(base + "/api/menubar-summary").get("active")
            tooltip, line = tray_tooltip(active), usage_line(active)
        except Exception as exc:  # noqa: BLE001 - a bad read must never stop the loop
            self._note_failure(f"could not read the usage summary ({type(exc).__name__})")
            return TRAY_TOOLTIP_UNAVAILABLE, USAGE_LINE_UNAVAILABLE
        self._note_recovery()
        return tooltip, line or USAGE_LINE_EMPTY

    def refresh(self) -> None:
        """Set the tooltip and, when it changed, rebuild the tray menu."""
        icon = self._get_icon()
        if icon is None:
            return
        tooltip, line = self.read()
        changed = line != self._menu_text
        self._menu_text = line
        try:
            icon.title = tooltip  # pystray sends the OS update only on a change
            if changed and self._has_menu(icon):
                icon.update_menu()  # GTK and AppIndicator build the menu once
        except Exception as exc:  # noqa: BLE001 - a backend refusal must not stop the loop
            self._note_failure(f"the tray refused the update ({type(exc).__name__})")

    def _has_menu(self, icon) -> bool:
        """False on a backend with no menu (pystray Xorg). Say so once: the
        usage then shows only where the tray displays the icon title."""
        if getattr(icon, "HAS_MENU", True):
            return True
        if not self._no_menu_noted:
            self._no_menu_noted = True
            logger.info(
                "Tray usage: this tray backend has no menu, so the usage line "
                "is not shown; the dashboard shows the usage"
            )
        return False

    def run(self) -> None:
        """Refresh on each usage change and at the heartbeat, until the stop event."""
        from jacked import usage_events

        seen = None
        last = float("-inf")
        while not self._stop_event.is_set():
            try:
                version = usage_events.version()
                now = time.monotonic()
                if version != seen or now - last >= HEARTBEAT_S:
                    seen, last = version, now
                    self.refresh()
            except Exception as exc:  # noqa: BLE001 - the loop outlives any one refresh
                self._note_failure(f"the refresh failed ({type(exc).__name__})")
            self._stop_event.wait(TICK_S)

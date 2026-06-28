"""macOS menu-bar agent for jacked (rumps + PyObjC).

The native face of the jacked service on macOS. Replaces the cross-platform
pystray tray with a real menu-bar app:

* a live **pill** (NSStatusItem title) showing the worst account's 5h·7d %,
  refreshed on a timer from ``/api/menubar-summary``;
* an **NSPopover dropdown** and an always-on-top, all-Spaces **NSPanel side
  panel**, each a ``WKWebView`` pointed at ``/panel`` (the same compact page,
  so they can never diverge from the dashboard);
* menu actions: Open Dashboard, Open Usage Dropdown, Toggle Side Panel,
  Auto-swap (reflects + toggles), Add account, Restart, Quit (clean uvicorn
  stop).

It does NOT start its own server: the owning :class:`ServiceRunner` starts the
single uvicorn on 127.0.0.1:8321 in a daemon thread, and this agent
health-checks + connects to it, showing a degraded pill if it goes down.

rumps + the pyobjc frameworks are darwin-only deps (see pyproject). Imports are
guarded so this module is importable (for ``RUMPS_AVAILABLE`` probing) even
where they're absent; the GUI class is only defined when they're present.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
import webbrowser

logger = logging.getLogger(__name__)

# Pill + stop-watch cadence (seconds).
PILL_INTERVAL = 30
STOP_POLL_INTERVAL = 1.0
PANEL_WIDTH = 360  # side-panel / popover width in points

try:
    import rumps
    from AppKit import (
        NSApplicationDidChangeScreenParametersNotification,
        NSBackingStoreBuffered,
        NSPanel,
        NSPopover,
        NSPopoverBehaviorTransient,
        NSScreen,
        NSStatusWindowLevel,
        NSViewController,
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowStyleMaskBorderless,
        NSWindowStyleMaskNonactivatingPanel,
    )
    from Foundation import (
        NSURL,
        NSURLRequest,
        NSMakeRect,
        NSNotificationCenter,
        NSOperationQueue,
        NSSize,
    )
    from WebKit import WKWebView, WKWebViewConfiguration

    RUMPS_AVAILABLE = True
except Exception:  # pragma: no cover - non-darwin / frameworks absent
    rumps = None
    RUMPS_AVAILABLE = False


# --- HTTP helpers (stdlib; loopback to our own uvicorn — fast, main-thread-safe) ---


def _http_get_json(url: str, timeout: float = 2.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_send_json(url: str, method: str, payload: dict | None, timeout: float = 3.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {}


if RUMPS_AVAILABLE:

    class MacMenuBarApp(rumps.App):
        """The rumps status-bar app + PyObjC popover/panel.

        Constructed with the owning :class:`~jacked.service.tray.ServiceRunner`;
        reuses its uvicorn lifecycle (``_start_uvicorn`` / ``_shutdown_uvicorn``
        / ``_on_restart``) so there is exactly one server in the process.
        """

        def __init__(self, runner):
            super().__init__("jacked", title="jacked …", quit_button=None)
            self._runner = runner
            self._base_url = f"http://{_loopback(runner.host)}:{runner.port}"
            self._panel = None
            self._panel_web = None
            self._panel_visible = False
            self._popover = None
            self._popover_web = None
            self._screen_observer = None
            self._auto_swap_enabled = False

            self._auto_swap_item = rumps.MenuItem(
                "Auto-swap", callback=self._on_toggle_auto_swap
            )
            self.menu = [
                rumps.MenuItem("Open Usage Dropdown", callback=self._on_dropdown),
                rumps.MenuItem("Toggle Side Panel", callback=self._on_toggle_panel),
                rumps.separator,
                rumps.MenuItem("Open Dashboard", callback=self._on_open_dashboard),
                rumps.MenuItem("Add Account", callback=self._on_add_account),
                self._auto_swap_item,
                rumps.separator,
                rumps.MenuItem("Restart", callback=self._on_restart),
                rumps.MenuItem("Quit", callback=self._on_quit),
            ]

            # Re-pin the panel when displays change (add/remove/resolution).
            self._screen_observer = (
                NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
                    NSApplicationDidChangeScreenParametersNotification,
                    None,
                    NSOperationQueue.mainQueue(),
                    lambda _note: self._reposition_panel(),
                )
            )

            # Immediate pill, then a refresh timer + a stop-watch timer.
            self._refresh_pill(None)
            self._pill_timer = rumps.Timer(self._refresh_pill, PILL_INTERVAL)
            self._pill_timer.start()
            self._stop_timer = rumps.Timer(self._check_stop, STOP_POLL_INTERVAL)
            self._stop_timer.start()

        # -- pill ------------------------------------------------------------

        def _refresh_pill(self, _timer):
            """Poll the summary + set the live title; degrade if the server is down."""
            from jacked.service.menubar_summary import menubar_title

            try:
                data = _http_get_json(self._base_url + "/api/menubar-summary")
                self.title = menubar_title(data.get("worst"))
            except Exception:
                self.title = "⚠︎ jacked"  # degraded — server unreachable

            # Keep the Auto-swap checkmark honest.
            try:
                s = _http_get_json(self._base_url + "/api/settings/swap-settings")
                self._auto_swap_enabled = bool(s.get("auto_swap_enabled"))
                self._auto_swap_item.state = 1 if self._auto_swap_enabled else 0
            except Exception:
                pass

        # -- native windows (M4) --------------------------------------------

        def _make_webview(self, width, height):
            cfg = WKWebViewConfiguration.alloc().init()
            web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, width, height), cfg
            )
            url = NSURL.URLWithString_(self._base_url + "/panel")
            web.loadRequest_(NSURLRequest.requestWithURL_(url))
            return web

        def _ensure_panel(self):
            if self._panel is not None:
                return
            frame = self._panel_frame()
            style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                frame, style, NSBackingStoreBuffered, False
            )
            panel.setLevel_(NSStatusWindowLevel)
            panel.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
                | NSWindowCollectionBehaviorFullScreenAuxiliary
            )
            panel.setOpaque_(False)
            panel.setHidesOnDeactivate_(False)
            panel.setBecomesKeyOnlyIfNeeded_(True)
            panel.setMovableByWindowBackground_(False)
            web = self._make_webview(frame.size.width, frame.size.height)
            panel.setContentView_(web)
            self._panel = panel
            self._panel_web = web

        def _panel_frame(self):
            """Right-edge frame within the main screen's visible area."""
            screen = NSScreen.mainScreen()
            vf = screen.visibleFrame()
            x = vf.origin.x + vf.size.width - PANEL_WIDTH
            return NSMakeRect(x, vf.origin.y, PANEL_WIDTH, vf.size.height)

        def _reposition_panel(self):
            if self._panel is None:
                return
            self._panel.setFrame_display_(self._panel_frame(), True)

        def _on_toggle_panel(self, _sender):
            self._ensure_panel()
            if self._panel_visible:
                self._panel.orderOut_(None)
                self._panel_visible = False
            else:
                self._reposition_panel()
                self._panel.orderFrontRegardless()
                self._panel_visible = True

        def _on_dropdown(self, _sender):
            """Show the rich /panel as an NSPopover anchored to the status button."""
            if self._popover is None:
                pop = NSPopover.alloc().init()
                pop.setBehavior_(NSPopoverBehaviorTransient)
                pop.setContentSize_(NSSize(PANEL_WIDTH, 480))
                web = self._make_webview(PANEL_WIDTH, 480)
                vc = NSViewController.alloc().init()
                vc.setView_(web)
                pop.setContentViewController_(vc)
                self._popover = pop
                self._popover_web = web
            button = self._status_button()
            if button is None:
                # No anchor available — fall back to the pinned panel.
                self._on_toggle_panel(_sender)
                return
            from AppKit import NSMinYEdge

            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, NSMinYEdge
            )

        def _status_button(self):
            """The NSStatusBarButton rumps created, for popover anchoring."""
            try:
                return self._nsapp.nsstatusitem.button()
            except Exception:
                return None

        # -- menu actions ----------------------------------------------------

        def _on_open_dashboard(self, _sender):
            webbrowser.open(self._base_url)

        def _on_add_account(self, _sender):
            """Kick the existing OAuth add flow (it opens the browser itself)."""
            try:
                _http_send_json(self._base_url + "/api/auth/accounts/add", "POST", {})
            except Exception:
                logger.exception("Add-account flow failed; opening dashboard instead")
                webbrowser.open(self._base_url)

        def _on_toggle_auto_swap(self, sender):
            try:
                cur = _http_get_json(self._base_url + "/api/settings/swap-settings")
                cur["auto_swap_enabled"] = not bool(cur.get("auto_swap_enabled"))
                _http_send_json(
                    self._base_url + "/api/settings/swap-settings", "PUT", cur
                )
                self._auto_swap_enabled = cur["auto_swap_enabled"]
                sender.state = 1 if self._auto_swap_enabled else 0
            except Exception:
                logger.exception("Auto-swap toggle failed")

        def _on_restart(self, _sender):
            # _on_restart blocks (shutdown + rebind uvicorn); run off the main
            # thread so the menu/run loop stays responsive. Its pystray-icon
            # writes are all guarded by `if self._icon:` (None here), so it is
            # safe to reuse verbatim.
            threading.Thread(
                target=self._runner._on_restart, name="jacked-mac-restart", daemon=True
            ).start()

        def _on_quit(self, _sender):
            self._shutdown()
            rumps.quit_application()

        # -- lifecycle -------------------------------------------------------

        def _check_stop(self, _timer):
            """Bridge a SIGTERM/SIGINT-set stop event to a clean GUI quit."""
            if self._runner._stop_event.is_set():
                self._shutdown()
                rumps.quit_application()

        def _shutdown(self):
            from jacked.service import PID_FILE
            from jacked.service.process import remove_pid

            try:
                self._runner._shutdown_uvicorn()
            except Exception:
                logger.exception("uvicorn shutdown during quit failed")
            try:
                remove_pid(PID_FILE)
            except Exception:
                logger.exception("PID cleanup during quit failed")
            if self._screen_observer is not None:
                try:
                    NSNotificationCenter.defaultCenter().removeObserver_(
                        self._screen_observer
                    )
                except Exception:
                    pass

else:  # pragma: no cover - exercised only where rumps/pyobjc are unavailable

    class MacMenuBarApp:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError(
                "macOS menu-bar agent requires rumps + pyobjc "
                "(install jacked on macOS so the darwin-only deps resolve)"
            )


def _loopback(host: str) -> str:
    """Always reach uvicorn over loopback even if it bound 0.0.0.0."""
    return "127.0.0.1" if host in ("0.0.0.0", "", None) else host

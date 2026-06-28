"""macOS menu-bar agent for jacked (rumps + PyObjC).

The native face of the jacked service on macOS. Replaces the cross-platform
pystray tray with a real menu-bar app:

* a live **pill**: a "J" icon tinted green/yellow/red by the **active** account's
  usage, plus that account's ``5h·7d`` % as text — refreshed on a timer from
  ``/api/menubar-summary``. It tracks the account you're actually using, not the
  worst account in the fleet.
* **left-click → an NSPopover dropdown** and a **Toggle Side Panel → always-on-top,
  all-Spaces NSPanel**, each a ``WKWebView`` at ``/panel`` (same compact page as
  the dashboard, so they can't diverge);
* **right-click (or control-click) → the actions menu**: Open Dashboard, Open
  Usage Dropdown, Toggle Side Panel, Auto-swap, Add account, Restart, Quit
  (clean uvicorn stop).

It does NOT start its own server: the owning :class:`ServiceRunner` starts the
single uvicorn on 127.0.0.1:8321 in a daemon thread, and this agent
health-checks + connects to it, showing a degraded (gray) pill if it goes down.

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

from jacked import __version__

logger = logging.getLogger(__name__)

# Pill + stop-watch cadence (seconds).
PILL_INTERVAL = 30
STOP_POLL_INTERVAL = 1.0
WIRE_RETRY_INTERVAL = 0.4  # poll for the status-item button after launch
PANEL_WIDTH = 360  # side-panel / popover width in points
POPOVER_HEIGHT = 600  # dropdown height — fits ~7 accounts without scrolling

# RGB fills for the "J" status icon, keyed by usage color class.
_ICON_FILL = {
    "green": (34, 197, 94),
    "yellow": (234, 179, 8),
    "red": (239, 68, 68),
    "gray": (130, 130, 130),  # degraded / no data
}

try:
    import objc
    import rumps
    from AppKit import (
        NSApp,
        NSApplicationDidChangeScreenParametersNotification,
        NSBackingStoreBuffered,
        NSEventMaskLeftMouseUp,
        NSEventMaskRightMouseUp,
        NSEventModifierFlagControl,
        NSEventTypeRightMouseUp,
        NSObject,
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


def _render_status_icon(color: str, update: bool = False) -> "str | None":
    """Draw a rounded-rect "J" glyph filled with the status color; return a PNG
    path (cached per color+update under ~/.claude). When *update* is set, overlay
    an "update available" badge (a blue dot with a white ring, top-right) so the
    menu bar makes a waiting update obvious without opening the menu. Returns None
    if rendering fails — the caller then falls back to text only."""
    try:
        from PIL import Image, ImageDraw

        from jacked.service import CLAUDE_DIR
        from jacked.service.tray import _load_glyph_font

        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        path = CLAUDE_DIR / f"jacked-menubar-{color}{'-upd' if update else ''}.png"
        if path.exists():
            return str(path)

        fill = _ICON_FILL.get(color, _ICON_FILL["gray"])
        size = 44  # retina-friendly; macOS scales to the menu-bar height
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([(3, 3), (size - 4, size - 4)], radius=11, fill=fill)
        font = _load_glyph_font(26)
        bbox = draw.textbbox((0, 0), "J", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        x = (size - tw) // 2 - bbox[0]
        y = (size - th) // 2 - bbox[1]
        draw.text((x, y), "J", fill="white", font=font)
        if update:
            # Update-available badge: blue dot + white ring, top-right corner.
            # Sized to stay legible when macOS scales the icon to ~18px tall.
            cx, cy, r = size - 11, 11, 8
            draw.ellipse([cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2], fill=(255, 255, 255, 255))
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(59, 130, 246, 255))
        img.save(str(path))
        return str(path)
    except Exception:
        logger.exception(
            "Could not render menu-bar status icon (color=%s, update=%s)", color, update
        )
        return None


if RUMPS_AVAILABLE:

    class _ClickHandler(NSObject):
        """Target for the status-item button so we can split left vs. right click."""

        def initWithApp_(self, app):  # noqa: N802 - objc selector
            self = objc.super(_ClickHandler, self).init()
            if self is None:
                return None
            self._app = app
            return self

        def onClick_(self, _sender):  # noqa: N802 - objc selector
            try:
                self._app._handle_status_click()
            except Exception:
                logger.exception("status-item click handling failed")

    class _PanelBridge(NSObject):
        """WKScriptMessageHandler: lets the /panel webview ask the native agent to
        open the actions menu — so the in-panel ⋯ button reaches the same menu as
        a right-click, for users who don't realize they can right-click."""

        def initWithApp_(self, app):  # noqa: N802 - objc selector
            self = objc.super(_PanelBridge, self).init()
            if self is None:
                return None
            self._app = app
            return self

        def userContentController_didReceiveScriptMessage_(self, _ucc, message):  # noqa: N802
            try:
                self._app._on_panel_message(str(message.body()))
            except Exception:
                logger.exception("panel bridge message handling failed")

    class MacMenuBarApp(rumps.App):
        """The rumps status-bar app + PyObjC popover/panel.

        Constructed with the owning :class:`~jacked.service.tray.ServiceRunner`;
        reuses its uvicorn lifecycle (``_start_uvicorn`` / ``_shutdown_uvicorn``
        / ``_on_restart``) so there is exactly one server in the process.
        """

        def __init__(self, runner):
            super().__init__("jacked", title="…", quit_button=None)
            self.template = False  # show the icon in COLOR, not as a mono template
            self._runner = runner
            self._base_url = f"http://{_loopback(runner.host)}:{runner.port}"
            self._panel = None
            self._panel_web = None
            self._panel_visible = False
            self._popover = None
            self._popover_web = None
            self._screen_observer = None
            self._auto_swap_enabled = False
            self._current_icon_key = None  # (color, update) — avoid redundant icon writes
            self._click_handler = None
            self._appkit_menu = None
            self._click_wired = False
            self._bridge = None  # WKScriptMessageHandler for the in-panel ⋯ button

            self._auto_swap_item = rumps.MenuItem(
                "Auto-swap", callback=self._on_toggle_auto_swap
            )
            self._autostart_item = rumps.MenuItem(
                "Start on Login", callback=self._on_toggle_autostart_item
            )
            # Version / update items — wired to the SAME ServiceRunner machinery
            # the pystray tray uses (version check, click-to-update, last-checked).
            self._version_item = rumps.MenuItem(f"v{__version__}", callback=None)
            self._last_check_item = rumps.MenuItem("Last checked: never", callback=None)
            self._check_updates_item = rumps.MenuItem(
                "Check for Updates…", callback=self._on_check_updates_item
            )
            self.menu = [
                rumps.MenuItem("Open Usage Dropdown", callback=self._on_dropdown),
                rumps.MenuItem("Toggle Side Panel", callback=self._on_toggle_panel),
                rumps.separator,
                rumps.MenuItem("Open Dashboard", callback=self._on_open_dashboard),
                rumps.MenuItem("Add Account", callback=self._on_add_account),
                self._auto_swap_item,
                self._autostart_item,
                rumps.separator,
                rumps.MenuItem("Restart", callback=self._on_restart),
                rumps.MenuItem("Quit", callback=self._on_quit),
                rumps.separator,
                self._version_item,
                self._last_check_item,
                self._check_updates_item,
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

            # Immediate pill, then refresh + stop-watch + click-wiring timers.
            self._refresh_pill(None)
            self._pill_timer = rumps.Timer(self._refresh_pill, PILL_INTERVAL)
            self._pill_timer.start()
            self._stop_timer = rumps.Timer(self._check_stop, STOP_POLL_INTERVAL)
            self._stop_timer.start()
            # The status-item button only exists after the app launches, so wire
            # the left/right click split from a short poll timer.
            self._wire_timer = rumps.Timer(self._try_wire_click, WIRE_RETRY_INTERVAL)
            self._wire_timer.start()

            # Version/update: the pystray path starts this in _setup; the mac path
            # must start it too, or the version line never learns about updates.
            self._refresh_version_menu()
            self._version_thread = threading.Thread(
                target=self._runner._check_version,
                name="jacked-version-check",
                daemon=True,
            )
            self._version_thread.start()

        # -- pill ------------------------------------------------------------

        def _refresh_pill(self, _timer):
            """Poll the summary; set the active account's % as text and tint the
            "J" icon by its color. Degrade to a gray "J" + em-dash if down."""
            from jacked.service.menubar_summary import menubar_title

            upd = self._update_available()
            try:
                data = _http_get_json(self._base_url + "/api/menubar-summary")
                active = data.get("active")
                self.title = " " + menubar_title(active)
                self._set_icon((active or {}).get("color") or "gray", upd)
            except Exception:
                self.title = " —"  # degraded — server unreachable
                self._set_icon("gray", upd)

            # Keep the Auto-swap checkmark honest.
            try:
                s = _http_get_json(self._base_url + "/api/settings/swap-settings")
                self._auto_swap_enabled = bool(s.get("auto_swap_enabled"))
                self._auto_swap_item.state = 1 if self._auto_swap_enabled else 0
            except Exception:
                pass

            # Keep the version line / last-checked / autostart state fresh.
            self._refresh_version_menu()

        def _set_icon(self, color, update=False):
            """Set the colored "J" icon (+ update badge), only when it changed."""
            key = (color, update)
            if key == self._current_icon_key:
                return
            path = _render_status_icon(color, update)
            if path:
                try:
                    self.icon = path
                    self.template = False
                    self._current_icon_key = key
                except Exception:
                    logger.exception("Could not apply menu-bar icon")

        def _update_available(self):
            """True when the version check has found a newer release available."""
            try:
                return bool(self._runner._version_is_clickable())
            except Exception:
                return False

        # -- click routing (left = dropdown, right = menu) -------------------

        def _try_wire_click(self, timer):
            """Once the status-item button exists, route left-click to the
            dropdown and right-click to the actions menu. Best-effort: on ANY
            failure we leave rumps' default menu-on-click behavior intact so the
            pill is never left dead."""
            if self._click_wired:
                timer.stop()
                return
            try:
                si = getattr(self._nsapp, "nsstatusitem", None)
                button = si.button() if si is not None else None
                if button is None:
                    return  # not ready yet; poll again
                # Capture the NSMenu rumps built, then detach it so the button
                # fires our action on plain left-click instead of opening it.
                self._appkit_menu = si.menu()
                si.setMenu_(None)
                self._click_handler = _ClickHandler.alloc().initWithApp_(self)
                button.setTarget_(self._click_handler)
                button.setAction_("onClick:")
                button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)
                self._click_wired = True
                timer.stop()
            except Exception:
                logger.exception(
                    "Could not wire status-item click split — leaving default menu"
                )
                # Re-attach the menu if we detached it, so click still works.
                try:
                    si = getattr(self._nsapp, "nsstatusitem", None)
                    if si is not None and self._appkit_menu is not None:
                        si.setMenu_(self._appkit_menu)
                except Exception:
                    pass
                timer.stop()

        def _handle_status_click(self):
            ev = NSApp.currentEvent()
            is_right = False
            try:
                is_right = ev.type() == NSEventTypeRightMouseUp or bool(
                    ev.modifierFlags() & NSEventModifierFlagControl
                )
            except Exception:
                is_right = False
            if is_right:
                self._show_actions_menu()
            else:
                self._on_dropdown(None)

        def _show_actions_menu(self):
            """Pop up the actions menu, then detach it again so the next plain
            left-click still routes to our handler."""
            si = getattr(self._nsapp, "nsstatusitem", None)
            if si is None or self._appkit_menu is None:
                return
            self._refresh_version_menu()  # version/last-checked current at open time
            si.setMenu_(self._appkit_menu)
            try:
                si.button().performClick_(None)  # opens + tracks the menu modally
            finally:
                si.setMenu_(None)

        # -- native windows --------------------------------------------------

        def _make_webview(self, width, height):
            cfg = WKWebViewConfiguration.alloc().init()
            # Bridge so the panel's ⋯ button can open the native actions menu.
            try:
                if self._bridge is None:
                    self._bridge = _PanelBridge.alloc().initWithApp_(self)
                cfg.userContentController().addScriptMessageHandler_name_(
                    self._bridge, "jacked"
                )
            except Exception:
                logger.exception("Could not install panel→native message bridge")
            web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, width, height), cfg
            )
            url = NSURL.URLWithString_(self._base_url + "/panel")
            web.loadRequest_(NSURLRequest.requestWithURL_(url))
            return web

        def _on_panel_message(self, body):
            """Handle a postMessage from the /panel webview (main thread)."""
            if body == "show-menu":
                # Close the transient popover first, then open the actions menu at
                # the status icon — feels like the right-click menu.
                try:
                    if self._popover is not None and self._popover.isShown():
                        self._popover.performClose_(None)
                except Exception:
                    pass
                self._show_actions_menu()

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
            """Toggle the rich /panel as an NSPopover anchored to the status button."""
            if self._popover is None:
                pop = NSPopover.alloc().init()
                pop.setBehavior_(NSPopoverBehaviorTransient)
                pop.setContentSize_(NSSize(PANEL_WIDTH, POPOVER_HEIGHT))
                web = self._make_webview(PANEL_WIDTH, POPOVER_HEIGHT)
                vc = NSViewController.alloc().init()
                vc.setView_(web)
                pop.setContentViewController_(vc)
                self._popover = pop
                self._popover_web = web
            if self._popover.isShown():
                self._popover.performClose_(None)
                return
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

        # -- version / update (parity with the pystray tray) -----------------

        def _refresh_version_menu(self):
            """Sync the version line, last-checked text, update-clickability, and
            the Start-on-Login checkmark from the ServiceRunner's state. rumps
            menu titles aren't lazily re-evaluated, so we push updates here (pill
            timer + on menu-open + after a manual check)."""
            r = self._runner
            try:
                self._version_item.title = r._version_menu_text()
                # The version line is clickable only when an update is available.
                if r._version_is_clickable():
                    self._version_item.set_callback(self._on_version_click)
                else:
                    self._version_item.set_callback(None)
            except Exception:
                logger.exception("version line refresh failed")
            try:
                self._last_check_item.title = r._last_check_menu_text()
                # Disable "Check for Updates…" while a check is already running.
                if getattr(r, "_version_check_in_progress", False):
                    self._check_updates_item.set_callback(None)
                else:
                    self._check_updates_item.set_callback(self._on_check_updates_item)
            except Exception:
                logger.exception("last-checked refresh failed")
            try:
                from jacked.service.platform import detect_autostart

                self._autostart_item.state = 1 if detect_autostart() else 0
            except Exception:
                pass

        def _on_version_click(self, _sender):
            """User clicked the 'update available' line — run the existing
            tray updater off the main thread (it spawns the detached updater and
            stops the service; our stop-watch timer then quits the app)."""
            threading.Thread(
                target=self._runner._on_update_click,
                name="jacked-mac-update",
                daemon=True,
            ).start()

        def _on_check_updates_item(self, _sender):
            """Force a fresh PyPI check now (runs in the runner's own thread),
            then refresh the menu so the result shows without reopening."""
            try:
                self._runner._on_check_for_updates()
            except Exception:
                logger.exception("manual update check failed")
            self._refresh_version_menu()  # shows "Checking PyPI…" immediately
            # The check runs in a background thread; pull its result in a moment.
            rumps.Timer(self._after_check_refresh, 3.0).start()

        def _after_check_refresh(self, timer):
            timer.stop()
            self._refresh_version_menu()

        def _on_toggle_autostart_item(self, _sender):
            try:
                self._runner._on_toggle_autostart()
            except Exception:
                logger.exception("Start-on-Login toggle failed")
            self._refresh_version_menu()

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

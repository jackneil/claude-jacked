"""A small stand-in for pystray, for tray tests on a machine without pystray.

pystray selects its GUI backend when it is imported. On a Linux runner with
no display, that import fails (Xlib raises DisplayNameError), so
``jacked.service.tray`` sets ``_TRAY_AVAILABLE = False`` and does not bind
``tray.pystray``. The ``tray_module`` fixture in ``conftest.py`` then puts
this module in that place. When the real pystray imports, the fixture uses
the real pystray, so the macOS and Windows legs test against pystray itself
and the Linux leg tests against this copy.

This module copies the parts of the pystray 0.19.5 API that jacked uses. It
shows no icon and starts no thread.

``MenuItem(text, action, checked=None, radio=False, default=False,
visible=True, enabled=True)``
    The parameters have the same order as in pystray. The ``text``,
    ``checked``, ``radio``, ``default``, ``visible`` and ``enabled``
    properties are evaluated the same way as in pystray: a callable value
    gets the item as its one argument, and the property returns the result.
    A value that is not callable is returned as it is. As in pystray,
    ``checked`` must be None or a callable, and ``radio`` is False for an
    item that cannot be checked. Calling an item with an icon calls its
    action with ``(icon, item)``, ``(icon)`` or ``()``, by the number of
    parameters that the action takes. ``str(item)`` is the text.

``Menu(*items)``
    ``items`` is the tuple of the items. A single callable item is a menu
    generator, as in pystray. Iteration gives the visible items: as in
    pystray, it removes a separator at the start or the end and a separator
    that follows a separator. ``Menu.SEPARATOR`` is the separator item, with
    the text ``"- - - -"`` as in pystray. The fake separator also has
    ``_separator = True``, which pystray does not have. To find a separator
    with the fake and with pystray, compare the item with ``Menu.SEPARATOR``
    by identity.

``Icon(name, icon=None, title=None, menu=None)``
    The class attributes ``HAS_DEFAULT_ACTION``, ``HAS_MENU``,
    ``HAS_MENU_RADIO`` and ``HAS_NOTIFICATION`` are True, as on the pystray
    base class. A subclass can set one to False to model a backend without
    that feature (the pystray Xorg backend has no menu). The ``name``,
    ``icon``, ``title``, ``menu`` and ``visible`` attributes are plain
    values. ``update_menu()`` counts its calls in ``menu_updates``,
    ``notify()`` records each ``(message, title)`` in ``notifications``, and
    ``stop()`` sets ``stopped``. The fake has no ``run()``: a test that
    drives ``ServiceRunner._run`` must replace ``Icon`` with its own stub.
"""

import inspect


def _wrap(value):
    """Return *value* if it is callable, else a callable that returns it."""
    return value if callable(value) else (lambda _item: value)


def _adapt_action(action):
    """Make *action* callable as ``action(icon, item)``, as pystray does.

    pystray counts the positional parameters of a plain function or a bound
    method (``__code__.co_argcount``, less one for ``self``). An action with
    0 or 1 parameter gets the first 0 or 1 argument. An action with more
    than 2 parameters is refused with ValueError. A callable object without
    ``__code__`` is called with both arguments.
    """
    if action is None:
        return lambda *_args: None
    if not hasattr(action, "__code__"):
        return action
    count = action.__code__.co_argcount - (1 if inspect.ismethod(action) else 0)
    if count == 0:
        return lambda *_args: action()
    if count == 1:
        return lambda icon, *_args: action(icon)
    if count == 2:
        return action
    raise ValueError(action)


class MenuItem:
    """One menu item. See the module docstring for what it copies."""

    def __init__(
        self,
        text,
        action,
        checked=None,
        radio=False,
        default=False,
        visible=True,
        enabled=True,
    ):
        if checked is not None and not callable(checked):
            raise ValueError(checked)  # pystray accepts only None or a callable
        self._text = _wrap(text or "")
        self._action = action if isinstance(action, Menu) else _adapt_action(action)
        self._checked = checked if checked is not None else (lambda _item: None)
        self._radio = _wrap(radio)
        self._default = _wrap(default)
        self._visible = _wrap(visible)
        self._enabled = _wrap(enabled)

    def __call__(self, icon):
        if not isinstance(self._action, Menu):
            return self._action(icon, self)
        return None

    def __str__(self):
        return self.text

    @property
    def text(self):
        return self._text(self)

    @property
    def checked(self):
        return self._checked(self)

    @property
    def radio(self):
        return self._radio(self) if self.checked is not None else False

    @property
    def default(self):
        return self._default(self)

    @property
    def visible(self):
        if isinstance(self._action, Menu):
            return self._visible(self) and self._action.visible
        return self._visible(self)

    @property
    def enabled(self):
        return self._enabled(self)

    @property
    def submenu(self):
        return self._action if isinstance(self._action, Menu) else None


class Menu:
    """A menu description. See the module docstring for what it copies."""

    def __init__(self, *items):
        self._items = tuple(items)

    @property
    def items(self):
        if (
            len(self._items) == 1
            and not isinstance(self._items[0], MenuItem)
            and callable(self._items[0])
        ):
            return self._items[0]()
        return self._items

    @property
    def visible(self):
        return bool(self)

    def __call__(self, icon):
        for item in self.items:
            if item.default:
                return item(icon)
        return None

    def __iter__(self):
        return iter(self._visible_items())

    def __bool__(self):
        return len(self._visible_items()) > 0

    def _visible_items(self):
        kept = []
        for item in self.items:
            if not item.visible:
                continue
            if item is self.SEPARATOR and (not kept or kept[-1] is self.SEPARATOR):
                continue
            kept.append(item)
        while kept and kept[-1] is self.SEPARATOR:
            kept.pop()
        return tuple(kept)


Menu.SEPARATOR = MenuItem("- - - -", None)
Menu.SEPARATOR._separator = True


class Icon:
    """A tray icon that shows nothing. See the module docstring."""

    HAS_DEFAULT_ACTION = True
    HAS_MENU = True
    HAS_MENU_RADIO = True
    HAS_NOTIFICATION = True

    def __init__(self, name, icon=None, title=None, menu=None, **kwargs):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False
        self.menu_updates = 0
        self.notifications = []
        self.stopped = False

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title=None):
        self.notifications.append((message, title))

    def stop(self):
        self.stopped = True

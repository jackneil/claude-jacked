"""Error text that can reach a client, and a values-free form of all other errors.

An API response, a WebSocket event, the OAuth callback page and a stored
``last_error`` can reach a browser on a different machine when remote access
is on. The text of an arbitrary exception can contain a path in the home
directory, a host name, or a response body. This module sets one rule for
all of these surfaces:

- An exception with text that is written for the user is a
  :class:`PublicError`. Its ``public_message`` goes to the client.
- All other exceptions go to the client only as a fixed message. The server
  log gets :func:`describe`, which gives the exception type and the errno
  name, and no value.

Use :func:`public_message` at each place that sends an error to a client.
"""

from __future__ import annotations

import errno as _errno
import os


class PublicError(Exception):
    """An exception with a message that is written for the user.

    Raise it (or a subclass) only with text that contains no secret, no
    token, and no text from a different exception. ``str(exc)`` and
    ``exc.public_message`` give the same text.

    >>> PublicError("Run `codex login` first.").public_message
    'Run `codex login` first.'
    >>> str(PublicError("Run `codex login` first."))
    'Run `codex login` first.'
    """

    def __init__(self, message: str = "", *args: object) -> None:
        super().__init__(message, *args)
        self.public_message = str(message)


def public_message(exc: BaseException, fallback: str) -> str:
    """Return the text of ``exc`` that a client can see.

    A :class:`PublicError` gives its own message. All other exceptions give
    ``fallback``.

    >>> public_message(PublicError("Run `codex login` first."), "Failed.")
    'Run `codex login` first.'
    >>> public_message(OSError(13, "Permission denied", "/home/u/x"), "Failed.")
    'Failed.'
    """
    if isinstance(exc, PublicError):
        return exc.public_message
    return fallback


def _errno_name(exc: BaseException) -> str | None:
    code = exc.errno if isinstance(exc, OSError) else None
    if isinstance(code, int) and code in _errno.errorcode:
        return _errno.errorcode[code]
    return None


def describe(exc: BaseException) -> str:
    """Return the exception type and the errno name. The result has no value.

    Use it in a log line or after a fixed message: it identifies the cause
    and does not show a path, a host or a token.

    >>> describe(PermissionError(13, "Permission denied", "/home/u/x"))
    'PermissionError(EACCES)'
    >>> describe(ValueError("some value"))
    'ValueError'
    """
    name = type(exc).__name__
    code = _errno_name(exc)
    return f"{name}({code})" if code else name


def os_error_reason(exc: OSError) -> str:
    """Return a short cause for ``exc``. The result has no path.

    The text comes from the system errno table, not from the exception.

    >>> os_error_reason(PermissionError(13, "Permission denied", "/home/u/x"))
    'Permission denied'
    >>> os_error_reason(OSError("no errno"))
    'a file system error'
    """
    if _errno_name(exc) is None:
        return "a file system error"
    return os.strerror(exc.errno)

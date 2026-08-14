"""Repair ``sys.std*`` under console-less interpreters.

Windows' ``pythonw.exe`` runs with NO console, so CPython sets ``sys.stdin``,
``sys.stdout`` and ``sys.stderr`` to ``None``. Any library that reaches for
them — ``sys.stdout.isatty()`` is the common one — then dies with
``AttributeError: 'NoneType' object has no attribute 'isatty'``, and because
there is no console the traceback goes nowhere at all.

This is not hypothetical: the login autostart launches the tray via
``pythonw.exe -m jacked service start``, and two separate ``isatty()`` calls
(our own first-run prompt, then uvicorn's log formatter) crashed it silently on
every boot. The user saw no tray icon, no error, and no log line.

Patching individual call sites is whack-a-mole — uvicorn's is third-party and
we do not control the next dependency to do the same thing. Instead we give the
interpreter real streams once, before anything else imports, so every consumer
downstream just works.
"""

import io
import sys
from pathlib import Path

# Keep the fallback log from growing without bound across reboots. Small on
# purpose: this file should be empty in the happy path, so anything in it is a
# crash worth reading.
_MAX_LOG_BYTES = 2_000_000


class _NullStream(io.TextIOBase):
    """A no-op stream that is honestly not a terminal.

    Deliberately used instead of a raw ``os.devnull`` handle: on Windows, NUL
    is a character device and ``open(os.devnull).isatty()`` returns **True**,
    which would tell rich and uvicorn to emit ANSI colour codes for a terminal
    that does not exist. A console-less process is not a tty, and everything
    branching on ``isatty()`` should see that.
    """

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def read(self, size=-1) -> str:
        return ""

    def readline(self, size=-1) -> str:
        return ""

    def write(self, s) -> int:
        return len(s)

    def flush(self) -> None:
        pass


def _headless_log_path() -> Path:
    """Where console-less stdout/stderr get parked."""
    return Path.home() / ".claude" / "jacked-headless.log"


def _open_log():
    """Open the headless log for append, rotating if it got large.

    Falls back to a discarding sink if the log cannot be opened (read-only
    home, permissions, a directory in the way, an unresolvable home dir).
    Losing output is bad; crashing the process we are trying to rescue is
    worse, so this never raises.
    """
    try:
        # Inside the try on purpose: Path.home() raises RuntimeError when the
        # home directory cannot be resolved, which is exactly the kind of
        # broken environment this function exists to survive.
        path = _headless_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w"
        try:
            if path.stat().st_size < _MAX_LOG_BYTES:
                mode = "a"
        except OSError:
            pass  # missing or unstattable — "w" is right either way
        return open(path, mode, encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        return _NullStream()


def ensure_std_streams() -> list[str]:
    """Give ``sys.stdin/stdout/stderr`` real objects when they are ``None``.

    Safe to call more than once and from any entry point. Returns the names of
    the streams that were actually replaced, which is what the tests assert on
    and what makes "did this fire?" answerable in the field.

    Only ``None`` streams are touched. A live console, a pytest capture object,
    or a caller's deliberate redirect is left exactly as it was.
    """
    repaired: list[str] = []

    if sys.stdin is None:
        sys.stdin = _NullStream()
        repaired.append("stdin")

    if sys.stdout is None or sys.stderr is None:
        log = _open_log()
        if sys.stdout is None:
            sys.stdout = log
            repaired.append("stdout")
        if sys.stderr is None:
            # Deliberately the same handle: interleaved output stays in
            # chronological order, and one file is one place to look.
            sys.stderr = log
            repaired.append("stderr")

    return repaired


__all__ = ["ensure_std_streams"]

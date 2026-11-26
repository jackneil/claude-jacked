"""The program that the preflight probe runs inside the service runtime.

``jacked.service.preflight_probe`` runs this code through the delivered
launcher and reads its marker lines. The code is generated from
``PROBE_STEPS`` and the marker constants, so the parser and the program
cannot drift apart.
"""

from __future__ import annotations

#: The probe steps, in the order that the probe runs them.
PROBE_STEPS = (
    "asyncio_event_loop",
    "socket",
    "ssl",
    "tempfile",
    "tray_import",
    "tray_backend",
    "cli_import",
    "api_import",
)
#: A failed step exits with this code plus its index. The exit code is the
#: verdict, because a windowless pythonw.exe can lose its stdout. Codes 40 to
#: 63 are free: Python uses 1 and 2, sysexits starts at 64 (EX_TEMPFAIL, 75,
#: tells a supervisor to restart), and shells use 126 and higher.
PROBE_EXIT_BASE = 40

STEP_MARK = "jacked-probe-step:"
FAIL_MARK = "jacked-probe-fail:"
OK_MARK = "jacked-probe-ok"
NOTE_MARK = "jacked-probe-note:"

_PRELUDE = r'''
import os
import sys


# The one-line wrapper (preflight_probe.probe_python_argv) points stdout
# and stderr at the report file before this code runs, with no buffer. So a
# print reaches the caller on every OS, also after os._exit or a kill.


class ProbeRefusal(Exception):
    """A check of the real start failed. args[0] is a fixed token."""


def _say(line):
    # With no report file, pythonw.exe can start with no usable stdout. The
    # exit code carries the verdict, so a lost marker line never changes it.
    try:
        print(line, flush=True)
    except Exception:
        pass


def _detail(exc):
    detail = type(exc).__name__
    if isinstance(exc, ProbeRefusal):
        return detail + " " + str(exc.args[0])
    if isinstance(exc, ImportError) and exc.name:
        detail += " module " + exc.name
    winerror = getattr(exc, "winerror", None)
    number = getattr(exc, "errno", None)
    if isinstance(winerror, int):
        detail += " winerror " + str(winerror)
    elif isinstance(number, int):
        detail += " errno " + str(number)
    return detail


def _step(code, name, action):
    _say(STEP_MARK + name)
    try:
        action()
    except BaseException as exc:
        import traceback

        _say(FAIL_MARK + name + ":" + _detail(exc))
        try:
            sys.stderr.write(traceback.format_exc())
        except Exception:
            pass
        sys.exit(code)


def asyncio_event_loop():
    import asyncio

    asyncio.new_event_loop().close()


def socket():
    import socket

    socket.socket().close()


def ssl():
    import ssl

    ssl.create_default_context()


def tempfile():
    import tempfile

    here = os.path.normcase(os.path.abspath(os.getcwd()))
    temp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    if temp == here:
        raise ProbeRefusal("temp_is_cwd")


def tray_import():
    import jacked.service.tray  # noqa: F401


def tray_backend():
    # The checks of ServiceRunner.run, in its order.
    import jacked.service.tray as tray

    if tray.select_menubar_backend(sys.platform, tray._mac_menubar_available()) != "mac":
        if not tray._TRAY_AVAILABLE and tray._PYSTRAY_LOAD_FAILURE is None:
            # A missing package (pystray or Pillow): a broken build.
            raise ProbeRefusal("tray_package_missing")
        if not tray._TRAY_AVAILABLE:
            # pystray is installed but cannot load here (no display, a missing
            # typelib). That is this host, not the build: the old build fails
            # the same way. Report it, do not refuse.
            kind = tray._PYSTRAY_LOAD_FAILURE.split(":", 1)[0]
            _say(
                NOTE_MARK + "The tray cannot run on this host (" + kind + "), so the "
                "jacked service cannot run here. See the README Linux tray notes: "
                + tray.LINUX_TRAY_NOTES_POINTER.rsplit(" ", 1)[-1]
            )
    if not tray._UVICORN_AVAILABLE:
        raise ProbeRefusal("uvicorn_missing")


def cli_import():
    import jacked.cli  # noqa: F401


def api_import():
    import jacked.api.main  # noqa: F401
'''


def _probe_code() -> str:
    """Build the probe code from the step and marker constants."""
    lines = [
        f"STEP_MARK = {STEP_MARK!r}",
        f"FAIL_MARK = {FAIL_MARK!r}",
        f"NOTE_MARK = {NOTE_MARK!r}",
        _PRELUDE,
    ]
    for index, step in enumerate(PROBE_STEPS):
        lines.append(f"_step({PROBE_EXIT_BASE + index}, {step!r}, {step})")
    lines.append(f"_say({OK_MARK!r})")
    return "\n".join(lines) + "\n"


PROBE_CODE = _probe_code()

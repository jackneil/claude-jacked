"""Shared fixtures for every unit test (tests/unit and its subdirectories)."""

import pytest


@pytest.fixture(autouse=True)
def _stop_signal_handlers_unchanged():
    """Fail a test that leaves a process-wide stop-signal handler changed.

    ``ServiceRunner.run`` routes the stop signals to its own runner while it
    runs and must put the previous handlers back when it returns (#138i).
    ``jacked upgrade`` turns SIGHUP and SIGTERM into its own interrupt for
    the length of the transaction and must also put them back. A handler
    left in place binds Ctrl+C (or a closed terminal) to a dead runner for
    every later test in this process, so Ctrl+C no longer raises
    KeyboardInterrupt.

    The fixture lives here, not in tests/unit/service, so it covers every
    unit test, including tests/unit/test_upgrade_command.py.

    The check uses the real ``signal`` functions captured before the test, so
    a test that patches ``signal.signal`` or ``signal.getsignal`` cannot hide
    a leak. A changed handler is put back before the failure is reported, so
    one leak fails one test and not every test after it.
    """
    import signal

    getsignal, setsignal = signal.getsignal, signal.signal
    names = ["SIGINT", "SIGTERM"]
    for optional in ("SIGHUP", "SIGBREAK"):  # POSIX only, Windows only
        if hasattr(signal, optional):
            names.append(optional)
    before = {name: getsignal(getattr(signal, name)) for name in names}

    yield

    changed = []
    for name, handler in before.items():
        if getsignal(getattr(signal, name)) is not handler:
            changed.append(name)
            setsignal(getattr(signal, name), handler)
    assert not changed, (
        f"the test left these stop-signal handlers changed: {changed}"
    )

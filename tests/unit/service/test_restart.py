"""Tests for the restart-hook registry (jacked/service/restart.py)."""

import logging

import pytest

from jacked.service import restart as restart_mod


@pytest.fixture(autouse=True)
def _clear_handler():
    restart_mod.set_restart_handler(None)
    yield
    restart_mod.set_restart_handler(None)


def test_set_get_round_trip():
    def _h():
        pass

    restart_mod.set_restart_handler(_h)
    assert restart_mod.get_restart_handler() is _h
    restart_mod.set_restart_handler(None)
    assert restart_mod.get_restart_handler() is None


def test_restart_calls_registered_handler():
    called = []
    restart_mod.set_restart_handler(lambda: called.append(True))
    restart_mod.restart_service_now()
    assert called == [True]


def test_handler_systemexit_is_caught_and_logged(caplog):
    """A SystemExit from inside the handler (e.g. a bind conflict deep in the
    tray restart) must be swallowed and logged LOUDLY, never propagated — it
    runs on a daemon thread where a raised SystemExit would vanish silently."""
    def _boom():
        raise SystemExit(1)

    restart_mod.set_restart_handler(_boom)
    with caplog.at_level(logging.ERROR, logger="jacked.service.restart"):
        restart_mod.restart_service_now()  # must not raise

    assert any(
        "Restart handler raised" in r.getMessage() for r in caplog.records
    ), "expected a loud error log when the handler raises"


def test_handler_generic_exception_is_caught(caplog):
    def _boom():
        raise RuntimeError("kaboom")

    restart_mod.set_restart_handler(_boom)
    with caplog.at_level(logging.ERROR, logger="jacked.service.restart"):
        restart_mod.restart_service_now()  # must not raise
    assert any("Restart handler raised" in r.getMessage() for r in caplog.records)


def test_no_handler_calls_execv(monkeypatch):
    """With no handler registered, restart replaces the process via os.execv."""
    restart_mod.set_restart_handler(None)
    calls = []
    monkeypatch.setattr(restart_mod.os, "execv", lambda *a: calls.append(a))
    restart_mod.restart_service_now()
    assert len(calls) == 1
    argv0, argv = calls[0]
    prog, expected_argv = restart_mod._exec_target()

    assert argv0 == prog
    assert argv == expected_argv


def test_execv_failure_is_logged_loudly_and_reraised(monkeypatch, caplog):
    """execv only returns on failure. When it does, we must log LOUDLY (like the
    handler path) so the tray-log reader sees why the restart stranded, and
    re-raise rather than continue on a half-applied restart."""
    restart_mod.set_restart_handler(None)

    def _boom(*_a):
        raise OSError("exec format error")

    monkeypatch.setattr(restart_mod.os, "execv", _boom)
    with caplog.at_level(logging.ERROR, logger="jacked.service.restart"):
        with pytest.raises(OSError):
            restart_mod.restart_service_now()
    assert any("execv restart failed" in r.getMessage() for r in caplog.records)


def test_exec_target_console_script(monkeypatch, tmp_path):
    """An executable argv[0] (console-script launch) is re-exec'd verbatim."""
    import sys

    script = tmp_path / "jacked"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(script), "webux", "--port", "8399"])
    prog, argv = restart_mod._exec_target()
    assert prog == str(script)
    assert argv == [str(script), "webux", "--port", "8399"]


def test_exec_target_python_dash_m(monkeypatch, tmp_path):
    """`python -m jacked` launches (argv[0] = __main__.py, no exec bit) re-exec
    through the interpreter; execv'ing __main__.py directly raises OSError and
    strands the dashboard on the old bind."""
    import sys

    main_py = tmp_path / "__main__.py"
    main_py.write_text("")
    monkeypatch.setattr(sys, "argv", [str(main_py), "webux", "--port", "8399"])
    prog, argv = restart_mod._exec_target()
    assert prog == sys.executable
    assert argv == [sys.executable, "-m", "jacked", "webux", "--port", "8399"]


def test_exec_target_nonexecutable_argv0(monkeypatch, tmp_path):
    """A non-executable argv[0] of any name falls back to the interpreter."""
    import sys

    blob = tmp_path / "jacked"
    blob.write_text("")
    blob.chmod(0o644)
    monkeypatch.setattr(sys, "argv", [str(blob), "service", "start"])
    prog, argv = restart_mod._exec_target()
    assert prog == sys.executable
    assert argv == [sys.executable, "-m", "jacked", "service", "start"]

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
    import sys

    assert argv0 == sys.argv[0]
    assert argv == sys.argv

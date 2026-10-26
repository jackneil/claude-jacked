"""Repo-root pytest configuration: isolation for doctests.

``tests/conftest.py`` applies only to items under ``tests/``. A doctest
collected from ``jacked/`` with ``--doctest-modules`` gets none of its
tripwires, so on 2026-10-26 the ``refresh_all_expiring_tokens`` doctest
opened the developer's real ``~/.claude/jacked.db`` and ran a migration on
it. This root conftest reaches every collected item, and its fixture gives
each doctest a throwaway home before the doctest runs.

The fixture is function-scoped and acts on doctest items only. A
session-scoped HOME move would also move HOME for every unit test, and the
per-test default-database isolation in ``tests/conftest.py`` would then
collapse into one database shared by the whole session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# The developer's real home, captured before any fixture can move HOME.
REAL_HOME = Path.home()


def _is_doctest(node) -> bool:
    from _pytest.doctest import DoctestItem

    return isinstance(node, DoctestItem)


@pytest.fixture(autouse=True)
def _isolate_doctest_home(request, tmp_path_factory, monkeypatch):
    """Give every doctest a temporary HOME, database, and blocked Keychain.

    - HOME and USERPROFILE point at a new temporary directory, so
      ``~/.claude/jacked.db`` and ``~/.claude/.credentials.json`` resolve
      there.
    - ``jacked.web.database._default_db_path`` resolves inside that home.
    - The real ``security`` tool (the macOS Keychain) raises instead of
      running.
    - A request to a real Anthropic host raises instead of leaving the
      machine.
    """
    if not _is_doctest(request.node):
        yield
        return

    home = tmp_path_factory.mktemp("doctest-home")
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    from jacked.web import database

    monkeypatch.setattr(
        database, "_default_db_path", lambda: str(home / ".claude" / "jacked.db")
    )

    import subprocess

    from jacked.credentials import macos_store

    backend = macos_store.SecurityCliBackend
    real_invoke = backend._invoke

    def guarded_invoke(self, args, *, is_interactive, stdin=None):
        if self._run is subprocess.run and self._tool == macos_store.SECURITY_TOOL:
            raise RuntimeError("doctest tripwire: real Keychain access via security")
        return real_invoke(self, args, is_interactive=is_interactive, stdin=stdin)

    monkeypatch.setattr(backend, "_invoke", guarded_invoke)

    import httpx

    real_get = httpx.get
    real_async_send = httpx.AsyncClient.send
    real_send = httpx.Client.send

    def _guard(url) -> None:
        host = httpx.URL(str(url)).host or ""
        if host.endswith(("anthropic.com", "claude.ai", "claude.com")):
            raise RuntimeError(f"doctest tripwire: real request to {host}")

    def guarded_get(url, *args, **kwargs):
        _guard(url)
        return real_get(url, *args, **kwargs)

    async def guarded_async_send(self, req, *args, **kwargs):
        _guard(req.url)
        return await real_async_send(self, req, *args, **kwargs)

    def guarded_send(self, req, *args, **kwargs):
        _guard(req.url)
        return real_send(self, req, *args, **kwargs)

    monkeypatch.setattr(httpx, "get", guarded_get)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)
    monkeypatch.setattr(httpx.Client, "send", guarded_send)

    # No doctest starts a service, drives a supervisor, or runs a jacked
    # install (the tests/-only tripwire does not reach doctest items).
    real_popen = subprocess.Popen
    real_run = subprocess.run
    supervisors = ("launchctl", "systemctl", "schtasks", "schtasks.exe", "security")

    def _refuse(args) -> None:
        text = " ".join(str(a) for a in args) if isinstance(args, (list, tuple)) else str(args)
        head = str(args[0]) if isinstance(args, (list, tuple)) and args else text.split(" ", 1)[0]
        first = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if first in supervisors:
            raise RuntimeError("doctest tripwire: real supervisor/keychain call: " + first)
        if "jacked" in text and any(
            f" {verb}" in text for verb in ("service", " install")
        ):
            raise RuntimeError("doctest tripwire: real jacked lifecycle command")

    class _GuardedPopen(real_popen):
        def __init__(self, args, *popen_args, **popen_kwargs):
            _refuse(args)
            super().__init__(args, *popen_args, **popen_kwargs)

    def _guarded_run(args, *run_args, **run_kwargs):
        _refuse(args)
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    monkeypatch.setattr(subprocess, "run", _guarded_run)
    yield

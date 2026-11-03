"""Shared fixtures for jacked tests."""

import atexit
import errno
import inspect
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from unittest.mock import patch

# The developer's real home, captured before anything can move HOME.
REAL_HOME = Path.home()

# Isolate HOME (and USERPROFILE on Windows) BEFORE the first jacked import.
# Eleven modules compute a path from ``Path.home()`` at import time
# (launch.ACCOUNTS_DIR, service.CLAUDE_DIR, cli.DB_PATH, version_check,
# install_manifest, ...). The per-test ``_isolate_home`` fixture runs AFTER
# the autouse fixtures that import those modules (pytest sets fixtures up in
# name order), so without this the constants froze to the real home and
# tests/unit/test_dual_token.py wrote test tokens into the real
# ~/.claude/accounts/1/ on every run (found 2026-11-02).
_IMPORT_HOME = Path(tempfile.mkdtemp(prefix="jacked-tests-home-"))
(_IMPORT_HOME / ".claude").mkdir()
os.environ["HOME"] = str(_IMPORT_HOME)
os.environ["USERPROFILE"] = str(_IMPORT_HOME)
atexit.register(shutil.rmtree, _IMPORT_HOME, True)

# The developer's Claude Code session sets variables that move the files
# Claude Code loads (CLAUDE_CONFIG_DIR, CLAUDE_CODE_PROJECT_DIR_NAME,
# CLAUDE_PROJECTS_DIR), turn auto memory off, or mark a subagent session. The
# load-budget and session-start code reads them, so a test that inherits them
# reads the developer's real settings and CLAUDE.md, and a byte-identical test
# fails only on that machine (found 2026-11-03). A test that needs one sets it.
CLAUDE_SESSION_ENV = (
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROJECT_DIR_NAME",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_PROJECTS_DIR",
    "CLAUDE_CODE_AGENT_TYPE",
    "CLAUDE_CODE_PARENT_SESSION_ID",
)
for _var in CLAUDE_SESSION_ENV:
    os.environ.pop(_var, None)

# Any jacked.db query that jacked code makes on an event-loop thread raises
# in tests (jacked/web/database.py, #162): a blocking query on the service
# loop freezes every route, whatever form the call takes.
os.environ.setdefault("JACKED_DB_LOOP_STRICT", "1")

from jacked.credentials.models import (  # noqa: E402
    CredentialIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchOutcome,
    SwitchResult,
)

# The real subprocess.run, captured before any test module or fixture can
# rebind the module attribute. The Keychain tripwire compares against THIS
# object: comparing against ``subprocess.run`` at call time went silent the
# moment anything patched it, and tests then ran the real ``security`` tool.
_REAL_SUBPROCESS_RUN = subprocess.run

# Import the Keychain backend NOW, so its ``run=subprocess.run`` default binds
# to the real callable. A first import from inside a test would bind it to
# that test's rebound ``subprocess.run`` for the rest of the process, and both
# Keychain guards below would go silent again.
from jacked.credentials import macos_store as _macos_store  # noqa: E402

assert (
    inspect.signature(_macos_store.SecurityCliBackend.__init__).parameters["run"].default
    is _REAL_SUBPROCESS_RUN
), "the Keychain backend default bound to a patched subprocess.run"



# --------------------------------------------------------------------------- #
# Tripwire: nothing may replace the pytest process.
#
# A background thread that outlives its test (the remote-access restart thread
# sleeps ~1.5 s, then calls os.execv when no restart handler is registered) can
# exec AFTER monkeypatch has restored the real os.execv. The exec'd command then
# exits on its own, often 0, so a CI job reported "pass" with the suite stopped
# at 8% and no summary line (claude-jacked #190pr, macOS run 40802422355). The
# guard is installed for the whole session, between tests too; a test that
# patches os.execv itself still sees its own patch. An attempt raises OSError
# (so the caller's own failure path runs) and fails the session at the end.
# --------------------------------------------------------------------------- #

EXEC_NAMES = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")
_EXEC_ATTEMPTS: list[str] = []


def _exec_guard(name):
    def _refuse(*args, **kwargs):
        import errno
        import threading

        _EXEC_ATTEMPTS.append(f"os.{name} from thread {threading.current_thread().name!r}")
        raise OSError(errno.EPERM, f"test tripwire: os.{name} would replace the pytest process")

    _refuse.__name__ = f"guarded_{name}"
    return _refuse


def pytest_configure(config):
    import os

    for name in EXEC_NAMES:
        if hasattr(os, name):
            setattr(os, name, _exec_guard(name))


def pytest_sessionfinish(session, exitstatus):
    if _EXEC_ATTEMPTS:
        session.config.get_terminal_writer().line(
            "\nTRIPWIRE: something tried to replace the pytest process: "
            + "; ".join(_EXEC_ATTEMPTS),
            red=True,
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def exec_attempts():
    """The session's recorded exec attempts (a test that triggers one on
    purpose removes its own entries so the session stays green)."""
    return _EXEC_ATTEMPTS


@pytest.fixture(autouse=True)
def _no_pypi_lookup_in_upgrade(monkeypatch):
    """`jacked upgrade` reads its target version from PyPI; no test may reach
    the network for it. A test that needs a target patches it explicitly."""
    import jacked.cli

    monkeypatch.setattr(jacked.cli, "_upgrade_target_version", lambda: None)


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path_factory):
    """Give every test a temporary HOME (and USERPROFILE on Windows).

    Code under test resolves the live Claude Code credentials, the Codex
    login, and jacked's own files from the home directory. Before this
    fixture, tests that did not mock the live store read the developer's
    REAL ``~/.claude/.credentials.json``: on 2026-10-27 three I2 tests
    passed on Jack's Mac only because that file existed, and failed on every
    CI runner where it does not. A test's result must never depend on (or
    reach) the developer's real credentials. A test that needs a home of its
    own still sets HOME itself; ``REAL_HOME`` keeps the real path for the
    tripwires below.
    """
    home = tmp_path_factory.mktemp("home")
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("JACKED_HOME", raising=False)
    for var in CLAUDE_SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    # The load-budget walk reads CLAUDE.md files up to the filesystem root.
    # It stops at the pytest base folder here, so a real over-budget file
    # above the pytest tree (a developer HOME, a Windows temp folder) cannot
    # change a test result. A test of the walk itself sets the stop itself.
    from jacked.memory import load_budget

    monkeypatch.setattr(load_budget, "ANCESTOR_STOP", tmp_path_factory.getbasetemp())
    yield home


@pytest.fixture(autouse=True)
def _isolate_accounts_dir(monkeypatch, _isolate_home):
    """Point the launch's per-account tree at this test's HOME.

    ``jacked.launch.ACCOUNTS_DIR`` is an import-time constant; the
    session-wide import HOME above keeps it off the real machine, and this
    puts it under the per-test home so a test's files are its own.
    """
    import jacked.launch

    monkeypatch.setattr(
        jacked.launch, "ACCOUNTS_DIR", _isolate_home / ".claude" / "accounts"
    )


@pytest.fixture(autouse=True)
def _tripwire_real_credential_files(monkeypatch):
    """Tripwire: no test may write a file under the real ~/.claude.

    Covers the one replace that every jacked file write goes through
    (``jacked.atomic_io._replace_once``: the per-account credential and
    config files, the settings files, the vault) and
    ``FileCredentialStore.write`` (the global file authority and mirror),
    which refuses before it stages anything. The guard sits on the private
    primitive, not on a public name, so a module that binds
    ``replace_with_retry`` or ``atomic_write_text`` at import is covered too.
    """
    from jacked import atomic_io
    from jacked.credentials import file_store

    real_replace_once = atomic_io._replace_once
    real_write = file_store.FileCredentialStore.write

    def guarded_replace_once(src, dst, **kwargs):
        if _under_real_claude_config(dst):
            raise RuntimeError("test tripwire: file write under the real ~/.claude")
        return real_replace_once(src, dst, **kwargs)

    def guarded_write(self, *args, **kwargs):
        target = getattr(self, "_path", None) or getattr(self, "path", None)
        if target is not None and _under_real_claude_dir(target):
            raise RuntimeError("test tripwire: credential store write under the real ~/.claude")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(atomic_io, "_replace_once", guarded_replace_once)
    monkeypatch.setattr(file_store.FileCredentialStore, "write", guarded_write)


@pytest.fixture(autouse=True)
def _reset_live_credential_cache():
    """The live-credential read cache (30 s TTL, keyed by account id) never
    expires inside a test run, so one test's cached payload leaked into the
    next test's reconcile. Every test starts and ends with it empty."""
    from jacked.api.credential_helpers import invalidate_live_cred_cache

    invalidate_live_cred_cache()
    yield
    invalidate_live_cred_cache()


@pytest.fixture(autouse=True)
def _isolate_default_db_path(monkeypatch, tmp_path_factory):
    """Tripwire: no test may open the developer's real ~/.claude/jacked.db.

    ``Database()`` with no path resolves ``jacked.web.database._default_db_path``.
    On 2026-10-10 two tests ran the real FastAPI lifespan, which built
    ``Database()`` against the real home: it migrated the real database and
    its background loops sent a real window-keeper ping. While HOME is still
    the real home, the default path now resolves to a per-test temporary
    file. A test that moves HOME (or USERPROFILE) keeps the production rule,
    HOME/.claude/jacked.db, because that path is already isolated. A test
    that must check the real resolver calls ``_default_db_path.original``.
    """
    from jacked.web import database

    original = database._default_db_path
    holder: dict = {}

    def _test_default_db_path() -> str:
        if Path.home() != REAL_HOME:
            return original()
        if "path" not in holder:
            claude_dir = tmp_path_factory.mktemp("jacked-home") / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            holder["path"] = str(claude_dir / "jacked.db")
        return holder["path"]

    _test_default_db_path.original = original  # type: ignore[attr-defined]
    monkeypatch.setattr(database, "_default_db_path", _test_default_db_path)


def _under_real_claude_config(path) -> bool:
    """True when ``path`` is the real Claude config, its lock, or its legacy file."""
    target = Path(path).absolute()
    guarded = {
        REAL_HOME / ".claude.json",
        REAL_HOME / ".claude.json.lock",
        REAL_HOME / ".claude" / ".config.json",
        REAL_HOME / ".claude" / ".config.json.lock",
    }
    if target in guarded:
        return True
    if target.parent == REAL_HOME and target.name.startswith(".claude_json_tmp_"):
        return True
    return _under_real_claude_dir(target)


def _under_real_claude_dir(path) -> bool:
    """True when ``path`` is inside the real ``~/.claude`` (any depth)."""
    target = Path(path).absolute()
    real = (REAL_HOME / ".claude").absolute()
    return target == real or real in target.parents


@pytest.fixture(autouse=True)
def _tripwire_real_claude_config(monkeypatch):
    """Tripwire: no test may write or lock the developer's real Claude config."""
    from jacked.api import claude_config_store

    real_write = claude_config_store._atomic_write_json
    real_acquire = claude_config_store._acquire_lock

    def guarded_write(config_path, data, **kwargs):
        if _under_real_claude_config(config_path):
            raise RuntimeError(f"test tripwire: write to the real {config_path.name}")
        return real_write(config_path, data, **kwargs)

    def guarded_acquire(lock_path, timeout):
        if _under_real_claude_config(lock_path):
            raise RuntimeError(f"test tripwire: lock on the real {lock_path.name}")
        return real_acquire(lock_path, timeout)

    monkeypatch.setattr(claude_config_store, "_atomic_write_json", guarded_write)
    monkeypatch.setattr(claude_config_store, "_acquire_lock", guarded_acquire)


class _InertIdentityWatcher:
    """Stands in for the service watcher, which reads the real authority."""

    def submit(self, *args, **kwargs):
        return False

    def run(self, *args, **kwargs):
        return None

    def drain_evidence(self):
        return ()

    def wait_idle(self, timeout=None):
        return True

    def status(self):
        return {"disabled": False, "accounts": {}}


@pytest.fixture(autouse=True)
def _tripwire_real_identity_watcher(monkeypatch):
    """Tripwire: the service identity watcher never runs under test.

    The real watcher reads the real credential authority (the macOS Keychain)
    and sends its token to the profile endpoint. A test that wants a watcher
    builds an ``IdentityWatcher`` with fakes, or patches this attribute.
    """
    from jacked.api import session_observer

    monkeypatch.setattr(session_observer, "IDENTITY_WATCHER", _InertIdentityWatcher())


@pytest.fixture(autouse=True)
def _tripwire_real_keychain(monkeypatch):
    """Tripwire: no test may run the real ``security`` tool against the Keychain.

    The breach is recorded AND raised. Raising alone is not enough: the store
    runs its backend on a worker thread behind ``except Exception`` and the
    credential helpers catch ``RuntimeError``, so a swallowed tripwire would
    let the test pass on the file fallback. The teardown assertion fails the
    test no matter which layer swallowed the exception.
    """
    from jacked.credentials import macos_store

    cls = macos_store.SecurityCliBackend
    real_invoke = cls._invoke
    breaches: list[str] = []

    def guarded_invoke(self, args, *, is_interactive, stdin=None):
        if self._run is _REAL_SUBPROCESS_RUN and self._tool == macos_store.SECURITY_TOOL:
            # Record the verb only (never the arguments: they can name the
            # Keychain item and its account).
            breaches.append(str(args[0]) if args else "<no-args>")
            raise RuntimeError("test tripwire: real Keychain access via security")
        return real_invoke(self, args, is_interactive=is_interactive, stdin=stdin)

    monkeypatch.setattr(cls, "_invoke", guarded_invoke)
    yield
    assert not breaches, (
        f"test reached the real Keychain through the security tool: {breaches}"
    )


@pytest.fixture(autouse=True)
def _isolate_keychain_reads(monkeypatch):
    """A store wired to the real ``security`` tool answers MISSING in tests.

    That is what a CI runner's empty Keychain answers, so every test sees the
    same thing on every machine, and no test can read the developer's real
    Claude Code credentials (13 tests did, unnoticed, while the tripwire was
    dead). A store built on an injected fake backend keeps its behavior, so
    the store's own tests still run. Writes are not stubbed: a write through
    the real backend still trips ``_tripwire_real_keychain``.
    """
    from jacked.credentials import macos_store
    from jacked.credentials.models import StoreReadResult, StoreStatus

    cls = macos_store.MacOSCredentialStore
    real_read = cls.read

    def isolated_read(self):
        backend = getattr(self, "_backend", None)
        if (
            isinstance(backend, macos_store.SecurityCliBackend)
            and getattr(backend, "_run", None) is _REAL_SUBPROCESS_RUN
        ):
            return StoreReadResult(
                StoreStatus.MISSING, reason="test isolation: Keychain not consulted"
            )
        return real_read(self)

    monkeypatch.setattr(cls, "read", isolated_read)


@pytest.fixture(autouse=True)
def _tripwire_real_anthropic_network(monkeypatch):
    """Tripwire: no test may send a request to a real Anthropic endpoint."""
    import httpx

    real_get = httpx.get
    real_async_send = httpx.AsyncClient.send

    def _guard(url) -> None:
        host = httpx.URL(str(url)).host or ""
        if host.endswith(("anthropic.com", "claude.ai", "claude.com")):
            raise RuntimeError(f"test tripwire: real request to {host}")

    def guarded_get(url, *args, **kwargs):
        _guard(url)
        return real_get(url, *args, **kwargs)

    async def guarded_async_send(self, request, *args, **kwargs):
        transport = getattr(self, "_transport", None)
        if isinstance(transport, httpx.AsyncHTTPTransport):
            _guard(request.url)
        return await real_async_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx, "get", guarded_get)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_async_send)


@pytest.fixture
def quiet_lifespan(monkeypatch, tmp_path):
    """Let a test run the real app lifespan with no real background work.

    Every coroutine the lifespan hands to ``asyncio.create_task`` is replaced
    by a no-op coroutine, so no sweep, poll, refresh or keeper loop runs and
    nothing goes out on the network. The loop names come from the lifespan
    source (AST), so a loop added later is stubbed too; a name that cannot
    be resolved fails the test instead of running for real. HOME moves to a
    temporary directory, so the database and the server log stay off the
    real ~/.claude. Returns the sorted names that were stubbed.
    """
    import ast
    import importlib
    import inspect

    from jacked.api import main as api_main

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JACKED_HOST", "127.0.0.1")
    monkeypatch.setenv("JACKED_PORT", "8321")

    tree = ast.parse(inspect.getsource(api_main.lifespan))
    local_imports: dict[str, str] = {}
    loop_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local_imports[alias.asname or alias.name] = node.module
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_task"
            and node.args
            and isinstance(node.args[0], ast.Call)
            and isinstance(node.args[0].func, ast.Name)
        ):
            loop_names.add(node.args[0].func.id)
    assert loop_names, "found no create_task(...) calls in lifespan()"

    async def _idle(*_args, **_kwargs):
        return None

    for name in loop_names:
        if name in local_imports:
            module = importlib.import_module(local_imports[name])
        elif hasattr(api_main, name):
            module = api_main
        else:
            raise AssertionError(f"cannot stub lifespan loop {name!r}")
        monkeypatch.setattr(module, name, _idle)
    return sorted(loop_names)


@pytest.fixture(autouse=True)
def _process_tree_through_subprocess_run(request):
    """jacked.service.updater.run_process_tree spawns with Popen. Tests
    script the upgrade's child processes through a mocked subprocess.run (and
    often mock Popen too, whose pid coerces to 1), so route it there: every
    child stays visible to the scripted run, and the call-order assertions
    keep meaning what they say (#178i). Tests of the real process-tree logic
    import run_process_tree / run_tree directly, or opt out with
    ``@pytest.mark.real_process_tree``."""
    if request.node.get_closest_marker("real_process_tree"):
        yield
        return
    import subprocess as _subprocess

    def _via_run(argv, *, timeout, log_fh=None, relay=False):
        return _subprocess.run(argv, stdout=log_fh, stderr=log_fh, check=False,
                               timeout=timeout).returncode

    with patch("jacked.service.updater.run_process_tree", side_effect=_via_run):
        yield


@pytest.fixture(autouse=True)
def _no_real_service_processes(request, monkeypatch):
    """Tripwire: a test must never start the real jacked service or drive a
    real supervisor.

    On 2026-09-04 a `service restart` test spawned a detached tray from the
    repo venv against the developer's real ~/.claude and another wrote a
    launchd transition lock into the real ~/Library/LaunchAgents. Tests that
    deliberately exec a sandboxed stub opt out with ``@pytest.mark.real_process``.
    """
    import subprocess

    if request.node.get_closest_marker("real_process"):
        return
    real_popen = subprocess.Popen
    real_run = subprocess.run
    supervisors = ("launchctl", "systemctl", "schtasks", "schtasks.exe")

    def _text(args):
        if isinstance(args, (list, tuple)):
            return " ".join(str(part) for part in args)
        return str(args)

    def _refuse(args) -> None:
        """One predicate for every subprocess entry point.

        A supervisor binary anywhere in argv[0], or a jacked service
        lifecycle command (start/restart/preflight/recover/install) reached
        through any launcher, is refused. subprocess.call/check_call/
        check_output route through Popen, so guarding run and Popen covers
        them too.
        """
        text = _text(args)
        head = str(args[0]) if isinstance(args, (list, tuple)) and args else text.split(" ", 1)[0]
        first = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if first in supervisors:
            raise RuntimeError("test tried to drive a real supervisor: " + text)
        # The macOS Keychain tool, reached around the backend's identity
        # guard (a wrapper around the real callable, or a direct call). Only
        # the head is reported: argv can carry the Keychain account name.
        if first == "security":
            raise RuntimeError("test tried to run the real security tool: " + head)
        # The Task Scheduler is also read through its COM API from PowerShell
        # (jacked.service.supervisors._task_xml.task_query_command, #165i).
        if first in {"powershell", "powershell.exe", "pwsh", "pwsh.exe"} and (
            "Schedule.Service" in text
        ):
            raise RuntimeError("test tried to drive a real supervisor: " + text)
        padded = f" {text} "
        after_service = " " + padded.split(" service ", 1)[1] if " service " in padded else ""
        if "jacked" in text and any(
            f" {verb} " in after_service
            for verb in ("start", "restart", "preflight", "recover", "install")
        ):
            raise RuntimeError("test tried to spawn a real jacked service: " + text)
        # `jacked install` rewrites the real ~/.claude/settings.json.
        if "jacked" in text and re.search(r"jacked(?:\.exe)?\"? install(?: |$)", padded):
            raise RuntimeError("test tried to run a real jacked install: " + text)

    class _GuardedPopen(real_popen):
        def __init__(self, args, *popen_args, **popen_kwargs):
            _refuse(args)
            super().__init__(args, *popen_args, **popen_kwargs)

    def _guarded_run(args, *run_args, **run_kwargs):
        _refuse(args)
        return real_run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    monkeypatch.setattr(subprocess, "run", _guarded_run)


@pytest.fixture(autouse=True)
def _skill_listing_stays_off_the_machine(monkeypatch):
    """The skill-listing report reads managed settings from a system path and
    runs ``claude --version``. Tests get neither, plus none of the Claude Code
    variables that change the report (the suite may run inside Claude Code)."""
    from jacked import skill_listing

    monkeypatch.setattr(skill_listing, "managed_settings_paths", lambda: [])
    monkeypatch.setattr(skill_listing, "claude_code_version",
                        lambda: skill_listing.CALIBRATED_CLAUDE_VERSION)
    for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_MODEL", "CLAUDE_CODE_DISABLE_1M_CONTEXT",
                "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", skill_listing.ENV_BUDGET_VAR):
        monkeypatch.delenv(var, raising=False)


def _find_bin_without_claude(find_bin):
    """Wrap ``find_bin`` so that it never resolves the Claude Code CLI."""

    def find_bin_without_claude(name, *args, **kwargs):
        if Path(str(name)).stem.lower() == "claude":  # claude, claude.exe, claude.cmd
            return None
        return find_bin(name, *args, **kwargs)

    find_bin_without_claude.__wrapped__ = find_bin
    return find_bin_without_claude


@pytest.fixture
def real_claude_binary():
    """Opt in: this test resolves and spawns ``claude`` for real.

    ``_no_real_claude_binary`` (the resolver) and ``_no_real_claude_spawn``
    (the spawn) keep the Claude Code CLI from every other test. Request this
    fixture only in a test that fakes what it finds (its own ``claude`` stub
    on PATH, or a patched ``shutil.which``), never to reach the developer's
    real CLI.
    """


@pytest.fixture(autouse=True)
def _no_real_claude_binary(request, monkeypatch):
    """No test may resolve the developer's real Claude Code CLI.

    Before this fixture, one CliRunner ``jacked install`` on a developer
    machine ran the real ``claude mcp get`` and four ``claude plugin
    install`` calls (found 2026-11-03): ``find_bin`` searches PATH first, and
    only CI has no ``claude``. tests/unit/test_findbin_guard.py makes every
    lookup go through ``jacked.findbin.find_bin``, so this fixture hides the
    name from that resolver: at its source, which the call-time imports
    read, and in every loaded ``jacked`` module that bound the name at
    import. A module that the test imports first binds the wrapper from the
    source. Other names resolve as before. A test that needs the real
    resolver requests ``real_claude_binary``.

    ``find_bin("claude") or "claude"`` (jacked/launch.py,
    jacked/memory/capture.py) lets the OS search PATH for the bare name, so
    ``_no_real_claude_spawn`` refuses the spawn itself.
    """
    import sys

    import jacked.findbin as findbin

    real = getattr(findbin.find_bin, "__wrapped__", findbin.find_bin)
    if "real_claude_binary" in request.fixturenames:
        resolver = real
    else:
        resolver = _find_bin_without_claude(real)
    monkeypatch.setattr(findbin, "find_bin", resolver)
    for name, module in list(sys.modules.items()):
        if not name.startswith("jacked.") or module is findbin:
            continue
        bound = getattr(module, "__dict__", {}).get("find_bin")
        # The real resolver, or a wrapper that an earlier test's first import
        # bound (monkeypatch cannot undo a binding it never made).
        if bound is real or getattr(bound, "__wrapped__", None) is real:
            monkeypatch.setattr(module, "find_bin", resolver)


_CLAUDE_NAMES = frozenset({"claude", "claude.exe", "claude.cmd"})
# Taken before any test runs: a test can replace shutil.which with a fake that
# answers "/usr/bin/claude" for every name, and the tripwire must not read
# that answer for `git`.
_REAL_WHICH = shutil.which


def _developer_claude_files() -> frozenset:
    """The real files of the developer's Claude Code CLI (empty on CI).

    ``~/.local/bin/claude`` is a link to a versioned file such as
    ``~/.local/share/claude/versions/2.1.317``, and
    jacked/credentials/runtime.py spawns that resolved file. Its name is not
    "claude", so the tripwire also matches these exact files.
    """
    candidates = [_REAL_WHICH("claude")]
    for folder in (REAL_HOME / ".local" / "bin", REAL_HOME / ".claude" / "local"):
        candidates += [folder / name for name in ("claude", "claude.exe")]
    return frozenset(
        os.path.realpath(path) for path in candidates if path and os.path.isfile(path)
    )


_DEVELOPER_CLAUDE_FILES = _developer_claude_files()


def _spawned_program(args) -> str:
    """The program of a subprocess call: argv[0], or the first word of a command line.

    A value that is not a path (a test double, for example) gives "", so
    Popen itself reports it as before.
    """
    try:
        if isinstance(args, (list, tuple)):
            return os.fsdecode(args[0]) if args else ""
        text = os.fsdecode(args).strip()
    except TypeError:
        return ""
    if text.startswith('"') and '"' in text[1:]:
        return text[1:text.index('"', 1)]
    return text.split()[0] if text else ""


def _spawns_claude(program: str) -> bool:
    """True when ``program`` names or resolves to the Claude Code CLI."""
    if not program:
        return False
    if Path(program).name.lower() in _CLAUDE_NAMES:
        return True
    resolved = _REAL_WHICH(program)
    if not resolved:
        return False
    real = os.path.realpath(resolved)
    return (
        Path(resolved).name.lower() in _CLAUDE_NAMES
        or Path(real).name.lower() in _CLAUDE_NAMES
        or real in _DEVELOPER_CLAUDE_FILES
    )


@pytest.fixture(autouse=True)
def _no_real_claude_spawn(request, monkeypatch):
    """No test may spawn the Claude Code CLI: a spawn fails as a missing binary.

    ``_no_real_claude_binary`` hides ``claude`` from the resolver, but
    ``find_bin("claude") or "claude"`` (jacked/launch.py,
    jacked/memory/capture.py) then spawns the bare name, and the OS searches
    PATH for it. So subprocess.run and subprocess.Popen refuse a program that
    is claude, claude.exe or claude.cmd by its name, by the path that
    shutil.which resolves for it, or by that path's real file, and any spawn
    of the developer's real CLI file. The refusal is FileNotFoundError, the
    error that CI gets with no ``claude`` installed, so a test behaves as it
    does on CI. Every other program runs as before. A test that spawns its
    own ``claude`` stub requests ``real_claude_binary``.
    """
    if "real_claude_binary" in request.fixturenames:
        return
    spawn, run = subprocess.Popen, subprocess.run

    def refuse(args, executable=None):
        # `executable=` names the program that runs, whatever argv[0] says.
        for program in (_spawned_program(args), _spawned_program([executable or ""])):
            if _spawns_claude(program):
                raise FileNotFoundError(
                    errno.ENOENT, "the tests must not run the real claude binary", program
                )

    class _NoClaudePopen(spawn):
        def __init__(self, args, *popen_args, **popen_kwargs):
            refuse(args, popen_kwargs.get("executable"))
            super().__init__(args, *popen_args, **popen_kwargs)

    def _no_claude_run(args, *run_args, **run_kwargs):
        refuse(args, run_kwargs.get("executable"))
        return run(args, *run_args, **run_kwargs)

    monkeypatch.setattr(subprocess, "Popen", _NoClaudePopen)
    monkeypatch.setattr(subprocess, "run", _no_claude_run)


@pytest.fixture(autouse=True)
def _block_keychain_writes():
    """Prevent tests from mutating the real credential authority."""
    def launch_result(account, _db):
        account_id = int(account["id"])
        identity = CredentialIdentity(
            account_id=account_id,
            email=account.get("email"),
            organization_id=account.get("organization_uuid") or None,
        )
        return SwitchResult(
            operation_id="launch-test-operation",
            outcome=SwitchOutcome.OBSERVED_TARGET_UNFENCED,
            desired_default=IdentityAxis(account_id, "desired"),
            storage=IdentityAxis(account_id, "observed"),
            committed_authority=IdentityAxis(None, "uncommitted"),
            existing_session_activation=SessionActivationState.RESTART_REQUIRED,
            provider_verification=ProviderVerificationState.UNVERIFIED,
            observed_identity=identity,
            message="test credential observation",
        )

    with patch(
        "jacked.launch._activate_launch_credentials",
        side_effect=launch_result,
    ), patch(
        # prepare_account_dir asks the real Claude install which authority it
        # uses; keep every launch test on the deterministic global-activation
        # path instead of the developer's own machine.
        "jacked.launch.scoped_launch_needs_global_activation",
        return_value=True,
    ):
        yield


@pytest.fixture(autouse=True)
def _reset_claude_identity_cache():
    from jacked.credentials.runtime import clear_identity_cache

    clear_identity_cache()
    yield
    clear_identity_cache()


@pytest.fixture(autouse=True)
def _reset_swap_recommendation_episode():
    """A recommendation is recorded once per episode; no test may inherit
    another test's episode."""
    from jacked.api import swap_recommendation

    swap_recommendation.reset()
    yield
    swap_recommendation.reset()


@pytest.fixture(autouse=True)
def _reset_mutation_gate_throttle():
    """The gate folds repeated denials per host for a minute; no test may
    inherit another test's fold."""
    from jacked.api import mutation_gate

    mutation_gate.reset_denial_throttle()
    yield
    mutation_gate.reset_denial_throttle()


@pytest.fixture(autouse=True)
def _reset_keychain_latches():
    from jacked.credentials.macos_store import (
        clear_keychain_latches,
        reset_argv_fallback_warning,
    )

    clear_keychain_latches()
    reset_argv_fallback_warning()
    yield
    clear_keychain_latches()
    reset_argv_fallback_warning()


@pytest.fixture(autouse=True)
def _block_browser_open():
    """Prevent any test from opening a real browser window.

    The OAuth flow calls webbrowser.open() which pops up the Anthropic
    login page during test runs. Block it globally.
    """
    with patch("webbrowser.open", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _block_profile_browser_launch():
    """Prevent any test from spawning a real browser process.

    Browser-mode OAuth launches a per-account browser profile through
    jacked.web.browser_launch, which uses subprocess.Popen rather than
    webbrowser.open. Reporting "no browser installed" makes open_auth_url
    report a "manual" result in profile and incognito mode (nothing opens;
    the dashboard shows the link), and in "default" mode it uses
    webbrowser.open, which the fixture above already blocks. Tests that
    exercise the launcher itself patch find_browser locally, which
    overrides this.
    """
    with patch("jacked.web.browser_launch.find_browser", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_reconcile_ownership_network():
    """Reconcile's ownership check must never reach the real profile endpoint.

    The default answer is "request did not complete", so reconcile imports
    nothing it cannot prove. A test that needs a proven owner patches
    ``jacked.api.credential_helpers._ownership_lookup`` itself.
    """
    from jacked.api.authority_guard import ProfileLookup

    from jacked.api.credential_helpers import clear_ownership_cache

    clear_ownership_cache()
    with patch(
        "jacked.api.credential_helpers._ownership_lookup",
        return_value=ProfileLookup(None),
    ):
        yield
    clear_ownership_cache()


"""Shared fixtures for jacked tests."""

import re
from pathlib import Path

import pytest
from unittest.mock import patch

from jacked.credentials.models import (
    CredentialIdentity,
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchOutcome,
    SwitchResult,
)



# The developer's real home, captured before any test can move HOME.
REAL_HOME = Path.home()


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
    yield home


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
    return target in guarded or target.parent == REAL_HOME and target.name.startswith(
        ".claude_json_tmp_"
    )


@pytest.fixture(autouse=True)
def _tripwire_real_claude_config(monkeypatch):
    """Tripwire: no test may write or lock the developer's real Claude config."""
    from jacked.api import claude_config_store

    real_write = claude_config_store._atomic_write_json
    real_acquire = claude_config_store._acquire_lock

    def guarded_write(config_path, data):
        if _under_real_claude_config(config_path):
            raise RuntimeError(f"test tripwire: write to the real {config_path.name}")
        return real_write(config_path, data)

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
    """Tripwire: no test may run the real ``security`` tool against the Keychain."""
    import subprocess

    from jacked.credentials import macos_store

    cls = macos_store.SecurityCliBackend
    real_invoke = cls._invoke

    def guarded_invoke(self, args, *, is_interactive, stdin=None):
        if self._run is subprocess.run and self._tool == macos_store.SECURITY_TOOL:
            raise RuntimeError("test tripwire: real Keychain access via security")
        return real_invoke(self, args, is_interactive=is_interactive, stdin=stdin)

    monkeypatch.setattr(cls, "_invoke", guarded_invoke)


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
        "jacked.api.credential_helpers.write_platform_credentials",
        return_value=True,
    ), patch(
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

    Browser-mode OAuth now launches a per-account browser profile through
    jacked.web.browser_launch, which uses subprocess.Popen rather than
    webbrowser.open. Reporting "no browser installed" sends open_auth_url
    down its webbrowser fallback, which the fixture above already blocks.
    Tests that exercise the launcher itself patch find_browser locally,
    which overrides this.
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


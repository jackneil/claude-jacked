"""Regression tests for the lifespan lock-reset contract.

Context: the tray restarts uvicorn in a new thread with a new event loop
while this process keeps modules imported. Module-level ``asyncio.Lock``
/ ``Event`` instances left bound to the dead loop raise "bound to a
different event loop" on the next acquire. To prevent this, every module
owning such a primitive must expose ``reset_locks()`` AND be wired into
the lifespan reset list at ``jacked/api/main.py``. These tests enforce
that contract at CI time so a future contributor who adds a new
module-level lock can't silently regress the fix.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path

import pytest

# Modules registered in jacked/api/main.py's lifespan lock-reset loop.
# Keep in sync with the imports + tuple at ``api/main.py::lifespan``.
REGISTERED_MODULES: tuple[str, ...] = (
    "jacked.api.routes.auth",
    "jacked.web.auth",
    "jacked.api.routes.features",
    "jacked.api.routes.integrations",
    "jacked.api.routes.permissions",
    "jacked.api.routes.system",
    "jacked.api.usage_monitor",
    "jacked.web.oauth",
)

LOOP_PRIMITIVES = {
    "Lock", "Event", "Semaphore", "BoundedSemaphore", "Condition",
    "Queue", "PriorityQueue", "LifoQueue",
}

# Known limitations of the AST enforcer below: only top-level ``_x = asyncio.X()``
# or ``_x: asyncio.X = asyncio.X()`` call expressions are caught. The following
# patterns are NOT detected and rely on contributor discipline + code review:
#   • Conditional:  ``_x = asyncio.Lock() if flag else None``  (wraps as IfExp)
#   • Lazy create:  ``_x = None`` at module scope + construction inside a function
#   • Tuple unpack: ``_a, _b = asyncio.Lock(), asyncio.Lock()``
#   • Wrapper fn:   ``_x = _make_lock()`` where the factory is what returns a Lock
#   • Indirect:     ``defaultdict(asyncio.Lock)`` — reference, not call
#   • Collection:   dict/list literals containing objects that carry loop state
# If you add a primitive in any of those shapes, register it manually in
# REGISTERED_MODULES and add an explicit rebind test below.


@pytest.mark.parametrize("mod_name", REGISTERED_MODULES)
def test_registered_module_exposes_callable_reset_locks(mod_name: str) -> None:
    """Each registered module must expose a callable ``reset_locks``."""
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, "reset_locks", None)
    assert callable(fn), f"{mod_name} is registered but missing reset_locks()"


@pytest.mark.parametrize("mod_name", REGISTERED_MODULES)
def test_reset_locks_is_idempotent(mod_name: str) -> None:
    """Calling reset_locks twice in a row is safe."""
    mod = importlib.import_module(mod_name)
    mod.reset_locks()
    mod.reset_locks()  # no exception


def test_reset_locks_rebinds_bulk_refresh_lock() -> None:
    """The canonical symptom: a fresh Lock object after reset."""
    from jacked.api.routes import auth as auth_routes
    before = auth_routes._bulk_refresh_lock
    auth_routes.reset_locks()
    after = auth_routes._bulk_refresh_lock
    assert before is not after
    assert isinstance(after, asyncio.Lock)
    assert auth_routes._bulk_refresh_acquired_at == 0.0
    assert auth_routes._bulk_refresh_task is None


# Explicit rebind checks for every module that rebinds a single global.
# Catches a regression where ``reset_locks`` body degenerates to ``pass``
# — the parametrized callable-exists tests above would still pass.
_REBIND_CASES: tuple[tuple[str, str], ...] = (
    ("jacked.api.routes.features", "_settings_lock"),
    ("jacked.api.routes.integrations", "_reach_lock"),
    ("jacked.api.routes.permissions", "_project_settings_lock"),
    ("jacked.api.routes.system", "_upgrade_lock"),
)


@pytest.mark.parametrize("mod_name,attr", _REBIND_CASES)
def test_reset_locks_rebinds_single_global(mod_name: str, attr: str) -> None:
    mod = importlib.import_module(mod_name)
    before = getattr(mod, attr)
    mod.reset_locks()
    after = getattr(mod, attr)
    assert before is not after, (
        f"{mod_name}.reset_locks() must rebind {attr} to a fresh Lock, but "
        f"the same object was returned (reset_locks body may be a no-op)."
    )
    assert isinstance(after, asyncio.Lock)


def test_fastapi_lifespan_actually_runs_reset_locks() -> None:
    """End-to-end: construct the app and run lifespan via TestClient as a
    context manager. Assert at least one module's lock got rebound and
    the startup counter advanced. Catches regressions where the lifespan
    loop body silently skips everything (bad callable() check, broken
    iteration, etc.) that unit tests of reset_locks() alone would miss.
    """
    # Force all runtime deps available in test env before importing app.
    from fastapi.testclient import TestClient

    from jacked.api import main as api_main
    from jacked.api.routes import auth as auth_routes

    before_lock = auth_routes._bulk_refresh_lock
    before_count = api_main._lifespan_startup_count

    with TestClient(api_main.app):
        pass  # lifespan startup runs on __enter__, shutdown on __exit__

    assert api_main._lifespan_startup_count == before_count + 1, (
        "Lifespan did not run — TestClient context manager should trigger "
        "startup. If the counter did not advance, the new lifespan block "
        "is not being reached."
    )
    assert auth_routes._bulk_refresh_lock is not before_lock, (
        "Lifespan ran but auth_routes._bulk_refresh_lock was not rebound. "
        "The reset loop in main.py::lifespan is broken."
    )


def test_reset_locks_clears_per_account_refresh_locks() -> None:
    """web/auth.py uses dict.clear() (not rebind) — verify lazy re-create."""
    from jacked.web import auth as web_auth
    lock_before = web_auth._get_refresh_lock(999)
    cc_before = web_auth._get_cc_refresh_lock(999)
    assert 999 in web_auth._refresh_locks
    web_auth.reset_locks()
    assert 999 not in web_auth._refresh_locks
    assert 999 not in web_auth._cc_refresh_locks
    lock_after = web_auth._get_refresh_lock(999)
    cc_after = web_auth._get_cc_refresh_lock(999)
    assert lock_after is not lock_before
    assert cc_after is not cc_before


def test_reset_locks_rebinds_sweep_wake_event() -> None:
    """usage_monitor._sweep_wake must rebind — the Event-not-Lock case."""
    from jacked.api import usage_monitor
    before = usage_monitor._sweep_wake
    usage_monitor.reset_locks()
    after = usage_monitor._sweep_wake
    assert before is not after
    assert isinstance(after, asyncio.Event)


def test_reset_locks_clears_oauth_flows() -> None:
    """In-flight OAuth flows from a dead loop must be dropped."""
    from jacked.web import oauth
    oauth._active_flows["probe-flow-id"] = object()  # type: ignore[assignment]
    oauth.reset_locks()
    assert "probe-flow-id" not in oauth._active_flows


def test_permissions_uses_attribute_access_not_aliased_import() -> None:
    """Regression guard: permissions.py must access features._settings_lock
    via module attribute, not a ``from features import _settings_lock``.
    The latter would snapshot the original Lock and miss every rebind.
    """
    perm_path = Path(__file__).parent.parent.parent.parent / "jacked" / "api" / "routes" / "permissions.py"
    src = perm_path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "jacked.api.routes.features":
            imported = {alias.name for alias in node.names}
            assert "_settings_lock" not in imported, (
                "permissions.py imports _settings_lock via from-import. "
                "This pins the original Lock object and defeats reset_locks() "
                "rebinds in features.py. Use attribute access instead: "
                "`from jacked.api.routes import features as _features_mod` + "
                "`_features_mod._settings_lock` at the call site."
            )


def test_permissions_sees_rebound_features_settings_lock() -> None:
    """After features.reset_locks(), permissions.py's use site picks up
    the new Lock — proves the module-attribute access works.
    """
    from jacked.api.routes import features, permissions  # noqa: F401
    before = features._settings_lock
    features.reset_locks()
    after = features._settings_lock
    assert before is not after
    # permissions.py accesses via _features_mod._settings_lock at use time
    assert permissions._features_mod._settings_lock is after


def test_every_module_level_asyncio_primitive_is_registered() -> None:
    """AST-walk every module under jacked/api and jacked/web. If it has
    a module-level assignment like ``_foo = asyncio.Lock()`` / ``Event()`` /
    etc., it MUST be in REGISTERED_MODULES.

    This is the convention enforcer — catches the "silent footgun" where
    a future contributor adds a module-level primitive and forgets to
    register it.
    """
    repo_root = Path(__file__).parent.parent.parent.parent
    scan_roots = [
        repo_root / "jacked" / "api",
        repo_root / "jacked" / "web",
    ]
    offenders: list[tuple[str, str, int]] = []
    for root in scan_roots:
        for path in root.rglob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not isinstance(value, ast.Call):
                    continue
                func = value.func
                name = None
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    if func.value.id == "asyncio" and func.attr in LOOP_PRIMITIVES:
                        name = func.attr
                elif isinstance(func, ast.Name) and func.id in LOOP_PRIMITIVES:
                    name = func.id
                if name is None:
                    continue
                rel = path.relative_to(repo_root).as_posix()
                mod_name = rel.replace("/", ".").removesuffix(".py")
                if mod_name not in REGISTERED_MODULES:
                    offenders.append((mod_name, name, node.lineno))

    assert not offenders, (
        "Module-level asyncio primitives found outside REGISTERED_MODULES. "
        "Add a reset_locks() helper to each module and register it in "
        "jacked/api/main.py's lifespan lock-reset loop AND in this test's "
        "REGISTERED_MODULES tuple. Offenders:\n"
        + "\n".join(f"  {m}:{ln} — asyncio.{n}()" for m, n, ln in offenders)
    )


def test_registered_modules_are_wired_into_lifespan() -> None:
    """Parse main.py's AST to find the ``_module_specs`` tuple inside the
    lifespan function and confirm every REGISTERED_MODULES entry appears
    in it. A plain import check is insufficient — a contributor could
    remove a module from the tuple while leaving a stray import, and
    ``reset_locks`` for that module would silently stop running. The
    tuple is the load-bearing structure.
    """
    main_path = Path(__file__).parent.parent.parent.parent / "jacked" / "api" / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))

    # Find the lifespan async function, then the `_module_specs` assignment,
    # then extract the first-element string of each tuple pair.
    wired: set[str] = set()
    for top in ast.walk(tree):
        if not (isinstance(top, ast.AsyncFunctionDef) and top.name == "lifespan"):
            continue
        for node in ast.walk(top):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "_module_specs" not in names:
                continue
            value = node.value
            if not isinstance(value, ast.Tuple):
                continue
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    wired.add(elt.value)

    assert wired, (
        "Could not locate ``_module_specs`` tuple inside ``lifespan`` in "
        "jacked/api/main.py. Either the variable was renamed or the lifespan "
        "function was restructured — update this test to match."
    )
    missing = [m for m in REGISTERED_MODULES if m not in wired]
    assert not missing, (
        f"REGISTERED_MODULES entries not present in main.py's "
        f"``_module_specs`` tuple: {missing}. Each must be registered in "
        f"that tuple so the lifespan actually calls its reset_locks()."
    )

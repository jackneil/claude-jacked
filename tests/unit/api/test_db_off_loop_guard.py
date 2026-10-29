"""No coroutine runs a synchronous jacked.db query on the event loop (#162).

The service has one event loop. A sqlite3 call made directly inside an
``async def`` blocks it, and under disk pressure every HTTP route and every
background loop stops (the 2026-10-10 Windows outage was the analytics
twin of this). This guard reads the source: every ``async def`` in the
package must send ``Database`` work to a thread, through
``asyncio.to_thread`` or by being a plain ``def`` route that FastAPI runs in
its threadpool.

It finds a ``Database`` method called on a receiver whose name says it is
the jacked DB (``db``, ``self.db``, ``request.app.state.db``, ``_get_db()``),
and a call to a synchronous function of this package that makes such a call
itself, at any depth. The analytics DB is out of scope here; its own guard
is tests/unit/test_analytics_off_loop.py.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import jacked

PACKAGE = Path(jacked.__file__).resolve().parent


def _database_methods() -> frozenset[str]:
    tree = ast.parse((PACKAGE / "web" / "database.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Database":
            # A staticmethod (parse_keeper_state, active_account_setting_key)
            # is pure computation with no connection, not a query.
            return frozenset(
                item.name for item in node.body
                if isinstance(item, ast.FunctionDef)
                and not item.name.startswith("__")
                and not any(
                    isinstance(d, ast.Name) and d.id == "staticmethod"
                    for d in item.decorator_list
                )
            )
    raise AssertionError("Database class not found")


# Database methods that run no query: detach_connection only hands the
# calling thread's connection object to the caller.
_NOT_QUERIES = frozenset({"detach_connection"})

METHODS = _database_methods() - _NOT_QUERIES


def _is_db_receiver(expr: ast.expr) -> bool:
    text = ast.unparse(expr).lower()
    if "analytics" in text:
        return False
    last = text.rsplit(".", 1)[-1]
    return last in {"db", "_db", "database", "jacked_db"} or last.endswith(("_db()", "db()"))


def _module_name(path: Path) -> str:
    rel = path.relative_to(PACKAGE.parent).with_suffix("")
    return ".".join(rel.parts)


@cache
def _modules() -> dict[str, ast.Module]:
    out = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        out[_module_name(path)] = ast.parse(path.read_text(encoding="utf-8"))
    return out


def _parents(tree: ast.AST) -> dict:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _own_calls(fn: ast.AST, parents: dict):
    """Calls made by ``fn`` itself: not inside a nested def or lambda."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        owner = parents.get(node)
        while owner is not None and owner is not fn:
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                break
            owner = parents.get(owner)
        if owner is fn:
            yield node


@cache
def _imports(module: str) -> tuple[dict, dict]:
    """(name -> module.function, alias -> module) for this module's imports,
    at any depth (the package imports lazily inside functions)."""
    tree = _modules()[module]
    names, modules = {}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = module.split(".")[: -node.level]
                base = ".".join(parts + ([base] if base else []))
            for alias in node.names:
                target = f"{base}.{alias.name}"
                names[alias.asname or alias.name] = target
                modules[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
    return names, modules


@cache
def _sync_functions() -> dict[str, tuple[str, ast.FunctionDef]]:
    """module.function -> (module, node) for every module-level sync def,
    and module.Class -> (module, __init__) so that constructing a class whose
    __init__ queries jacked.db counts as a call to it."""
    out = {}
    for module, tree in _modules().items():
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                out[f"{module}.{node.name}"] = (module, node)
            elif isinstance(node, ast.ClassDef):
                init = next(
                    (item for item in node.body
                     if isinstance(item, ast.FunctionDef) and item.name == "__init__"),
                    None,
                )
                if init is not None:
                    out[f"{module}.{node.name}"] = (module, init)
    return out


@cache
def _aliases(module: str) -> dict[str, str]:
    """Module-level ``alias = function`` assignments, resolved to the
    function they name (``_read_enabled_scope = read_enabled_scope``)."""
    names, _ = _imports(module)
    out = {}
    for node in _modules()[module].body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
        ):
            target = node.value.id
            if f"{module}.{target}" in _sync_functions():
                out[node.targets[0].id] = f"{module}.{target}"
            elif target in names:
                out[node.targets[0].id] = names[target]
    return out


def _resolve(call: ast.Call, module: str) -> str | None:
    names, modules = _imports(module)
    func = call.func
    if isinstance(func, ast.Name):
        if f"{module}.{func.id}" in _sync_functions():
            return f"{module}.{func.id}"
        if func.id in _aliases(module):
            return _aliases(module)[func.id]
        return names.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        base = modules.get(func.value.id)
        if base:
            return f"{base}.{func.attr}"
    return None


def _direct_db_calls(fn: ast.AST, parents: dict) -> list[ast.Call]:
    return [
        call for call in _own_calls(fn, parents)
        if isinstance(call.func, ast.Attribute)
        and call.func.attr in METHODS
        and _is_db_receiver(call.func.value)
    ]


@cache
def _blocking_sync_functions() -> frozenset[str]:
    """Sync functions that run a jacked.db query, directly or through
    another sync function of the package (fixed point)."""
    parents_by_module = {m: _parents(t) for m, t in _modules().items()}
    blocking = {
        name for name, (module, node) in _sync_functions().items()
        if _direct_db_calls(node, parents_by_module[module])
    }
    changed = True
    while changed:
        changed = False
        for name, (module, node) in _sync_functions().items():
            if name in blocking:
                continue
            for call in _own_calls(node, parents_by_module[module]):
                if _resolve(call, module) in blocking:
                    blocking.add(name)
                    changed = True
                    break
    return frozenset(blocking)


def _local_blocking(fn: ast.AST, module: str, parents: dict) -> set[str]:
    """Names of sync defs nested in ``fn`` (any depth) and, for a method,
    sync methods of its class, that run a jacked.db query. Calling one of
    them directly from the coroutine blocks the loop like an inline call."""
    blocking = _blocking_sync_functions()
    candidates = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.FunctionDef) and node is not fn
    ]
    owner = parents.get(fn)
    methods = []
    if isinstance(owner, ast.ClassDef):
        methods = [item for item in owner.body if isinstance(item, ast.FunctionDef)]
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in candidates + methods:
            key = node.name if node in candidates else f"self.{node.name}"
            if key in found:
                continue
            calls = list(_own_calls(node, parents))
            if _direct_db_calls(node, parents) or any(
                _resolve(call, module) in blocking
                or _call_name(call) in found
                for call in calls
            ):
                found.add(key)
                changed = True
    return found


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in {"self", "cls"}:
        return f"self.{func.attr}"
    return None


def _violations() -> list[str]:
    blocking = _blocking_sync_functions()
    found = []
    for module, tree in _modules().items():
        parents = _parents(tree)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in _direct_db_calls(fn, parents):
                found.append(f"{module}:{call.lineno} {fn.name}() calls {ast.unparse(call.func)}")
            local = _local_blocking(fn, module, parents)
            for call in _own_calls(fn, parents):
                target = _resolve(call, module)
                if target in blocking:
                    found.append(
                        f"{module}:{call.lineno} {fn.name}() calls {target} (a sync jacked.db query)"
                    )
                elif _call_name(call) in local:
                    found.append(
                        f"{module}:{call.lineno} {fn.name}() calls {_call_name(call)} "
                        "(a local sync jacked.db query)"
                    )
    return sorted(set(found))


def test_no_coroutine_runs_a_jacked_db_query_on_the_event_loop():
    violations = _violations()
    assert not violations, (
        "Send these to a thread (`await asyncio.to_thread(...)`), or make the "
        "route a plain `def`:\n" + "\n".join(violations)
    )


def test_the_guard_sees_a_direct_and_an_indirect_call(tmp_path, monkeypatch):
    """The detector itself: one direct call, one through a sync helper, and
    the to_thread form that is allowed."""
    src = (
        "import asyncio\n"
        "def helper(db):\n    return db.get_account(1)\n"
        "async def direct(db):\n    return db.get_account(1)\n"
        "async def indirect(db):\n    return helper(db)\n"
        "async def allowed(db):\n"
        "    await asyncio.to_thread(db.get_account, 1)\n"
        "    return await asyncio.to_thread(helper, db)\n"
        "async def nested(db):\n"
        "    def work():\n        return db.get_account(1)\n"
        "    await asyncio.to_thread(work)\n"
        "    return work()\n"
        "aliased = helper\n"
        "async def via_alias(db):\n"
        "    return aliased(db)\n"
        "class Flow:\n"
        "    def __init__(self, db):\n        self.row = db.get_account(1)\n"
        "async def build(db):\n"
        "    return Flow(db)\n"
        "class Loop:\n"
        "    def _read(self):\n        return self.db.get_account(1)\n"
        "    async def run(self):\n"
        "        await asyncio.to_thread(self._read)\n"
        "        return self._read()\n"
    )
    tree = ast.parse(src)
    module = "jacked._guard_probe"
    _modules.cache_clear(); _sync_functions.cache_clear(); _blocking_sync_functions.cache_clear()
    _imports.cache_clear(); _aliases.cache_clear()
    real = dict(_modules())
    real[module] = tree
    monkeypatch.setattr(
        __import__(__name__, fromlist=["_modules"]), "_modules", lambda: real
    )
    try:
        _sync_functions.cache_clear(); _blocking_sync_functions.cache_clear(); _imports.cache_clear(); _aliases.cache_clear()
        probe = [v for v in _violations() if v.startswith(module)]
    finally:
        _sync_functions.cache_clear(); _blocking_sync_functions.cache_clear(); _imports.cache_clear()
        _aliases.cache_clear()
    assert probe == [
        f"{module}:15 nested() calls work (a local sync jacked.db query)",
        f"{module}:18 via_alias() calls {module}.helper (a sync jacked.db query)",
        f"{module}:23 build() calls {module}.Flow (a sync jacked.db query)",
        f"{module}:29 run() calls self._read (a local sync jacked.db query)",
        f"{module}:5 direct() calls db.get_account",
        f"{module}:7 indirect() calls {module}.helper (a sync jacked.db query)",
    ]

"""Keep an interpreter that is mid-upgrade importable.

``jacked upgrade`` and the tray updater (``python -m jacked.service.updater``)
replace the package on disk while the process that runs them keeps every
module it imported before the install. A module of this release that the
process imports for the first time after that point reads the new file.
When that module needs a name that this release ADDED to a module the
process already holds, the import fails: a 0.110.2 interpreter importing
the 0.111.0 ``jacked.service.supervisors`` package (the CLI path) or the
0.111.0 ``jacked.proctree`` (the tray path) stopped on
``from jacked.winproc import system_program``, because its ``jacked.winproc``
was the 0.110.2 module, and the upgrade transaction was lost.

Two halves. Forward: both transactions call :func:`preload_upgrade_modules`
before the install, so a build from this release onward runs the whole
transaction on one release and the new build's code runs only in the child
processes it spawns. Backward, for the interpreters already out there: a
module that needs a name added to a module an older release may hold takes
it through :func:`names_from`, which reloads the held module from disk when
a name is missing. The missing name is the evidence: the import of the new
module proves the new release is on disk, so the held module is reloaded
into the same module object, and every earlier import of it sees the new
names too.

Only modules without import-time side effects and without state are safe
to reload this way.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import sys
from pathlib import Path

_VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"', re.MULTILINE)


def installed_version() -> str:
    """The version of the package on disk, read from ``jacked/__init__.py``.

    Empty when the file cannot be read or carries no version line.

    >>> import jacked
    >>> installed_version() == jacked.__version__
    True
    """
    try:
        text = (Path(__file__).resolve().parent / "__init__.py").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _VERSION_LINE.search(text)
    return match.group(1) if match else ""


def loaded_version() -> str:
    """The version of the ``jacked`` package this interpreter holds.

    >>> import jacked
    >>> loaded_version() == jacked.__version__
    True
    """
    module = sys.modules.get("jacked")
    return str(getattr(module, "__version__", "") or "")


def interpreter_is_stale() -> bool:
    """True when the loaded package and the package on disk are different releases.

    That is the state of the process that runs ``jacked upgrade`` after the
    install step. Two builds with one version string (a development tree)
    are not told apart here; :func:`reload_missing` does not depend on it.

    >>> interpreter_is_stale()
    False
    """
    loaded = loaded_version()
    on_disk = installed_version()
    return bool(loaded) and bool(on_disk) and loaded != on_disk


def reload_missing(name: str, *attributes: str) -> bool:
    """Reload the loaded module ``name`` when it lacks any of ``attributes``.

    Returns True when a reload happened. A module that is not loaded needs
    no reload, because its first import reads the file on disk; a module
    that already has every name is left alone.

    >>> reload_missing("jacked.winproc", "system_program")
    False
    >>> reload_missing("jacked.module_that_is_not_loaded", "anything")
    False
    """
    module = sys.modules.get(name)
    if module is None:
        return False
    if all(hasattr(module, attribute) for attribute in attributes):
        return False
    importlib.reload(module)
    return True


def names_from(module: str, *names: str) -> tuple:
    """Import ``names`` from ``module``, reloading a held copy that lacks one.

    The caller is a module of this release that needs a name this release
    added to ``module``; an interpreter mid-upgrade from an older release
    holds the older ``module``. Returns the attributes in order. A name that
    the file on disk does not have either raises ImportError, as
    ``from module import name`` does.

    >>> (no_window,) = names_from("jacked.winproc", "NO_WINDOW")
    >>> isinstance(no_window, int)
    True
    >>> names_from("jacked.winproc", "no_such_name")
    Traceback (most recent call last):
    ImportError: cannot import name 'no_such_name' from 'jacked.winproc'
    """
    importlib.import_module(module)
    reload_missing(module, *names)
    held = sys.modules[module]
    for name in names:
        if not hasattr(held, name):
            raise ImportError(f"cannot import name {name!r} from {module!r}", name=module)
    return tuple(getattr(held, name) for name in names)


# The roots of an upgrade transaction, on the CLI path (``jacked upgrade``)
# and on the tray path (``jacked.service.updater.run_update``): the process
# tree runner, the legacy-tray resolver, the service platform and lifecycle
# with the supervisors package under them, the service process helpers, and
# the updater itself (the CLI path's migration and restart helpers live
# there). :func:`preload_upgrade_modules` imports these, then every module
# they import lazily, until no new module appears.
UPGRADE_MODULES = (
    "jacked.proctree",
    "jacked.service.legacy",
    "jacked.service.lifecycle",
    "jacked.service.platform",
    "jacked.service.process",
    "jacked.service.updater",
)


def lazy_imports(module: str, entry: str | None = None) -> set[str]:
    """The ``jacked`` modules that ``module`` imports inside its functions.

    Reads the source file of ``module``; a lazy import runs only when its
    function runs, which in an upgrade transaction can be after the install.
    ``from jacked.pkg import name`` counts ``jacked.pkg.name`` too when that
    is a submodule. With ``entry``, only the module-level function ``entry``
    and the module-level functions it names, directly or through each other,
    count. A module without a source file gives an empty set.

    >>> "jacked.service.supervisors.launchd" in lazy_imports("jacked.service.supervisors")
    True
    >>> "jacked.version_check" in lazy_imports("jacked.cli", entry="_run_upgrade_inline")
    True
    >>> lazy_imports("jacked.module_that_does_not_exist")
    set()
    """
    tree = _source_tree(module)
    if tree is None:
        return set()
    if entry is None:
        bodies = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    else:
        top = _top_functions(tree)
        bodies = [top[name] for name in sorted(reachable_functions(tree, entry))]
    found: set[str] = set()
    for function in bodies:
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                found.update(a.name for a in node.names if _is_jacked(a.name))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and _is_jacked(node.module):
                found.add(node.module)
                found.update(
                    f"{node.module}.{a.name}" for a in node.names if _is_submodule(f"{node.module}.{a.name}")
                )
    return found


def reachable_functions(tree: ast.Module, entry: str) -> set[str]:
    """The module-level functions that ``entry`` names, directly or in a chain.

    A name counts when the function body loads it (a call, or a callback
    passed on), so the set errs on the side of more functions.
    """
    top = _top_functions(tree)
    if entry not in top:
        return set()
    seen, todo = set(), [entry]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(
            node.id for node in ast.walk(top[name])
            if isinstance(node, ast.Name) and node.id in top and node.id not in seen
        )
    return seen


def _top_functions(tree: ast.Module) -> dict:
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _source_tree(module: str) -> ast.Module | None:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    origin = getattr(spec, "origin", None) if spec else None
    if not origin or not origin.endswith(".py"):
        return None
    try:
        return ast.parse(Path(origin).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None


def _is_jacked(name: str | None) -> bool:
    return bool(name) and (name == "jacked" or name.startswith("jacked."))


def _is_submodule(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# Loaded modules the walk does not read: the CLI module imports every command
# lazily, so reading all of it would load the whole application. The CLI
# transaction passes its entry function instead (``entry`` below).
_WALK_SKIP = frozenset({"jacked.cli"})


def preload_upgrade_modules(log=None, entry: tuple[str, str] | None = None) -> tuple[str, ...]:
    """Import every module an upgrade transaction can import, before the install.

    First the :data:`UPGRADE_MODULES` roots: an import error there raises,
    because a package that cannot import its own upgrade modules must refuse
    the transaction before anything changes on disk. Then every ``jacked``
    module that a loaded ``jacked`` module (except ``jacked.cli``) imports
    inside a function, again
    and again until no new module appears. A lazy module that does not
    import on this system is skipped and named to ``log``: its call site
    meets the same error with or without an upgrade. ``entry`` is
    ``(module, function)`` for a transaction that runs in a module the walk
    skips: the lazy imports of that function and the functions it names are
    loaded the same way. Returns the sorted names of the ``jacked`` modules
    that this call loaded.

    >>> imported = preload_upgrade_modules()
    >>> import sys
    >>> all(name in sys.modules for name in UPGRADE_MODULES)
    True
    >>> "jacked.service.supervisors.launchd" in sys.modules
    True
    """
    before = {m for m in sys.modules if _is_jacked(m)}
    for name in UPGRADE_MODULES:
        importlib.import_module(name)
    walked: set[str] = set()
    skipped: set[str] = set()
    pending = sorted(lazy_imports(*entry)) if entry else []
    while True:
        for target in pending:
            _import_or_skip(target, skipped, log)
        pending = []
        loaded = sorted(m for m in sys.modules if _is_jacked(m) and m not in walked and m not in _WALK_SKIP)
        if not loaded:
            break
        for name in loaded:
            walked.add(name)
            for target in sorted(lazy_imports(name)):
                _import_or_skip(target, skipped, log)
    return tuple(sorted(m for m in sys.modules if _is_jacked(m) and m not in before))


def _import_or_skip(target: str, skipped: set, log) -> None:
    if target in sys.modules or target in skipped:
        return
    try:
        importlib.import_module(target)
    except Exception as exc:  # the call site meets this error too
        skipped.add(target)
        if log is not None:
            log(f"preload: skipped {target} ({type(exc).__name__})")

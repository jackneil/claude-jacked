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

import importlib
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
    holds the older ``module``. Returns the attributes in order.

    >>> (no_window,) = names_from("jacked.winproc", "NO_WINDOW")
    >>> isinstance(no_window, int)
    True
    """
    importlib.import_module(module)
    reload_missing(module, *names)
    return tuple(getattr(sys.modules[module], name) for name in names)


# Every module an upgrade transaction imports lazily after its install step,
# on the CLI path (``jacked upgrade``) and on the tray path
# (``jacked.service.updater.run_update``): the process tree runner, the
# legacy-tray resolver, the service platform and lifecycle with the
# supervisors package under them, the service process helpers, and the
# updater itself (the CLI path's migration and restart helpers live there).
UPGRADE_MODULES = (
    "jacked.proctree",
    "jacked.service.legacy",
    "jacked.service.lifecycle",
    "jacked.service.platform",
    "jacked.service.process",
    "jacked.service.updater",
)


def preload_upgrade_modules() -> tuple[str, ...]:
    """Import every module an upgrade transaction needs, before the install.

    Returns the module names in order. Raises what the import raises: a
    package that cannot import its own upgrade modules must refuse the
    transaction before anything changes on disk.

    >>> preload_upgrade_modules() == UPGRADE_MODULES
    True
    """
    for name in UPGRADE_MODULES:
        importlib.import_module(name)
    return UPGRADE_MODULES

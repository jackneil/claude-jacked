"""Keep an interpreter that is mid-upgrade importable.

``jacked upgrade`` replaces the package on disk while the process that runs
it keeps every module it imported before the install. A module of this
release that the process imports for the first time after that point reads
the new file. When that module needs a name that this release ADDED to a
module the process already holds, the import fails: a 0.110.2 interpreter
importing the 0.111.0 ``jacked.service.supervisors`` package stopped on
``from jacked.winproc import system_program``, because its ``jacked.winproc``
was the 0.110.2 module, and the upgrade's restart step was lost.

The upgrade command imports every module it needs before the install, so a
build from this release onward never reaches that state. The helpers here
cover the interpreters that are already out there: a module that needs a
name added to a module an older release may hold imports it in a
``try``/``except ImportError`` and calls :func:`reload_missing` in the
``except`` branch. The missing name is the evidence: the import of the new
module proves the new release is on disk, so the held module is reloaded
from it, into the same module object, and every earlier import of that
module sees the new names too.

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

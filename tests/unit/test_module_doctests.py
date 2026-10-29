"""Run every doctest in the jacked package as part of the normal suite.

CI runs ``uv run --frozen python -m pytest`` without ``--doctest-modules``,
so a doctest in jacked/ only ran when a developer asked for it: an example
that rotted, or one that pinned a behavior (a refusal, a parser edge case),
went unchecked on every OS. This collects them all, one test per module.
``jacked/data`` is excluded: it ships skill scripts and assets, not package
code, and some of its scripts import optional packages.
"""
from __future__ import annotations

import doctest
import importlib
import pkgutil

import pytest

import jacked

_SKIP_PREFIXES = ("jacked.data",)


def _modules() -> list[str]:
    names = []
    for info in pkgutil.walk_packages(jacked.__path__, prefix="jacked."):
        # __main__ runs the CLI on import; it holds no doctests.
        if info.name.startswith(_SKIP_PREFIXES) or info.name.endswith(".__main__"):
            continue
        names.append(info.name)
    return sorted(names)


MODULES = _modules()


def test_the_walk_finds_the_package():
    # Guard against an empty walk silently running zero doctests.
    assert len(MODULES) > 50
    assert "jacked.credentials.oauth_extra" in MODULES


@pytest.mark.parametrize("name", MODULES)
def test_module_doctests(name):
    try:
        module = importlib.import_module(name)
    except ImportError as exc:  # a platform-only module (winreg, ctypes.windll)
        pytest.skip(f"{name} does not import here: {exc}")
    result = doctest.testmod(module, optionflags=doctest.ELLIPSIS, verbose=False)
    assert result.failed == 0, f"{result.failed} doctest(s) failed in {name}"

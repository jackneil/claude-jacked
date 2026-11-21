"""The root conftest removes the terminal size that readline writes.

readline calls ``setenv`` for COLUMNS and LINES, which ``os.environ`` does
not see, and pytest-xdist workers inherited ``COLUMNS=80`` from it (see
``drop_readline_terminal_size`` in the repo-root ``conftest.py``). These
tests put a value in the C environment only, the way readline does, and read
what a child process gets.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import conftest as root_conftest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no readline, so nothing writes COLUMNS behind os.environ there",
)


def _child_value(name: str) -> str:
    """The value of ``name`` that a child process inherits."""
    code = f"import os; print(os.environ.get({name!r}, '<unset>'))"
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    ).stdout.strip()


@pytest.fixture
def c_only_columns(monkeypatch):
    """COLUMNS=80 in the C environment and not in os.environ."""
    monkeypatch.delenv("COLUMNS", raising=False)
    os.putenv("COLUMNS", "80")
    yield
    if "COLUMNS" not in os.environ:
        os.unsetenv("COLUMNS")


def test_the_root_conftest_is_the_module_under_test():
    assert callable(root_conftest.drop_readline_terminal_size)


def test_a_size_only_in_the_c_environment_does_not_reach_a_child(c_only_columns):
    assert _child_value("COLUMNS") == "80"  # the leak is real before the cleanup
    assert "COLUMNS" in root_conftest.drop_readline_terminal_size()
    assert _child_value("COLUMNS") == "<unset>"


def test_an_exported_size_stays(c_only_columns, monkeypatch):
    monkeypatch.setenv("LINES", "40")
    dropped = root_conftest.drop_readline_terminal_size()
    assert "LINES" not in dropped
    assert _child_value("LINES") == "40"
    assert os.environ["LINES"] == "40"

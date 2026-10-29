"""Guard: a doctest never resolves the developer's real jacked database.

On 2026-10-26 ``pytest --doctest-modules jacked/web/auth.py`` ran the
``refresh_all_expiring_tokens`` doctest against the real
``~/.claude/jacked.db``: ``tests/conftest.py`` does not reach doctests
collected under ``jacked/``. The repo-root ``conftest.py`` now isolates every
doctest. These tests run the probe doctest on ``_default_db_path`` in a
subprocess, with the real home named, and prove both that the isolation holds
and that the probe fails without it.

The probe only computes a path string. No run opens a database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = "jacked/web/database.py::jacked.web.database._default_db_path"


def _real_home() -> str:
    if sys.platform == "win32":
        pytest.skip("the probe compares POSIX home paths")
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


def _run_probe(*extra: str) -> subprocess.CompletedProcess:
    real_home = _real_home()
    env = dict(os.environ)
    env["HOME"] = real_home
    env["JACKED_DOCTEST_GUARD_REAL_HOME"] = real_home
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "--doctest-modules", "-q",
            "-p", "no:cacheprovider", *extra, PROBE,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_doctest_default_db_path_is_under_a_temporary_home():
    proc = _run_probe()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout


def test_the_probe_fails_without_the_root_conftest():
    """Negative control: without the isolation the probe sees the real home."""
    proc = _run_probe("--noconftest")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "1 failed" in proc.stdout

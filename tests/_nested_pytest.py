"""Run a probe test file in a nested pytest with this suite's conftest.

Some guards in ``tests/conftest.py`` act at session level or across
pytest-xdist workers, so only a separate pytest run can prove them. The
probe runs from the repository root with ``tests.conftest`` loaded as a
plugin, in a temporary rootdir, serial or with ``-n``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def nested_pytest(tmp_path: Path, probe: str, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run ``probe`` (the source of a test file) and return the result."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_probe.py").write_text(probe, encoding="utf-8")
    run_env = dict(os.environ if env is None else env, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "tests.conftest",
         "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path),
         *extra, str(tmp_path / "test_probe.py")],
        cwd=REPO_ROOT, env=run_env, capture_output=True, text=True, timeout=180,
    )

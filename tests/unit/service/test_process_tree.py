"""run_process_tree kills the whole child tree on a timeout (#178i).

subprocess.run kills only its direct child, so the npx worker under
`jacked packs update` kept writing after the upgrade gave up on it. These
tests use real processes: a parent that starts a sleeping grandchild.
"""

import os
import subprocess
import sys
import time

import pytest

from jacked.service.updater import run_process_tree

PARENT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
    "time.sleep(60)\n"
)


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_timeout_kills_the_grandchild_too(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        run_process_tree(
            [sys.executable, "-c", PARENT, str(pid_file)], timeout=5
        )
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild)


def test_the_exit_code_comes_back(tmp_path):
    log = tmp_path / "out.log"
    with open(log, "w") as fh:
        code = run_process_tree(
            [sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"],
            timeout=30, log_fh=fh,
        )
    assert code == 3
    assert log.read_text().strip() == "hi"

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


LEAVES_A_WORKER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "open(sys.argv[1], 'w').write(str(child.pid))\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="group cleanup after exit is POSIX")
def test_a_worker_left_behind_by_a_clean_exit_is_stopped(tmp_path):
    """An inner timeout (npx under `jacked packs update`) can exit the child
    while a worker still runs; the runner stops what is left of the group."""
    pid_file = tmp_path / "worker.pid"
    code = run_process_tree([sys.executable, "-c", LEAVES_A_WORKER, str(pid_file)], timeout=30)
    assert code == 0
    worker = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while _alive(worker) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(worker)


def test_relay_keeps_the_child_running_when_the_terminal_is_gone(tmp_path, monkeypatch):
    """A closed terminal: writes to stdout raise. The child still finishes and
    the log still gets every line."""
    class Closed:
        def write(self, _):
            raise OSError(5, "Input/output error")

        def flush(self):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(sys, "stdout", Closed())
    log = tmp_path / "relay.log"
    with open(log, "w") as fh:
        code = run_process_tree(
            [sys.executable, "-c", "for i in range(3): print('line', i)"],
            timeout=30, log_fh=fh, relay=True,
        )
    assert code == 0
    assert log.read_text().split() == ["line", "0", "line", "1", "line", "2"]


def test_capture_timeout_still_reports_what_was_printed():
    from jacked.proctree import run_tree

    with pytest.raises(subprocess.TimeoutExpired) as err:
        run_tree(
            [sys.executable, "-c", "import sys, time; print('started', flush=True); time.sleep(60)"],
            timeout=3, capture=True,
        )
    assert "started" in (err.value.output or "")


def test_relay_and_capture_read_utf8_output(tmp_path):
    """uv and npx write UTF-8: the runner decodes it as UTF-8 on every
    platform (Windows' default code page would mangle it)."""
    from jacked.proctree import run_tree

    script = "import sys; sys.stdout.buffer.write('caf\\u00e9 \\u2026\\n'.encode('utf-8'))"
    result = run_tree([sys.executable, "-c", script], timeout=30, capture=True)
    assert result.stdout == "caf\u00e9 \u2026\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX group signals (Windows uses taskkill)")
def test_a_mocked_popen_never_reaches_a_group_signal(monkeypatch):
    """A MagicMock pid coerces to 1, and killpg(1) is kill(-1) on Linux: it
    signals every process the user owns (it killed a CI runner). A mocked
    Popen must never reach os.killpg at all."""
    from unittest.mock import MagicMock

    from jacked import proctree

    sent = []
    monkeypatch.setattr(proctree.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    monkeypatch.setattr(proctree.subprocess, "Popen", MagicMock())
    proctree.run_tree(["anything"], timeout=1)
    assert sent == []

    fake = MagicMock()
    fake.return_value.wait.side_effect = subprocess.TimeoutExpired("x", 1)
    monkeypatch.setattr(proctree.subprocess, "Popen", fake)
    with pytest.raises(subprocess.TimeoutExpired):
        proctree.run_tree(["anything"], timeout=1)
    assert sent == []

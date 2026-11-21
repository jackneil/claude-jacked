"""The session-wide exec tripwire in tests/conftest.py (see its comment)."""
import os
import threading

import pytest

from tests._nested_pytest import nested_pytest

EXEC_NAMES = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")


def test_exec_is_refused_and_recorded_from_any_thread(exec_attempts):
    """A real os.execv from a background thread raises instead of replacing
    the pytest process, and the attempt is recorded so the session fails."""
    before = len(exec_attempts)
    errors = []

    def _worker():
        try:
            os.execv("/bin/true", ["/bin/true"])
        except OSError as exc:
            errors.append(exc)

    worker = threading.Thread(target=_worker, name="leaked-restart")
    worker.start()
    worker.join(timeout=5)
    assert errors and "test tripwire" in str(errors[0])
    assert exec_attempts[before:] == ["os.execv from thread 'leaked-restart'"]
    # This attempt was deliberate: take it back so the session stays green.
    del exec_attempts[before:]


@pytest.mark.parametrize("name", EXEC_NAMES)
def test_every_exec_variant_is_guarded(name):
    if not hasattr(os, name):
        pytest.skip(f"os.{name} does not exist on this platform")
    assert getattr(os, name).__name__ == f"guarded_{name}"


# A probe test that a nested pytest run collects. The attempt is caught, as
# restart_service_now catches it, so only the session-level tripwire can
# fail the run.
_LEAVES_AN_ATTEMPT = '''
import os, sys

def test_catches_a_refused_exec():
    try:
        os.execv(sys.executable, [sys.executable, "-c", "pass"])
    except OSError:
        pass
'''

_DOES_NOTHING = '''
def test_nothing():
    pass
'''


@pytest.mark.parametrize("mode", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_a_caught_exec_attempt_fails_the_run(tmp_path, mode):
    """Under pytest-xdist the worker's own exit status never reaches the
    controller, so the attempt must travel in the worker output."""
    proc = nested_pytest(tmp_path, _LEAVES_AN_ATTEMPT, *mode)
    out = proc.stdout + proc.stderr
    assert proc.returncode == pytest.ExitCode.TESTS_FAILED, out
    assert "1 passed" in out, out  # the test itself passed: only the tripwire failed the run
    assert "TRIPWIRE: something tried to replace the pytest process: os.execv" in out, out


@pytest.mark.parametrize("mode", [(), ("-n", "2")], ids=["serial", "xdist"])
def test_a_run_with_no_attempt_passes(tmp_path, mode):
    """Negative control: the nested run itself is green without an attempt."""
    proc = nested_pytest(tmp_path, _DOES_NOTHING, *mode)
    assert proc.returncode == pytest.ExitCode.OK, proc.stdout + proc.stderr

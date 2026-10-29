"""The session-wide exec tripwire in tests/conftest.py (see its comment)."""
import os
import threading

import pytest

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

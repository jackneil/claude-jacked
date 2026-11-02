"""The suite's Keychain guards stay live (tests/conftest.py).

Three layers keep a test off the developer's real Keychain: the backend
identity tripwire, the read isolation stub, and the subprocess refusal.
Each one here fails if its layer goes silent again.
"""

import subprocess

import pytest

from jacked.credentials import macos_store
from tests.conftest import _REAL_SUBPROCESS_RUN


def test_the_backend_default_still_binds_to_the_real_subprocess_run():
    # Inside a test, subprocess.run is the service-process guard; the backend
    # default must still be the real callable, or the identity guard is dead.
    assert subprocess.run is not _REAL_SUBPROCESS_RUN
    assert macos_store.SecurityCliBackend()._run is _REAL_SUBPROCESS_RUN


def test_a_default_backend_read_answers_missing_without_the_tool():
    result = macos_store.MacOSCredentialStore("nobody").read()
    assert result.status is macos_store.StoreStatus.MISSING
    assert "Keychain not consulted" in (result.reason or "")


def test_the_security_tool_is_refused_at_the_subprocess_boundary():
    with pytest.raises(RuntimeError, match=r"real security tool: /usr/bin/security$"):
        subprocess.run(["/usr/bin/security", "find-generic-password", "-a", "x"])


def test_a_wrapped_real_backend_is_refused_too():
    # ``run=subprocess.run`` evaluated inside a test is the guard wrapper, not
    # the real callable, so the identity tripwire cannot see it; the
    # subprocess boundary must.
    backend = macos_store.SecurityCliBackend(run=subprocess.run)
    with pytest.raises(RuntimeError, match="real security tool"):
        backend.read(service="s", account="a", is_interactive=False)

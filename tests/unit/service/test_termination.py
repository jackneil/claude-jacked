import os
import signal
from unittest.mock import patch

from jacked.service.process import (
    OwnedProcess,
    TerminationResult,
    terminate_owned_process,
)


def test_macos_never_force_signals_unmanaged_process(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    owned = OwnedProcess(
        pid=os.getpid(),
        creation_id="created",
        executable=os.path.realpath(os.sys.executable),
        managed=False,
    )
    with (
        patch("jacked.service.process.verify_owned_process", return_value=True),
        patch("os.kill") as kill,
    ):
        result = terminate_owned_process(owned, force=True)
    assert result is TerminationResult.REFUSED_UNMANAGED
    kill.assert_not_called()


def test_generation_or_creation_mismatch_sends_no_signal():
    owned = OwnedProcess(
        pid=os.getpid(), creation_id="wrong", executable="/wrong", managed=False
    )
    with (
        patch("jacked.service.process.verify_owned_process", return_value=False),
        patch("os.kill") as kill,
    ):
        result = terminate_owned_process(owned)
    assert result is TerminationResult.REFUSED_IDENTITY
    kill.assert_not_called()


def test_linux_uses_pidfd_when_available(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    owned = OwnedProcess(
        pid=123,
        creation_id="created",
        executable="/owned/python",
        managed=False,
    )
    with (
        patch("jacked.service.process.verify_owned_process", return_value=True),
        patch("jacked.service.process._linux_pidfd_signal", return_value=True) as pidfd,
        patch("os.kill") as kill,
    ):
        result = terminate_owned_process(owned, force=False)
    assert result is TerminationResult.SIGNALLED
    pidfd.assert_called_once_with(123, signal.SIGTERM)
    kill.assert_not_called()

"""A refused switch lease says why: another holder, or an unusable lock file."""

import logging
import os
import re

import pytest

from jacked.credentials.lease import (
    REFUSAL_HELD,
    REFUSAL_UNUSABLE,
    CompositeSwitchLease,
    FileSwitchLease,
    ProcessSwitchLease,
)


def test_process_lease_reports_held():
    lease = ProcessSwitchLease()
    with lease.acquire() as first:
        assert first is True and lease.refusal is None
        with lease.acquire() as second:
            assert second is False
            assert lease.refusal == REFUSAL_HELD


def test_file_lease_reports_held_by_another_handle(tmp_path):
    path = tmp_path / "switch.lock"
    holder, other = FileSwitchLease(path), FileSwitchLease(path)
    with holder.acquire() as held:
        assert held is True and holder.refusal is None
        with other.acquire() as got:
            assert got is False
            assert other.refusal == REFUSAL_HELD


def test_file_lease_reports_unusable_when_the_file_cannot_be_opened(tmp_path):
    path = tmp_path / "switch.lock"
    path.mkdir()  # a directory where the lock file belongs
    lease = FileSwitchLease(path)
    with lease.acquire() as got:
        assert got is False
        assert lease.refusal == REFUSAL_UNUSABLE


def test_composite_lease_reports_the_first_refusers_reason(tmp_path):
    path = tmp_path / "switch.lock"
    path.mkdir()
    composite = CompositeSwitchLease(ProcessSwitchLease(), FileSwitchLease(path))
    with composite.acquire() as got:
        assert got is False
        assert composite.refusal == REFUSAL_UNUSABLE
    free = CompositeSwitchLease(ProcessSwitchLease(), FileSwitchLease(tmp_path / "ok.lock"))
    with free.acquire() as got:
        assert got is True
        assert free.refusal is None


# The update lock reuses FileSwitchLease, and the text of an OSError carries
# the full path. The log lines name the lease file and the exception class.


def _lease_warnings(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "jacked.credentials.lease"
        and record.levelno == logging.WARNING
    ]


def test_an_unusable_lease_logs_its_file_name_and_class_only(tmp_path, caplog):
    path = tmp_path / "private" / "switch.lock"
    path.mkdir(parents=True)  # a directory where the lock file belongs

    with caplog.at_level(logging.WARNING, logger="jacked.credentials.lease"):
        with FileSwitchLease(path).acquire() as got:
            assert got is False

    [message] = _lease_warnings(caplog)
    assert re.fullmatch(r"Could not open the lease switch\.lock \(\w+Error\)", message)
    assert str(tmp_path) not in caplog.text
    assert "credential switch" not in caplog.text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs exist on POSIX only")
def test_a_lease_path_that_is_not_a_regular_file_says_so(tmp_path, caplog):
    """The class name alone (OSError) would hide why the lease was refused."""
    path = tmp_path / "switch.lock"
    os.mkfifo(path)

    with caplog.at_level(logging.WARNING, logger="jacked.credentials.lease"):
        with FileSwitchLease(path).acquire() as got:
            assert got is False

    assert _lease_warnings(caplog) == [
        "Could not open the lease switch.lock (not a regular file)"
    ]


@pytest.mark.skipif(os.name == "nt", reason="the POSIX unlock path (fcntl)")
def test_a_release_failure_logs_the_file_name_and_class_only(
    tmp_path, caplog, monkeypatch
):
    import fcntl

    path = tmp_path / "switch.lock"
    real_flock = fcntl.flock

    def flock(descriptor, operation):
        if operation == fcntl.LOCK_UN:
            raise OSError(9, "Bad file descriptor", str(path))
        return real_flock(descriptor, operation)

    monkeypatch.setattr(fcntl, "flock", flock)
    with caplog.at_level(logging.WARNING, logger="jacked.credentials.lease"):
        with FileSwitchLease(path).acquire() as held:
            assert held is True

    assert _lease_warnings(caplog) == ["Could not release the lease switch.lock (OSError)"]
    assert str(tmp_path) not in caplog.text

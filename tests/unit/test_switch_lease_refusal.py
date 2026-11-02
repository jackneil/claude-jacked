"""A refused switch lease says why: another holder, or an unusable lock file."""

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

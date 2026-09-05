"""Tests for jacked.service.start_failures (bounded start-failure memory)."""

from __future__ import annotations

import pytest

import json

from jacked.service.start_failures import (
    clear_start_failures,
    read_start_failures,
    record_start_failure,
)


def test_record_counts_failures_inside_window_and_prunes_old_ones(tmp_path):
    path = tmp_path / "start-failures.json"

    assert record_start_failure(path, 1000.0, window=600.0) == 1
    assert record_start_failure(path, 1100.0, window=600.0) == 2
    assert (
        record_start_failure(path, 1700.0, window=600.0) == 2
    )  # 1000.0 pruned, 1100.0 on the edge kept
    assert [item.at for item in read_start_failures(path, 1700.0, window=600.0)] == [
        1100.0,
        1700.0,
    ]


def test_record_drops_future_stamps(tmp_path):
    path = tmp_path / "start-failures.json"

    assert record_start_failure(path, 1000.0, window=600.0) == 1
    assert record_start_failure(path, 500.0, window=600.0) == 1  # 1000.0 is in the future


def test_record_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "start-failures.json"
    path.write_text("not json")

    assert record_start_failure(path, 5.0, window=600.0) == 1


def test_read_tolerates_corrupt_and_missing_files(tmp_path):
    missing = tmp_path / "absent.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json")
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"at": 1.0}))

    assert read_start_failures(missing, 10.0) == []
    assert read_start_failures(corrupt, 10.0) == []
    assert read_start_failures(wrong_shape, 10.0) == []


def test_reasons_round_trip(tmp_path):
    path = tmp_path / "start-failures.json"

    record_start_failure(path, 1000.0, window=600.0, reason="ValueError: bad runtime")
    record_start_failure(path, 1001.0, window=600.0)

    failures = read_start_failures(path, 1002.0, window=600.0)
    assert [(item.at, item.reason) for item in failures] == [
        (1000.0, "ValueError: bad runtime"),
        (1001.0, None),
    ]


def test_legacy_float_entries_are_still_read(tmp_path):
    """Files written before the object form must keep counting."""
    path = tmp_path / "start-failures.json"
    path.write_text(json.dumps([1000.0, 1100.0]), encoding="utf-8")

    failures = read_start_failures(path, 1200.0, window=600.0)
    assert [(item.at, item.reason) for item in failures] == [
        (1000.0, None),
        (1100.0, None),
    ]
    # A record on top of legacy data keeps them and rewrites the object form.
    assert record_start_failure(path, 1200.0, window=600.0, reason="OSError: nope") == 3
    assert json.loads(path.read_text(encoding="utf-8"))[-1] == {
        "at": 1200.0,
        "reason": "OSError: nope",
    }


def test_read_applies_the_window_filter(tmp_path):
    path = tmp_path / "start-failures.json"
    record_start_failure(path, 1000.0, window=600.0)
    record_start_failure(path, 1500.0, window=600.0)

    assert len(read_start_failures(path, 1550.0, window=600.0)) == 2
    assert len(read_start_failures(path, 1900.0, window=600.0)) == 1
    assert read_start_failures(path, 3000.0, window=600.0) == []


def test_clear_removes_file_and_is_idempotent(tmp_path):
    path = tmp_path / "start-failures.json"
    record_start_failure(path, 1.0)

    clear_start_failures(path)
    clear_start_failures(path)

    assert not path.exists()



def test_record_is_atomic_and_private(tmp_path, monkeypatch):
    import os
    import stat as stat_module

    from jacked.service import start_failures

    path = tmp_path / "start-failures.json"
    start_failures.record_start_failure(path, 100.0, reason="first")
    mode = path.stat().st_mode & 0o777
    if os.name == "posix":
        assert mode == 0o600
    before = path.read_text(encoding="utf-8")

    def _crash(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _crash)
    with pytest.raises(OSError):
        start_failures.record_start_failure(path, 101.0, reason="second")
    # The previous history survives intact and no temporary file is left behind.
    assert path.read_text(encoding="utf-8") == before
    leftovers = [p.name for p in tmp_path.iterdir() if "start-failures" in p.name]
    assert leftovers == ["start-failures.json"]
    assert stat_module.S_ISREG(path.stat().st_mode)


def test_record_works_without_fchmod(tmp_path, monkeypatch):
    """Windows has no os.fchmod; the atomic write must not depend on it."""
    import os

    from jacked.service import start_failures

    monkeypatch.delattr(os, "fchmod", raising=False)
    path = tmp_path / "start-failures.json"
    assert start_failures.record_start_failure(path, 100.0, reason="boot") == 1
    assert path.exists()


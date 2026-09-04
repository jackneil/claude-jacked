"""Tests for jacked.service.start_failures (bounded start-failure memory)."""

from __future__ import annotations

import json

from jacked.service.start_failures import clear_start_failures, record_start_failure


def test_record_counts_failures_inside_window_and_prunes_old_ones(tmp_path):
    path = tmp_path / "start-failures.json"

    assert record_start_failure(path, 1000.0, window=600.0) == 1
    assert record_start_failure(path, 1100.0, window=600.0) == 2
    assert (
        record_start_failure(path, 1700.0, window=600.0) == 2
    )  # 1000.0 pruned, 1100.0 on the edge kept
    assert json.loads(path.read_text()) == [1100.0, 1700.0]


def test_record_drops_future_stamps(tmp_path):
    path = tmp_path / "start-failures.json"

    assert record_start_failure(path, 1000.0, window=600.0) == 1
    assert record_start_failure(path, 500.0, window=600.0) == 1  # 1000.0 is in the future


def test_record_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "start-failures.json"
    path.write_text("not json")

    assert record_start_failure(path, 5.0, window=600.0) == 1


def test_clear_removes_file_and_is_idempotent(tmp_path):
    path = tmp_path / "start-failures.json"
    record_start_failure(path, 1.0)

    clear_start_failures(path)
    clear_start_failures(path)

    assert not path.exists()

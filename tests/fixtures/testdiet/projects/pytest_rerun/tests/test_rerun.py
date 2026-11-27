"""A test that fails on the first attempt and passes on the second.

The attempt counter is a file in the current folder. The capture script runs
pytest in a new temporary folder, so the first attempt always fails.
"""

from pathlib import Path


def test_passes():
    assert True


def test_fails_then_passes():
    marker = Path("first-attempt.marker")
    if not marker.exists():
        marker.write_text("1", encoding="utf-8")
        raise AssertionError("sample-first-attempt-failure")

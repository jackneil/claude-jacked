"""A small pytest project for the test-diet sample reports."""

import time

import pytest


def test_passes():
    assert 1 + 1 == 2


def test_fails():
    assert "sample-failure-message" == "a different value", "sample-failure-message"


@pytest.mark.skip(reason="sample-skip-reason")
def test_skipped():
    pass


@pytest.mark.parametrize("value", [1, 2])
def test_parametrized(value):
    assert value > 0


class TestGroup:
    def test_in_class(self):
        assert True

    class TestNested:
        def test_in_nested_class(self):
            assert True


def test_sleeps_half_a_second():
    time.sleep(0.5)

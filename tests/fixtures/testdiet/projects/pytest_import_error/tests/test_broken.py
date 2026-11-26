"""A module that cannot import: the module that it imports does not exist."""

import sample_module_that_does_not_exist  # noqa: F401


def test_never_collected():
    assert True

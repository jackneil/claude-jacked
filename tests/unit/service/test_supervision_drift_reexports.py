"""The supervision drift probes moved out of ``jacked.service.platform``.

``jacked.service.platform`` still re-exports each public name, so no caller
moves. These tests prove three things that the move must keep: each re-exported
name is the same object as the new module's name, a patch on
``jacked.service.platform.inspect_autostart`` still reaches ``supervision_drift``
(the status tests and the doctor tests rely on it), and the log lines keep the
logger name that the moved code always had.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jacked.service import platform
from jacked.service import supervision_drift as drift_module
from jacked.service.autostart import AutostartState

PUBLIC_NAMES = (
    "LAUNCHD_JOB_NOT_LOADED",
    "drift_consequence",
    "launchd_job_never_ran",
    "launchd_job_probe",
    "supervision_drift",
)


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_platform_reexports_the_drift_name_as_the_same_object(name):
    assert getattr(platform, name) is getattr(drift_module, name)


def test_platform_no_longer_defines_the_drift_probes():
    """A second copy in platform.py would be the split coming undone."""
    assert platform.supervision_drift.__module__ == "jacked.service.supervision_drift"
    assert platform.launchd_job_probe.__module__ == "jacked.service.supervision_drift"


def test_a_patch_on_platform_inspect_autostart_reaches_supervision_drift():
    inspection = SimpleNamespace(state=AutostartState.ABSENT, generation=None)
    with patch(
        "jacked.service.platform.inspect_autostart", return_value=inspection
    ) as inspect:
        assert drift_module.supervision_drift() is None
    inspect.assert_called_once()


def test_the_drift_probes_keep_the_logger_name_of_the_old_module():
    assert drift_module.logger is logging.getLogger("jacked.service.platform")

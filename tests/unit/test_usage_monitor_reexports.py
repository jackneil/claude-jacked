"""The window keeper moved out of usage_monitor into jacked.api.keeper (#227i).

Every keeper name that callers imported from :mod:`jacked.api.usage_monitor`
before the move still imports from there, and the name is the keeper
module's own object, not a copy. The keeper modules log under the usage
monitor's logger name, so log filters and caplog filters keep working.

The sweep wake Event has one owner, :mod:`jacked.api.keeper.state`. Its
``reset_locks`` (the lifespan calls it) rebinds the Event, and every reader
gets the current Event through ``sweep_wake()``. usage_monitor keeps no
copy: a copy goes stale at the first ``reset_locks``.
"""

import asyncio
import importlib
import logging
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.api import usage_monitor

# The module that owns each keeper name after the move. These are all the
# module-level names that the keeper code defined in usage_monitor (except
# the sweep wake Event, see the tests at the end of this file), and the
# keeper_confirmation names that usage_monitor imported before the move.
OWNERS = {
    "jacked.api.keeper.state": (
        "_ping_backoff",
        "_PING_BACKOFF_CAP_SECONDS",
        "_PING_BACKOFF_MAX_DOUBLINGS",
        "_PING_BACKOFF_MAX_FAILS",
        "_RESTORED_SCHEDULE_STATUSES",
        "_KEEPER_PING_PACING_SECONDS",
        "_SWEEP_SLEEP_SLICE_SECONDS",
        "_PREWAKE_DONE_KEY",
        "clear_ping_backoff",
        "_keeper_state_of",
        "_write_keeper_state",
        "_note_keeper_skip",
        "_backoff_step",
        "_arm_ping_backoff",
        "_restore_ping_backoff",
        "_sync_backoff",
    ),
    "jacked.api.keeper.confirm": (
        "_bounded_reset",
        "_ping_window_cache_fields",
        "_keeper_window_needed",
        "_window_confirmed",
        "_keeper_record_success",
    ),
    "jacked.api.keeper.auth_ladder": (
        "_LadderOutcome",
        "_ROTATION_TERMINAL_ERRORS",
        "_KEEPER_MAX_RETRY_SECONDS",
        "_KEEPER_MAX_7D_RESET_SECONDS",
        "_keeper_candidates",
        "_keeper_primary",
        "_candidate_fingerprint",
        "_rotation_is_transient",
        "_keeper_auth_ladder",
    ),
    "jacked.api.keeper.process": (
        "_keeper_record_failure",
        "_keeper_process_account",
    ),
    "jacked.api.keeper.sweep": (
        "_sleep_until",
        "_run_window_keeper",
        "_next_active_start_epoch",
        "_note_keeper_disabled",
        "full_sweep_loop",
    ),
    "jacked.api.keeper_confirmation": (
        "_KEEPER_CONFIRM_MIN_SECONDS",
        "_PENDING_REASON_TEXT",
        "_confirm_pending",
        "_confirm_pending_from_poll",
        "_keeper_usage_read",
        "_pending_reason",
        "_read_landed",
        "_window_phrase",
        "_window_read_after",
    ),
}

CASES = [(module, name) for module, names in OWNERS.items() for name in names]
KEEPER_MODULES = (
    "jacked.api.keeper.auth_ladder",
    "jacked.api.keeper.confirm",
    "jacked.api.keeper.process",
    "jacked.api.keeper.state",
    "jacked.api.keeper.sweep",
)


@pytest.mark.parametrize(("module", "name"), CASES, ids=[name for _, name in CASES])
def test_a_moved_name_imports_from_usage_monitor_as_the_keeper_object(module, name):
    owner = importlib.import_module(module)
    namespace: dict = {}
    exec(f"from jacked.api.usage_monitor import {name}", namespace)
    assert namespace[name] is getattr(owner, name)


@pytest.mark.parametrize("first", (*KEEPER_MODULES, "jacked.api.keeper", "jacked.api.usage_monitor"))
def test_the_keeper_modules_import_in_any_order(first):
    """No keeper module imports usage_monitor when it loads (usage_monitor
    imports them), so each one can load first in a new process."""
    code = (
        f"import importlib; importlib.import_module({first!r}); "
        "import jacked.api.keeper.sweep as sweep, jacked.api.usage_monitor as um; "
        "assert um.full_sweep_loop is sweep.full_sweep_loop"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("module", KEEPER_MODULES)
def test_a_keeper_module_logs_under_the_usage_monitor_logger_name(module):
    """Log filters (and caplog in the keeper tests) select this name."""
    owner = importlib.import_module(module)
    assert owner.logger.name == "jacked.api.usage_monitor"


def test_usage_monitor_keeps_no_copy_of_the_sweep_wake_event():
    """keeper.state.reset_locks rebinds the Event, so a copy here would go
    stale. Read it through keeper.state.sweep_wake()."""
    assert not hasattr(usage_monitor, "_sweep_wake")


def test_reset_locks_rebinds_the_sweep_wake_in_keeper_state():
    from jacked.api.keeper import state

    before = state.sweep_wake()
    state.reset_locks()
    after = state.sweep_wake()
    assert after is not before
    assert after is state._sweep_wake
    assert isinstance(after, asyncio.Event)


def test_reset_locks_clears_the_ping_backoff_in_keeper_state():
    from jacked.api.keeper import state

    state._ping_backoff[7] = {"fails": 3, "skip_until": 0.0, "token_fp": "x"}
    state.reset_locks()
    assert state._ping_backoff == {}
    assert usage_monitor._ping_backoff is state._ping_backoff


def test_a_wake_set_through_the_accessor_after_reset_locks_stops_sleep_until():
    """_sleep_until reads the Event that reset_locks bound, not a copy that
    was bound when the module loaded."""
    from jacked.api.keeper import state, sweep

    state.reset_locks()
    state.sweep_wake().set()
    sleeper = AsyncMock(side_effect=AssertionError("_sleep_until did not see the wake"))
    try:
        with patch("jacked.api.keeper.sweep.asyncio.sleep", sleeper):
            asyncio.run(sweep._sleep_until(time.time() + 300))
    finally:
        state.sweep_wake().clear()
    sleeper.assert_not_called()


def test_the_full_sweep_clears_the_current_wake_after_reset_locks(caplog):
    """The tail of a pass clears the Event that reset_locks bound: the next
    pass waits its interval instead of running again at once."""
    from jacked.api.keeper import state, sweep

    state.reset_locks()
    state.sweep_wake().set()
    stored = {"window_keeper_enabled": "false", "usage_check_interval": "300"}
    db = MagicMock()
    db.get_settings.side_effect = lambda keys: {k: stored.get(k) for k in keys}
    db.list_accounts.return_value = []
    app = MagicMock()
    app.state.db = db

    async def _stop_at_the_first_wait(seconds):
        raise asyncio.CancelledError

    caplog.set_level(logging.INFO, logger="jacked.api.usage_monitor")
    try:
        with patch("jacked.api.keeper.sweep.asyncio.sleep", _stop_at_the_first_wait):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(asyncio.wait_for(sweep.full_sweep_loop(app), timeout=2))
        assert not state.sweep_wake().is_set()
    finally:
        state.sweep_wake().clear()
    woken = [r for r in caplog.records if r.getMessage() == "Full sweep woken early"]
    assert len(woken) == 1

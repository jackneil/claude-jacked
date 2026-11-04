"""The 30-minute pass copies a rotated Codex or Grok login into its slot.

The copy runs in a worker thread: the Grok lock can wait seconds, and the
event loop must not stop for it. One provider that fails must not stop the
other.
"""

import asyncio
import threading
from unittest.mock import patch

from jacked.api import provider_slot_sync as main


def test_both_providers_sync_off_the_event_loop():
    threads = []

    def _record(*_args, **_kwargs):
        threads.append(threading.current_thread())
        return False

    with patch("jacked.codex.switching.sync_active_slot_from_live", side_effect=_record), \
            patch("jacked.grok.accounts.sync_active_slot_from_live", side_effect=_record):
        asyncio.run(main.sync_provider_slots(db=object()))

    assert len(threads) == 2
    assert all(t is not threading.main_thread() for t in threads)


def test_a_codex_failure_does_not_stop_the_grok_copy():
    with patch("jacked.codex.switching.sync_active_slot_from_live",
               side_effect=RuntimeError("boom")), \
            patch("jacked.grok.accounts.sync_active_slot_from_live",
                  return_value=True) as grok:
        asyncio.run(main.sync_provider_slots(db=object()))
    grok.assert_called_once()

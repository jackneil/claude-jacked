"""Event-loop stall watchdog for the jacked service.

The service runs all HTTP routes and background loops on ONE asyncio event
loop. When code on that loop does blocking work (for example a slow SQLite
query), every HTTP request waits. The 2026-10-10 Windows outage was this
type of stall: the port accepted TCP, but no route answered, and the log
did not show the cause.

This module starts a daemon thread. The thread sends a no-op probe to the
event loop at a fixed interval through ``loop.call_soon_threadsafe``. When
the loop does not run the probe for ``STALL_SECONDS``, the thread writes ONE
WARNING. The warning gives the stall duration and the current Python stack of
the event-loop thread (``file:line:function`` only, no local values). When
the loop runs the probe again, the thread writes ONE INFO with the total
stall time. During a long stall, the thread writes the warning again each
``REWARN_SECONDS`` with the new stack.

The thread does not touch the loop in a different way. It does only one
probe at a time, so a stalled loop does not collect a queue of probes.

>>> STALL_SECONDS
5.0
>>> format_frame_stack(None)
['<no frame>']
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from types import FrameType
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Time in seconds without a probe response that counts as a stall.
STALL_SECONDS = 5.0
#: Time in seconds between two probes when the loop is healthy.
PROBE_INTERVAL_SECONDS = 1.0
#: Time in seconds between two warnings in the same long stall.
REWARN_SECONDS = 60.0
#: Maximum number of stack frames in one warning.
MAX_STACK_FRAMES = 40


def format_frame_stack(frame: Optional[FrameType], limit: int = MAX_STACK_FRAMES) -> list[str]:
    """Return the stack of *frame* as ``file:line:function`` strings.

    The outermost frame is first. The function does not read source lines
    and does not read local values.

    >>> import sys
    >>> lines = format_frame_stack(sys._getframe())
    >>> lines[-1].endswith(":<module>")
    True
    >>> all(line.count(":") >= 2 for line in lines)
    True
    """
    if frame is None:
        return ["<no frame>"]
    summary = traceback.StackSummary.extract(
        traceback.walk_stack(frame), limit=limit, lookup_lines=False,
    )
    summary.reverse()
    return [f"{fs.filename}:{fs.lineno}:{fs.name}" for fs in summary]


class LoopWatchdog:
    """Watch one asyncio event loop from a daemon thread.

    Use ``start()`` from the event-loop thread (it records that thread id),
    and ``stop()`` at shutdown.

    >>> loop = asyncio.new_event_loop()
    >>> wd = LoopWatchdog(loop, loop_thread_id=0)
    >>> wd.stall_seconds
    5.0
    >>> wd.is_running
    False
    >>> loop.close()
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        loop_thread_id: Optional[int] = None,
        *,
        stall_seconds: float = STALL_SECONDS,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
        rewarn_seconds: float = REWARN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop = loop
        self._loop_thread_id = loop_thread_id
        self.stall_seconds = float(stall_seconds)
        self.probe_interval = float(probe_interval)
        self.rewarn_seconds = float(rewarn_seconds)
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Probe state. The loop thread writes only ``_acked_at``; the
        # watchdog thread writes all other fields.
        self._sent_at: Optional[float] = None
        self._acked_at: Optional[float] = None
        self._stall_warned_at: Optional[float] = None
        #: Number of stall episodes seen (for tests and diagnostics).
        self.stall_episodes = 0

    @property
    def is_running(self) -> bool:
        """Return True while the watchdog thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the daemon thread.

        Call this from the event-loop thread when ``loop_thread_id`` is None.
        """
        if self.is_running:
            return
        if self._loop_thread_id is None:
            self._loop_thread_id = threading.get_ident()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="jacked-loop-watchdog", daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the daemon thread and wait for it to exit."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _on_probe(self) -> None:
        """Record the probe response. This runs on the event loop."""
        self._acked_at = self._clock()

    def _send_probe(self) -> bool:
        """Send one probe. Return False when the loop is closed."""
        if self._loop.is_closed():
            return False
        self._acked_at = None
        self._sent_at = self._clock()
        try:
            self._loop.call_soon_threadsafe(self._on_probe)
        except RuntimeError:
            return False
        return True

    def _loop_stack(self) -> list[str]:
        frame = sys._current_frames().get(self._loop_thread_id or -1)
        return format_frame_stack(frame)

    def _warn(self, stalled_for: float, *, again: bool) -> None:
        stack = "\n  ".join(self._loop_stack())
        logger.warning(
            "Event loop stalled for %.1fs%s; HTTP requests wait until it "
            "recovers. Event-loop thread stack (outermost first):\n  %s",
            stalled_for,
            " (still stalled)" if again else "",
            stack,
        )

    def check(self) -> bool:
        """Run one watchdog step. Return False when the thread must exit.

        The thread calls this method each ``probe_interval``. Tests can call
        it directly with a fake clock.
        """
        now = self._clock()
        if self._sent_at is not None and self._acked_at is None:
            stalled_for = now - self._sent_at
            if stalled_for < self.stall_seconds:
                return not self._loop.is_closed()
            if self._stall_warned_at is None:
                self.stall_episodes += 1
                self._stall_warned_at = now
                self._warn(stalled_for, again=False)
            elif now - self._stall_warned_at >= self.rewarn_seconds:
                self._stall_warned_at = now
                self._warn(stalled_for, again=True)
            return not self._loop.is_closed()

        if self._stall_warned_at is not None and self._sent_at is not None:
            acked_at = self._acked_at if self._acked_at is not None else now
            logger.info(
                "Event loop recovered after a %.1fs stall",
                acked_at - self._sent_at,
            )
            self._stall_warned_at = None
        return self._send_probe()

    def resync(self) -> bool:
        """Forget the current probe and send a new one.

        The thread calls this method after a clock jump, for example when
        the computer resumes from sleep. ``time.monotonic`` on Windows
        counts the time asleep, so the open probe would otherwise look
        like a stall of the full sleep time. Return False when the loop is
        closed.
        """
        self._stall_warned_at = None
        self._sent_at = None
        return self._send_probe()

    def _run(self) -> None:
        try:
            if not self._send_probe():
                return
            last = self._clock()
            while not self._stop.wait(self.probe_interval):
                now = self._clock()
                # This thread waits probe_interval between steps, and a
                # stalled event loop does not delay it. A much longer gap
                # means the clock jumped (sleep or resume), not a stall.
                jumped = now - last > self.probe_interval + self.stall_seconds
                last = now
                step = self.resync if jumped else self.check
                if not step():
                    return
        except Exception:  # noqa: BLE001 - a watchdog must never crash the service
            logger.debug("Loop watchdog exited on error", exc_info=True)

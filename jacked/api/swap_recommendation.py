"""Auto-swap recommendations: one record and one banner for each episode.

A build with no certified credential engine (production today) cannot swap, so
each poll tick that warrants a swap ends as a recommendation. A wall can hold
for days. Without a limit, each tick wrote a ``swap_log`` row, which pushed the
real swaps out of the recent list, and raised the banner again after the user
dismissed it.

An episode is the time in which a swap stays warranted. In one episode, this
module records each (trigger, from account, to account) once. It sends the
banner once, at the first tick that has a dashboard connected to receive it. A
swap that is not warranted for ``_EPISODE_GAP_SECONDS`` ends the episode, so a
reason that flaps on a threshold does not start a new one at each change.

The state is in memory: a restart starts a new episode, with one more row and
one more banner.
"""

import asyncio
import logging
import time

# The monitor's logger name: log filters already use it for these lines.
logger = logging.getLogger("jacked.api.usage_monitor")

# The same length as the swap cooldown: the time that a recommendation of the
# same swap stays one event.
_EPISODE_GAP_SECONDS = 300

# (trigger, from account, to account) -> {"seen": last tick, "announced": bool}
_episode: dict[tuple[str, int, int], dict] = {}


def reset() -> None:
    """Forget the episode (tests and a lifespan restart).

    >>> _episode[("t", 1, 2)] = {"seen": 0.0, "announced": True}
    >>> reset()
    >>> _episode
    {}
    """
    _episode.clear()


def end_episode(now: float | None = None) -> None:
    """Note a tick on which no swap is warranted.

    Drops each recommendation that was last warranted at least
    ``_EPISODE_GAP_SECONDS`` ago. A newer one stays: the reason can return on
    the next tick.

    >>> reset()
    >>> _episode[("t", 1, 2)] = {"seen": 1000.0, "announced": True}
    >>> end_episode(1299.0)
    >>> list(_episode)
    [('t', 1, 2)]
    >>> end_episode(1300.0)
    >>> _episode
    {}
    """
    now = time.time() if now is None else now
    for key in [k for k, entry in _episode.items() if now - entry["seen"] >= _EPISODE_GAP_SECONDS]:
        del _episode[key]


async def recommend(
    db, from_id: int, target: dict, reason: str, trigger: str,
    usage_5h: float | None, usage_7d: float | None, ws_registry=None,
) -> None:
    """Record and announce this recommendation, once in the episode.

    The key is added only after the row is written, so a failed write is tried
    again on the next tick. The banner waits for a connected dashboard: a
    broadcast to nobody would use up the only one.
    """
    key = (trigger, from_id, target["id"])
    entry = _episode.get(key)
    if entry is None:
        await asyncio.to_thread(
            db.record_swap,
            from_account_id=from_id,
            to_account_id=target["id"],
            reason=reason,
            trigger=trigger,
            from_5h=usage_5h,
            from_7d=usage_7d,
            to_5h=target.get("cached_usage_5h"),
            to_7d=target.get("cached_usage_7d"),
            status="recommendation_only",
        )
        logger.info(
            "Auto-swap recommendation only: account %d -> %d; no certified "
            "cooperative credential capability/writer fence is installed",
            from_id,
            target["id"],
        )
        entry = _episode[key] = {"seen": 0.0, "announced": False}
    entry["seen"] = time.time()
    topic = "auto_swap_recommended"
    if ws_registry and not entry["announced"] and ws_registry.has_subscribers(topic):
        await ws_registry.broadcast(
            topic,
            {
                "from_account_id": from_id,
                "to_account_id": target["id"],
                "reason": reason,
            },
        )
        entry["announced"] = True

"""External-integration runners managed by jacked (agent-reach is the first).

Each integration is locked to a vendored pin under ``jacked/data/integrations/``
so a poisoned upstream release or transitive dependency cannot reach the machine.
"""
from typing import Callable, Optional

from jacked.integrations.agent_reach import AgentReachRunner
from jacked.integrations.pinfile import PinFile, PinFileError, load_pin

__all__ = [
    "AgentReachRunner",
    "PinFile",
    "PinFileError",
    "load_pin",
    "reach_db_accessors",
]


def reach_db_accessors(db) -> tuple[Callable[[str], Optional[str]], Callable[[str, Optional[str]], None]]:
    """Build ``(get_setting, set_setting)`` bound to a jacked ``Database``.

    Single-sources the setter's None->delete contract (``Database.set_setting``
    only stores strings, so clearing a key is a row delete) so the CLI and the API
    never drift on how a break-glass override is cleared.
    """
    def _get(key: str) -> Optional[str]:
        return db.get_setting(key)

    def _set(key: str, value: Optional[str]) -> None:
        if value is None:
            db.delete_setting(key)
        else:
            db.set_setting(key, value)

    return _get, _set

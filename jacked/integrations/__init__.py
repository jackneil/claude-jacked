"""External-integration runners managed by jacked (agent-reach is the first).

Each integration is locked to a vendored pin under ``jacked/data/integrations/``
so a poisoned upstream release or transitive dependency cannot reach the machine.
"""
from jacked.integrations.agent_reach import AgentReachRunner
from jacked.integrations.pinfile import PinFile, PinFileError, load_pin

__all__ = ["AgentReachRunner", "PinFile", "PinFileError", "load_pin"]

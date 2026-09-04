"""Compatibility facade for the owned service lifecycle.

The implementation is split by responsibility to keep security-sensitive
modules reviewable. Existing imports from this module remain supported.
"""

from jacked.service.instance_discovery import (
    choose_quarantine_port,
    discover_endpoint,
    inspect_instance,
    reserve_service_bind,
)
from jacked.service.instance_models import (
    BindIdentity,
    Discovery,
    Inspection,
    InspectState,
    InstanceManifest,
    ProcessIdentity,
    ServicePaths,
)
from jacked.service.instance_ownership import (
    ServiceInstance,
    ServiceLease,
    ServiceLeaseBusy,
    ServiceOwnership,
    ServiceOwnershipInvalid,
)
from jacked.service.instance_storage import (
    current_process_identity,
    current_user_identity,
    load_or_create_machine_id,
    manifest_is_proven_stale,
    process_identity,
    process_is_stale,
    process_user_identity,
    publish_manifest,
    read_manifest,
    remove_manifest_if_current,
)

__all__ = [
    "BindIdentity",
    "Discovery",
    "InspectState",
    "Inspection",
    "InstanceManifest",
    "ProcessIdentity",
    "ServiceInstance",
    "ServiceLease",
    "ServiceLeaseBusy",
    "ServiceOwnership",
    "ServiceOwnershipInvalid",
    "ServicePaths",
    "choose_quarantine_port",
    "current_process_identity",
    "current_user_identity",
    "discover_endpoint",
    "inspect_instance",
    "load_or_create_machine_id",
    "manifest_is_proven_stale",
    "process_identity",
    "process_is_stale",
    "process_user_identity",
    "publish_manifest",
    "read_manifest",
    "remove_manifest_if_current",
    "reserve_service_bind",
]

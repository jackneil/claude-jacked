"""Runtime assembly for local Claude credential activation."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from jacked.api.credential_helpers import build_oauth_data
from jacked.findbin import find_bin
from jacked.web.credential_repository import DatabaseCredentialSwitchRepository

from .canonical import CredentialPayload
from .capabilities import CapabilityRegistry, resolve_executable
from .file_store import FileCredentialStore
from .key import FileInstallKeyProvider, machine_install_id
from .lease import ProcessSwitchLease
from .macos_store import MacOSCredentialStore
from .models import (
    CapabilityMode,
    CredentialCapability,
    CredentialIdentity,
    ExecutableIdentity,
    IdentityAxis,
    InteractionMode,
    ProviderVerificationState,
    SessionActivationState,
    StoreDeclaration,
    StoreRole,
    SwitchContext,
    SwitchOutcome,
    SwitchResult,
)
from .resolver import FileResolverSnapshotSink
from .resolver import CanonicalCredentialResolver, ResolverObservation, ResolverState
from .transaction import (
    CredentialTransactionEngine,
    StaticInstallKeyProvider,
    SwitchRequest,
    TransactionDependencies,
)
from .writer_fence import StaticWriterInspector, WriterFence

_VERSION_PATTERN = re.compile(r"^(\d+\.\d+\.\d+)\b")

# This exact macOS arm64 Claude 2.1.259 build was inspected for the shared
# Keychain-first topology. It remains uncooperative, so it can only produce an
# observed-target result and never a committed/live-session claim.
_SHIPPED_CAPABILITIES = (
    CredentialCapability(
        executable=ExecutableIdentity(
            "<resolved-at-runtime>",
            "884baa38fe1a624be25c4a91568bf5a08b5cf4e7d7acf29b7760e3525d964898",
            "2.1.259",
            "global",
            "darwin",
            "arm64",
        ),
        mode=CapabilityMode.GLOBAL_UNCOOPERATIVE,
        authority=StoreDeclaration(
            "macOS Keychain", "macos-keychain", StoreRole.AUTHORITY
        ),
        required_mirrors=(
            StoreDeclaration(
                "global credential file",
                "global-credential-file",
                StoreRole.REQUIRED_MIRROR,
            ),
        ),
        consumers=("claude-code",),
        capability_epoch=1,
        writer_protocol_epoch=2,
        provenance="shipped:claude-2.1.259-macos-arm64",
        registry_version=1,
    ),
)

SHIPPED_REGISTRY = CapabilityRegistry(_SHIPPED_CAPABILITIES)
_PROCESS_SWITCH_LEASE = ProcessSwitchLease()


def _config_mode(home: Path) -> str:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if not configured:
        return "global"
    configured_path = Path(configured).resolve()
    return "global" if configured_path == (home / ".claude").resolve() else "scoped"


def detect_claude_identity(home: Path) -> ExecutableIdentity:
    executable_name = find_bin("claude")
    if not executable_name:
        raise OSError("Claude executable was not found")
    executable = Path(executable_name).resolve(strict=True)
    result = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    match = _VERSION_PATTERN.match(result.stdout.strip())
    if result.returncode != 0 or not match:
        raise OSError("Claude executable version could not be identified")
    return resolve_executable(
        executable,
        build_version=match.group(1),
        config_mode=_config_mode(home),
    )


def _unsupported(operation_id: str, account: dict, reason: str) -> SwitchResult:
    account_id = int(account["id"])
    return SwitchResult(
        operation_id=operation_id,
        outcome=SwitchOutcome.UNSUPPORTED,
        desired_default=IdentityAxis(account_id, "desired"),
        storage=IdentityAxis(None, "unchanged"),
        committed_authority=IdentityAxis(None, "unchanged"),
        existing_session_activation=SessionActivationState.UNCHANGED,
        provider_verification=ProviderVerificationState.UNVERIFIED,
        observed_identity=CredentialIdentity(),
        message=reason,
    )


def activate_account(
    db,
    account: dict,
    context: SwitchContext,
    operation_id: str,
) -> SwitchResult:
    """Resolve the exact runtime contract and activate one local account."""
    home = Path.home()
    try:
        identity = detect_claude_identity(home)
    except OSError as exc:
        return _unsupported(operation_id, account, str(exc))
    resolution = SHIPPED_REGISTRY.resolve(identity)
    if not resolution.can_mutate:
        return _unsupported(operation_id, account, resolution.reason)
    if sys.platform != "darwin":
        return _unsupported(
            operation_id, account, "certified platform adapter unavailable"
        )
    if resolution.capability.mode is CapabilityMode.GLOBAL_UNCOOPERATIVE:
        key_provider = StaticInstallKeyProvider(None)
        install_id = "unfenced-local"
    else:
        key_provider = FileInstallKeyProvider(
            home / ".claude" / "credential-recovery.key"
        )
        key = key_provider.get_key()
        if key is None:
            return _unsupported(
                operation_id, account, "private recovery key unavailable"
            )
        install_id = machine_install_id(key)
    authority = MacOSCredentialStore()
    file_store = FileCredentialStore(
        home / ".claude" / ".credentials.json", trusted_root=home
    )
    engine = CredentialTransactionEngine(
        TransactionDependencies(
            capability=resolution.capability,
            repository=DatabaseCredentialSwitchRepository(db),
            authority=authority,
            mirrors={"global-credential-file": file_store},
            writer_fence=WriterFence(
                StaticWriterInspector((), is_complete=False)
            ),
            install_key=key_provider,
            machine_install_id=install_id,
            snapshot_sink=FileResolverSnapshotSink(
                home / ".claude" / "jacked-resolver-snapshot.json"
            ),
            switch_lease=_PROCESS_SWITCH_LEASE,
        )
    )
    payload = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": int(account["id"]),
            "claudeAiOauth": build_oauth_data(account),
        }
    )
    request = SwitchRequest(
        operation_id=operation_id,
        account_id=int(account["id"]),
        email=account.get("email") or "",
        organization_id=account.get("organization_uuid") or None,
        payload=payload,
        context=context,
        interaction=InteractionMode.FOREGROUND,
    )
    return engine.activate(request)


def resolve_active_identity() -> ResolverObservation:
    """Resolve current identity through the exact certified store topology."""
    home = Path.home()
    try:
        identity = detect_claude_identity(home)
    except OSError as exc:
        return ResolverObservation(
            ResolverState.UNSUPPORTED, CredentialIdentity(), (str(exc),)
        )
    resolution = SHIPPED_REGISTRY.resolve(identity)
    if not resolution.can_mutate or resolution.capability is None:
        return ResolverObservation(
            ResolverState.UNSUPPORTED,
            CredentialIdentity(),
            (resolution.reason,),
        )
    if sys.platform != "darwin":
        return ResolverObservation(
            ResolverState.UNSUPPORTED,
            CredentialIdentity(),
            ("certified platform adapter unavailable",),
        )
    file_store = FileCredentialStore(
        home / ".claude" / ".credentials.json", trusted_root=home
    )
    authority = MacOSCredentialStore()
    return CanonicalCredentialResolver(
        resolution.capability,
        {
            authority.locator: authority,
            "global-credential-file": file_store,
        },
    ).resolve()

"""Runtime assembly for local Claude credential activation."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

from jacked.api.credential_helpers import build_oauth_data
from jacked.findbin import find_bin
from jacked.web.credential_repository import DatabaseCredentialSwitchRepository

from .canonical import CredentialPayload
from .capabilities import (
    NEWER_THAN_INSPECTED,
    CapabilityRecord,
    CapabilityRegistry,
    resolve_executable,
)
from .file_store import FileCredentialStore
from .key import FileInstallKeyProvider, machine_install_id
from .lease import ProcessSwitchLease
from .macos_store import MacOSCredentialStore
from .models import (
    CapabilityMode,
    CapabilityResolution,
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
from .resolver import (
    CanonicalCredentialResolver,
    FileResolverSnapshotSink,
    ResolverObservation,
    ResolverState,
)
from .store import CredentialStore
from .transaction import (
    CredentialTransactionEngine,
    StaticInstallKeyProvider,
    SwitchRequest,
    TransactionDependencies,
)
from .writer_fence import StaticWriterInspector, WriterFence

_VERSION_PATTERN = re.compile(r"^(\d+\.\d+\.\d+)\b")

# Claude Code keeps its credentials in a per-platform store topology that is
# stable across builds: the macOS Keychain item mirrored to the global file,
# or the global file alone on Linux and Windows. Certification is keyed by
# that topology; the observed build and hash travel as evidence.
MIN_CLAUDE_BUILD = "2.1.0"
INSPECTED_CLAUDE_BUILD = "2.1.260"
KEYCHAIN_LOCATOR = "macos-keychain"
GLOBAL_FILE_LOCATOR = "global-credential-file"

_KEYCHAIN_AUTHORITY = StoreDeclaration(
    "macOS Keychain", KEYCHAIN_LOCATOR, StoreRole.AUTHORITY
)
_GLOBAL_FILE_AUTHORITY = StoreDeclaration(
    "global credential file", GLOBAL_FILE_LOCATOR, StoreRole.AUTHORITY
)
_GLOBAL_FILE_MIRROR = StoreDeclaration(
    "global credential file", GLOBAL_FILE_LOCATOR, StoreRole.REQUIRED_MIRROR
)


def _shipped_record(
    platform_system: str,
    authority: StoreDeclaration,
    mirrors: tuple[StoreDeclaration, ...] = (),
) -> CapabilityRecord:
    return CapabilityRecord(
        platform_system=platform_system,
        config_mode="global",
        min_build=MIN_CLAUDE_BUILD,
        inspected_through=INSPECTED_CLAUDE_BUILD,
        capability=CredentialCapability(
            executable=ExecutableIdentity(
                "<resolved-at-runtime>",
                "<observed-at-runtime>",
                INSPECTED_CLAUDE_BUILD,
                "global",
                platform_system,
                "",
            ),
            mode=CapabilityMode.GLOBAL_UNCOOPERATIVE,
            authority=authority,
            required_mirrors=mirrors,
            consumers=("claude-code",),
            capability_epoch=1,
            writer_protocol_epoch=2,
            provenance=f"shipped:claude-{platform_system}-global-topology",
            registry_version=2,
        ),
    )


_SHIPPED_RECORDS = (
    _shipped_record("darwin", _KEYCHAIN_AUTHORITY, (_GLOBAL_FILE_MIRROR,)),
    _shipped_record("linux", _GLOBAL_FILE_AUTHORITY),
    _shipped_record("windows", _GLOBAL_FILE_AUTHORITY),
)

SHIPPED_REGISTRY = CapabilityRegistry(_SHIPPED_RECORDS)
_PROCESS_SWITCH_LEASE = ProcessSwitchLease()

_identity_cache_lock = threading.Lock()
_identity_cache: dict[tuple[str, str], tuple[tuple[int, int], ExecutableIdentity]] = {}


def clear_identity_cache() -> None:
    with _identity_cache_lock:
        _identity_cache.clear()


def build_stores(capability: CredentialCapability, home: Path) -> dict[str, CredentialStore]:
    """Instantiate one adapter per declared store, keyed by the declaration locator.

    The resolver and the transaction engine look stores up by the locator in
    the capability declaration, never by the adapter's own locator string.
    """
    stores: dict[str, CredentialStore] = {}
    for declaration in (
        capability.authority,
        *capability.required_mirrors,
        *capability.optional_metadata,
    ):
        if declaration.locator == KEYCHAIN_LOCATOR:
            stores[declaration.locator] = MacOSCredentialStore()
        elif declaration.locator == GLOBAL_FILE_LOCATOR:
            stores[declaration.locator] = FileCredentialStore(
                home / ".claude" / ".credentials.json", trusted_root=home
            )
        else:
            raise ValueError(f"no adapter for credential store {declaration.locator!r}")
    return stores


def _config_mode(home: Path) -> str:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if not configured:
        return "global"
    configured_path = Path(configured).resolve()
    return "global" if configured_path == (home / ".claude").resolve() else "scoped"


def detect_claude_identity(home: Path) -> ExecutableIdentity:
    """Identify the exact Claude executable, hashing it once per build.

    Every poll loop asks for the identity; hashing a ~200 MB binary and
    spawning ``claude --version`` each time is wasteful, so the result is
    cached until the resolved file's size or mtime changes.
    """
    executable_name = find_bin("claude")
    if not executable_name:
        raise OSError("Claude executable was not found")
    executable = Path(executable_name).resolve(strict=True)
    config_mode = _config_mode(home)
    status = executable.stat()
    stamp = (status.st_size, status.st_mtime_ns)
    key = (str(executable), config_mode)
    with _identity_cache_lock:
        cached = _identity_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except subprocess.SubprocessError as exc:
        raise OSError("Claude executable version probe failed") from exc
    match = _VERSION_PATTERN.match(result.stdout.strip())
    if result.returncode != 0 or not match:
        raise OSError("Claude executable version could not be identified")
    identity = resolve_executable(
        executable, build_version=match.group(1), config_mode=config_mode
    )
    with _identity_cache_lock:
        _identity_cache[key] = (stamp, identity)
    return identity


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


def _engine_for(
    db, resolution: CapabilityResolution, home: Path
) -> CredentialTransactionEngine | str:
    """Build the transaction engine for a resolved capability, or a refusal reason."""
    capability = resolution.capability
    if capability.mode is CapabilityMode.GLOBAL_UNCOOPERATIVE:
        key_provider = StaticInstallKeyProvider(None)
        install_id = "unfenced-local"
    else:
        key_provider = FileInstallKeyProvider(home / ".claude" / "credential-recovery.key")
        key = key_provider.get_key()
        if key is None:
            return "private recovery key unavailable"
        install_id = machine_install_id(key)
    try:
        stores = build_stores(capability, home)
    except (ValueError, RuntimeError) as exc:
        return str(exc)
    authority = stores.pop(capability.authority.locator)
    return CredentialTransactionEngine(
        TransactionDependencies(
            capability=capability,
            repository=DatabaseCredentialSwitchRepository(db),
            authority=authority,
            mirrors=stores,
            writer_fence=WriterFence(StaticWriterInspector((), is_complete=False)),
            install_key=key_provider,
            machine_install_id=install_id,
            snapshot_sink=FileResolverSnapshotSink(
                home / ".claude" / "jacked-resolver-snapshot.json"
            ),
            switch_lease=_PROCESS_SWITCH_LEASE,
            # An uninspected newer build may have moved its store; a missing
            # authority then looks identical to "never logged in". Refuse to
            # create it so jacked never writes where Claude no longer reads.
            allow_missing_authority=NEWER_THAN_INSPECTED not in resolution.evidence,
        )
    )


def activate_account(db, account: dict, context: SwitchContext, operation_id: str) -> SwitchResult:
    """Resolve the exact runtime contract and activate one local account."""
    home = Path.home()
    try:
        identity = detect_claude_identity(home)
    except OSError as exc:
        return _unsupported(operation_id, account, str(exc))
    resolution = SHIPPED_REGISTRY.resolve(identity)
    if not resolution.can_mutate:
        return _unsupported(operation_id, account, resolution.reason)
    engine = _engine_for(db, resolution, home)
    if isinstance(engine, str):
        return _unsupported(operation_id, account, engine)
    payload = CredentialPayload.from_mapping(
        {"_jackedAccountId": int(account["id"]), "claudeAiOauth": build_oauth_data(account)}
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


def scoped_launch_needs_global_activation() -> bool:
    """True unless the certified authority is the global credential file.

    On macOS the Keychain outranks ``CLAUDE_CONFIG_DIR``, so a scoped launch
    must also switch the global authority. Where the file is the authority,
    Claude reads the scoped file and touching the global one would change
    every other session's account. Unknown state fails closed (True).
    """
    try:
        resolution = SHIPPED_REGISTRY.resolve(detect_claude_identity(Path.home()))
    except OSError:
        return True
    if not resolution.can_mutate:
        return True
    return resolution.capability.authority.locator != GLOBAL_FILE_LOCATOR


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
    try:
        stores = build_stores(resolution.capability, home)
    except (ValueError, RuntimeError) as exc:
        return ResolverObservation(
            ResolverState.UNSUPPORTED, CredentialIdentity(), (str(exc),)
        )
    observation = CanonicalCredentialResolver(resolution.capability, stores).resolve()
    return ResolverObservation(
        observation.state,
        observation.identity,
        (*observation.evidence, *resolution.evidence),
    )

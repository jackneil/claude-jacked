"""macOS Security.framework credential store adapter."""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass
from typing import Protocol

from .canonical import CredentialFormatError, CredentialPayload
from .models import InteractionMode, StoreReadResult, StoreStatus, StoreWriteResult

SERVICE_NAME = "Claude Code-credentials"
DEFAULT_NONINTERACTIVE_TIMEOUT_SECONDS = 3.0
_timed_out_reads: set[tuple[str, str]] = set()
_timed_out_reads_lock = threading.Lock()


@dataclass(frozen=True)
class NativeReadResult:
    status: StoreStatus
    data: bytes | None = None
    reason: str = ""


class NativeSecurityBackend(Protocol):
    def read(
        self, *, service: str, account: str, is_interactive: bool
    ) -> NativeReadResult: ...

    def update(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]: ...

    def add(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]: ...


def system_account_name() -> str:
    """Resolve the account through OS identity APIs, not environment variables."""
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


class PyObjCSecurityBackend:
    """Lazy PyObjC bridge for Security.framework and LocalAuthentication."""

    def __init__(self) -> None:
        try:
            import LocalAuthentication  # type: ignore[import-not-found]
            import Security  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "macOS Security.framework bridge is unavailable"
            ) from exc
        self._local_auth = LocalAuthentication
        self._security = Security

    def _query(self, service: str, account: str, is_interactive: bool) -> dict:
        security = self._security
        query = {
            security.kSecClass: security.kSecClassGenericPassword,
            security.kSecAttrService: service,
            security.kSecAttrAccount: account,
        }
        if is_interactive:
            context = self._local_auth.LAContext.alloc().init()
            query[security.kSecUseAuthenticationContext] = context
        else:
            auth_ui_key = getattr(security, "kSecUseAuthenticationUI", None)
            auth_ui_fail = getattr(security, "kSecUseAuthenticationUIFail", None)
            if auth_ui_key is None or auth_ui_fail is None:
                raise RuntimeError("Security.framework cannot disable authentication UI")
            query[auth_ui_key] = auth_ui_fail
        return query

    def _map_status(self, status: int) -> tuple[StoreStatus, str]:
        security = self._security
        if status == security.errSecSuccess:
            return StoreStatus.OK, ""
        if status == security.errSecItemNotFound:
            return StoreStatus.MISSING, "Keychain item not found"
        if status == security.errSecInteractionNotAllowed:
            return StoreStatus.INTERACTIVE_REQUIRED, "Keychain interaction required"
        if status == security.errSecUserCanceled:
            return StoreStatus.DENIED, "Keychain authorization canceled"
        if status == security.errSecDuplicateItem:
            return StoreStatus.CONCURRENT_WRITE, "Keychain item appeared concurrently"
        return StoreStatus.ERROR, f"Security.framework status {status}"

    @staticmethod
    def _status_value(result) -> int:
        return result[0] if isinstance(result, tuple) else result

    def read(
        self, *, service: str, account: str, is_interactive: bool
    ) -> NativeReadResult:
        query = self._query(service, account, is_interactive)
        query[self._security.kSecReturnData] = True
        query[self._security.kSecMatchLimit] = self._security.kSecMatchLimitOne
        result = self._security.SecItemCopyMatching(query, None)
        if isinstance(result, tuple):
            status, data = result
        else:
            status, data = result, None
        mapped, reason = self._map_status(status)
        return NativeReadResult(
            mapped, bytes(data) if data is not None else None, reason
        )

    def update(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]:
        query = self._query(service, account, is_interactive)
        result = self._security.SecItemUpdate(
            query, {self._security.kSecValueData: data}
        )
        return self._map_status(self._status_value(result))

    def add(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]:
        attributes = self._query(service, account, is_interactive)
        attributes[self._security.kSecValueData] = data
        result = self._security.SecItemAdd(attributes, None)
        return self._map_status(self._status_value(result))


class MacOSCredentialStore:
    """Full-locator Keychain adapter with foreground-only creation."""

    def __init__(
        self,
        account: str | None = None,
        *,
        backend: NativeSecurityBackend | None = None,
        noninteractive_timeout: float = DEFAULT_NONINTERACTIVE_TIMEOUT_SECONDS,
    ) -> None:
        self.account = account or system_account_name()
        self._backend = backend or PyObjCSecurityBackend()
        self._noninteractive_timeout = noninteractive_timeout

    @property
    def locator(self) -> str:
        return f"keychain:generic-password:{SERVICE_NAME}:{self.account}"

    def read(self) -> StoreReadResult:
        result = self._bounded_noninteractive_read()
        if result.status is not StoreStatus.OK or result.data is None:
            return StoreReadResult(result.status, reason=result.reason)
        try:
            return StoreReadResult(
                StoreStatus.OK, CredentialPayload.from_json(result.data)
            )
        except CredentialFormatError as exc:
            return StoreReadResult(StoreStatus.UNUSABLE, reason=str(exc))

    def _bounded_noninteractive_read(self) -> NativeReadResult:
        locator = (SERVICE_NAME, self.account)
        with _timed_out_reads_lock:
            if locator in _timed_out_reads:
                return NativeReadResult(
                    StoreStatus.ERROR,
                    reason="Keychain read disabled after a prior timeout",
                )
        results: queue.Queue[NativeReadResult] = queue.Queue(maxsize=1)

        def execute() -> None:
            try:
                result = self._backend.read(
                    service=SERVICE_NAME,
                    account=self.account,
                    is_interactive=False,
                )
            except Exception as exc:
                result = NativeReadResult(
                    StoreStatus.ERROR,
                    reason=f"Keychain read failed: {type(exc).__name__}",
                )
            try:
                results.put_nowait(result)
            except queue.Full:
                pass

        worker = threading.Thread(
            target=execute,
            name="jacked-keychain-read",
            daemon=True,
        )
        worker.start()
        try:
            return results.get(timeout=self._noninteractive_timeout)
        except queue.Empty:
            with _timed_out_reads_lock:
                _timed_out_reads.add(locator)
            return NativeReadResult(
                StoreStatus.ERROR,
                reason="Keychain read timed out; noninteractive access disabled",
            )

    def write(
        self, payload: CredentialPayload, interaction: InteractionMode
    ) -> StoreWriteResult:
        is_interactive = interaction is InteractionMode.FOREGROUND
        current = (
            self._backend.read(
                service=SERVICE_NAME,
                account=self.account,
                is_interactive=True,
            )
            if is_interactive
            else self._bounded_noninteractive_read()
        )
        if current.status is StoreStatus.INTERACTIVE_REQUIRED:
            return StoreWriteResult(current.status, current.reason)
        if current.status is StoreStatus.OK:
            status, reason = self._backend.update(
                service=SERVICE_NAME,
                account=self.account,
                data=payload.to_bytes(),
                is_interactive=is_interactive,
            )
            return self._readback(payload, status, reason, is_interactive)
        if current.status is not StoreStatus.MISSING:
            return StoreWriteResult(current.status, current.reason)
        if not is_interactive:
            return StoreWriteResult(
                StoreStatus.INTERACTIVE_REQUIRED,
                "foreground authorization is required to create the Keychain item",
            )
        status, reason = self._backend.add(
            service=SERVICE_NAME,
            account=self.account,
            data=payload.to_bytes(),
            is_interactive=True,
        )
        return self._readback(payload, status, reason, True)

    def _readback(
        self,
        target: CredentialPayload,
        status: StoreStatus,
        reason: str,
        is_interactive: bool,
    ) -> StoreWriteResult:
        if status is not StoreStatus.OK:
            return StoreWriteResult(status, reason)
        observed = self._backend.read(
            service=SERVICE_NAME,
            account=self.account,
            is_interactive=is_interactive,
        )
        if observed.status is not StoreStatus.OK or observed.data is None:
            return StoreWriteResult(observed.status, observed.reason)
        try:
            payload = CredentialPayload.from_json(observed.data)
        except CredentialFormatError as exc:
            return StoreWriteResult(StoreStatus.UNUSABLE, str(exc))
        if payload.digest != target.digest:
            return StoreWriteResult(
                StoreStatus.CONCURRENT_WRITE,
                "Keychain readback differs from the requested credential revision",
            )
        return StoreWriteResult(StoreStatus.OK)

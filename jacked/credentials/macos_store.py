"""macOS Keychain credential store adapter driven by the signed security tool."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .canonical import CredentialFormatError, CredentialPayload
from .models import InteractionMode, StoreReadResult, StoreStatus, StoreWriteResult

SERVICE_NAME = "Claude Code-credentials"
DEFAULT_NONINTERACTIVE_TIMEOUT_SECONDS = 3.0


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


logger = logging.getLogger(__name__)

SECURITY_TOOL = "/usr/bin/security"
DEFAULT_INTERACTIVE_TIMEOUT_SECONDS = 60.0
DEFAULT_SUBPROCESS_TIMEOUT_SECONDS = 2.0  # strictly below the store's thread timeout
DEFAULT_LATCH_COOLDOWN_SECONDS = 600.0

# security(1) exit statuses are the OSStatus truncated to a byte:
# errSecItemNotFound (-25300) -> 44, errSecInteractionNotAllowed (-25308) -> 36,
# errSecUserCanceled (-128) -> 128. Text is a fallback only; it is versioned
# by macOS and never surfaces in a reason string.
_EXIT_STATUS = {
    44: (StoreStatus.MISSING, "Keychain item not found"),
    36: (StoreStatus.INTERACTIVE_REQUIRED, "Keychain interaction required"),
    128: (StoreStatus.DENIED, "Keychain authorization canceled"),
}
_STDERR_HINTS = (
    (b"could not be found", StoreStatus.MISSING, "Keychain item not found"),
    (b"User interaction is not allowed", StoreStatus.INTERACTIVE_REQUIRED, "Keychain interaction required"),
    (b"User canceled", StoreStatus.DENIED, "Keychain authorization canceled"),
)


SECURITY_STDIN_MAX_LINE = 4095  # security -i splits longer lines
_HEX_ALPHABET = frozenset(b"0123456789abcdefABCDEF")

# The tool's own timeout expires inside the store's thread budget, so the
# bounded read returns normally; the store matches on this reason to latch.
_TOOL_TIMEOUT_REASON = "security tool timed out"


def _classify_failure(
    returncode: int, stderr: bytes, *, log_stderr: bool = True
) -> tuple[StoreStatus, str]:
    known = _EXIT_STATUS.get(returncode)
    if known is not None:
        return known
    for needle, status, reason in _STDERR_HINTS:
        if needle in stderr:
            return status, reason
    if log_stderr:
        logger.debug("security exit %d: %r", returncode, stderr[:300])
    else:
        logger.debug("security exit %d (write path; stderr withheld)", returncode)
    return StoreStatus.ERROR, f"security exit {returncode}"


def _unhex_if_needed(data: bytes) -> bytes:
    """``find-generic-password -w`` prints hex for any non-ASCII payload."""
    if data and len(data) % 2 == 0 and set(data) <= _HEX_ALPHABET:
        return bytes.fromhex(data.decode("ascii"))
    return data


def _quoted(value: str) -> str:
    """Quote one security -i argument; reject values its lexer cannot carry."""
    if any(ch in value for ch in '"\\\n\r\0'):
        raise ValueError("Keychain locator part contains unquotable characters")
    return f'"{value}"'


def _ascii_json(data: bytes) -> str:
    """Re-serialise a canonical payload as escaped ASCII for a quoted -w value.

    The lexer honours backslash escapes inside double quotes, so quotes and
    backslashes are escaped and non-ASCII becomes a JSON unicode escape. The parsed
    mapping, and therefore the readback digest, is unchanged.
    """
    text = json.dumps(json.loads(data.decode("utf-8")), ensure_ascii=True, separators=(",", ":"))
    return text.replace("\\", "\\\\").replace('"', '\\"')


_argv_fallback_warned = False


def _warn_argv_fallback_once() -> None:
    global _argv_fallback_warned
    if not _argv_fallback_warned:
        _argv_fallback_warned = True
        logger.warning(
            "Keychain payload exceeds the security stdin line limit; "
            "passing it as a process argument (JACKED_KEYCHAIN_ARGV_FALLBACK=1)"
        )


def reset_argv_fallback_warning() -> None:
    """Test hook: the warning is once per process, so tests must reset it."""
    global _argv_fallback_warned
    _argv_fallback_warned = False


try:  # PyObjC's own exception class is not an OSError
    import objc as _objc

    _PROBE_ERRORS: tuple[type[BaseException], ...] = (
        AttributeError, TypeError, ValueError, OSError, _objc.error
    )
except ImportError:  # pragma: no cover - non-macOS
    _PROBE_ERRORS = (AttributeError, TypeError, ValueError, OSError)

# One latch per Keychain locator for the whole process: stores are rebuilt on
# every resolution, so instance state would never survive a poll.
_latches: dict[tuple[str, str], float] = {}
_latches_lock = threading.Lock()


def clear_keychain_latches() -> None:
    with _latches_lock:
        _latches.clear()


def keychain_is_locked(*, security_module=None) -> bool:
    """Prompt-free: reads the default keychain's status bits, touches no item.

    Returns False when the framework bridge is unavailable so the caller
    falls through to the bounded tool call instead of failing closed twice.
    """
    module = security_module
    if module is None:
        try:
            import Security as module  # type: ignore[import-not-found]
        except ImportError:
            return False
    try:
        status, keychain = module.SecKeychainCopyDefault(None)
        if status != 0 or keychain is None:
            return False
        status, flags = module.SecKeychainGetStatus(keychain, None)
        if status != 0:
            return False
        return not bool(flags & module.kSecUnlockStateStatus)
    except _PROBE_ERRORS:
        logger.debug("keychain status probe failed", exc_info=True)
        return False


class SecurityCliBackend:
    """Drive Apple's signed ``security`` tool, the same client Claude Code uses.

    A Keychain item carries an access list of the applications allowed to read
    it. Claude Code creates its item through ``security``, so ``security`` is
    trusted from the start, while an in-process Security.framework caller is
    identified as the Python binary and prompts for the login password on
    every new Python build. Using the same tool removes that prompt class.

    Reads pass only the locator on argv. Writes run ``security -i`` and send
    the command on stdin so the secret is never a process argument.
    ``subprocess.run`` kills the child when a timeout expires.
    """

    def __init__(
        self,
        *,
        run=subprocess.run,
        tool: str = SECURITY_TOOL,
        interactive_timeout: float = DEFAULT_INTERACTIVE_TIMEOUT_SECONDS,
        noninteractive_timeout: float = DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    ) -> None:
        self._run = run
        self._tool = tool
        self._interactive_timeout = interactive_timeout
        self._noninteractive_timeout = noninteractive_timeout

    def _invoke(self, args: list[str], *, is_interactive: bool, stdin: bytes | None = None):
        timeout = (
            self._interactive_timeout if is_interactive else self._noninteractive_timeout
        )
        kwargs = {"capture_output": True, "text": False, "timeout": timeout, "check": False}
        if stdin is not None:
            kwargs["input"] = stdin
        return self._run([self._tool, *args], **kwargs)

    def read(
        self, *, service: str, account: str, is_interactive: bool
    ) -> NativeReadResult:
        try:
            completed = self._invoke(
                ["find-generic-password", "-a", account, "-s", service, "-w"],
                is_interactive=is_interactive,
            )
        except subprocess.TimeoutExpired:
            return NativeReadResult(StoreStatus.ERROR, reason=_TOOL_TIMEOUT_REASON)
        except OSError as exc:
            return NativeReadResult(StoreStatus.ERROR, reason=f"security tool unavailable: {exc}")
        if completed.returncode != 0:
            status, reason = _classify_failure(completed.returncode, completed.stderr or b"")
            return NativeReadResult(status, reason=reason)
        return NativeReadResult(StoreStatus.OK, _unhex_if_needed((completed.stdout or b"").strip()))

    def _stdin_command(self, *, service: str, account: str, data: bytes) -> bytes:
        """Build the ``security -i`` upsert line, hex first then escaped JSON."""
        command = (
            f"add-generic-password -U -a {_quoted(account)} -s {_quoted(service)} "
            f"-X {data.hex()}\n"
        ).encode("utf-8")
        if len(command) > SECURITY_STDIN_MAX_LINE:
            # security -i splits lines at 4096 bytes, which would store a
            # truncated secret. Hex doubles the payload; the escaped JSON form
            # (-w) fits roughly twice as much. _ascii_json has already applied
            # the lexer's escapes, so the value is wrapped, not re-quoted.
            command = (
                f"add-generic-password -U -a {_quoted(account)} -s {_quoted(service)} "
                f'-w "{_ascii_json(data)}"\n'
            ).encode("utf-8")
        return command

    def _upsert(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]:
        try:
            command = self._stdin_command(service=service, account=account, data=data)
        except ValueError:
            return StoreStatus.ERROR, "Keychain locator part contains unquotable characters"
        if len(command) <= SECURITY_STDIN_MAX_LINE:
            args, stdin = ["-i"], command
        elif os.environ.get("JACKED_KEYCHAIN_ARGV_FALLBACK") == "1":
            # argv is readable by other local users through setuid ps and by
            # endpoint agents, so it is opt-in only.
            _warn_argv_fallback_once()
            args = ["add-generic-password", "-U", "-a", account, "-s", service, "-X", data.hex()]
            stdin = None
        else:
            return (
                StoreStatus.UNUSABLE,
                f"Keychain payload of {len(data)} bytes exceeds the security tool's "
                f"{SECURITY_STDIN_MAX_LINE}-byte stdin line limit; set "
                "JACKED_KEYCHAIN_ARGV_FALLBACK=1 to allow a process-argument write",
            )
        try:
            completed = self._invoke(args, is_interactive=is_interactive, stdin=stdin)
        except subprocess.TimeoutExpired:
            # Never stringify the exception: its .cmd carries the full argv.
            return StoreStatus.ERROR, _TOOL_TIMEOUT_REASON
        except OSError as exc:
            return StoreStatus.ERROR, f"security tool unavailable: {exc}"
        if completed.returncode != 0:
            # Never log write-path stderr: a split or rejected line can echo
            # fragments of the hex payload.
            return _classify_failure(completed.returncode, completed.stderr or b"", log_stderr=False)
        return StoreStatus.OK, ""

    def update(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]:
        return self._upsert(service=service, account=account, data=data, is_interactive=is_interactive)

    def add(
        self, *, service: str, account: str, data: bytes, is_interactive: bool
    ) -> tuple[StoreStatus, str]:
        return self._upsert(service=service, account=account, data=data, is_interactive=is_interactive)


class MacOSCredentialStore:
    """Full-locator Keychain adapter with foreground-only creation."""

    def __init__(
        self,
        account: str | None = None,
        *,
        backend: NativeSecurityBackend | None = None,
        noninteractive_timeout: float = DEFAULT_NONINTERACTIVE_TIMEOUT_SECONDS,
        latch_cooldown: float = DEFAULT_LATCH_COOLDOWN_SECONDS,
        lock_probe: Callable[[], bool] = keychain_is_locked,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.account = account or system_account_name()
        self._backend = backend or SecurityCliBackend()
        self._noninteractive_timeout = noninteractive_timeout
        self._latch_cooldown = latch_cooldown
        self._lock_probe = lock_probe
        self._clock = clock

    @property
    def locator(self) -> str:
        return f"keychain:generic-password:{SERVICE_NAME}:{self.account}"

    @property
    def _latch_key(self) -> tuple[str, str]:
        return (SERVICE_NAME, self.account)

    def _latched(self) -> bool:
        with _latches_lock:
            return _latches.get(self._latch_key, 0.0) > self._clock()

    def _latch(self) -> None:
        with _latches_lock:
            _latches[self._latch_key] = self._clock() + self._latch_cooldown

    def _note_interactive_success(self) -> None:
        with _latches_lock:
            _latches.pop(self._latch_key, None)

    def _latched_refusal(self) -> NativeReadResult | None:
        if self._latched():
            return NativeReadResult(
                StoreStatus.ERROR, reason="Keychain access paused after a prior timeout"
            )
        return None

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
        refusal = self._latched_refusal()
        if refusal is not None:
            return refusal
        results: queue.Queue[NativeReadResult] = queue.Queue(maxsize=1)

        def execute() -> None:
            try:
                if self._lock_probe():
                    result = NativeReadResult(
                        StoreStatus.INTERACTIVE_REQUIRED,
                        reason="login keychain is locked",
                    )
                else:
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
            result = results.get(timeout=self._noninteractive_timeout)
        except queue.Empty:
            self._latch()
            return NativeReadResult(
                StoreStatus.ERROR,
                reason="Keychain read timed out; noninteractive access disabled",
            )
        # The tool's own timeout is shorter than this wait, so a blocked call
        # returns here rather than through queue.Empty. Latch either way.
        if result.status is StoreStatus.ERROR and result.reason == _TOOL_TIMEOUT_REASON:
            self._latch()
        return result

    def write(
        self, payload: CredentialPayload, interaction: InteractionMode
    ) -> StoreWriteResult:
        if interaction is InteractionMode.BACKGROUND and (
            refusal := self._latched_refusal()
        ) is not None:
            return StoreWriteResult(StoreStatus.INTERACTIVE_REQUIRED, refusal.reason)
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
        if is_interactive and current.status in {StoreStatus.OK, StoreStatus.MISSING}:
            self._note_interactive_success()
        if current.status is StoreStatus.INTERACTIVE_REQUIRED:
            return StoreWriteResult(current.status, current.reason)
        if current.status is StoreStatus.OK:
            status, reason = self._backend.update(
                service=SERVICE_NAME,
                account=self.account,
                data=payload.to_bytes(),
                is_interactive=is_interactive,
            )
            if is_interactive and status is StoreStatus.OK:
                self._note_interactive_success()
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
        if status is StoreStatus.OK:
            self._note_interactive_success()
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
                StoreStatus.ERROR,
                "Keychain readback differs from the requested credential revision",
            )
        return StoreWriteResult(StoreStatus.OK)

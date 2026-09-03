"""Win32 named-pipe lifecycle-control transport."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jacked.service.ipc_protocol import (
    ControlAction,
    FrameError,
    MAX_FRAME_BYTES,
    ReplayGuard,
    _build_control_request,
    _dispatch_control_request,
    _validate_control_response,
    decode_frame,
    encode_frame,
    windows_named_pipe_policy,
)


def _validate_windows_pipe_name(address: str) -> str:
    prefix = "\\\\.\\pipe\\jacked-v2-"
    suffix = address.removeprefix(prefix)
    if (
        not suffix
        or address != prefix + suffix
        or len(suffix) > 64
        or any(char not in "0123456789abcdef" for char in suffix)
    ):
        raise ValueError("invalid jacked named-pipe address")
    return address


class _WindowsPipeApi:
    """Small ctypes boundary for the Win32 named-pipe calls we use."""

    INVALID_HANDLE_VALUE = -1
    ERROR_PIPE_CONNECTED = 535
    ERROR_MORE_DATA = 234
    ERROR_OPERATION_ABORTED = 995
    PIPE_ACCESS_DUPLEX = 0x00000003
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_MESSAGE = 0x00000004
    PIPE_READMODE_MESSAGE = 0x00000002
    PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    SECURITY_SQOS_PRESENT = 0x00100000
    SECURITY_IDENTIFICATION = 0x00010000

    def __init__(self, sddl: str | None = None):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
        self.kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.kernel32.ConnectNamedPipe.restype = wintypes.BOOL
        self.kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self.kernel32.WaitNamedPipeW.restype = wintypes.BOOL
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.SetNamedPipeHandleState.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.SetNamedPipeHandleState.restype = wintypes.BOOL
        self.kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.kernel32.ReadFile.restype = wintypes.BOOL
        self.kernel32.WriteFile.argtypes = self.kernel32.ReadFile.argtypes
        self.kernel32.WriteFile.restype = wintypes.BOOL
        self.kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.kernel32.CancelIoEx.restype = wintypes.BOOL
        self.kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
        self.kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel32.LocalFree.restype = ctypes.c_void_p
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            wintypes.BOOL
        )
        self.security_descriptor = ctypes.c_void_p()
        self.security_attributes = None
        if sddl is not None:

            class SecurityAttributes(ctypes.Structure):
                _fields_ = [
                    ("nLength", wintypes.DWORD),
                    ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL),
                ]

            converted = (
                self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl, 1, ctypes.byref(self.security_descriptor), None
                )
            )
            if not converted:
                raise ctypes.WinError(ctypes.get_last_error())
            self.security_attributes = SecurityAttributes(
                ctypes.sizeof(SecurityAttributes), self.security_descriptor, False
            )

    def close(self) -> None:
        if self.security_descriptor:
            self.kernel32.LocalFree(self.security_descriptor)
            self.security_descriptor = self.ctypes.c_void_p()

    def create_server(self, address: str) -> int:
        attributes = (
            self.ctypes.byref(self.security_attributes)
            if self.security_attributes is not None
            else None
        )
        handle = self.kernel32.CreateNamedPipeW(
            address,
            self.PIPE_ACCESS_DUPLEX | self.FILE_FLAG_FIRST_PIPE_INSTANCE,
            self.PIPE_TYPE_MESSAGE
            | self.PIPE_READMODE_MESSAGE
            | self.PIPE_REJECT_REMOTE_CLIENTS,
            1,
            MAX_FRAME_BYTES + 4,
            MAX_FRAME_BYTES + 4,
            3_000,
            attributes,
        )
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return handle

    def connect_server(self, handle: int) -> None:
        if self.kernel32.ConnectNamedPipe(handle, None):
            return
        error = self.ctypes.get_last_error()
        if error != self.ERROR_PIPE_CONNECTED:
            raise self.ctypes.WinError(error)

    def open_client(self, address: str, timeout: float) -> int:
        milliseconds = max(1, min(30_000, int(timeout * 1000)))
        if not self.kernel32.WaitNamedPipeW(address, milliseconds):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        handle = self.kernel32.CreateFileW(
            address,
            self.GENERIC_READ | self.GENERIC_WRITE,
            0,
            None,
            self.OPEN_EXISTING,
            self.SECURITY_SQOS_PRESENT | self.SECURITY_IDENTIFICATION,
            None,
        )
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        mode = self.wintypes.DWORD(self.PIPE_READMODE_MESSAGE)
        if not self.kernel32.SetNamedPipeHandleState(
            handle, self.ctypes.byref(mode), None, None
        ):
            self.close_handle(handle)
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return handle

    def read(self, handle: int) -> bytes:
        buffer = self.ctypes.create_string_buffer(MAX_FRAME_BYTES + 5)
        count = self.wintypes.DWORD()
        ok = self.kernel32.ReadFile(
            handle, buffer, len(buffer), self.ctypes.byref(count), None
        )
        if not ok:
            error = self.ctypes.get_last_error()
            if error == self.ERROR_MORE_DATA:
                raise FrameError("control frame is too large")
            raise self.ctypes.WinError(error)
        return buffer.raw[: count.value]

    def write(self, handle: int, payload: bytes) -> None:
        count = self.wintypes.DWORD()
        buffer = self.ctypes.create_string_buffer(payload)
        if not self.kernel32.WriteFile(
            handle, buffer, len(payload), self.ctypes.byref(count), None
        ):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        if count.value != len(payload):
            raise OSError("named-pipe write was incomplete")

    def cancel(self, handle: int) -> None:
        self.kernel32.CancelIoEx(handle, None)

    def disconnect(self, handle: int) -> None:
        self.kernel32.DisconnectNamedPipe(handle)

    def close_handle(self, handle: int) -> None:
        self.kernel32.CloseHandle(handle)


class WindowsControlServer:
    """Current-user-only, local-only, first-instance Windows pipe server."""

    def __init__(
        self,
        address: str,
        *,
        manifest_provider: Callable[[], Any],
        handler: Callable[[ControlAction], dict[str, Any]],
    ):
        self.address = _validate_windows_pipe_name(address)
        self.manifest_provider = manifest_provider
        self.handler = handler
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._replay = ReplayGuard()
        self._handle: int | None = None
        self._lock = threading.Lock()
        self._api: _WindowsPipeApi | None = None

    def start(self) -> None:
        from jacked.service.instance import current_user_identity

        identity = current_user_identity()
        if not identity.startswith("sid:"):
            raise OSError("Windows control requires a current-user SID")
        policy = windows_named_pipe_policy(identity.removeprefix("sid:"))
        self._api = _WindowsPipeApi(policy.sddl)
        try:
            self._handle = self._api.create_server(self.address)
        except BaseException:
            self._api.close()
            self._api = None
            raise
        self._thread = threading.Thread(
            target=self._serve, name="jacked-control", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        from jacked.service.instance import current_user_identity

        assert self._api is not None
        while not self._stop.is_set():
            with self._lock:
                handle = self._handle
            if handle is None:
                return
            try:
                self._api.connect_server(handle)
                payload = decode_frame(self._api.read(handle))
                response = _dispatch_control_request(
                    payload,
                    peer=current_user_identity(),
                    manifest_provider=self.manifest_provider,
                    handler=self.handler,
                    replay_guard=self._replay,
                )
            except Exception as exc:
                if self._stop.is_set():
                    return
                response = {"ok": False, "error": type(exc).__name__}
            try:
                self._api.write(handle, encode_frame(response))
            except OSError:
                pass
            self._api.disconnect(handle)
            self._api.close_handle(handle)
            with self._lock:
                if self._stop.is_set():
                    self._handle = None
                    return
                try:
                    self._handle = self._api.create_server(self.address)
                except OSError:
                    self._handle = None
                    return

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            handle = self._handle
            self._handle = None
        if self._api is not None and handle is not None:
            self._api.cancel(handle)
            self._api.close_handle(handle)
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._api is not None:
            self._api.close()
            self._api = None


def send_windows_control(
    manifest_path: Path,
    action: ControlAction,
    *,
    timeout: float = 3,
) -> dict[str, Any]:
    """Send one framed authenticated request through the manifest-named pipe."""

    manifest, request = _build_control_request(manifest_path, action, timeout)
    address = _validate_windows_pipe_name(manifest.control_address)
    api = _WindowsPipeApi()
    handle: int | None = None
    try:
        handle = api.open_client(address, timeout)
        api.write(handle, encode_frame(request.to_wire()))
        return _validate_control_response(decode_frame(api.read(handle)))
    finally:
        if handle is not None:
            api.close_handle(handle)
        api.close()

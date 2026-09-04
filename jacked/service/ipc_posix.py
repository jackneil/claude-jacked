"""Unix-domain-socket lifecycle-control transport."""

from __future__ import annotations

import os
import socket
import stat
import struct
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jacked.service.ipc_protocol import (
    ControlAction,
    ReplayGuard,
    _build_control_request,
    _dispatch_control_request,
    _validate_control_response,
    encode_frame,
    recv_frame,
)


_HOST_IS_DARWIN = sys.platform == "darwin"


def _darwin_peer_identity(connection: socket.socket) -> str:
    """Read peer credentials with Darwin's native getpeereid(2)."""

    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    getpeereid = libc.getpeereid
    getpeereid.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    getpeereid.restype = ctypes.c_int
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return f"uid:{uid.value}"


def posix_peer_identity(connection: socket.socket) -> str:
    """Return the kernel-authenticated UID of a UDS peer."""

    if hasattr(connection, "getpeereid"):
        uid, _gid = connection.getpeereid()
        return f"uid:{uid}"
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return f"uid:{uid}"
    if _HOST_IS_DARWIN:
        return _darwin_peer_identity(connection)
    raise OSError("peer credentials are unavailable on this POSIX platform")


def create_posix_listener(path: Path, backlog: int = 8) -> socket.socket:
    """Create a private first-owner UDS endpoint.

    Existing endpoints are never unlinked here.  Their ownership must first be
    classified by instance reconciliation.
    """

    if path.exists() or path.is_symlink():
        raise FileExistsError(f"control endpoint already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(backlog)
        return listener
    except BaseException:
        listener.close()
        raise


class PosixControlServer:
    """Small UDS server for the three allowlisted lifecycle actions."""

    def __init__(
        self,
        path: Path,
        *,
        manifest_provider: Callable[[], Any],
        handler: Callable[[ControlAction], dict[str, Any]],
    ):
        self.path = path
        self.manifest_provider = manifest_provider
        self.handler = handler
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._replay = ReplayGuard()

    def start(self) -> None:
        self._listener = create_posix_listener(self.path)
        self._listener.settimeout(0.5)
        self._thread = threading.Thread(
            target=self._serve, name="jacked-control", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with connection:
                connection.settimeout(2)
                try:
                    peer = posix_peer_identity(connection)
                    response = _dispatch_control_request(
                        recv_frame(connection),
                        peer=peer,
                        manifest_provider=self.manifest_provider,
                        handler=self.handler,
                        replay_guard=self._replay,
                    )
                except Exception as exc:
                    response = {"ok": False, "error": type(exc).__name__}
                try:
                    connection.sendall(encode_frame(response))
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            status = self.path.lstat()
            if stat.S_ISSOCK(status.st_mode) and (
                os.name != "posix" or status.st_uid == os.getuid()
            ):
                self.path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def send_posix_control(
    manifest_path: Path,
    action: ControlAction,
    *,
    timeout: float = 3,
) -> dict[str, Any]:
    """Send one authenticated action to the exact manifest-named UDS."""

    manifest, request = _build_control_request(manifest_path, action, timeout)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(manifest.control_address)
        connection.sendall(encode_frame(request.to_wire()))
        response = recv_frame(connection)
    finally:
        connection.close()
    return _validate_control_response(response)

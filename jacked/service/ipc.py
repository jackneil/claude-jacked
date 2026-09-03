"""Compatibility facade for native lifecycle-control transports."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from jacked.service.ipc_posix import (
    PosixControlServer,
    create_posix_listener,
    posix_peer_identity,
    send_posix_control,
)
from jacked.service.ipc_protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ControlAction,
    ControlRequest,
    FrameError,
    NativeControlAddress,
    ReplayGuard,
    WindowsNamedPipePolicy,
    decode_frame,
    encode_frame,
    native_control_address,
    recv_frame,
    verify_request,
    windows_named_pipe_policy,
)
from jacked.service.ipc_windows import WindowsControlServer, send_windows_control


def create_control_server(
    address: Path | str,
    *,
    manifest_provider: Callable[[], Any],
    handler: Callable[[ControlAction], dict[str, Any]],
    platform: str | None = None,
) -> PosixControlServer | WindowsControlServer:
    selected = os.sys.platform if platform is None else platform
    if selected == "win32":
        return WindowsControlServer(
            str(address), manifest_provider=manifest_provider, handler=handler
        )
    return PosixControlServer(
        Path(address), manifest_provider=manifest_provider, handler=handler
    )


def send_native_control(
    manifest_path: Path,
    action: ControlAction,
    *,
    timeout: float = 3,
    platform: str | None = None,
) -> dict[str, Any]:
    selected = os.sys.platform if platform is None else platform
    if selected == "win32":
        return send_windows_control(manifest_path, action, timeout=timeout)
    return send_posix_control(manifest_path, action, timeout=timeout)


__all__ = [
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "ControlAction",
    "ControlRequest",
    "FrameError",
    "NativeControlAddress",
    "PosixControlServer",
    "ReplayGuard",
    "WindowsControlServer",
    "WindowsNamedPipePolicy",
    "create_control_server",
    "create_posix_listener",
    "decode_frame",
    "encode_frame",
    "native_control_address",
    "posix_peer_identity",
    "recv_frame",
    "send_native_control",
    "send_posix_control",
    "send_windows_control",
    "verify_request",
    "windows_named_pipe_policy",
]

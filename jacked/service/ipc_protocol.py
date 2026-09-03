"""Framed, authenticated lifecycle-control protocol primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 65_536


class FrameError(ValueError):
    pass


class ControlAction(str, Enum):
    STATUS = "status"
    SHUTDOWN = "graceful_shutdown"
    RESTART_HANDOFF = "restart_handoff"


@dataclass(frozen=True)
class ControlRequest:
    action: ControlAction
    instance_id: str
    creation_id: str
    generation: str
    peer_id: str
    request_nonce: str
    expires_at: int
    protocol_version: int = PROTOCOL_VERSION
    authentication: str = ""

    def _transcript(self) -> bytes:
        payload = asdict(self)
        payload["action"] = self.action.value
        payload.pop("authentication", None)
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )

    def sign(self, secret: bytes) -> "ControlRequest":
        authentication = hmac.new(
            secret, self._transcript(), hashlib.sha256
        ).hexdigest()
        return replace(self, authentication=authentication)

    def to_wire(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.value
        return payload

    def with_field(self, name: str, value: Any) -> "ControlRequest":
        return replace(self, **{name: value})

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> "ControlRequest":
        expected = {
            "action",
            "instance_id",
            "creation_id",
            "generation",
            "peer_id",
            "request_nonce",
            "expires_at",
            "protocol_version",
            "authentication",
        }
        if set(payload) != expected:
            raise FrameError("control request has missing or unknown fields")
        try:
            return cls(**{**payload, "action": ControlAction(payload["action"])})
        except (TypeError, ValueError) as exc:
            raise FrameError("control request is invalid") from exc


class ReplayGuard:
    """Thread-safe, bounded one-time nonce consumer."""

    def __init__(self, capacity: int = 1024):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, nonce: str, expires_at: int, *, now: int) -> bool:
        with self._lock:
            for key in tuple(self._seen):
                if self._seen[key] < now:
                    del self._seen[key]
            if nonce in self._seen:
                return False
            self._seen[nonce] = expires_at
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            return True


def verify_request(
    request: ControlRequest,
    secret: bytes,
    *,
    expected_peer: str,
    expected_instance: str,
    expected_creation: str,
    expected_generation: str,
    replay_guard: ReplayGuard,
    now: int | None = None,
    max_future_seconds: int = 60,
) -> None:
    current = int(time.time()) if now is None else now
    if request.protocol_version != PROTOCOL_VERSION:
        raise ValueError("unsupported control protocol")
    if request.expires_at < current:
        raise ValueError("control request expired")
    if request.expires_at > current + max_future_seconds:
        raise ValueError("control request expiry is outside the allowed window")
    expected_auth = hmac.new(secret, request._transcript(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.authentication, expected_auth):
        raise ValueError("control request authentication failed")
    comparisons = (
        (request.peer_id, expected_peer, "peer"),
        (request.instance_id, expected_instance, "instance"),
        (request.creation_id, expected_creation, "creation identity"),
        (request.generation, expected_generation, "generation"),
    )
    for observed, expected, label in comparisons:
        if not hmac.compare_digest(observed, expected):
            raise ValueError(f"control request {label} mismatch")
    if not replay_guard.consume(request.request_nonce, request.expires_at, now=current):
        raise ValueError("control request replay rejected")


def encode_frame(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise FrameError("control frame is too large")
    return struct.pack("!I", len(body)) + body


def decode_frame(frame: bytes) -> dict[str, Any]:
    if len(frame) < 4:
        raise FrameError("control frame is truncated")
    length = struct.unpack("!I", frame[:4])[0]
    if length > MAX_FRAME_BYTES:
        raise FrameError("control frame is too large")
    if len(frame) != 4 + length:
        raise FrameError("control frame length mismatch")
    try:
        payload = json.loads(
            frame[4:].decode("utf-8"), object_pairs_hook=_strict_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError("control frame is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FrameError("control frame payload must be an object")
    return payload


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError(f"duplicate control field: {key}")
        result[key] = value
    return result


def recv_frame(connection: socket.socket) -> dict[str, Any]:
    header = _recv_exact(connection, 4)
    length = struct.unpack("!I", header)[0]
    if length > MAX_FRAME_BYTES:
        raise FrameError("control frame is too large")
    return decode_frame(header + _recv_exact(connection, length))


def _recv_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise FrameError("control frame is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


@dataclass(frozen=True)
class NativeControlAddress:
    kind: str
    address: str
    owner_identity: str


@dataclass(frozen=True)
class WindowsNamedPipePolicy:
    sddl: str
    first_pipe_instance: bool = True
    reject_remote_clients: bool = True
    message_mode: bool = True
    max_frame_bytes: int = MAX_FRAME_BYTES


def windows_named_pipe_policy(user_sid: str) -> WindowsNamedPipePolicy:
    """Security contract for the Win32 CreateNamedPipeW adapter."""

    if not user_sid.startswith("S-") or any(char in user_sid for char in "()\x00\r\n"):
        raise ValueError("a canonical Windows SID is required")
    return WindowsNamedPipePolicy(sddl=f"D:P(A;;GA;;;{user_sid})")


def native_control_address(
    path: Path, user_identity: str, *, platform: str
) -> NativeControlAddress:
    if platform == "win32":
        safe_user = hashlib.sha256(user_identity.encode("utf-8")).hexdigest()[:20]
        return NativeControlAddress(
            "named-pipe", rf"\\.\pipe\jacked-v2-{safe_user}", user_identity
        )
    if not path.is_absolute():
        raise ValueError("UDS path must be absolute")
    return NativeControlAddress("unix", str(path), user_identity)


def _build_control_request(
    manifest_path: Path, action: ControlAction, timeout: float
) -> tuple[Any, ControlRequest]:
    from jacked.service.instance import current_user_identity, read_manifest

    manifest = read_manifest(manifest_path)
    request = ControlRequest(
        action=action,
        instance_id=manifest.instance_id,
        creation_id=manifest.process.creation_id,
        generation=manifest.generation,
        peer_id=current_user_identity(),
        request_nonce=secrets.token_urlsafe(24),
        expires_at=int(time.time()) + min(30, max(1, int(timeout) + 2)),
    ).sign(manifest.control_nonce.encode("utf-8"))
    return manifest, request


def _validate_control_response(response: dict[str, Any]) -> dict[str, Any]:
    if set(response) not in ({"ok", "result"}, {"ok", "error"}):
        raise FrameError("control response has missing or unknown fields")
    if not isinstance(response.get("ok"), bool):
        raise FrameError("control response status is invalid")
    return response


def _dispatch_control_request(
    payload: dict[str, Any],
    *,
    peer: str,
    manifest_provider: Callable[[], Any],
    handler: Callable[[ControlAction], dict[str, Any]],
    replay_guard: ReplayGuard,
) -> dict[str, Any]:
    request = ControlRequest.from_wire(payload)
    manifest = manifest_provider()
    if manifest is None:
        raise ValueError("service manifest is unavailable")
    verify_request(
        request,
        manifest.control_nonce.encode("utf-8"),
        expected_peer=peer,
        expected_instance=manifest.instance_id,
        expected_creation=manifest.process.creation_id,
        expected_generation=manifest.generation,
        replay_guard=replay_guard,
    )
    return {"ok": True, "result": handler(request.action)}

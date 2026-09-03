import time

import pytest

from jacked.service.ipc import (
    ControlAction,
    ControlRequest,
    FrameError,
    ReplayGuard,
    decode_frame,
    encode_frame,
    verify_request,
    create_control_server,
    native_control_address,
    send_native_control,
    windows_named_pipe_policy,
)


def _request(**changes):
    values = {
        "action": ControlAction.STATUS,
        "instance_id": "instance-1",
        "creation_id": "creation-1",
        "generation": "a" * 64,
        "peer_id": "uid:501",
        "request_nonce": "nonce-1",
        "expires_at": int(time.time()) + 30,
    }
    values.update(changes)
    return ControlRequest(**values).sign(b"control-secret")


def test_frame_roundtrip_is_bounded_and_versioned():
    request = _request()
    assert decode_frame(encode_frame(request.to_wire())) == request.to_wire()
    with pytest.raises(FrameError, match="large"):
        encode_frame({"value": "x" * 70_000})


def test_complete_transcript_is_authenticated():
    request = _request()
    verify_request(
        request,
        b"control-secret",
        expected_peer="uid:501",
        expected_instance="instance-1",
        expected_creation="creation-1",
        expected_generation="a" * 64,
        replay_guard=ReplayGuard(),
    )
    tampered = request.with_field("action", ControlAction.SHUTDOWN)
    with pytest.raises(ValueError, match="authentication"):
        verify_request(
            tampered,
            b"control-secret",
            expected_peer="uid:501",
            expected_instance="instance-1",
            expected_creation="creation-1",
            expected_generation="a" * 64,
            replay_guard=ReplayGuard(),
        )


def test_peer_generation_expiry_and_replay_fail_closed():
    guard = ReplayGuard()
    request = _request()
    verify_request(
        request,
        b"control-secret",
        expected_peer="uid:501",
        expected_instance="instance-1",
        expected_creation="creation-1",
        expected_generation="a" * 64,
        replay_guard=guard,
    )
    with pytest.raises(ValueError, match="replay"):
        verify_request(
            request,
            b"control-secret",
            expected_peer="uid:501",
            expected_instance="instance-1",
            expected_creation="creation-1",
            expected_generation="a" * 64,
            replay_guard=guard,
        )
    with pytest.raises(ValueError, match="expired"):
        verify_request(
            _request(request_nonce="nonce-2", expires_at=1),
            b"control-secret",
            expected_peer="uid:501",
            expected_instance="instance-1",
            expected_creation="creation-1",
            expected_generation="a" * 64,
            replay_guard=ReplayGuard(),
            now=2,
        )


def test_only_safe_lifecycle_actions_exist():
    assert {item.value for item in ControlAction} == {
        "status",
        "graceful_shutdown",
        "restart_handoff",
    }


def test_windows_pipe_policy_is_first_instance_local_and_sid_private():
    policy = windows_named_pipe_policy("S-1-5-21-123")
    assert policy.first_pipe_instance is True
    assert policy.reject_remote_clients is True
    assert policy.sddl == "D:P(A;;GA;;;S-1-5-21-123)"


def test_native_control_address_and_server_select_windows_pipe(tmp_path):
    from jacked.service.ipc import WindowsControlServer

    address = native_control_address(
        tmp_path / "ignored.sock", "sid:S-1-5-21-123", platform="win32"
    )
    assert address.kind == "named-pipe"
    assert address.address.startswith(r"\\.\pipe\jacked-v2-")
    server = create_control_server(
        address.address,
        manifest_provider=lambda: None,
        handler=lambda _action: {},
        platform="win32",
    )
    assert isinstance(server, WindowsControlServer)


def test_send_native_control_dispatches_without_platform_fallback(
    monkeypatch, tmp_path
):
    calls = []

    def fake_windows(path, action, *, timeout):
        calls.append((path, action, timeout))
        return {"ok": True, "result": {}}

    monkeypatch.setattr("jacked.service.ipc.send_windows_control", fake_windows)
    result = send_native_control(
        tmp_path / "manifest",
        ControlAction.STATUS,
        timeout=1.5,
        platform="win32",
    )
    assert result["ok"] is True
    assert calls == [(tmp_path / "manifest", ControlAction.STATUS, 1.5)]

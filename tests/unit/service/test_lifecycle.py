from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jacked.service.lifecycle import handoff_owned_service
from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import SupervisorAction


def test_handoff_authenticates_shutdown_then_uses_exact_supervisor(
    monkeypatch, tmp_path
):
    old = SimpleNamespace(
        instance_id="old",
        generation="old-generation",
        supervisor=SupervisorKind.SYSTEMD_USER.value,
    )
    reads = iter((old, FileNotFoundError()))

    def read(_path):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    spec = MagicMock(
        generation="a" * 64,
        supervisor=SupervisorKind.SYSTEMD_USER,
    )
    paths = SimpleNamespace(manifest=tmp_path / "manifest", root=tmp_path)
    control = MagicMock(
        side_effect=[
            {"ok": True, "result": {"accepted": True}},
            {
                "ok": True,
                "result": {
                    "state": "running",
                    "generation": spec.generation,
                    "instance_id": "new",
                },
            },
        ]
    )
    install = MagicMock(return_value=SupervisorAction(True, "install", "exact"))
    spawn = MagicMock()
    monkeypatch.setattr("jacked.service.instance.read_manifest", read)
    monkeypatch.setattr("jacked.service.ipc.send_native_control", control)
    monkeypatch.setattr("jacked.service.lifecycle.install_owned_supervisor", install)
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", spawn)

    result = handoff_owned_service(spec, environment={}, paths=paths)

    assert result.ok is True
    assert control.call_count == 2
    install.assert_called_once()
    spawn.assert_not_called()


def test_handoff_never_spawns_when_old_owner_does_not_exit(monkeypatch, tmp_path):
    old = SimpleNamespace(instance_id="old", generation="old-generation")
    spec = MagicMock(generation="a" * 64, supervisor=SupervisorKind.MANUAL)
    paths = SimpleNamespace(manifest=tmp_path / "manifest", root=tmp_path)
    monkeypatch.setattr("jacked.service.instance.read_manifest", lambda _path: old)
    monkeypatch.setattr(
        "jacked.service.ipc.send_native_control",
        lambda *_args, **_kwargs: {"ok": True, "result": {"accepted": True}},
    )
    spawn = MagicMock()
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", spawn)

    result = handoff_owned_service(spec, environment={}, paths=paths, timeout=0)

    assert result.ok is False
    assert "did not exit" in result.reason
    spawn.assert_not_called()


def test_managed_handoff_never_spawns_after_foreign_artifact_refusal(
    monkeypatch, tmp_path
):
    old = SimpleNamespace(
        instance_id="old",
        generation="old-generation",
        supervisor=SupervisorKind.LAUNCHD.value,
    )
    reads = iter((old, FileNotFoundError()))

    def read(_path):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    spec = MagicMock(generation="a" * 64, supervisor=SupervisorKind.LAUNCHD)
    paths = SimpleNamespace(manifest=tmp_path / "manifest", root=tmp_path)
    monkeypatch.setattr("jacked.service.instance.read_manifest", read)
    monkeypatch.setattr(
        "jacked.service.ipc.send_native_control",
        lambda *_args, **_kwargs: {"ok": True, "result": {"accepted": True}},
    )
    monkeypatch.setattr(
        "jacked.service.lifecycle.install_owned_supervisor",
        lambda *_args, **_kwargs: SupervisorAction(
            False, "refused", "supervisor artifact is foreign"
        ),
    )
    spawn = MagicMock()
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", spawn)

    result = handoff_owned_service(spec, environment={}, paths=paths)

    assert result.ok is False
    assert "managed supervisor" in result.reason
    spawn.assert_not_called()


def test_default_paths_use_real_legacy_pid_location(monkeypatch, tmp_path):
    import jacked.service.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "CLAUDE_DIR", tmp_path)
    paths = lifecycle.default_service_paths()
    assert paths.root == tmp_path / "jacked-service-v2"
    assert paths.legacy_pid == tmp_path / "jacked-service.pid"


def test_reconcile_compatibility_name_is_read_only(monkeypatch, tmp_path):
    import jacked.service.lifecycle as lifecycle

    artifact = MagicMock()
    inspection = MagicMock()
    monkeypatch.setattr(lifecycle, "render_for_spec", lambda *_args, **_kwargs: artifact)
    inspect = MagicMock(return_value=inspection)
    monkeypatch.setattr(lifecycle, "inspect_artifact", inspect)
    path = tmp_path / "jacked.service"

    result = lifecycle.reconcile_native_artifact(
        MagicMock(), path, environment={"PATH": "/safe"}
    )

    assert result.artifact is inspection
    assert result.path == path
    assert not path.exists()
    inspect.assert_called_once_with(path, artifact)


def test_invalid_private_manifest_is_quarantined_under_lease(tmp_path):
    from jacked.service.instance import ServicePaths
    from jacked.service.lifecycle import quarantine_invalid_ownership

    paths = ServicePaths.in_directory(tmp_path)
    paths.manifest.write_text("not-json", encoding="utf-8")
    paths.manifest.chmod(0o600)
    from tests.unit.service.supervisor_test_support import _secure_test_path

    _secure_test_path(paths.manifest)

    backup = quarantine_invalid_ownership(paths)

    assert backup is not None
    assert backup.read_text(encoding="utf-8") == "not-json"
    assert not paths.manifest.exists()


def test_invalid_manifest_recovery_refuses_while_lease_is_active(tmp_path):
    from jacked.service.instance import ServiceLease, ServiceLeaseBusy, ServicePaths
    from jacked.service.lifecycle import quarantine_invalid_ownership

    paths = ServicePaths.in_directory(tmp_path)
    paths.manifest.write_text("not-json", encoding="utf-8")
    paths.manifest.chmod(0o600)
    from tests.unit.service.supervisor_test_support import _secure_test_path

    _secure_test_path(paths.manifest)
    lease = ServiceLease(paths.lease)
    lease.acquire()
    try:
        with pytest.raises(ServiceLeaseBusy):
            quarantine_invalid_ownership(paths)
    finally:
        lease.release()
    assert paths.manifest.exists()


def test_manual_spawn_refuses_a_changed_runtime_target(monkeypatch):
    from jacked.service import lifecycle

    spec = MagicMock()
    spec.runtime_target_matches.return_value = False
    popen = MagicMock()
    monkeypatch.setattr(lifecycle.subprocess, "Popen", popen)

    result = lifecycle.spawn_exact_service(spec, environment={})

    assert result.ok is False
    assert result.reason == "runtime target changed"
    popen.assert_not_called()


def test_handoff_treats_dead_owner_pid_as_exit(monkeypatch, tmp_path):
    process = SimpleNamespace(pid=999_999, creation_id="gone", executable="/x")
    old = SimpleNamespace(
        instance_id="old",
        generation="old-generation",
        supervisor=SupervisorKind.LAUNCHD.value,
        process=process,
    )
    spec = MagicMock(generation="a" * 64, supervisor=SupervisorKind.LAUNCHD)
    paths = SimpleNamespace(manifest=tmp_path / "manifest", root=tmp_path)
    control = MagicMock(
        side_effect=[
            {"ok": True, "result": {"accepted": True}},
            {
                "ok": True,
                "result": {
                    "state": "running",
                    "generation": spec.generation,
                    "instance_id": "new",
                },
            },
        ]
    )
    install = MagicMock(return_value=SupervisorAction(True, "install", "exact"))
    monkeypatch.setattr("jacked.service.instance.read_manifest", lambda _path: old)
    monkeypatch.setattr("jacked.service.instance.process_is_stale", lambda p: True)
    monkeypatch.setattr("jacked.service.ipc.send_native_control", control)
    monkeypatch.setattr("jacked.service.lifecycle.install_owned_supervisor", install)
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", MagicMock())

    result = handoff_owned_service(spec, environment={}, paths=paths, timeout=2)

    assert result.ok is True
    install.assert_called_once()


def test_handoff_keeps_waiting_while_owner_pid_is_alive(monkeypatch, tmp_path):
    process = SimpleNamespace(pid=4242, creation_id="live", executable="/x")
    old = SimpleNamespace(instance_id="old", generation="old-generation", process=process)
    spec = MagicMock(generation="a" * 64, supervisor=SupervisorKind.MANUAL)
    paths = SimpleNamespace(manifest=tmp_path / "manifest", root=tmp_path)
    monkeypatch.setattr("jacked.service.instance.read_manifest", lambda _path: old)
    monkeypatch.setattr("jacked.service.instance.process_is_stale", lambda p: False)
    monkeypatch.setattr(
        "jacked.service.ipc.send_native_control",
        lambda *_args, **_kwargs: {"ok": True, "result": {"accepted": True}},
    )
    spawn = MagicMock()
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", spawn)

    result = handoff_owned_service(spec, environment={}, paths=paths, timeout=0.3)

    assert result.ok is False
    assert "did not exit" in result.reason
    spawn.assert_not_called()


def test_handoff_budgets_come_from_service_constants():
    import inspect

    from jacked.service import HANDOFF_EXIT_TIMEOUT, REPLACEMENT_READY_TIMEOUT

    params = inspect.signature(handoff_owned_service).parameters
    assert params["timeout"].default == HANDOFF_EXIT_TIMEOUT
    assert params["ready_timeout"].default == REPLACEMENT_READY_TIMEOUT
    assert REPLACEMENT_READY_TIMEOUT > 90


def _handoff_fixture(monkeypatch, tmp_path, install_result, ready: bool):
    old = SimpleNamespace(
        instance_id="old",
        generation="old-generation",
        supervisor=SupervisorKind.LAUNCHD.value,
    )
    reads = iter((old, FileNotFoundError()))

    def read(_path):
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    spec = MagicMock(generation="a" * 64, supervisor=SupervisorKind.LAUNCHD)
    paths = SimpleNamespace(manifest=tmp_path / "manifest", root=tmp_path)
    status = {"state": "running", "generation": spec.generation, "instance_id": "new"}
    responses = [{"ok": True, "result": {"accepted": True}}]
    responses += [{"ok": True, "result": status}] if ready else [
        {"ok": False, "error": "down"}
    ] * 400
    control = MagicMock(side_effect=responses)
    spawn = MagicMock()
    monkeypatch.setattr("jacked.service.instance.read_manifest", read)
    monkeypatch.setattr("jacked.service.ipc.send_native_control", control)
    monkeypatch.setattr(
        "jacked.service.lifecycle.install_owned_supervisor",
        lambda *_a, **_k: install_result,
    )
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", spawn)
    return spec, paths, spawn


def test_timed_out_native_restart_succeeds_when_the_generation_comes_up(
    monkeypatch, tmp_path
):
    """launchd waits for the old process to drain inside kickstart -k; a
    timed-out call is not evidence that nothing started (2026-09-05)."""
    spec, paths, spawn = _handoff_fixture(
        monkeypatch, tmp_path, SupervisorAction(False, "install", "TimeoutExpired"), True
    )

    result = handoff_owned_service(spec, environment={}, paths=paths)

    assert result.ok is True, result.reason
    spawn.assert_not_called()


def test_timed_out_native_restart_still_refuses_when_nothing_comes_up(
    monkeypatch, tmp_path
):
    spec, paths, spawn = _handoff_fixture(
        monkeypatch, tmp_path, SupervisorAction(False, "install", "TimeoutExpired"), False
    )

    result = handoff_owned_service(spec, environment={}, paths=paths, ready_timeout=0.3)

    assert result.ok is False
    assert "TimeoutExpired" in result.reason
    spawn.assert_not_called()


def test_non_timeout_native_refusal_does_not_wait_for_a_generation(
    monkeypatch, tmp_path
):
    spec, paths, spawn = _handoff_fixture(
        monkeypatch, tmp_path, SupervisorAction(False, "refused", "artifact is foreign"), True
    )

    result = handoff_owned_service(spec, environment={}, paths=paths)

    assert result.ok is False
    assert "artifact is foreign" in result.reason
    spawn.assert_not_called()


from types import SimpleNamespace
from unittest.mock import MagicMock

from jacked.service.lifecycle import handoff_owned_service
from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import SupervisorAction


def test_handoff_authenticates_shutdown_then_uses_exact_supervisor(
    monkeypatch, tmp_path
):
    old = SimpleNamespace(instance_id="old", generation="old-generation")
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
    control = MagicMock(return_value={"ok": True, "result": {"accepted": True}})
    restart = MagicMock(return_value=SupervisorAction(True, "restart", "exact"))
    spawn = MagicMock()
    monkeypatch.setattr("jacked.service.instance.read_manifest", read)
    monkeypatch.setattr("jacked.service.ipc.send_native_control", control)
    monkeypatch.setattr("jacked.service.lifecycle.restart_native_owned", restart)
    monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", spawn)

    result = handoff_owned_service(spec, environment={}, paths=paths)

    assert result.ok is True
    control.assert_called_once()
    restart.assert_called_once()
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
        "jacked.service.lifecycle.restart_native_owned",
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

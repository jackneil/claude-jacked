import json
import os

import pytest

from jacked.service.instance import (
    BindIdentity,
    InstanceManifest,
    InspectState,
    ProcessIdentity,
    ServiceLease,
    ServicePaths,
    inspect_instance,
    publish_manifest,
    read_manifest,
    remove_manifest_if_current,
    discover_endpoint,
    reserve_service_bind,
)
from jacked.service.spec import ServiceSpec, SupervisorKind


def _spec():
    return ServiceSpec(
        service_id="ai.hank.jacked",
        protocol_version=2,
        build_version="test",
        runtime_path=os.path.realpath(os.sys.executable),
        launcher_path="/opt/jacked/launcher-v2",
        launcher_sha256="b" * 64,
        supervisor=SupervisorKind.MANUAL,
        arguments=("-I", "-m", "jacked"),
    )


def _manifest(spec, *, instance_id="instance-1", quarantine=False):
    from jacked.service.instance import current_user_identity

    return InstanceManifest.create(
        spec=spec,
        process=ProcessIdentity(
            pid=os.getpid(),
            creation_id="test-creation",
            executable=os.path.realpath(os.sys.executable),
        ),
        user_id=current_user_identity(),
        machine_id="machine-test",
        instance_id=instance_id,
        bind=BindIdentity(
            host="127.0.0.1",
            port=49152 if quarantine else 8321,
            quarantine=quarantine,
        ),
        control_address="/tmp/jacked-test.sock",
    )


def test_lease_is_kernel_held_and_exclusive(tmp_path):
    paths = ServicePaths.in_directory(tmp_path)
    first = ServiceLease(paths.lease)
    second = ServiceLease(paths.lease)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="already holds"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_private_manifest_roundtrip_and_instance_guarded_cleanup(tmp_path):
    paths = ServicePaths.in_directory(tmp_path)
    spec = _spec()
    manifest = _manifest(spec)
    publish_manifest(paths.manifest, manifest)

    assert read_manifest(paths.manifest) == manifest
    if os.name == "posix":
        assert paths.manifest.stat().st_mode & 0o777 == 0o600
    assert not remove_manifest_if_current(paths.manifest, "another-instance")
    assert paths.manifest.exists()
    assert remove_manifest_if_current(paths.manifest, manifest.instance_id)


def test_manifest_tamper_is_rejected(tmp_path):
    paths = ServicePaths.in_directory(tmp_path)
    manifest = _manifest(_spec())
    publish_manifest(paths.manifest, manifest)
    payload = json.loads(paths.manifest.read_text())
    payload["bind"]["port"] = 1
    paths.manifest.write_text(json.dumps(payload))
    if os.name == "posix":
        paths.manifest.chmod(0o600)
    with pytest.raises(ValueError, match="signature"):
        read_manifest(paths.manifest)


def test_inspection_reports_exact_owned_and_quarantined_states(tmp_path, monkeypatch):
    paths = ServicePaths.in_directory(tmp_path)
    spec = _spec()
    manifest = _manifest(spec)
    monkeypatch.setattr(
        "jacked.service.instance_discovery.process_identity",
        lambda _pid: manifest.process,
    )
    monkeypatch.setattr(
        "jacked.service.instance_discovery.process_user_identity",
        lambda _pid: manifest.user_id,
    )
    publish_manifest(paths.manifest, manifest)
    result = inspect_instance(paths, spec)
    assert result.state is InspectState.VERIFIED_UNMANAGED

    publish_manifest(paths.manifest, _manifest(spec, quarantine=True))
    result = inspect_instance(paths, spec)
    assert result.state is InspectState.QUARANTINED


def test_inspection_fails_closed_on_generation_or_process_mismatch(
    tmp_path, monkeypatch
):
    paths = ServicePaths.in_directory(tmp_path)
    spec = _spec()
    manifest = _manifest(spec)
    publish_manifest(paths.manifest, manifest)
    other = ServiceSpec(**{**spec.constructor_fields(), "build_version": "other"})
    assert inspect_instance(paths, other).state is InspectState.STALE_MANIFEST

    changed = manifest.replace_process(creation_id="definitely-wrong")
    monkeypatch.setattr(
        "jacked.service.instance_discovery.process_identity",
        lambda _pid: manifest.process,
    )
    monkeypatch.setattr(
        "jacked.service.instance_discovery.process_user_identity",
        lambda _pid: manifest.user_id,
    )
    publish_manifest(paths.manifest, changed)
    assert inspect_instance(paths, spec).state is InspectState.STALE_MANIFEST


def test_manifest_symlink_is_never_followed(tmp_path):
    paths = ServicePaths.in_directory(tmp_path)
    target = tmp_path / "target"
    target.write_text("{}")
    paths.manifest.symlink_to(target)
    with pytest.raises(ValueError, match="regular private file"):
        read_manifest(paths.manifest)


def test_manifest_presence_suppresses_legacy_port_fallback(tmp_path):
    paths = ServicePaths.in_directory(tmp_path)
    paths.manifest.write_text("not-json")
    if os.name == "posix":
        paths.manifest.chmod(0o600)
    endpoint = discover_endpoint(paths)
    assert endpoint.host is None
    assert endpoint.port is None
    assert endpoint.source == "manifest-invalid"


def test_occupied_8321_reserves_dynamic_quarantine_port(monkeypatch):
    class FakeSocket:
        count = 0

        def __init__(self, *_args):
            self.number = FakeSocket.count
            FakeSocket.count += 1

        def setsockopt(self, *_args):
            pass

        def bind(self, _address):
            if self.number == 0:
                raise OSError("occupied")

        def listen(self):
            pass

        def getsockname(self):
            return ("127.0.0.1", 49152)

        def close(self):
            pass

    monkeypatch.setattr("jacked.service.instance_discovery.socket.socket", FakeSocket)
    reserved, bind = reserve_service_bind("127.0.0.1", 8321)
    assert bind.quarantine is True
    assert bind.port == 49152
    reserved.close()

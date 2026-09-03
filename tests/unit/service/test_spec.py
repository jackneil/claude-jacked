from pathlib import Path

import pytest

from jacked.service.spec import ServiceSpec, SupervisorKind


def _spec(**overrides):
    values = {
        "service_id": "ai.hank.jacked",
        "protocol_version": 2,
        "build_version": "0.99.0",
        "runtime_path": "/opt/jacked/python",
        "launcher_path": "/opt/jacked/launcher-v2",
        "launcher_sha256": "a" * 64,
        "supervisor": SupervisorKind.LAUNCHD,
        "arguments": ("-I", "-m", "jacked", "service", "start"),
    }
    values.update(overrides)
    return ServiceSpec(**values)


def test_generation_is_deterministic_and_covers_runtime_identity():
    first = _spec()
    assert first.generation == _spec().generation
    assert first.generation != _spec(build_version="0.99.1").generation
    assert first.generation != _spec(runtime_path="/other/python").generation


def test_rejects_relative_or_unresolved_runtime_paths(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        _spec(runtime_path="python")
    link = tmp_path / "python"
    link.symlink_to(Path("/bin/sh"))
    with pytest.raises(ValueError, match="resolved"):
        _spec(runtime_path=str(link))


def test_artifact_marker_requires_exact_owner_and_generation():
    spec = _spec()
    marker = spec.artifact_marker()
    assert spec.matches_artifact_marker(marker)
    assert not spec.matches_artifact_marker({**marker, "generation": "0" * 64})
    assert not spec.matches_artifact_marker({**marker, "owner": "foreign"})

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from jacked.service.spec import ServiceSpec, SupervisorKind


def _spec(**overrides):
    values = {
        "service_id": "ai.hank.jacked",
        "protocol_version": 2,
        "build_version": "0.99.0",
        "runtime_path": os.path.realpath(sys.executable),
        "launcher_path": os.path.join(
            tempfile.gettempdir(), "jacked", "launcher-v2"
        ),
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
    other_runtime = os.path.realpath(
        os.path.join(tempfile.gettempdir(), "jacked", "other-python")
    )
    assert first.generation != _spec(runtime_path=other_runtime).generation


def test_runtime_path_rejects_relative():
    with pytest.raises(ValueError, match="absolute"):
        _spec(runtime_path="python")


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_runtime_path_accepts_virtualenv_symlink(tmp_path):
    foreign_link = tmp_path / "python"
    foreign_link.symlink_to(Path(sys.executable))
    with pytest.raises(ValueError):
        _spec(runtime_path=str(foreign_link))

    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / "python3.99"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)
    spec = _spec(runtime_path=str(link))
    assert spec.runtime_path == str(link)
    assert spec.runtime_target_path == str(target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
@pytest.mark.parametrize("target_name", ["python3.13t", "pypy3", "graalpy"])
def test_virtualenv_runtime_accepts_supported_python_targets(tmp_path, target_name):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / target_name
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)

    assert _spec(runtime_path=str(link)).runtime_target_path == str(target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_virtualenv_runtime_rejects_broken_non_python_and_writable_paths(tmp_path):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")

    broken = venv / "bin" / "python"
    broken.symlink_to(venv / "missing" / "python3")
    with pytest.raises(ValueError):
        _spec(runtime_path=str(broken))
    broken.unlink()

    non_python = tmp_path / "not-an-interpreter"
    non_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    non_python.chmod(0o700)
    broken.symlink_to(non_python)
    with pytest.raises(ValueError, match="Python executable"):
        _spec(runtime_path=str(broken))
    broken.unlink()

    broken.symlink_to(Path(sys.executable))
    venv.joinpath("bin").chmod(0o775)
    with pytest.raises(ValueError, match="untrusted writable directory"):
        _spec(runtime_path=str(broken))


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_virtualenv_runtime_rejects_symlinked_ancestor(tmp_path):
    real_venv = tmp_path / "real-venv"
    real_venv.joinpath("bin").mkdir(parents=True)
    real_venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    real_venv.joinpath("bin", "python").symlink_to(Path(sys.executable))
    alias = tmp_path / "alias"
    alias.symlink_to(real_venv, target_is_directory=True)

    with pytest.raises(ValueError, match="virtualenv Python entrypoint"):
        _spec(runtime_path=str(alias / "bin" / "python"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_runtime_target_is_bound_and_retargeting_changes_fresh_generation(tmp_path):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    original_target = tmp_path / "python3.11"
    original_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    original_target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(original_target)
    original = _spec(runtime_path=str(link))
    original_generation = original.generation

    replacement = tmp_path / "python3.99"
    replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    replacement.chmod(stat.S_IRWXU)
    link.unlink()
    link.symlink_to(replacement)

    assert original.generation == original_generation
    assert not original.runtime_target_matches()
    assert _spec(runtime_path=str(link)).generation != original_generation


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL model")
@pytest.mark.parametrize(
    ("acl_target", "acl_rule"),
    [
        ("runtime-parent", "everyone allow add_file,delete_child"),
        ("target", "everyone allow write,delete"),
    ],
)
def test_virtualenv_runtime_rejects_mutating_darwin_acls(
    tmp_path, acl_target, acl_rule
):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / "python3.99"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)
    path = venv / "bin" if acl_target == "runtime-parent" else target
    subprocess.run(["/bin/chmod", "+a", acl_rule, str(path)], check=True)

    with pytest.raises(ValueError, match="mutating extended ACL"):
        _spec(runtime_path=str(link))


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_group_writable_runtime_directory_is_always_rejected(tmp_path):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("bin").chmod(0o770)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / "python3.99"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="untrusted writable directory"):
        _spec(runtime_path=str(link))


def test_artifact_marker_requires_exact_owner_and_generation():
    spec = _spec()
    marker = spec.artifact_marker()
    assert spec.matches_artifact_marker(marker)
    assert not spec.matches_artifact_marker({**marker, "generation": "0" * 64})
    assert not spec.matches_artifact_marker({**marker, "owner": "foreign"})

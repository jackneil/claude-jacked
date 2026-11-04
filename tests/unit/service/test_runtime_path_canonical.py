"""One venv interpreter gives one service generation, whatever name started it.

Regression for #293i: ``bin/python``, ``bin/python3`` and ``bin/python3.12``
in a uv tool venv are symlinks to one interpreter. Before the fix each name
produced its own ``runtime_path`` and so its own ServiceSpec generation, and
``native_restart()`` from a process started as ``bin/python`` refused with
"supervisor artifact is owned_drift".
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jacked.service.lifecycle import _service_runtime_path, build_service_spec
from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors import (
    ArtifactDisposition,
    inspect_artifact,
    reconcile_artifact,
    render_launchd,
    render_systemd_user,
    restart_owned_supervisor,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX venv symlinks")

ENVIRONMENT = {"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"}


def _interpreter(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _uv_tool_venv(tmp_path: Path) -> Path:
    """Lay out a venv the way ``uv tool install`` does on this Mac.

    ``bin/python`` points at the base interpreter, and ``python3`` and
    ``python3.12`` point at ``python`` (relative links).
    """

    base = _interpreter(tmp_path / "base" / "python3.12")
    venv = tmp_path / "tool"
    bin_dir = venv / "bin"
    bin_dir.mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = base\n", encoding="utf-8")
    bin_dir.joinpath("python").symlink_to(base)
    bin_dir.joinpath("python3").symlink_to("python")
    bin_dir.joinpath("python3.12").symlink_to("python")
    return bin_dir


def _legacy_runtime_path(executable: str) -> str:
    """The pre-#293i rule, kept here to pin the migration contract."""

    absolute = Path(os.path.normpath(os.path.abspath(executable)))
    if (
        os.name == "posix"
        and absolute.is_symlink()
        and (absolute.parent.parent / "pyvenv.cfg").is_file()
    ):
        return str(Path(os.path.realpath(absolute.parent)) / absolute.name)
    return os.path.realpath(absolute)


def _launcher(tmp_path: Path) -> Path:
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    return launcher


def _spec_for(runtime: str, launcher: Path, kind=SupervisorKind.LAUNCHD):
    return build_service_spec(
        runtime_path=runtime,
        launcher_path=str(launcher),
        launcher_content=b"launcher",
        supervisor=kind,
        build_version="0.99.0",
    )


@posix_only
@pytest.mark.parametrize("name", ["python", "python3", "python3.12"])
def test_every_alias_resolves_to_the_shebang_name(tmp_path, name):
    bin_dir = _uv_tool_venv(tmp_path)

    runtime = _service_runtime_path(str(bin_dir / name))

    assert runtime == str(Path(os.path.realpath(bin_dir)) / "python3")


@posix_only
def test_every_alias_gives_one_generation(tmp_path):
    bin_dir = _uv_tool_venv(tmp_path)
    launcher = _launcher(tmp_path)

    generations = {
        _spec_for(_service_runtime_path(str(bin_dir / name)), launcher).generation
        for name in ("python", "python3", "python3.12")
    }

    assert len(generations) == 1


@posix_only
def test_shebang_name_keeps_its_installed_generation(tmp_path):
    """A normal tray/CLI install (bin/python3) sees no generation change."""

    bin_dir = _uv_tool_venv(tmp_path)
    launcher = _launcher(tmp_path)
    executable = str(bin_dir / "python3")

    assert _service_runtime_path(executable) == _legacy_runtime_path(executable)
    assert (
        _spec_for(_service_runtime_path(executable), launcher).generation
        == _spec_for(_legacy_runtime_path(executable), launcher).generation
    )


@posix_only
def test_names_for_different_interpreters_are_not_merged(tmp_path):
    bin_dir = _uv_tool_venv(tmp_path)
    other = _interpreter(tmp_path / "other" / "python3.13")
    bin_dir.joinpath("python3").unlink()
    bin_dir.joinpath("python3").symlink_to(other)
    launcher = _launcher(tmp_path)

    from_python = _service_runtime_path(str(bin_dir / "python"))
    from_python3 = _service_runtime_path(str(bin_dir / "python3"))

    assert from_python == str(Path(os.path.realpath(bin_dir)) / "python")
    assert from_python3 == str(Path(os.path.realpath(bin_dir)) / "python3")
    assert (
        _spec_for(from_python, launcher).generation
        != _spec_for(from_python3, launcher).generation
    )


@posix_only
def test_missing_python3_falls_back_to_python(tmp_path):
    bin_dir = _uv_tool_venv(tmp_path)
    bin_dir.joinpath("python3").unlink()
    real_bin = Path(os.path.realpath(bin_dir))

    assert _service_runtime_path(str(bin_dir / "python")) == str(real_bin / "python")
    assert _service_runtime_path(str(bin_dir / "python3.12")) == str(
        real_bin / "python"
    )


@posix_only
def test_lone_entrypoint_keeps_its_own_name(tmp_path):
    base = _interpreter(tmp_path / "base" / "python3.12")
    venv = tmp_path / "tool"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = base\n", encoding="utf-8")
    venv.joinpath("bin", "python3.12").symlink_to(base)
    executable = str(venv / "bin" / "python3.12")

    assert _service_runtime_path(executable) == _legacy_runtime_path(executable)


@posix_only
def test_non_venv_symlink_still_resolves_to_its_target(tmp_path):
    base = _interpreter(tmp_path / "base" / "python3.12")
    link_dir = tmp_path / "plain" / "bin"
    link_dir.mkdir(parents=True)
    link_dir.joinpath("python3").symlink_to(base)
    link_dir.joinpath("python").symlink_to(base)

    assert _service_runtime_path(str(link_dir / "python")) == os.path.realpath(base)


@pytest.mark.parametrize("name", ["python.exe", "pythonw.exe"])
def test_windows_entrypoints_are_untouched(tmp_path, name):
    venv = tmp_path / "tool"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = base\n", encoding="utf-8")
    _interpreter(scripts / "python.exe")
    _interpreter(scripts / "pythonw.exe")
    executable = str(scripts / name)

    assert _service_runtime_path(executable, os_name="nt") == os.path.realpath(
        executable
    )


@posix_only
@pytest.mark.parametrize(
    ("kind", "render", "artifact_name"),
    [
        (SupervisorKind.LAUNCHD, render_launchd, "ai.hank.jacked.plist"),
        (SupervisorKind.SYSTEMD_USER, render_systemd_user, "jacked.service"),
    ],
)
def test_restart_from_an_alias_accepts_the_shebang_artifact(
    tmp_path, kind, render, artifact_name
):
    """The #293i refusal: provisioned from python3, restarted from python."""

    bin_dir = _uv_tool_venv(tmp_path)
    launcher = _launcher(tmp_path)
    provisioner = _spec_for(
        _service_runtime_path(str(bin_dir / "python3")), launcher, kind
    )
    path = tmp_path / artifact_name
    reconcile_artifact(path, render(provisioner, environment=ENVIRONMENT))
    restarter = _spec_for(_service_runtime_path(str(bin_dir / "python")), launcher, kind)
    if kind is SupervisorKind.LAUNCHD:
        loaded = f"{restarter.generation}\n{restarter.launcher_path}\n"
    else:
        loaded = f"LoadState=loaded\nFragmentPath={path}\nNeedDaemonReload=no\n"
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=loaded),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = restart_owned_supervisor(
        restarter, path, environment=ENVIRONMENT, run=runner, uid=501
    )

    assert result.ok is True, result.reason
    assert result.action == "restart"


@posix_only
def test_install_reprovisions_an_artifact_written_under_an_alias(tmp_path):
    """An artifact a pre-fix build wrote under bin/python is owned drift.

    The install path (updater ensure_native_lifecycle, handoff, re-arm)
    rewrites owned drift from the current spec. It does not refuse it.
    """

    bin_dir = _uv_tool_venv(tmp_path)
    launcher = _launcher(tmp_path)
    real_bin = Path(os.path.realpath(bin_dir))
    legacy = ServiceSpec(
        **{
            **_spec_for(str(real_bin / "python3"), launcher).constructor_fields(),
            "runtime_path": str(real_bin / "python"),
        }
    )
    current = _spec_for(_service_runtime_path(str(bin_dir / "python")), launcher)
    assert legacy.generation != current.generation
    path = tmp_path / "ai.hank.jacked.plist"
    reconcile_artifact(path, render_launchd(legacy, environment=ENVIRONMENT))
    expected = render_launchd(current, environment=ENVIRONMENT)

    assert inspect_artifact(path, expected).disposition is ArtifactDisposition.OWNED_DRIFT
    assert reconcile_artifact(path, expected).disposition is ArtifactDisposition.INSTALLED
    assert path.read_bytes() == expected.content
    assert inspect_artifact(path, expected).disposition is ArtifactDisposition.MATCHING


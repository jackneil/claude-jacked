"""Installed interpreter regression for native service activation."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from jacked.service.instance import ServicePaths
from jacked.service.spec import SupervisorKind


def _controlled_posix_tool_runtime(tmp_path: Path) -> str:
    base = Path(os.path.realpath(sys.executable))
    target = tmp_path / "runtime" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    target.parent.mkdir()
    shutil.copy2(base, target)
    target.chmod(0o700)
    bundled_lib = tmp_path / "lib"
    bundled_lib.mkdir()
    for library in base.parent.parent.joinpath("lib").glob("libpython*"):
        if library.resolve().is_file():
            shutil.copy2(library.resolve(), bundled_lib / library.name)
    stdlib_name = f"python{sys.version_info.major}.{sys.version_info.minor}"
    bundled_lib.joinpath(stdlib_name).symlink_to(
        base.parent.parent / "lib" / stdlib_name, target_is_directory=True
    )
    venv = tmp_path / "tool"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text(
        f"home = {base.parent}\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}\n"
        "include-system-site-packages = false\n",
        encoding="utf-8",
    )
    site_packages = venv / "lib" / stdlib_name / "site-packages"
    site_packages.mkdir(parents=True)
    site_packages.joinpath("jacked-test.pth").write_text(
        f"{Path(__file__).parents[2]}\n", encoding="utf-8"
    )
    runtime = venv / "bin" / "python"
    runtime.symlink_to(target)
    return str(runtime)


def test_native_contract_preserves_an_isolated_importable_tool_runtime(
    tmp_path, monkeypatch
):
    import jacked.service.lifecycle as lifecycle

    paths = ServicePaths.in_directory(tmp_path / "service-state")
    if os.name == "posix":
        monkeypatch.setattr(
            lifecycle.sys, "executable", _controlled_posix_tool_runtime(tmp_path)
        )

    spec, _environment = lifecycle.provision_service_contract(
        paths=paths,
        platform=sys.platform,
        supervisor=SupervisorKind.MANUAL,
    )

    absolute = os.path.normpath(os.path.abspath(sys.executable))
    executable = os.path.realpath(sys.executable)
    absolute_path = Path(absolute)
    expected = (
        str(Path(os.path.realpath(absolute_path.parent)) / absolute_path.name)
        if absolute_path.is_symlink()
        and absolute_path.parent.parent.joinpath("pyvenv.cfg").is_file()
        else executable
    )
    assert spec.runtime_path == expected
    assert spec.runtime_target_path == executable
    imported = subprocess.run(
        [
            spec.runtime_path,
            "-I",
            "-c",
            "import jacked; print(jacked.__version__)",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
    # Compare against the package the child actually imports, never a literal:
    # a literal goes stale on every release bump (0.101.0 shipped with this
    # test red because the bump landed after the gate ran).
    import jacked

    assert imported.stdout.strip() == jacked.__version__

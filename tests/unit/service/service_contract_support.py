"""Provision a real service contract in a test, on every hosted runner.

``ServiceSpec`` refuses a runtime in a directory that other users can write.
The ubuntu-latest runner keeps its Python under /opt/hostedtoolcache, which
the image makes world-writable, so a test that provisions with the runner's
own ``sys.executable`` fails there and nowhere else. ``trusted_runtime``
builds a private copy of the interpreter in a private virtualenv shape (the
pattern of tests/integration/test_native_runtime.py), which the trust check
accepts on every host.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

from jacked.service.windows_env import WindowsResolvers

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Fixed Windows values for tests that build a win32 contract on any host.
WINDOWS_VALUES = {
    "SystemRoot": r"C:\Windows",
    "USERNAME": "alice",
    "COMPUTERNAME": "DESK",
    "platform": "win-amd64",
    "APPDATA": r"C:\Users\alice\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    "ProgramData": r"C:\ProgramData",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramW6432": r"C:\Program Files",
    "CommonProgramFiles": r"C:\Program Files\Common Files",
    "CommonProgramFiles(x86)": r"C:\Program Files (x86)\Common Files",
    "CommonProgramW6432": r"C:\Program Files\Common Files",
    "PUBLIC": r"C:\Users\Public",
}


def fixed_windows(**changes: str) -> WindowsResolvers:
    """Return fixed Windows resolvers, with ``changes`` applied."""
    return WindowsResolvers.fixed({**WINDOWS_VALUES, **changes})


def trusted_runtime(tmp_path: Path, *, importable: bool = False) -> str:
    """Return a runtime path that the runtime trust check accepts.

    On Windows this is ``sys.executable``. On POSIX it is a private copy of
    the interpreter behind a virtualenv entry point. ``importable`` adds
    this test environment's site-packages, so the runtime can import the
    dependencies of jacked (the boot probe imports the tray module).
    """
    if os.name != "posix":
        return sys.executable
    base = Path(os.path.realpath(sys.executable))
    stdlib_name = f"python{sys.version_info.major}.{sys.version_info.minor}"
    target = tmp_path / "runtime" / stdlib_name
    target.parent.mkdir(parents=True)
    shutil.copy2(base, target)
    target.chmod(0o700)
    bundled_lib = tmp_path / "lib"
    bundled_lib.mkdir()
    for library in base.parent.parent.joinpath("lib").glob("libpython*"):
        if library.resolve().is_file():
            shutil.copy2(library.resolve(), bundled_lib / library.name)
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
    lines = [str(REPO_ROOT)]
    if importable:
        lines.append(sysconfig.get_paths()["purelib"])
        lines.append(sysconfig.get_paths()["platlib"])
    site_packages.joinpath("jacked-test.pth").write_text(
        "".join(f"{line}\n" for line in dict.fromkeys(lines)), encoding="utf-8"
    )
    runtime = venv / "bin" / "python"
    runtime.symlink_to(target)
    return str(runtime)


def provision_contract(
    tmp_path: Path,
    monkeypatch,
    *,
    platform: str | None = None,
    runtime: str | None = None,
    windows: WindowsResolvers | None = None,
    state: Path | None = None,
):
    """Provision the real contract with a trusted runtime. Return the triple.

    The result is ``(spec, environment, paths)``. ``state`` is the service
    state directory (default ``tmp_path / "state"``). On a POSIX host a
    win32 contract uses ``windows`` (default: ``fixed_windows()``).
    """
    import jacked.service.environment as environment_module
    import jacked.service.lifecycle as lifecycle
    from jacked.service.instance_models import ServicePaths
    from jacked.service.spec import SupervisorKind

    monkeypatch.setattr(
        lifecycle.sys, "executable", runtime or trusted_runtime(tmp_path / "rt")
    )
    if (platform or sys.platform) == "win32" and (sys.platform != "win32" or windows):
        resolvers = windows or fixed_windows()
        monkeypatch.setattr(
            environment_module, "default_windows_resolvers", lambda: resolvers
        )
    paths = ServicePaths.in_directory(state or tmp_path / "state")
    spec, environment = lifecycle.provision_service_contract(
        paths=paths,
        platform=platform or sys.platform,
        supervisor=SupervisorKind.MANUAL,
    )
    return spec, environment, paths

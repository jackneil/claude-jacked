"""Installed interpreter regression for native service activation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from jacked.service.instance import ServicePaths
from jacked.service.lifecycle import provision_service_contract
from jacked.service.spec import SupervisorKind


def test_native_contract_preserves_an_isolated_importable_tool_runtime(tmp_path):
    paths = ServicePaths.in_directory(tmp_path / "service-state")

    spec, _environment = provision_service_contract(
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
    assert imported.stdout.strip() == "0.99.2"

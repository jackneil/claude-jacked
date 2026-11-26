"""The preflight boot probe: one short process under the exact service env."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from jacked.service.lifecycle import (
    default_environment,
    inherited_allowlisted_environment,
)
from jacked.service.preflight_probe import (
    PROBE_CODE,
    PROBE_EXIT_BASE,
    PROBE_STEPS,
    ProbeResult,
    run_service_probe,
)

_GENERATION = "a" * 64


def _service_environment() -> dict[str, str]:
    """The environment exactly as provision_service_contract builds it."""
    return default_environment(
        home=str(Path.home()),
        user_id="uid:test",
        inherited_allowlisted=inherited_allowlisted_environment(sys.platform),
        platform=sys.platform,
    )


def test_every_step_is_in_the_probe_code_in_order():
    calls = [
        PROBE_CODE.index(f'_step({PROBE_EXIT_BASE + index}, "{step}",')
        for index, step in enumerate(PROBE_STEPS)
    ]
    assert calls == sorted(calls)


def test_the_real_probe_passes_on_this_host_under_the_service_environment(tmp_path):
    result = run_service_probe(
        runtime=sys.executable,
        environment=_service_environment(),
        generation=_GENERATION,
        cwd=tmp_path,
    )
    assert result == ProbeResult(True)


def test_a_failing_step_is_named_with_its_error_number_and_no_values(tmp_path):
    # The Windows failure, simulated on every OS through the code seam.
    code = PROBE_CODE.replace(
        "asyncio.new_event_loop().close()",
        'raise OSError(10106, "provider canary-value")',
    )
    environment = {**_service_environment(), "JACKED_CANARY": "canary-value"}
    result = run_service_probe(
        runtime=sys.executable,
        environment=environment,
        generation=_GENERATION,
        cwd=tmp_path,
        code=code,
    )
    assert result == ProbeResult(False, "asyncio_event_loop", "OSError 10106")
    assert "canary" not in repr(result)


def test_a_missing_import_fails_the_tray_step(tmp_path):
    code = PROBE_CODE.replace("import jacked.service.tray", "import jacked_no_such_module")
    result = run_service_probe(
        runtime=sys.executable,
        environment=_service_environment(),
        generation=_GENERATION,
        cwd=tmp_path,
        code=code,
    )
    assert result == ProbeResult(False, "tray_import", "ModuleNotFoundError")


def test_the_probe_reads_only_the_given_environment(tmp_path, monkeypatch):
    """A variable that only os.environ holds must not reach the probe."""
    monkeypatch.setenv("JACKED_PROBE_LEAK", "1")
    code = PROBE_CODE.replace(
        "socket.socket().close()",
        "socket.socket().close()\n"
        "    import os\n"
        "    if 'JACKED_PROBE_LEAK' in os.environ or "
        "len(os.environ.get('JACKED_SERVICE_GENERATION', '')) != 64:\n"
        "        raise LookupError('environment')",
    )
    result = run_service_probe(
        runtime=sys.executable,
        environment=_service_environment(),
        generation=_GENERATION,
        cwd=tmp_path,
        code=code,
    )
    assert result == ProbeResult(True)


def test_the_exit_code_is_the_verdict_when_stdout_is_lost(tmp_path):
    """pythonw.exe can run with no usable stdout: the step still shows."""
    code = PROBE_CODE.replace("import ssl\n", "import ssl\n    raise ImportError('x')\n")
    code = "import sys, os\nsys.stdout = None\n" + code
    result = run_service_probe(
        runtime=sys.executable,
        environment=_service_environment(),
        generation=_GENERATION,
        cwd=tmp_path,
        code=code,
    )
    assert result == ProbeResult(False, "ssl", "exit 72")


def test_a_crash_names_the_last_started_step(tmp_path):
    code = 'import os\nprint("jacked-probe-step:ssl", flush=True)\nos._exit(9)\n'
    result = run_service_probe(
        runtime=sys.executable,
        environment=_service_environment(),
        generation=_GENERATION,
        cwd=tmp_path,
        code=code,
    )
    assert result == ProbeResult(False, "ssl", "exit 9")


def test_a_hung_probe_is_a_failure_within_the_timeout(tmp_path):
    code = 'import time\nprint("jacked-probe-step:socket", flush=True)\ntime.sleep(30)\n'
    result = run_service_probe(
        runtime=sys.executable,
        environment=_service_environment(),
        generation=_GENERATION,
        cwd=tmp_path,
        code=code,
        timeout=1.0,
    )
    assert result.ok is False
    assert result.detail == "timeout after 1s"
    assert result.step in {"socket", "interpreter_start"}


def test_an_unlaunchable_runtime_is_a_failure(tmp_path):
    result = run_service_probe(
        runtime=str(tmp_path / "no-python"),
        environment={},
        generation=_GENERATION,
        cwd=tmp_path,
    )
    assert result == ProbeResult(False, "interpreter_start", "FileNotFoundError")


def test_the_probe_argv_and_environment_are_the_service_ones(tmp_path):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "jacked-probe-ok\n", ""))
    result = run_service_probe(
        runtime="/opt/jacked/bin/python",
        environment={"PATH": "/usr/bin"},
        generation=_GENERATION,
        cwd=tmp_path,
        run=run,
    )
    assert result.ok
    argv = run.call_args.args[0]
    assert argv[:3] == ["/opt/jacked/bin/python", "-I", "-c"]
    assert run.call_args.kwargs["env"] == {
        "PATH": "/usr/bin",
        "JACKED_SERVICE_GENERATION": _GENERATION,
    }


def _spec():
    return MagicMock(
        runtime_path="/opt/jacked/bin/python",
        runtime_target_path="/opt/jacked/bin/python3.11",
        supervisor=MagicMock(value="launchd"),
        generation=_GENERATION,
    )


@pytest.mark.parametrize("as_json", [False, True])
def test_preflight_fails_and_names_the_probe_step(as_json):
    from jacked.cli import main

    secret_env = {"SystemRoot": "canary-value"}
    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(_spec(), secret_env),
        ),
        patch(
            "jacked.service.preflight_probe.run_service_probe",
            return_value=ProbeResult(False, "asyncio_event_loop", "OSError 10106"),
        ),
    ):
        args = ["service", "preflight", *(["--json"] if as_json else [])]
        result = CliRunner().invoke(main, args)

    assert result.exit_code == 1, result.output
    assert "canary" not in result.output
    message = (
        "ServiceProbeError: the service runtime failed its start probe at "
        "asyncio_event_loop: OSError 10106"
    )
    if as_json:
        data = json.loads(result.output)
        assert data["ok"] is False
        assert data["probe_step"] == "asyncio_event_loop"
        assert data["error"] == message
    else:
        assert "[FAIL]" in result.output
        assert "asyncio_event_loop" in result.output
        assert "OSError 10106" in result.output


def test_preflight_bounds_the_probe_by_its_timeout():
    from jacked.cli import main

    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(_spec(), {}),
        ),
        patch(
            "jacked.service.preflight_probe.run_service_probe",
            return_value=ProbeResult(True),
        ) as probe,
    ):
        result = CliRunner().invoke(main, ["service", "preflight", "--timeout", "5"])

    assert result.exit_code == 0, result.output
    assert probe.call_args.kwargs["timeout"] == 5.0


def test_preflight_real_probe_end_to_end_on_this_host():
    """The real gate: provision in the isolated HOME, then the real probe."""
    from jacked.cli import main

    result = CliRunner().invoke(main, ["service", "preflight", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["probe_step"] is None, result.output
    assert data["ok"] is True, result.output
    assert os.path.isabs(data["runtime_path"])

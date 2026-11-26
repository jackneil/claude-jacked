"""The preflight boot probe: one short process through the delivered launcher."""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from jacked.service import EX_TEMPFAIL
from jacked.service.preflight_probe import (
    OK_MARK,
    PREFLIGHT_TIMEOUT_SECONDS,
    PROBE_CODE,
    PROBE_EXIT_BASE,
    PROBE_REPORT_NAME,
    PROBE_STDERR_NAME,
    PROBE_STEPS,
    STEP_MARK,
    ProbeOptions,
    ProbeResult,
    ServiceProbeError,
    run_service_probe,
)
from tests.unit.service.service_contract_support import provision_contract, trusted_runtime


@pytest.fixture
def contract(tmp_path, monkeypatch):
    """A real provisioned contract whose runtime can import jacked."""
    runtime = trusted_runtime(tmp_path / "rt", importable=True)
    spec, environment, paths = provision_contract(tmp_path, monkeypatch, runtime=runtime)
    return spec, environment, paths


def _probe(contract, code=PROBE_CODE, timeout=120.0, environment=None):
    spec, default_environment, paths = contract
    return run_service_probe(
        spec,
        default_environment if environment is None else environment,
        paths.root,
        ProbeOptions(timeout=timeout, code=code),
    )


def _seam(old, new):
    """Return PROBE_CODE with one change, and prove the change happened."""
    code = PROBE_CODE.replace(old, new)
    assert code != PROBE_CODE, f"the seam {old!r} is not in PROBE_CODE"
    return code


def test_the_probe_code_follows_its_constants():
    calls = [
        PROBE_CODE.index(f"_step({PROBE_EXIT_BASE + index}, {step!r}, {step})")
        for index, step in enumerate(PROBE_STEPS)
    ]
    assert calls == sorted(calls)
    assert PROBE_CODE.rstrip().endswith(f"_say({OK_MARK!r})")
    assert f"STEP_MARK = {STEP_MARK!r}" in PROBE_CODE


def test_the_exit_codes_stay_clear_of_sysexits_and_the_launcher():
    # The POSIX launcher exits 64, 66, 77 and 78; EX_TEMPFAIL restarts.
    assert PROBE_EXIT_BASE + len(PROBE_STEPS) <= 64
    assert PROBE_EXIT_BASE + len(PROBE_STEPS) <= EX_TEMPFAIL
    assert PROBE_EXIT_BASE > 2


def test_the_real_probe_passes_through_the_delivered_launcher(contract):
    result = _probe(contract)
    assert result == ProbeResult(True), result
    assert not (contract[2].root / PROBE_STDERR_NAME).exists()


def test_a_failing_step_is_named_with_its_error_number_and_no_values(contract):
    code = _seam(
        "asyncio.new_event_loop().close()",
        'raise OSError(10106, "provider canary-value")',
    )
    result = _probe(contract, code=code)
    assert (result.ok, result.step) == (False, "asyncio_event_loop")
    assert result.detail == "OSError errno 10106"
    assert "canary" not in result.detail


def test_a_missing_module_names_the_module(contract):
    code = _seam("import jacked.cli  # noqa: F401", "import jacked_no_such_module")
    result = _probe(contract, code=code)
    assert (result.step, result.detail) == (
        "cli_import",
        "ModuleNotFoundError module jacked_no_such_module",
    )


def test_a_refused_start_check_names_its_token(contract):
    code = _seam("if not tray._UVICORN_AVAILABLE:", "if True:")
    result = _probe(contract, code=code)
    assert (result.step, result.detail) == ("tray_backend", "ProbeRefusal uvicorn_missing")


def _tray_state(tray_available, load_failure, uvicorn=True):
    """A seam that puts the tray module in one state, on a non-mac backend."""
    return _seam(
        "    import jacked.service.tray as tray\n",
        "    import jacked.service.tray as tray\n"
        "    tray._mac_menubar_available = lambda: False\n"
        f"    tray._TRAY_AVAILABLE = {tray_available!r}\n"
        f"    tray._PYSTRAY_LOAD_FAILURE = {load_failure!r}\n"
        f"    tray._UVICORN_AVAILABLE = {uvicorn!r}\n",
    )


def test_a_missing_tray_package_is_a_broken_build_on_every_platform(contract):
    result = _probe(contract, code=_tray_state(False, None))
    assert (result.step, result.detail) == ("tray_backend", "ProbeRefusal tray_package_missing")


def test_a_tray_that_cannot_load_on_this_host_passes_with_a_note(contract):
    code = _tray_state(False, "DisplayNameError: Bad display name ':99'")
    result = _probe(contract, code=code)
    assert result.ok, result
    assert result.note.startswith(
        "The tray cannot run on this host (DisplayNameError), so the jacked service "
        "cannot run here. See the README Linux tray notes: https://"
    )
    assert "linux-tray-notes" in result.note
    assert ":99" not in result.note  # the error type only, never its text


def test_a_missing_uvicorn_refuses_even_with_a_working_tray(contract):
    result = _probe(contract, code=_tray_state(True, None, uvicorn=False))
    assert (result.step, result.detail) == ("tray_backend", "ProbeRefusal uvicorn_missing")


def test_preflight_prints_the_host_note_on_a_pass(monkeypatch):
    from jacked.cli import main

    note = "The tray cannot run on this host (ValueError), so the jacked service cannot run here."
    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(_spec(), {}),
        ),
        patch(
            "jacked.service.preflight_probe.run_service_probe",
            return_value=ProbeResult(True, note=note),
        ),
    ):
        text = CliRunner().invoke(main, ["service", "preflight"])
        data = CliRunner().invoke(main, ["service", "preflight", "--json"])

    assert text.exit_code == 0 and data.exit_code == 0
    assert "Note: " + note in " ".join(text.output.split())
    assert json.loads(data.output)["note"] == note


def test_the_report_file_carries_the_detail_when_stdout_is_lost(contract):
    """On Windows the launcher child writes to a new hidden console, so the
    probe's stdout never reaches the caller. The report file still does."""
    code = "import sys\nsys.stdout = None\n" + _seam(
        "    ssl.create_default_context()", "    raise OSError(10106, 'x')"
    )
    result = _probe(contract, code=code)
    assert (result.step, result.detail) == ("ssl", "OSError errno 10106")
    assert "Traceback" in Path(result.stderr_file).read_text(encoding="utf-8")
    assert not (contract[2].root / PROBE_REPORT_NAME).exists()


def test_the_exit_code_is_the_verdict_when_every_channel_is_lost(contract):
    code = "import sys\nsys.stdout = None\nsys.argv[1:] = []\n" + _seam(
        "    ssl.create_default_context()", "    raise ImportError('x')"
    )
    code = code.replace('REPORT = sys.argv[1] if len(sys.argv) > 1 else None', "REPORT = None")
    assert "REPORT = None" in code
    result = _probe(contract, code=code)
    assert (result.step, result.detail) == ("ssl", f"exit {PROBE_EXIT_BASE + 2}")


def test_a_crash_names_the_last_started_step(contract):
    code = f'import os\nprint("{STEP_MARK}ssl", flush=True)\nos._exit(9)\n'
    result = _probe(contract, code=code)
    assert (result.step, result.detail) == ("ssl", "exit 9")


def test_a_hung_probe_names_the_running_step(contract):
    code = f'import time\nprint("{STEP_MARK}socket", flush=True)\ntime.sleep(60)\n'
    result = _probe(contract, code=code, timeout=3.0)
    assert (result.step, result.detail) == ("socket", "timeout after 3s")
    assert not (contract[2].root / PROBE_REPORT_NAME).exists()


def test_a_hang_after_the_ok_marker_is_an_interpreter_exit(contract):
    code = f'import time\nprint("{OK_MARK}", flush=True)\ntime.sleep(60)\n'
    result = _probe(contract, code=code, timeout=3.0)
    assert result.step == "interpreter_exit"


def test_a_launch_error_is_named_with_its_number(tmp_path):
    def runner(argv, **kwargs):
        raise FileNotFoundError(2, "No such file", argv[0])

    spec = MagicMock(
        runtime_path="/opt/none/python",
        runtime_target_path="/opt/none/python3",
        launcher_path="/opt/none/launch",
        generation="a" * 64,
    )
    result = run_service_probe(
        spec, {"PATH": "/usr/bin"}, tmp_path, ProbeOptions(timeout=5, runner=runner)
    )
    assert result == ProbeResult(False, "interpreter_start", "FileNotFoundError errno 2")


def test_a_failed_probe_keeps_its_stderr_in_a_private_file(contract):
    code = _seam(
        "    socket.socket().close()",
        "    sys.stderr.write('stderr-canary\\n'); raise OSError(5, 'x')",
    )
    result = _probe(contract, code=code)
    path = Path(result.stderr_file)
    assert path == contract[2].root / PROBE_STDERR_NAME
    saved = path.read_text(encoding="utf-8")
    assert "stderr-canary" in saved
    assert "Traceback" in saved and "OSError" in saved
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    message = str(ServiceProbeError(result))
    assert message.startswith("at socket: OSError errno 5.")
    assert str(path) in message
    assert "stderr-canary" not in message


def test_the_probe_sees_only_the_service_environment(contract, monkeypatch):
    """A variable that only os.environ holds must not reach the probe."""
    monkeypatch.setenv("JACKED_PROBE_LEAK", "1")
    spec, environment, _paths = contract
    code = _seam(
        "    socket.socket().close()",
        "    socket.socket().close()\n"
        "    if 'JACKED_PROBE_LEAK' in os.environ:\n"
        "        raise LookupError('leak')\n"
        f"    if os.environ.get('JACKED_SERVICE_GENERATION') != {spec.generation!r}:\n"
        "        raise LookupError('generation')\n"
        f"    if os.environ.get('JACKED_SERVICE_USER') != {environment['JACKED_SERVICE_USER']!r}:\n"
        "        raise LookupError('user')",
    )
    assert _probe(contract, code=code) == ProbeResult(True)


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell launcher is Windows only")
def test_windows_probe_environment_is_exactly_the_delivered_mapping(contract):
    """PowerShell decodes every value, non-ASCII ones too, byte for byte."""
    spec, environment, paths = contract
    delivered = {**environment, "JACKED_TEST_NAME": "Владимир NUÑEZ Òscar O’Neil"}
    dump = paths.root / "probe-env.json"
    code = (
        "import json, os\n"
        f"open({str(dump)!r}, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n"
    )
    result = _probe(contract, code=code, environment=delivered)
    assert result.ok, result
    seen = json.loads(dump.read_text(encoding="utf-8"))
    expected = {**delivered, "JACKED_SERVICE_GENERATION": spec.generation}
    # Windows environment names are not case-sensitive; os.environ upper-cases them.
    assert seen == {key.upper(): value for key, value in expected.items()}


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell launcher is Windows only")
def test_windows_probe_without_systemroot_fails_with_10106(contract):
    """The negative control: the outage shape, through the real launcher."""
    _spec, environment, _paths = contract
    broken = {k: v for k, v in environment.items() if k not in {"SystemRoot", "windir"}}
    result = _probe(contract, environment=broken)
    assert result.step == "asyncio_event_loop", result
    assert "10106" in result.detail


def _spec():
    return MagicMock(
        runtime_path="/opt/jacked/bin/python",
        runtime_target_path="/opt/jacked/bin/python3.11",
        supervisor=MagicMock(value="launchd"),
        generation="a" * 64,
    )


_FAILED = ProbeResult(False, "asyncio_event_loop", "OSError winerror 10106", "/x/stderr.log")


def test_preflight_text_failure_puts_the_step_and_number_on_line_one(monkeypatch):
    from rich.console import Console

    import jacked.preflight_cli as preflight_cli
    from jacked.cli import main

    monkeypatch.setattr(preflight_cli, "console", Console(width=80))
    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(_spec(), {"SystemRoot": "canary-value"}),
        ),
        patch("jacked.service.preflight_probe.run_service_probe", return_value=_FAILED),
    ):
        result = CliRunner().invoke(main, ["service", "preflight"])

    assert result.exit_code == 1, result.output
    first = result.output.splitlines()[0]
    assert first.startswith(
        "[FAIL] ServiceProbeError at asyncio_event_loop: OSError winerror 10106"
    )
    assert "/x/stderr.log" in first
    assert "canary" not in result.output


def test_preflight_json_failure_keeps_the_contract_fields():
    from jacked.cli import main

    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(_spec(), {"SystemRoot": "canary-value"}),
        ),
        patch("jacked.service.preflight_probe.run_service_probe", return_value=_FAILED),
    ):
        result = CliRunner().invoke(main, ["service", "preflight", "--json"])

    assert result.exit_code == 1, result.output
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["probe_step"] == "asyncio_event_loop"
    assert data["runtime_path"] == "/opt/jacked/bin/python"
    assert data["generation"] == "a" * 64
    assert data["error"].startswith(
        "ServiceProbeError at asyncio_event_loop: OSError winerror 10106"
    )
    assert "canary" not in result.output


@pytest.mark.parametrize(("argv", "ceiling"), [([], 120.0), (["--timeout", "5"], 5.0)])
def test_the_probe_gets_what_is_left_of_one_budget(argv, ceiling):
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
        result = CliRunner().invoke(main, ["service", "preflight", *argv])

    assert result.exit_code == 0, result.output
    timeout = probe.call_args.args[3].timeout
    assert 0 < timeout <= ceiling


def test_no_time_left_after_provisioning_is_a_failure(monkeypatch):
    import jacked.preflight_cli as preflight_cli
    from jacked.cli import main

    clock = iter([0.0, 999.0])
    monkeypatch.setattr(preflight_cli.time, "monotonic", lambda: next(clock))
    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(_spec(), {}),
        ),
        patch("jacked.service.preflight_probe.run_service_probe") as probe,
    ):
        result = CliRunner().invoke(main, ["service", "preflight", "--json"])

    assert result.exit_code == 1
    probe.assert_not_called()
    assert json.loads(result.output)["probe_step"] == "interpreter_start"


def test_one_number_ties_the_budget_the_callers_and_the_batches():
    import jacked.service.updater as updater
    from jacked.cli import main
    from jacked.preflight_cli import preflight_budget

    assert PREFLIGHT_TIMEOUT_SECONDS == updater.PREFLIGHT_TIMEOUT_SECONDS
    # A 0.111.x caller passes no --timeout and stops the command after 120 s
    # (its own PREFLIGHT_TIMEOUT_SECONDS). The default must end first.
    old_caller_limit = 120
    assert preflight_budget(None) == PREFLIGHT_TIMEOUT_SECONDS - 10
    assert preflight_budget(None) < old_caller_limit
    assert updater.preflight_argv("j")[-2:] == ["--timeout", str(PREFLIGHT_TIMEOUT_SECONDS)]
    literal = f"jacked service preflight --timeout {PREFLIGHT_TIMEOUT_SECONDS} 2>&1"
    root = Path(__file__).resolve().parents[3]
    assert literal in (root / "jacked/cli.py").read_text(encoding="utf-8")
    assert literal in (root / "jacked/service/updater.py").read_text(encoding="utf-8")
    help_text = CliRunner().invoke(main, ["service", "preflight", "--help"]).output
    assert f"default {PREFLIGHT_TIMEOUT_SECONDS - 10}" in " ".join(help_text.split())


def test_preflight_end_to_end_through_the_real_launcher(tmp_path, monkeypatch):
    """The real gate: real provisioning, the real probe, a trusted runtime."""
    import jacked.service.lifecycle as lifecycle
    from jacked.cli import main

    monkeypatch.setattr(
        lifecycle.sys, "executable", trusted_runtime(tmp_path / "rt", importable=True)
    )
    result = CliRunner().invoke(main, ["service", "preflight", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["ok"] is True and data["probe_step"] is None, result.output


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes")
def test_a_world_writable_runtime_directory_is_refused(tmp_path, monkeypatch):
    """Why the tests above use a trusted copy: ubuntu-latest keeps Python
    under a world-writable /opt/hostedtoolcache, and provisioning refuses it."""
    import jacked.service.lifecycle as lifecycle
    from jacked.cli import main

    runtime = Path(trusted_runtime(tmp_path / "rt", importable=True))
    runtime.parent.chmod(0o777)
    monkeypatch.setattr(lifecycle.sys, "executable", str(runtime))
    result = CliRunner().invoke(main, ["service", "preflight", "--json"])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["probe_step"] is None


def test_the_probe_argv_carries_no_service_lifecycle_words(contract):
    """The probe argv never matches the test tripwire for a real service start."""
    from jacked.service.preflight_probe import probe_argv

    spec, environment, paths = contract
    argv, temporary = probe_argv(spec, environment, paths.root, PROBE_CODE)
    if temporary is not None:
        temporary.unlink()
    padded = f" {' '.join(argv)} "
    for verb in ("start", "restart", "preflight", "recover", "install"):
        assert f" service {verb} " not in padded


def _windows_probe_launcher(contract, monkeypatch, environment=None):
    """Render the real Windows probe launcher on any OS. Return (text, argv)."""
    from types import SimpleNamespace

    import jacked.service.preflight_probe as preflight_probe

    spec, default_environment, paths = contract
    monkeypatch.setattr(preflight_probe, "sys", SimpleNamespace(platform="win32"))
    report = paths.root / PROBE_REPORT_NAME
    argv, launcher = preflight_probe.probe_argv(
        spec, default_environment if environment is None else environment,
        paths.root, PROBE_CODE, report,
    )
    data = launcher.read_bytes()
    launcher.unlink()
    return data, argv, report


def _decoded_launcher(text):
    import base64
    import re

    found = {}
    pattern = r"\$psi\.(FileName|Arguments|Environment\['([^']+)'\]) = \(Decode '([^']*)'\)"
    for match in re.finditer(pattern, text):
        found[match.group(2) or match.group(1)] = base64.b64decode(match.group(3)).decode("utf-8")
    return found


def test_the_real_windows_probe_launcher_renders_on_every_os(contract, monkeypatch):
    """The counterpart of the Windows-only probe tests: the launcher is pure."""
    import base64
    import re
    import subprocess

    from jacked.service.preflight_probe import probe_python_argv

    data, argv, report = _windows_probe_launcher(contract, monkeypatch)
    text = data.decode("ascii")  # pure ASCII or this raises
    assert argv[0].endswith("powershell.exe") and argv[-2:] == ["-Generation", contract[0].generation]
    arguments = _decoded_launcher(text)["Arguments"]
    assert arguments == subprocess.list2cmdline(list(probe_python_argv(PROBE_CODE, report)))
    encoded = re.search(r"b64decode\('([A-Za-z0-9+/=]+)'\)", arguments).group(1)
    assert base64.b64decode(encoded).decode("utf-8") == PROBE_CODE


def test_the_windows_probe_launcher_carries_the_exact_environment(contract, monkeypatch):
    """The counterpart of the Windows-only environment round trip."""
    _spec, environment, _paths = contract
    delivered = {**environment, "JACKED_TEST_NAME": "Владимир NUÑEZ Òscar O’Neil"}
    data, _argv, _report = _windows_probe_launcher(contract, monkeypatch, delivered)
    decoded = _decoded_launcher(data.decode("ascii"))
    assert {k: v for k, v in decoded.items() if k not in {"FileName", "Arguments"}} == delivered


def test_a_launcher_that_cannot_render_is_a_named_refusal(contract, monkeypatch):
    from types import SimpleNamespace

    import jacked.service.preflight_probe as preflight_probe

    spec, environment, paths = contract
    monkeypatch.setattr(preflight_probe, "sys", SimpleNamespace(platform="win32"))
    ran = []
    result = run_service_probe(
        spec, {**environment, "BAD KEY": "x"}, paths.root,
        ProbeOptions(timeout=5, runner=lambda *a, **k: ran.append(a)),
    )
    assert (result.ok, result.step) == (False, "interpreter_start")
    assert result.detail.startswith("ValueError: invalid environment name")
    assert ran == []
    assert not (paths.root / PROBE_REPORT_NAME).exists()


def test_the_probe_program_is_one_line_on_every_path():
    from jacked.service.preflight_probe import probe_python_argv

    argv = probe_python_argv(PROBE_CODE, Path("/x/report.log"))
    assert all("\n" not in part and "\r" not in part for part in argv)
    assert argv[-1] == str(Path("/x/report.log"))


def test_program_paths_come_from_the_os_not_the_caller(monkeypatch):
    """A caller with SYSTEMROOT=D:\\Other renders the same program paths."""
    import xml.etree.ElementTree as ElementTree

    import jacked.service.environment as environment
    from jacked.service.batch_programs import pin_programs, utf8_code_page_line
    from jacked.service.supervisors import _append_task_action
    from jacked.winproc import system_program
    from tests.unit.service.service_contract_support import fixed_windows

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(environment, "default_windows_resolvers", lambda: fixed_windows())
    monkeypatch.setenv("SystemRoot", r"D:\Other")
    monkeypatch.setenv("windir", r"D:\Other")
    monkeypatch.setenv("ComSpec", r"D:\Other\cmd.exe")

    assert system_program("powershell").startswith("C:\\Windows\\")
    assert system_program("cmd") == r"C:\Windows\System32\cmd.exe"
    assert utf8_code_page_line().startswith('"C:\\Windows\\System32\\chcp.com"')
    pinned = pin_programs("@echo off\r\nping -n 2 h\r\npowershell -x\r\n", '"C:\\j.exe"')
    assert "D:\\Other" not in pinned
    root = ElementTree.Element("Task")
    _append_task_action(root, MagicMock(launcher_path=r"C:\l\x.ps1"), {}, "-File x")
    assert root.find("./Actions/Exec/Command").text.startswith("C:\\Windows\\")

    # When the OS call fails, the environment is the fallback.
    def broken():
        raise OSError("no API")

    monkeypatch.setattr(
        environment, "default_windows_resolvers",
        lambda: MagicMock(windows_directory=broken),
    )
    assert system_program("powershell").startswith("D:\\Other\\")

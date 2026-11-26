import os
import subprocess
import sys
from pathlib import Path

import pytest

from jacked.service.environment import (
    WINDOWS_DEFAULT_PATHEXT,
    EnvironmentInputs,
    build_service_environment,
    posix_preinterpreter_command,
    render_windows_launcher,
)


def test_environment_is_allowlist_only_and_drops_secret_and_python_injection():
    hostile = {
        "LANG": "en_US.UTF-8",
        "DISPLAY": ":0",
        "OPENAI_API_KEY": "secret-canary",
        "ANTHROPIC_API_KEY": "secret-canary",
        "PYTHONPATH": "/evil",
        "PYTHONHOME": "/evil",
        "LD_PRELOAD": "/evil.so",
    }
    env = build_service_environment(
        EnvironmentInputs(home="/home/alice", user_id="uid:1000", platform="linux"),
        inherited=hostile,
    )
    assert env["HOME"] == "/home/alice"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["DISPLAY"] == ":0"
    assert "secret-canary" not in repr(env)
    assert not ({"PYTHONPATH", "PYTHONHOME", "LD_PRELOAD"} & env.keys())


def test_proxy_userinfo_is_rejected():
    with pytest.raises(ValueError, match="userinfo"):
        build_service_environment(
            EnvironmentInputs(home="/tmp/user", user_id="uid:1", platform="linux"),
            inherited={"HTTPS_PROXY": "https://user:password@example.com"},
            allow_proxy=True,
        )


def test_posix_command_clears_environment_before_python():
    command = posix_preinterpreter_command(
        runtime="/opt/jacked/python",
        argv=("-I", "-m", "jacked", "service", "start"),
        environment={"HOME": "/home/alice", "PATH": "/usr/bin:/bin"},
    )
    assert command[:2] == ("/usr/bin/env", "-i")
    assert command[-6:] == (
        "/opt/jacked/python",
        "-I",
        "-m",
        "jacked",
        "service",
        "start",
    )


def test_posix_launcher_command_binds_runtime_target():
    command = posix_preinterpreter_command(
        runtime="/opt/jacked/venv/bin/python",
        runtime_target="/opt/jacked/python-build/bin/python3.14",
        argv=("-I", "-m", "jacked", "service", "start"),
        environment={"HOME": "/home/alice"},
        launcher="/home/alice/.claude/jacked-launch",
    )
    assert command[-8:] == (
        "/home/alice/.claude/jacked-launch",
        "/opt/jacked/venv/bin/python",
        "/opt/jacked/python-build/bin/python3.14",
        "-I",
        "-m",
        "jacked",
        "service",
        "start",
    )


def test_windows_launcher_clears_environment_and_never_embeds_inherited_secret():
    script = render_windows_launcher(
        runtime=r"C:\\jacked\\python.exe",
        argv=("-I", "-m", "jacked", "service", "start"),
        environment={
            "USERPROFILE": r"C:\\Users\\alice",
            "PATH": r"C:\\Windows\\System32",
        },
    )
    assert ".Environment.Clear()" in script
    assert "$psi.Environment['JACKED_SERVICE_GENERATION'] = $Generation" in script
    assert "^[0-9a-f]{64}$" in script
    assert "UseShellExecute = $false" in script
    assert "secret-canary" not in script
    assert os.linesep not in "unused"  # keep this test platform-neutral


def test_windows_process_arguments_use_commandlinetoargvw_quoting():
    arguments = ("-I", "-m", "jacked", "value with spaces", 'quote"inside')
    script = render_windows_launcher(
        runtime=r"C:\Program Files\Jacked\python.exe",
        argv=arguments,
        environment={"USERPROFILE": r"C:\Users\Alice Example"},
    )
    expected = subprocess.list2cmdline(list(arguments)).replace("'", "''")
    assert f"$psi.Arguments = '{expected}'" in script


_WINDOWS_INPUTS = EnvironmentInputs(
    home=r"C:\Users\alice", user_id="sid:S-1-5-21-1", platform="win32"
)


def test_windows_environment_keeps_the_os_variables_from_the_inherited_mapping():
    inherited = {
        "SystemRoot": r"C:\Windows",
        "windir": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "ProgramFiles(x86)": r"C:\Program Files (x86)",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    }
    env = build_service_environment(_WINDOWS_INPUTS, inherited=inherited)
    for key, value in inherited.items():
        assert env[key] == value
    assert env["TEMP"] == env["TMP"] == r"C:\Users\alice\AppData\Local\Temp"
    assert env["PATHEXT"] == WINDOWS_DEFAULT_PATHEXT
    assert env["PATH"] == r"C:\Windows\System32;C:\Windows"
    assert env["USERPROFILE"] == r"C:\Users\alice"


def test_windows_environment_matches_names_without_case_and_writes_canonical_names():
    inherited = {
        "SYSTEMROOT": r"D:\Win",
        "WINDIR": r"D:\Win",
        "programfiles(x86)": r"D:\PF86",
        "LocalAppData": r"D:\Users\alice\Local",
    }
    env = build_service_environment(_WINDOWS_INPUTS, inherited=inherited)
    assert env["SystemRoot"] == r"D:\Win"
    assert env["windir"] == r"D:\Win"
    assert env["ProgramFiles(x86)"] == r"D:\PF86"
    assert env["LOCALAPPDATA"] == r"D:\Users\alice\Local"
    assert not ({"SYSTEMROOT", "WINDIR", "programfiles(x86)", "LocalAppData"} & env.keys())
    assert env["PATH"] == r"D:\Win\System32;D:\Win"


def test_windows_environment_sets_systemroot_through_the_winproc_resolver():
    # Nothing inherited: the resolver default.
    env = build_service_environment(_WINDOWS_INPUTS, inherited={})
    assert env["SystemRoot"] == env["windir"] == r"C:\Windows"
    # Only windir inherited: the resolver takes windir for both names.
    env = build_service_environment(_WINDOWS_INPUTS, inherited={"windir": r"E:\Windows"})
    assert env["SystemRoot"] == env["windir"] == r"E:\Windows"
    # A relative SystemRoot is not a root.
    env = build_service_environment(_WINDOWS_INPUTS, inherited={"SystemRoot": "Windows"})
    assert env["SystemRoot"] == env["windir"] == r"C:\Windows"


def test_windows_temp_is_localappdata_temp_and_never_the_inherited_temp():
    def temp(inherited):
        env = build_service_environment(_WINDOWS_INPUTS, inherited=inherited)
        assert env["TMP"] == env["TEMP"]
        return env["TEMP"]

    local = r"D:\Profiles\alice\Local"
    # Remote Desktop gives each session its own TEMP subfolder: never used.
    assert temp({"LOCALAPPDATA": local, "TEMP": r"C:\T1\2", "TMP": r"C:\T2"}) == (
        local + r"\Temp"
    )
    assert temp({"LOCALAPPDATA": "relative", "TEMP": r"C:\T1"}) == (
        r"C:\Users\alice\AppData\Local\Temp"
    )
    assert temp({"TEMP": r"C:\T1"}) == r"C:\Users\alice\AppData\Local\Temp"
    assert temp({}) == r"C:\Users\alice\AppData\Local\Temp"


def test_windows_pathext_is_the_default_and_number_of_processors_is_dropped():
    env = build_service_environment(
        _WINDOWS_INPUTS,
        inherited={
            "PATHEXT": ".COM;.EXE;.BAT;.CMD;.CPL",
            "NUMBER_OF_PROCESSORS": "16",
        },
    )
    assert env["PATHEXT"] == ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"
    assert "NUMBER_OF_PROCESSORS" not in env


def test_windows_explicit_temp_dir_wins():
    inputs = EnvironmentInputs(
        home=r"C:\Users\alice",
        user_id="sid:S-1-5-21-1",
        platform="win32",
        temp_dir=r"C:\jacked\tmp",
    )
    env = build_service_environment(inputs, inherited={"TEMP": r"C:\T1"})
    assert env["TEMP"] == env["TMP"] == r"C:\jacked\tmp"


def test_windows_environment_still_drops_secrets_proxy_and_python_injection():
    hostile = {
        "SystemRoot": r"C:\Windows",
        "OPENAI_API_KEY": "secret-canary",
        "ANTHROPIC_API_KEY": "secret-canary",
        "GITHUB_TOKEN": "secret-canary",
        "HTTPS_PROXY": "https://user:secret-canary@proxy.example",
        "PYTHONPATH": r"C:\evil",
        "PYTHONHOME": r"C:\evil",
        "PYTHONSTARTUP": r"C:\evil.py",
        "pythonpath": r"C:\evil",
        "PSModulePath": r"C:\evil",
    }
    env = build_service_environment(_WINDOWS_INPUTS, inherited=hostile)
    assert "secret-canary" not in repr(env)
    assert not any(key.upper().startswith("PYTHON") for key in env)
    assert not ({"HTTPS_PROXY", "PSModulePath"} & env.keys())


def test_windows_allowlisted_inherited_names_carry_the_os_variables_only():
    from jacked.service.environment import WINDOWS_OS_KEYS
    from jacked.service.lifecycle import inherited_allowlisted_environment

    source = {
        "SYSTEMROOT": r"C:\Windows",
        "TEMP": r"C:\T",
        "PATHEXT": ".COM;.CPL",
        "NUMBER_OF_PROCESSORS": "16",
        "OPENAI_API_KEY": "secret-canary",
        "PYTHONPATH": r"C:\evil",
        "LANG": "en_US.UTF-8",
    }
    inherited = inherited_allowlisted_environment("win32", source)
    # Session values (TEMP, PATHEXT, NUMBER_OF_PROCESSORS) are not carried.
    assert inherited == {
        "SystemRoot": r"C:\Windows",
        "LANG": "en_US.UTF-8",
    }
    # No allowlisted name may carry a secret or change the interpreter.
    for name in WINDOWS_OS_KEYS:
        upper = name.upper()
        assert not upper.startswith("PYTHON")
        assert not any(part in upper for part in ("TOKEN", "KEY", "SECRET", "PROXY"))
    # POSIX hosts keep the old allowlist: no Windows names.
    assert inherited_allowlisted_environment("linux", source) == {"LANG": "en_US.UTF-8"}


def test_windows_launcher_sets_systemroot_from_the_service_environment():
    env = build_service_environment(
        _WINDOWS_INPUTS, inherited={"SystemRoot": r"C:\Windows"}
    )
    script = render_windows_launcher(
        runtime=r"C:\jacked\python.exe",
        argv=("-I", "-m", "jacked", "service", "start"),
        environment=env,
    )
    clear = script.index(".Environment.Clear()")
    for key in ("SystemRoot", "windir", "TEMP", "TMP"):
        line = f"$psi.Environment['{key}'] = "
        assert script.index(line) > clear
    assert r"$psi.Environment['SystemRoot'] = 'C:\Windows'" in script


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="proves the Windows runtime starts under the service environment; Windows only",
)
def test_windows_runtime_starts_under_the_exact_service_environment(tmp_path):
    from jacked.service.lifecycle import (
        default_environment,
        inherited_allowlisted_environment,
    )

    # Build the environment exactly as provision_service_contract does.
    env = default_environment(
        home=str(Path.home()),
        user_id="sid:test",
        inherited_allowlisted=inherited_allowlisted_environment(sys.platform),
        platform=sys.platform,
    )
    probe = (
        "import asyncio, socket, ssl, tempfile; "
        "socket.socket().close(); "
        "asyncio.new_event_loop().close(); "
        "print(tempfile.gettempdir())"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    temp_dir = result.stdout.strip()
    assert temp_dir
    assert os.path.normcase(os.path.abspath(temp_dir)) != os.path.normcase(str(tmp_path))
    assert os.path.normcase(os.path.abspath(temp_dir)) != os.path.normcase(os.getcwd())


def test_windows_service_environment_is_a_fixed_point_of_its_own_rebuild():
    """The service re-provisions its contract at boot (tray.py) from the
    environment that the launcher gave it. That rebuild must give the same
    environment, so the same launcher digest and the same generation."""
    from jacked.service.lifecycle import (
        default_environment,
        inherited_allowlisted_environment,
    )

    messy = {
        "SYSTEMROOT": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD;.CPL",
        "LocalAppData": r"C:\Users\alice\AppData\Local",
        "TMP": r"C:\Users\alice\AppData\Local\Temp\2",
        "USERNAME": "alice",
        "LANG": "en_US.UTF-8",
        "OPENAI_API_KEY": "secret-canary",
        "PYTHONPATH": r"C:\evil",
        "Path": r"C:\tools;C:\Windows\System32",
    }

    def build(source):
        return default_environment(
            home=r"C:\Users\alice",
            user_id="sid:S-1-5-21-1",
            inherited_allowlisted=inherited_allowlisted_environment("win32", source),
            platform="win32",
        )

    first = build(messy)
    service_view = {**first, "JACKED_SERVICE_GENERATION": "a" * 64}
    assert build(service_view) == first
    assert "secret-canary" not in repr(first)


_WINDOWS_CALLER = {
    "SystemRoot": r"C:\Windows",
    "windir": r"C:\Windows",
    "ComSpec": r"C:\Windows\System32\cmd.exe",
    "ProgramFiles": r"C:\Program Files",
    "APPDATA": r"C:\Users\alice\AppData\Roaming",
    "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    "USERNAME": "alice",
    "COMPUTERNAME": "DESK",
    "PATHEXT": ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC",
    "TEMP": r"C:\Users\alice\AppData\Local\Temp",
    "TMP": r"C:\Users\alice\AppData\Local\Temp",
    "NUMBER_OF_PROCESSORS": "8",
}


def _windows_contract(tmp_path, monkeypatch, caller):
    """Provision the win32 contract exactly as a caller with ``caller`` does.

    Both callers in a test share one state directory, as two shells of one
    user do, so the launcher path differs only when its content does."""
    import jacked.service.lifecycle as lifecycle
    from jacked.service.instance_models import ServicePaths
    from jacked.service.spec import SupervisorKind

    monkeypatch.setattr(lifecycle.os, "environ", dict(caller))
    monkeypatch.setattr(lifecycle.Path, "home", lambda: Path(r"C:\Users\alice"))
    monkeypatch.setattr(
        "jacked.service.instance.current_user_identity", lambda: "sid:S-1-5-21-1"
    )
    paths = ServicePaths.in_directory(tmp_path / "state")
    spec, environment = lifecycle.provision_service_contract(
        paths=paths, platform="win32", supervisor=SupervisorKind.MANUAL
    )
    return spec.launcher_sha256, spec.generation, environment


@pytest.mark.parametrize(
    "session_change",
    [
        pytest.param({"TEMP": r"C:\Users\alice\AppData\Local\Temp\2",
                      "TMP": r"C:\Users\alice\AppData\Local\Temp\2"}, id="rdp-temp"),
        pytest.param({"PATHEXT": ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.CPL"},
                     id="pwsh-pathext"),
        pytest.param({"NUMBER_OF_PROCESSORS": "16"}, id="vm-resize"),
        pytest.param({"TERM_PROGRAM": "vscode", "SESSIONNAME": "RDP-Tcp#3"}, id="extra"),
        pytest.param({"SystemRoot": None, "SYSTEMROOT": r"C:\Windows"}, id="key-case"),
    ],
)
def test_windows_generation_does_not_depend_on_the_caller_session(
    tmp_path, monkeypatch, session_change
):
    base = _windows_contract(tmp_path, monkeypatch, _WINDOWS_CALLER)
    other = dict(_WINDOWS_CALLER)
    for key, value in session_change.items():
        if value is None:
            other.pop(key)
        else:
            other[key] = value
    changed = _windows_contract(tmp_path, monkeypatch, other)
    assert changed[0] == base[0], "launcher digest changed"
    assert changed[1] == base[1], "generation changed"
    assert changed[2] == base[2]


def test_windows_generation_follows_a_stable_value(tmp_path, monkeypatch):
    base = _windows_contract(tmp_path, monkeypatch, _WINDOWS_CALLER)
    other = {**_WINDOWS_CALLER, "LOCALAPPDATA": r"D:\Profiles\alice\Local"}
    changed = _windows_contract(tmp_path, monkeypatch, other)
    assert changed[0] != base[0]
    assert changed[1] != base[1]

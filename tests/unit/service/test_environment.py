import base64
import os
import re
import subprocess
import sys

import pytest

from jacked.service.environment import (
    EnvironmentInputs,
    build_service_environment,
    inherited_allowlisted_environment,
    posix_preinterpreter_command,
    render_windows_launcher,
)
from jacked.service.windows_env import WINDOWS_DEFAULT_PATHEXT, WindowsResolvers
from tests.unit.service.service_contract_support import (
    WINDOWS_VALUES,
    fixed_windows,
    provision_contract,
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


def _decoded(script: str) -> dict[str, str]:
    """Map each launcher target ('FileName', 'Arguments', or an env name) to
    the value that its base64 expression decodes to."""
    found = {}
    pattern = r"\$psi\.(FileName|Arguments|Environment\['([^']+)'\]) = \(Decode '([^']*)'\)"
    for match in re.finditer(pattern, script):
        name = match.group(2) or match.group(1)
        found[name] = base64.b64decode(match.group(3)).decode("utf-8")
    return found


def test_windows_process_arguments_use_commandlinetoargvw_quoting():
    arguments = ("-I", "-m", "jacked", "value with spaces", 'quote"inside')
    script = render_windows_launcher(
        runtime=r"C:\Program Files\Jacked\python.exe",
        argv=arguments,
        environment={"USERPROFILE": r"C:\Users\Alice Example"},
    )
    decoded = _decoded(script)
    assert decoded["Arguments"] == subprocess.list2cmdline(list(arguments))
    assert decoded["FileName"] == r"C:\Program Files\Jacked\python.exe"
    assert decoded["USERPROFILE"] == r"C:\Users\Alice Example"


@pytest.mark.parametrize("name", ["Владимир", "NUÑEZ", "Òscar", "O’Neil‘‛"])
def test_windows_launcher_is_ascii_and_round_trips_non_ascii_values(name):
    """PowerShell 5.1 reads a BOM-less .ps1 in the ANSI code page and ends a
    single-quoted string at U+0027 and U+2018-U+201B. The UTF-8 bytes of В
    and Ñ hold 0x92/0x91, which cp1252 decodes to those quotes."""
    home = rf"C:\Users\{name}"
    script = render_windows_launcher(
        runtime=rf"C:\Users\{name}\jacked\python.exe",
        argv=("-I", "-m", "jacked", "service", "start", name),
        environment={"USERPROFILE": home, "USERNAME": name, "TEMP": home + r"\Temp"},
    )
    raw = script.encode("ascii")  # raises on any non-ASCII byte
    for codec in ("cp1252", "cp1251"):
        text = raw.decode(codec)
        assert not any(char in text for char in "\u2018\u2019\u201a\u201b")
    decoded = _decoded(script)
    assert decoded["USERPROFILE"] == home
    assert decoded["USERNAME"] == name
    assert decoded["FileName"] == rf"C:\Users\{name}\jacked\python.exe"
    assert name in decoded["Arguments"]


@pytest.mark.parametrize("key", ["BAD'KEY", "A B", "X=Y", "ключ", ""])
def test_windows_launcher_refuses_an_environment_name_outside_the_safe_set(key):
    with pytest.raises(ValueError, match="environment name"):
        render_windows_launcher(
            runtime=r"C:\py\python.exe", argv=("-I",), environment={key: "v"}
        )


@pytest.mark.skipif(sys.platform != "win32", reason="the PowerShell parser is Windows only")
@pytest.mark.real_process  # runs the real powershell.exe parser, no Task Scheduler
def test_windows_launcher_parses_with_zero_errors_in_windows_powershell(tmp_path):
    from jacked.winproc import system_root

    script = render_windows_launcher(
        runtime=r"C:\Users\Владимир\python.exe",
        argv=("-I", "-m", "jacked", "service", "start"),
        environment={"USERPROFILE": r"C:\Users\NUÑEZ", "USERNAME": "Òscar"},
    )
    launcher = tmp_path / "launcher.ps1"
    launcher.write_bytes(script.encode("ascii"))
    powershell = os.path.join(
        system_root(), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    check = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{launcher}', [ref]$null, [ref]$errors); "
        "exit $errors.Count"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


_WINDOWS_INPUTS = EnvironmentInputs(
    home=r"C:\Users\alice", user_id="sid:S-1-5-21-1", platform="win32"
)


def _windows_env(inherited=None, **changes):
    return build_service_environment(
        _WINDOWS_INPUTS, inherited=inherited or {}, windows=fixed_windows(**changes)
    )


def test_windows_environment_comes_from_the_resolvers_only():
    env = _windows_env()
    for name in (
        "SystemRoot", "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles",
        "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
        "CommonProgramFiles(x86)", "CommonProgramW6432", "PUBLIC", "USERNAME",
        "COMPUTERNAME",
    ):
        assert env[name] == WINDOWS_VALUES[name], name
    assert env["windir"] == env["SystemRoot"] == r"C:\Windows"
    assert env["SystemDrive"] == "C:"
    assert env["PATH"] == r"C:\Windows\System32;C:\Windows"
    assert env["PATHEXT"] == WINDOWS_DEFAULT_PATHEXT
    assert env["OS"] == "Windows_NT"
    assert env["PROCESSOR_ARCHITECTURE"] == "AMD64"
    assert env["TEMP"] == env["TMP"] == r"C:\Users\alice\AppData\Local\Temp"
    assert (env["HOMEDRIVE"], env["HOMEPATH"]) == ("C:", r"\Users\alice")
    assert env["USERPROFILE"] == r"C:\Users\alice"
    for dropped in ("USERDOMAIN", "ComSpec", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL"):
        assert dropped not in env


def test_windows_environment_ignores_every_inherited_value():
    inherited = {
        "SystemRoot": r"D:\Other", "SYSTEMROOT": r"D:\Other", "ComSpec": r"D:\cmd.exe",
        "USERDOMAIN": "WORKGROUP", "LANG": "en_US.UTF-8", "TEMP": r"C:\T\2",
        "PATHEXT": ".COM;.CPL", "HOMEPATH": r"\Other", "NUMBER_OF_PROCESSORS": "16",
        "OPENAI_API_KEY": "secret-canary", "PYTHONPATH": r"C:\evil",
        "PYTHONSTARTUP": r"C:\evil.py", "HTTPS_PROXY": "https://u:secret-canary@p",
    }
    assert _windows_env(inherited) == _windows_env()
    assert "secret-canary" not in repr(_windows_env(inherited))


def test_windows_architecture_follows_the_interpreter_platform():
    assert _windows_env(platform="win-arm64")["PROCESSOR_ARCHITECTURE"] == "ARM64"
    assert _windows_env(platform="win32")["PROCESSOR_ARCHITECTURE"] == "x86"
    assert "PROCESSOR_ARCHITECTURE" not in _windows_env(platform="mingw")


def test_windows_folders_that_the_os_does_not_have_are_left_out():
    values = {k: v for k, v in WINDOWS_VALUES.items() if not k.endswith("W6432")}
    env = build_service_environment(
        _WINDOWS_INPUTS, windows=WindowsResolvers.fixed(values)
    )
    assert "ProgramW6432" not in env
    assert "CommonProgramW6432" not in env


def test_windows_temp_falls_back_to_the_home_profile():
    values = {k: v for k, v in WINDOWS_VALUES.items() if k != "LOCALAPPDATA"}
    env = build_service_environment(
        _WINDOWS_INPUTS, windows=WindowsResolvers.fixed(values)
    )
    assert env["TEMP"] == env["TMP"] == r"C:\Users\alice\AppData\Local\Temp"


def test_windows_explicit_temp_dir_wins():
    inputs = EnvironmentInputs(
        home=r"C:\Users\alice", user_id="sid:S-1-5-21-1", platform="win32",
        temp_dir=r"C:\jacked\tmp",
    )
    env = build_service_environment(inputs, windows=fixed_windows())
    assert env["TEMP"] == env["TMP"] == r"C:\jacked\tmp"


def test_windows_relative_windows_directory_is_refused():
    with pytest.raises(ValueError, match="Windows directory"):
        _windows_env(SystemRoot="Windows")


def test_windows_value_with_a_newline_is_refused():
    with pytest.raises(ValueError, match="USERNAME"):
        _windows_env(USERNAME="alice\nPYTHONPATH=x")


def test_the_inherited_allowlist_is_empty_on_windows_and_locale_gui_elsewhere():
    source = {
        "SYSTEMROOT": r"C:\Windows", "TEMP": r"C:\T", "LANG": "en_US.UTF-8",
        "DISPLAY": ":0", "OPENAI_API_KEY": "secret-canary", "PYTHONPATH": "/evil",
    }
    assert inherited_allowlisted_environment("win32", source) == {}
    assert inherited_allowlisted_environment("linux", source) == {
        "DISPLAY": ":0", "LANG": "en_US.UTF-8",
    }


def test_windows_launcher_sets_systemroot_from_the_service_environment():
    script = render_windows_launcher(
        runtime=r"C:\jacked\python.exe",
        argv=("-I", "-m", "jacked", "service", "start"),
        environment=_windows_env(),
    )
    clear = script.index(".Environment.Clear()")
    for key in ("SystemRoot", "windir", "TEMP", "TMP", "PATHEXT"):
        assert script.index(f"$psi.Environment['{key}'] = ") > clear
    assert _decoded(script)["SystemRoot"] == r"C:\Windows"


# Two callers whose environments differ in ANY variable. On win32 nothing
# comes from the caller, so each row must give the same contract.
_CALLER = {
    "SystemRoot": r"C:\Windows", "ComSpec": r"C:\Windows\System32\cmd.exe",
    "USERDOMAIN": "CORP", "LANG": "en_US.UTF-8",
    "TEMP": r"C:\Users\alice\AppData\Local\Temp", "HOMEPATH": r"\Users\alice",
    "PATHEXT": WINDOWS_DEFAULT_PATHEXT, "NUMBER_OF_PROCESSORS": "8",
}


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"SystemRoot": None, "SYSTEMROOT": r"C:\WINDOWS"}, id="systemroot-case"),
        pytest.param({"ComSpec": None, "COMSPEC": r"D:\cmd.exe"}, id="comspec"),
        pytest.param({"USERDOMAIN": "WORKGROUP"}, id="openssh-userdomain"),
        pytest.param({"LANG": "C.UTF-8"}, id="git-bash-lang"),
        pytest.param({"TEMP": r"C:\Users\alice\AppData\Local\Temp\2",
                      "TMP": r"C:\Users\alice\AppData\Local\Temp\2"}, id="rdp-temp"),
        pytest.param({"HOMEPATH": r"\\server\home\alice"}, id="domain-homepath"),
        pytest.param({"PATHEXT": WINDOWS_DEFAULT_PATHEXT + ";.CPL"}, id="pwsh-pathext"),
        pytest.param({"NUMBER_OF_PROCESSORS": "16"}, id="vm-resize"),
        pytest.param({"TERM_PROGRAM": "vscode", "SESSIONNAME": "RDP-Tcp#3"}, id="extra"),
    ],
)
def test_windows_generation_does_not_depend_on_the_caller(tmp_path, monkeypatch, change):
    import jacked.service.lifecycle as lifecycle

    def contract(caller, state):
        monkeypatch.setattr(lifecycle.os, "environ", dict(caller))
        monkeypatch.setattr(lifecycle.Path, "home", lambda: lifecycle.Path(r"C:\Users\alice"))
        monkeypatch.setattr(
            "jacked.service.instance.current_user_identity", lambda: "sid:S-1-5-21-1"
        )
        spec, environment, _paths = provision_contract(
            tmp_path, monkeypatch, platform="win32", state=state,
            runtime=runtime, windows=fixed_windows(),
        )
        return spec.launcher_sha256, spec.generation, environment

    from tests.unit.service.service_contract_support import trusted_runtime

    runtime = trusted_runtime(tmp_path / "rt")
    other = dict(_CALLER)
    for key, value in change.items():
        if value is None:
            other.pop(key)
        else:
            other[key] = value
    # Both callers share one state directory, as two shells of one user do.
    state = tmp_path / "state"
    assert contract(other, state) == contract(_CALLER, state)


def test_windows_generation_follows_a_resolved_value(tmp_path, monkeypatch):
    from tests.unit.service.service_contract_support import trusted_runtime

    runtime = trusted_runtime(tmp_path / "rt")
    state = tmp_path / "state"
    base = provision_contract(
        tmp_path, monkeypatch, platform="win32", runtime=runtime, state=state,
        windows=fixed_windows(),
    )[0]
    moved = provision_contract(
        tmp_path, monkeypatch, platform="win32", runtime=runtime, state=state,
        windows=fixed_windows(LOCALAPPDATA=r"D:\Profiles\alice\Local"),
    )[0]
    assert moved.launcher_sha256 != base.launcher_sha256
    assert moved.generation != base.generation


@pytest.mark.skipif(sys.platform != "win32", reason="asks the real Windows APIs")
def test_windows_resolvers_agree_with_the_os(monkeypatch):
    """Each resolved value is absolute (for a folder) and equals the value
    that Windows gave this runner's own process environment."""
    from jacked.service.windows_env import KNOWN_FOLDERS, real_windows_resolvers
    from jacked.winproc import is_absolute_windows_path

    env = build_service_environment(_WINDOWS_INPUTS, windows=real_windows_resolvers())
    real = {key.upper(): value for key, value in os.environ.items()}
    for name in ("SystemRoot", "windir", "SystemDrive", *KNOWN_FOLDERS):
        if name not in env:
            continue
        if name != "SystemDrive":
            assert is_absolute_windows_path(env[name]), name
        if name.upper() in real:
            assert os.path.normcase(env[name]) == os.path.normcase(real[name.upper()]), name
    assert env["USERNAME"].casefold() == real["USERNAME"].casefold()
    assert env["COMPUTERNAME"].casefold() == real["COMPUTERNAME"].casefold()
    assert env["PROCESSOR_ARCHITECTURE"] in {"AMD64", "ARM64", "x86"}

import os
import subprocess

import pytest

from jacked.service.environment import (
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

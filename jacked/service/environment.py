"""Secret-negative environment and pre-interpreter launcher specifications."""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit

from jacked.winproc import _absolute as _windows_absolute
from jacked.winproc import system_root


_SAFE_PATHS = {
    "darwin": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
    "linux": "/usr/local/bin:/usr/bin:/bin",
    "win32": r"C:\Windows\System32;C:\Windows",
}
_LOCALE_RE = re.compile(
    r"^(?:[A-Za-z]{1,16}(?:_[A-Za-z]{1,16})?)(?:\.[A-Za-z0-9_-]+)?$"
)
_LINUX_GUI = {
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
}
_LOCALE_KEYS = {"LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES"}
_PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
#: Windows OS variables that the service copies from the inherited mapping.
#: The Windows runtime needs them: without SystemRoot, ``import asyncio``
#: fails with WinError 10106 when Winsock loads its providers. No value here
#: holds a secret. The names use the canonical Windows spelling.
#:
#: The Windows launcher holds this environment, and its digest is part of the
#: service generation. So each value here must be the same for one user on
#: one machine in every logon session and every shell:
#:
#: - SystemRoot, windir, SystemDrive, ComSpec: the Windows installation.
#: - ProgramData, ProgramFiles, ProgramFiles(x86), ProgramW6432,
#:   CommonProgramFiles, CommonProgramFiles(x86), CommonProgramW6432, PUBLIC:
#:   machine folders that Windows sets at installation.
#: - APPDATA, LOCALAPPDATA: the folders of the user profile.
#: - USERNAME, USERDOMAIN, COMPUTERNAME: the account and the machine name.
#: - PROCESSOR_ARCHITECTURE, OS: the machine type and the OS family.
#: - HOMEDRIVE, HOMEPATH: the home folder of the user account.
#:
#: These variables are NOT copied, because they change between sessions or
#: shells:
#:
#: - TEMP and TMP: Remote Desktop gives each session its own subfolder. The
#:   service uses ``LOCALAPPDATA\Temp`` instead.
#: - PATHEXT: PowerShell adds ``.CPL``. The service uses
#:   ``WINDOWS_DEFAULT_PATHEXT`` instead.
#: - NUMBER_OF_PROCESSORS: a virtual machine resize changes it, and Python
#:   does not need it.
WINDOWS_OS_KEYS = (
    "SystemRoot",
    "windir",
    "SystemDrive",
    "ComSpec",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "CommonProgramFiles",
    "CommonProgramFiles(x86)",
    "CommonProgramW6432",
    "APPDATA",
    "LOCALAPPDATA",
    "USERNAME",
    "USERDOMAIN",
    "COMPUTERNAME",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "PUBLIC",
    "HOMEDRIVE",
    "HOMEPATH",
)
#: The Windows default PATHEXT. The service always gets this value.
WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"


@dataclass(frozen=True)
class EnvironmentInputs:
    home: str
    user_id: str
    platform: str
    temp_dir: str | None = None
    app_dir: str | None = None


def _validate_plain(value: str, name: str) -> str:
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"invalid {name}")
    return value


def _windows_os_environment(source: dict[str, str], home: str) -> dict[str, str]:
    """Return the Windows OS variables for a service environment.

    Windows variable names are not case-sensitive. This function matches the
    inherited names without case and writes the canonical spelling. Only the
    stable values in ``WINDOWS_OS_KEYS`` come from ``source``.

    - SystemRoot and windir are always set. When the inherited mapping has
      no absolute value, ``jacked.winproc.system_root`` gives the fallback.
    - TEMP and TMP are always ``LOCALAPPDATA\\Temp``, else
      ``USERPROFILE\\AppData\\Local\\Temp``. They never come from the inherited
      TEMP or TMP. Python ``tempfile`` thus never uses the current directory.
    - PATHEXT is always ``WINDOWS_DEFAULT_PATHEXT``.

    >>> env = _windows_os_environment({"SYSTEMROOT": r"D:\\Win"}, r"C:\\Users\\a")
    >>> env["SystemRoot"], env["windir"], env["TEMP"]
    ('D:\\\\Win', 'D:\\\\Win', 'C:\\\\Users\\\\a\\\\AppData\\\\Local\\\\Temp')
    """
    folded = {key.casefold(): value for key, value in source.items()}
    env: dict[str, str] = {}
    for key in WINDOWS_OS_KEYS:
        value = folded.get(key.casefold())
        if value:
            env[key] = _validate_plain(value, key)
    # Resolve one root for both names. A relative or missing value falls back
    # through the same resolver that every Windows system program call uses.
    root = system_root(env)
    env["SystemRoot"] = root
    env["windir"] = root
    env["PATHEXT"] = WINDOWS_DEFAULT_PATHEXT
    local = env.get("LOCALAPPDATA")
    if not _windows_absolute(local):
        local = ntpath.join(home, "AppData", "Local")
    env["TEMP"] = _validate_plain(ntpath.join(local, "Temp"), "temp directory")
    env["TMP"] = env["TEMP"]
    return env


def build_service_environment(
    inputs: EnvironmentInputs,
    *,
    inherited: dict[str, str] | None = None,
    allow_proxy: bool = False,
) -> dict[str, str]:
    """Build a new environment from fixed values and a tiny reviewed allowlist.

    Callers must pass the inherited mapping explicitly.  Nothing in this
    function implicitly copies ``os.environ``.

    On win32 the environment also holds the stable Windows OS variables in
    ``WINDOWS_OS_KEYS``, the default PATHEXT, and one TEMP and TMP directory.
    PATH starts from the resolved SystemRoot. No win32 value depends on the
    logon session or the shell of the caller.
    """

    source = inherited or {}
    platform = inputs.platform
    env: dict[str, str] = {
        "PATH": _SAFE_PATHS.get(platform, _SAFE_PATHS["linux"]),
        "JACKED_SERVICE_USER": _validate_plain(inputs.user_id, "user identity"),
    }
    home = _validate_plain(inputs.home, "home")
    if platform == "win32":
        env["USERPROFILE"] = home
        env.update(_windows_os_environment(source, home))
        root = env["SystemRoot"]
        env["PATH"] = f"{ntpath.join(root, 'System32')};{root}"
        if inputs.temp_dir:
            env["TEMP"] = _validate_plain(inputs.temp_dir, "temp directory")
            env["TMP"] = env["TEMP"]
    else:
        env["HOME"] = home
        if inputs.temp_dir:
            env["TMPDIR"] = _validate_plain(inputs.temp_dir, "temp directory")
    if inputs.app_dir:
        env["JACKED_APP_DIR"] = _validate_plain(inputs.app_dir, "application directory")

    for key in _LOCALE_KEYS:
        value = source.get(key)
        if value and _LOCALE_RE.fullmatch(value):
            env[key] = value
    if platform == "linux":
        for key in _LINUX_GUI:
            value = source.get(key)
            if value:
                env[key] = _validate_plain(value, key)
    if allow_proxy:
        for key in _PROXY_KEYS:
            value = source.get(key)
            if not value:
                continue
            if key != "NO_PROXY" and urlsplit(value).username is not None:
                raise ValueError(f"proxy userinfo is not allowed in {key}")
            env[key] = _validate_plain(value, key)
    return dict(sorted(env.items()))


def posix_preinterpreter_command(
    *,
    runtime: str,
    argv: tuple[str, ...],
    environment: dict[str, str],
    launcher: str | None = None,
    runtime_target: str | None = None,
) -> tuple[str, ...]:
    """Return argv that clears the environment before Python is executed."""

    # ServiceSpec already validates the runtime using the host platform's path
    # rules. Keep this boundary independently defensive without interpreting
    # a Windows drive path as POSIX when cross-platform serializer tests render
    # every supervisor on every CI host.
    if not os.path.isabs(runtime) or not argv or argv[0] != "-I":
        raise ValueError("an absolute runtime and isolated Python argv are required")
    assignments = tuple(
        f"{key}={_validate_plain(value, key)}"
        for key, value in sorted(environment.items())
    )
    if launcher is None:
        launch = (runtime, *argv)
    else:
        if runtime_target is None or not os.path.isabs(runtime_target):
            raise ValueError("launcher commands require an absolute runtime target")
        launch = (launcher, runtime, runtime_target, *argv)
    return ("/usr/bin/env", "-i", *assignments, *launch)


def render_windows_launcher(
    *, runtime: str, argv: tuple[str, ...], environment: dict[str, str]
) -> str:
    """Render the fixed PowerShell boundary used by Task Scheduler."""

    def ps_quote(value: str) -> str:
        return "'" + _validate_plain(value, "launcher value").replace("'", "''") + "'"

    # ProcessStartInfo.Arguments follows CommandLineToArgvW rules, not POSIX
    # shell quoting. list2cmdline is the stdlib encoder for those exact rules.
    argument_line = subprocess.list2cmdline(list(argv))
    lines = [
        "param([string]$Generation)",
        "$ErrorActionPreference = 'Stop'",
        "if ($Generation -notmatch '^[0-9a-f]{64}$') { exit 64 }",
        "$psi = [System.Diagnostics.ProcessStartInfo]::new()",
        f"$psi.FileName = {ps_quote(runtime)}",
        f"$psi.Arguments = {ps_quote(argument_line)}",
        "$psi.UseShellExecute = $false",
        "$psi.CreateNoWindow = $true",
        "$psi.Environment.Clear()",
        "$psi.Environment['JACKED_SERVICE_GENERATION'] = $Generation",
    ]
    for key, value in sorted(environment.items()):
        lines.append(f"$psi.Environment[{ps_quote(key)}] = {ps_quote(value)}")
    lines.extend(
        (
            "$process = [System.Diagnostics.Process]::Start($psi)",
            "$process.WaitForExit()",
            "exit $process.ExitCode",
        )
    )
    return "\r\n".join(lines) + "\r\n"

"""Secret-negative environment and pre-interpreter launcher specifications."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit


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


def build_service_environment(
    inputs: EnvironmentInputs,
    *,
    inherited: dict[str, str] | None = None,
    allow_proxy: bool = False,
) -> dict[str, str]:
    """Build a new environment from fixed values and a tiny reviewed allowlist.

    Callers must pass the inherited mapping explicitly.  Nothing in this
    function implicitly copies ``os.environ``.
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
) -> tuple[str, ...]:
    """Return argv that clears the environment before Python is executed."""

    if not PurePosixPath(runtime).is_absolute() or not argv or argv[0] != "-I":
        raise ValueError("an absolute runtime and isolated Python argv are required")
    assignments = tuple(
        f"{key}={_validate_plain(value, key)}"
        for key, value in sorted(environment.items())
    )
    launch = (runtime, *argv) if launcher is None else (launcher, runtime, *argv)
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

"""Secret-negative environment and pre-interpreter launcher specifications."""

from __future__ import annotations

import base64
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from jacked.service.windows_env import (
    WindowsResolvers,
    real_windows_resolvers,
    windows_os_environment,
)


_SAFE_PATHS = {
    "darwin": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
    "linux": "/usr/local/bin:/usr/bin:/bin",
}
#: The OS programs that the service runs on macOS and Linux by a bare name.
#: The service PATH there is the fixed ``_SAFE_PATHS`` value, never inherited,
#: and each name resolves under an OS folder (/usr/bin, /bin, /usr/sbin,
#: /sbin). A test fails on a bare program name in the service code that is
#: not in this tuple. Every other program goes by an absolute path.
SERVICE_POSIX_PROGRAMS = ("launchctl", "systemctl", "ps", "pgrep", "osascript")
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


def default_windows_resolvers() -> WindowsResolvers:
    """Return the resolvers that ask Windows. Tests on POSIX replace this."""
    return real_windows_resolvers()


def inherited_allowlisted_environment(
    platform: str, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the allowlisted variables of ``source`` (default ``os.environ``).

    The allowlist is the locale and Linux GUI variables. On win32 it is
    empty: every Windows value comes from an OS API (see
    ``jacked.service.windows_env``), never from the caller.
    ``build_service_environment`` validates each value.
    """

    if platform == "win32":
        return {}
    source = os.environ if source is None else source
    names = sorted(_LOCALE_KEYS | _LINUX_GUI)
    return {name: source[name] for name in names if name in source}


def _windows_part(
    inputs: EnvironmentInputs, home: str, resolvers: WindowsResolvers | None
) -> dict[str, str]:
    """Return the Windows part of the service environment.

    USERPROFILE is ``home``, which the caller takes from ``Path.home()``. It
    is the one value that does not come from an OS API, on purpose: it must
    be the same home folder that holds ``~/.claude`` (``JACKED_APP_DIR``),
    and jacked finds that folder through ``Path.home()``.
    """
    resolvers = default_windows_resolvers() if resolvers is None else resolvers
    env = {"USERPROFILE": home, **windows_os_environment(home, resolvers, _validate_plain)}
    if inputs.temp_dir:
        env["TEMP"] = env["TMP"] = _validate_plain(inputs.temp_dir, "temp directory")
    return env


def _posix_part(
    inputs: EnvironmentInputs, home: str, source: Mapping[str, str]
) -> dict[str, str]:
    env = {"PATH": _SAFE_PATHS.get(inputs.platform, _SAFE_PATHS["linux"]), "HOME": home}
    if inputs.temp_dir:
        env["TMPDIR"] = _validate_plain(inputs.temp_dir, "temp directory")
    for key in _LOCALE_KEYS:
        value = source.get(key)
        if value and _LOCALE_RE.fullmatch(value):
            env[key] = value
    if inputs.platform == "linux":
        for key in _LINUX_GUI:
            value = source.get(key)
            if value:
                env[key] = _validate_plain(value, key)
    return env


def _proxy_part(source: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _PROXY_KEYS:
        value = source.get(key)
        if not value:
            continue
        if key != "NO_PROXY" and urlsplit(value).username is not None:
            raise ValueError(f"proxy userinfo is not allowed in {key}")
        env[key] = _validate_plain(value, key)
    return env


def build_service_environment(
    inputs: EnvironmentInputs,
    *,
    inherited: dict[str, str] | None = None,
    allow_proxy: bool = False,
    windows: WindowsResolvers | None = None,
) -> dict[str, str]:
    """Build a new environment from fixed values and a tiny reviewed allowlist.

    Callers must pass the inherited mapping explicitly.  Nothing in this
    function implicitly copies ``os.environ``.

    On win32 no value comes from ``inherited``: every Windows value comes
    from an OS API through ``windows`` (default: the real Windows APIs).
    So the launcher digest and the generation are the same in every logon
    session and every shell of one user.
    """

    source = inherited or {}
    home = _validate_plain(inputs.home, "home")
    env = {"JACKED_SERVICE_USER": _validate_plain(inputs.user_id, "user identity")}
    if inputs.platform == "win32":
        env.update(_windows_part(inputs, home, windows))
    else:
        env.update(_posix_part(inputs, home, source))
    if inputs.app_dir:
        env["JACKED_APP_DIR"] = _validate_plain(inputs.app_dir, "application directory")
    if allow_proxy:
        env.update(_proxy_part(source))
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


_LAUNCHER_KEY_RE = re.compile(r"^[A-Za-z0-9_()]+$")


def _ps_base64(value: str, name: str) -> str:
    """Return ``value`` as a PowerShell expression that decodes base64 UTF-8.

    >>> _ps_base64("NU\\u00d1EZ", "value")
    "(Decode 'TlXDkUVa')"
    """
    encoded = base64.b64encode(_validate_plain(value, name).encode("utf-8"))
    return f"(Decode '{encoded.decode('ascii')}')"


def render_windows_launcher(
    *, runtime: str, argv: tuple[str, ...], environment: dict[str, str]
) -> str:
    """Render the fixed PowerShell boundary used by Task Scheduler.

    The script is ASCII only. PowerShell 5.1 reads a .ps1 file with no BOM
    in the ANSI code page, and it ends a single-quoted string at U+0027 and
    at U+2018 to U+201B. So each value (the program, the arguments and each
    environment value) is base64 of its UTF-8 bytes, and the script decodes
    it. Each environment name must match ``^[A-Za-z0-9_()]+$``.
    """

    for key in environment:
        if not _LAUNCHER_KEY_RE.fullmatch(key):
            raise ValueError("invalid environment name for the Windows launcher")
    # ProcessStartInfo.Arguments follows CommandLineToArgvW rules, not POSIX
    # shell quoting. list2cmdline is the stdlib encoder for those exact rules.
    argument_line = subprocess.list2cmdline(list(argv))
    lines = [
        "param([string]$Generation)",
        "$ErrorActionPreference = 'Stop'",
        "if ($Generation -notmatch '^[0-9a-f]{64}$') { exit 64 }",
        "function Decode([string]$Value) { "
        "[System.Text.Encoding]::UTF8.GetString("
        "[System.Convert]::FromBase64String($Value)) }",
        "$psi = [System.Diagnostics.ProcessStartInfo]::new()",
        f"$psi.FileName = {_ps_base64(runtime, 'launcher runtime')}",
        f"$psi.Arguments = {_ps_base64(argument_line, 'launcher arguments')}",
        "$psi.UseShellExecute = $false",
        "$psi.CreateNoWindow = $true",
        "$psi.Environment.Clear()",
        "$psi.Environment['JACKED_SERVICE_GENERATION'] = $Generation",
    ]
    for key, value in sorted(environment.items()):
        lines.append(f"$psi.Environment['{key}'] = {_ps_base64(value, key)}")
    lines.extend(
        (
            "$process = [System.Diagnostics.Process]::Start($psi)",
            "$process.WaitForExit()",
            "exit $process.ExitCode",
        )
    )
    script = "\r\n".join(lines) + "\r\n"
    script.encode("ascii")  # A non-ASCII byte here is a bug: fail loudly.
    return script

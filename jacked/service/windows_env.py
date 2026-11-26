"""The Windows part of the service environment, from OS APIs only.

The Windows launcher holds the service environment, and the launcher digest
is part of the service generation. So no value here comes from the
environment of the caller: a Git Bash LANG, a Win32-OpenSSH USERDOMAIN, a
Remote Desktop TEMP subfolder or a PowerShell PATHEXT would give each shell
its own generation. Each value comes from a Windows API, from the home
folder, or is a constant.

Each variable, the program that reads it, and the source of its value:

- SystemRoot, windir: Winsock reads SystemRoot to load its providers
  (``import asyncio`` fails with WinError 10106 without it). Source:
  ``GetSystemWindowsDirectoryW``.
- SystemDrive: Windows components and installers. Source: the drive of
  SystemRoot.
- PATH: CreateProcess and the system programs. Source: SystemRoot.
- PATHEXT: program lookup by name. Source: the Windows default constant.
- APPDATA, LOCALAPPDATA: per-user application data (Python ``platformdirs``,
  Windows components). Source: ``SHGetKnownFolderPath``.
- ProgramData, ProgramFiles, ProgramFiles(x86), ProgramW6432,
  CommonProgramFiles, CommonProgramFiles(x86), CommonProgramW6432, PUBLIC:
  machine folders that installers and Windows components read. Source:
  ``SHGetKnownFolderPath``. A folder that the OS does not have (the x64
  folders on a 32-bit Windows) is left out.
- TEMP, TMP: Python ``tempfile`` and Windows ``GetTempPathW``. Source:
  ``LOCALAPPDATA\\Temp``.
- USERNAME: Python ``getpass.getuser``. Source: ``GetUserNameW``.
- COMPUTERNAME: Windows components. Source: ``GetComputerNameW``.
- HOMEDRIVE, HOMEPATH: programs that build the home folder from two parts.
  Source: the home folder that jacked uses.
- OS: scripts that test for Windows NT. Source: the constant ``Windows_NT``.
- PROCESSOR_ARCHITECTURE: native components that select a binary. Source:
  the platform of the interpreter that provisions the service, which is
  the runtime of the service.

USERDOMAIN, ComSpec, NUMBER_OF_PROCESSORS and the locale variables are not
set. Python does not need them, and their values change between sessions,
shells or machine changes.
"""

from __future__ import annotations

import ntpath
import sysconfig
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from jacked.mixed_generation import names_from

# An interpreter mid-upgrade can hold an older jacked.winproc without this
# name (see jacked.mixed_generation).
(is_absolute_windows_path,) = names_from("jacked.winproc", "is_absolute_windows_path")

#: The Windows default PATHEXT. The service always gets this value.
WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC"

#: Each known folder variable and its KNOWNFOLDERID, from
#: https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid
KNOWN_FOLDERS: Mapping[str, str] = {
    "APPDATA": "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",
    "LOCALAPPDATA": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
    "ProgramData": "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",
    "ProgramFiles": "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",
    "ProgramFiles(x86)": "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",
    "ProgramW6432": "{6D809377-6AF0-444B-8957-A3773F02200E}",
    "CommonProgramFiles": "{F7F1ED05-9F6D-47A2-AAAE-29D317C6F066}",
    "CommonProgramFiles(x86)": "{DE974D24-D9C6-4D3E-BF91-F4455120B917}",
    "CommonProgramW6432": "{6365D5A7-0F0D-45E5-87F6-0DA56B6A4F7D}",
    "PUBLIC": "{DFDF76A2-C82A-4D63-906A-5644AC457385}",
}

#: The Windows PROCESSOR_ARCHITECTURE value for each interpreter platform.
_ARCHITECTURES = {"win-amd64": "AMD64", "win-arm64": "ARM64", "win32": "x86"}


@dataclass(frozen=True)
class WindowsResolvers:
    """The sources of the Windows values. Tests on POSIX give fixed values.

    ``known_folder`` gets a KNOWNFOLDERID and returns the folder path. It
    raises ``OSError`` for a folder that the OS does not have.
    """

    windows_directory: Callable[[], str]
    known_folder: Callable[[str], str]
    user_name: Callable[[], str]
    computer_name: Callable[[], str]
    platform_tag: Callable[[], str]

    @classmethod
    def fixed(cls, values: Mapping[str, str]) -> "WindowsResolvers":
        """Return resolvers that give the values in ``values``.

        The keys are ``SystemRoot``, ``USERNAME``, ``COMPUTERNAME``, the
        ``KNOWN_FOLDERS`` names, and ``platform`` (for example
        ``win-amd64``). A known folder that is not in ``values`` raises
        ``OSError``.

        >>> fixed = WindowsResolvers.fixed({"SystemRoot": r"C:\\Windows"})
        >>> fixed.windows_directory()
        'C:\\\\Windows'
        """
        by_id = {
            folder_id: values[name]
            for name, folder_id in KNOWN_FOLDERS.items()
            if name in values
        }

        def known_folder(folder_id: str) -> str:
            try:
                return by_id[folder_id]
            except KeyError:
                raise OSError(f"no known folder {folder_id}") from None

        return cls(
            windows_directory=lambda: values.get("SystemRoot", r"C:\Windows"),
            known_folder=known_folder,
            user_name=lambda: values.get("USERNAME", "user"),
            computer_name=lambda: values.get("COMPUTERNAME", "COMPUTER"),
            platform_tag=lambda: values.get("platform", "win-amd64"),
        )


def _windows_directory() -> str:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetSystemWindowsDirectoryW(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise OSError(ctypes.get_last_error(), "GetSystemWindowsDirectoryW failed")
    return buffer.value


def _known_folder(folder_id: str) -> str:
    import ctypes
    import uuid
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    raw = uuid.UUID(folder_id).bytes_le
    guid = _GUID.from_buffer_copy(raw)
    path = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0, None, ctypes.byref(path)
    )
    try:
        if result != 0 or not path.value:
            raise OSError(result, f"SHGetKnownFolderPath failed for {folder_id}")
        return path.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


def _named(function_name: str, library: str) -> Callable[[], str]:
    def resolve() -> str:
        import ctypes
        from ctypes import wintypes

        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        function = getattr(ctypes.WinDLL(library, use_last_error=True), function_name)
        if not function(buffer, ctypes.byref(size)) or not buffer.value:
            raise OSError(ctypes.get_last_error(), f"{function_name} failed")
        return buffer.value

    return resolve


def real_windows_resolvers() -> WindowsResolvers:
    """Return the resolvers that ask Windows. Call them only on Windows."""
    return WindowsResolvers(
        windows_directory=_windows_directory,
        known_folder=_known_folder,
        user_name=_named("GetUserNameW", "advapi32"),
        computer_name=_named("GetComputerNameW", "kernel32"),
        platform_tag=sysconfig.get_platform,
    )


def windows_os_environment(
    home: str, resolvers: WindowsResolvers, validate: Callable[[str, str], str]
) -> dict[str, str]:
    """Return the Windows OS variables of the service environment.

    No value comes from ``os.environ``. ``validate`` checks each value (the
    plain-value rule of ``build_service_environment``).

    >>> env = windows_os_environment(
    ...     r"C:\\Users\\a",
    ...     WindowsResolvers.fixed({"SystemRoot": r"D:\\Win"}),
    ...     lambda value, name: value,
    ... )
    >>> env["SystemRoot"], env["SystemDrive"], env["TEMP"], env["HOMEPATH"]
    ('D:\\\\Win', 'D:', 'C:\\\\Users\\\\a\\\\AppData\\\\Local\\\\Temp', '\\\\Users\\\\a')
    """
    root = resolvers.windows_directory()
    if not is_absolute_windows_path(root):
        raise ValueError("the Windows directory is not an absolute path")
    env: dict[str, str] = {
        "SystemRoot": root,
        "windir": root,
        "SystemDrive": ntpath.splitdrive(root)[0],
        "PATH": f"{ntpath.join(root, 'System32')};{root}",
        "PATHEXT": WINDOWS_DEFAULT_PATHEXT,
        "OS": "Windows_NT",
        "USERNAME": resolvers.user_name(),
        "COMPUTERNAME": resolvers.computer_name(),
    }
    architecture = _ARCHITECTURES.get(resolvers.platform_tag())
    if architecture:
        env["PROCESSOR_ARCHITECTURE"] = architecture
    for name, folder_id in KNOWN_FOLDERS.items():
        try:
            value = resolvers.known_folder(folder_id)
        except OSError:
            continue
        if is_absolute_windows_path(value):
            env[name] = value
    local = env.get("LOCALAPPDATA") or ntpath.join(home, "AppData", "Local")
    env["TEMP"] = env["TMP"] = ntpath.join(local, "Temp")
    drive, path = ntpath.splitdrive(home)
    if drive and path:
        env["HOMEDRIVE"], env["HOMEPATH"] = drive, path
    return {key: validate(value, key) for key, value in env.items()}


__all__ = [
    "KNOWN_FOLDERS",
    "WINDOWS_DEFAULT_PATHEXT",
    "WindowsResolvers",
    "real_windows_resolvers",
    "windows_os_environment",
]

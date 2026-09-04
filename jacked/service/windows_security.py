"""Handle-bound Windows ownership and DACL primitives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_READ_ATTRIBUTES = 0x0080
_READ_CONTROL = 0x00020000
_FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PROTECTED = 0x1000
_ACL_SIZE_INFORMATION_CLASS = 2
_ACCESS_ALLOWED_ACE_TYPE = 0
_FILE_ALL_ACCESS = 0x001F01FF


@dataclass(frozen=True)
class WindowsPathSecurity:
    is_directory: bool
    is_reparse_point: bool
    link_count: int
    owner_matches: bool
    dacl_private: bool

    def private_for(self, *, directory: bool) -> bool:
        return (
            self.is_directory is directory
            and not self.is_reparse_point
            and (directory or self.link_count == 1)
            and self.owner_matches
            and self.dacl_private
        )


@dataclass(frozen=True)
class WindowsApi:
    """Configured Win32 libraries and dynamically declared structures."""

    kernel32: Any
    advapi32: Any
    by_handle_type: type[Any]
    acl_size_type: type[Any]
    access_ace_type: type[Any]


def _windows_types():
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    class AccessAllowedAce(ctypes.Structure):
        _fields_ = [
            ("Header", AceHeader),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    return ByHandleFileInformation, AclSizeInformation, AccessAllowedAce


def _configure_kernel32(kernel32, by_handle_type) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(by_handle_type),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL


def _configure_process_api(kernel32) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _configure_token_api(advapi32) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL


def _configure_descriptor_api(advapi32) -> None:
    import ctypes
    from ctypes import wintypes

    pointer = ctypes.POINTER(ctypes.c_void_p)
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL


def _configure_acl_api(advapi32, acl_size_type) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(acl_size_type),
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL


def _configure_named_security_api(advapi32) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD


def windows_libraries() -> WindowsApi:
    """Return Win32 libraries with pointer-safe 64-bit signatures."""

    import ctypes

    by_handle_type, acl_size_type, access_ace_type = _windows_types()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _configure_kernel32(kernel32, by_handle_type)
    _configure_process_api(kernel32)
    _configure_token_api(advapi32)
    _configure_descriptor_api(advapi32)
    _configure_acl_api(advapi32, acl_size_type)
    _configure_named_security_api(advapi32)
    return WindowsApi(
        kernel32,
        advapi32,
        by_handle_type,
        acl_size_type,
        access_ace_type,
    )


def windows_token_sid(token: Any, api: WindowsApi) -> str:
    """Read one token SID and free the API-owned string safely."""

    import ctypes
    from ctypes import wintypes

    needed = wintypes.DWORD()
    api.advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise OSError("GetTokenInformation size query failed")
    buffer = ctypes.create_string_buffer(needed.value)
    if not api.advapi32.GetTokenInformation(
        token, 1, buffer, needed.value, ctypes.byref(needed)
    ):
        raise OSError("GetTokenInformation failed")
    sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
    sid_string = wintypes.LPWSTR()
    if not api.advapi32.ConvertSidToStringSidW(
        sid_pointer, ctypes.byref(sid_string)
    ):
        raise OSError("ConvertSidToStringSidW failed")
    try:
        if not sid_string.value:
            raise OSError("token SID was empty")
        return sid_string.value
    finally:
        api.kernel32.LocalFree(ctypes.cast(sid_string, ctypes.c_void_p))


def current_user_sid() -> str:
    import ctypes
    from ctypes import wintypes

    api = windows_libraries()
    token = wintypes.HANDLE()
    if not api.advapi32.OpenProcessToken(
        api.kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise OSError("OpenProcessToken failed")
    try:
        return windows_token_sid(token, api)
    finally:
        api.kernel32.CloseHandle(token)


def _open_path_handle(path: Path, api: WindowsApi) -> Any:
    import ctypes

    handle = api.kernel32.CreateFileW(
        str(path),
        _READ_CONTROL | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _single_private_ace(dacl: Any, current_sid: Any, api: WindowsApi) -> bool:
    import ctypes

    info = api.acl_size_type()
    if not api.advapi32.GetAclInformation(
        dacl,
        ctypes.byref(info),
        ctypes.sizeof(info),
        _ACL_SIZE_INFORMATION_CLASS,
    ) or info.AceCount != 1:
        return False
    ace_pointer = ctypes.c_void_p()
    if not api.advapi32.GetAce(dacl, 0, ctypes.byref(ace_pointer)):
        return False
    ace = ctypes.cast(ace_pointer, ctypes.POINTER(api.access_ace_type)).contents
    sid_pointer = ace_pointer.value + api.access_ace_type.SidStart.offset
    return bool(
        ace.Header.AceType == _ACCESS_ALLOWED_ACE_TYPE
        and ace.Mask == _FILE_ALL_ACCESS
        and api.advapi32.EqualSid(sid_pointer, current_sid)
    )


def _descriptor_security(handle: Any, api: WindowsApi) -> tuple[bool, bool]:
    import ctypes
    from ctypes import wintypes

    descriptor = ctypes.c_void_p()
    current_sid = ctypes.c_void_p()
    try:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        result = api.advapi32.GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise OSError(result, "GetSecurityInfo failed")
        if not api.advapi32.ConvertStringSidToSidW(
            current_user_sid(), ctypes.byref(current_sid)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        protected = bool(
            api.advapi32.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            )
            and control.value & _SE_DACL_PROTECTED
        )
        private_ace = bool(
            dacl
            and protected
            and _single_private_ace(dacl, current_sid, api)
        )
        return (
            bool(owner and api.advapi32.EqualSid(owner, current_sid)),
            private_ace,
        )
    finally:
        if current_sid:
            api.kernel32.LocalFree(current_sid)
        if descriptor:
            api.kernel32.LocalFree(descriptor)


def inspect_windows_path(path: Path) -> WindowsPathSecurity:
    """Inspect type, reparse state, owner, and exact DACL through one handle."""

    import ctypes

    api = windows_libraries()
    handle = _open_path_handle(path, api)
    try:
        info = api.by_handle_type()
        if not api.kernel32.GetFileInformationByHandle(
            handle, ctypes.byref(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        owner_matches, dacl_private = _descriptor_security(handle, api)
        attributes = info.dwFileAttributes
        return WindowsPathSecurity(
            is_directory=bool(attributes & _FILE_ATTRIBUTE_DIRECTORY),
            is_reparse_point=bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT),
            link_count=info.nNumberOfLinks,
            owner_matches=owner_matches,
            dacl_private=dacl_private,
        )
    finally:
        api.kernel32.CloseHandle(handle)


def secure_windows_path(path: Path) -> None:
    """Apply and verify a protected current-user-only full-control DACL."""

    import ctypes
    from ctypes import wintypes

    api = windows_libraries()
    descriptor = ctypes.c_void_p()
    sid = current_user_sid()
    if not api.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;;FA;;;{sid})", 1, ctypes.byref(descriptor), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not api.advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present:
            raise OSError("could not read the service-state DACL")
        result = api.advapi32.SetNamedSecurityInfoW(
            str(path),
            1,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise OSError(result, "could not apply the service-state DACL")
    finally:
        api.kernel32.LocalFree(descriptor)
    expected_directory = path.is_dir()
    if not inspect_windows_path(path).private_for(directory=expected_directory):
        raise OSError("service-state DACL verification failed")

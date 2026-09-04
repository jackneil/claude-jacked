"""Value objects shared by the Windows security primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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

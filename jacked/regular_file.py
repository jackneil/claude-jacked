"""Open and read a path only when it is a regular file.

A FIFO with no writer blocks a plain ``open()`` or ``read()`` forever. A
settings file or a memory index that is a FIFO (or a symlink to one) would
then stop a session start or the dashboard. These functions check the type
before the open and again on the open handle, and they open with
``O_NONBLOCK``, so a FIFO, a device or a folder is never read.

``jacked.memory.claude_paths.open_regular`` (returns None) and
``jacked.memory.settings_io.read_settings`` (raises) are built on these.
"""
from __future__ import annotations

import os
import stat


class NotRegularFileError(OSError):
    """The path exists, but it is not a regular file."""


class FileTooLargeError(OSError):
    """The file is larger than the limit of the read. ``size`` is its size."""

    def __init__(self, size: int, limit: int):
        super().__init__(f"{size} bytes is larger than the limit of {limit} bytes")
        self.size = size
        self.limit = limit


def open_regular_fd(path) -> int:
    """Open ``path`` read-only and return the descriptor, when it is a regular file.

    A missing path raises ``FileNotFoundError`` (or ``NotADirectoryError``). A
    path that is not a regular file raises ``NotRegularFileError``. Other OS
    errors go up as they are. The caller closes the descriptor.

    >>> try:
    ...     open_regular_fd("/nonexistent-jacked-path/settings.json")
    ... except FileNotFoundError:
    ...     print("missing")
    missing
    """
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise NotRegularFileError(f"{os.fspath(path)} is not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    raise NotRegularFileError(f"{os.fspath(path)} is not a regular file")


def read_regular(path, limit: int) -> bytes:
    """Return the bytes of the regular file at ``path``, at most ``limit`` of them.

    A file larger than ``limit`` raises ``FileTooLargeError`` before any read
    (the size comes from ``fstat``), and also when it grows past ``limit``
    while it is read. The errors of ``open_regular_fd`` go up as they are.
    """
    fd = open_regular_fd(path)
    try:
        size = os.fstat(fd).st_size
        if size > limit:
            raise FileTooLargeError(size, limit)
        parts = []
        total = 0
        while chunk := os.read(fd, limit + 1 - total):
            parts.append(chunk)
            total += len(chunk)
            if total > limit:
                raise FileTooLargeError(total, limit)
        return b"".join(parts)
    finally:
        os.close(fd)

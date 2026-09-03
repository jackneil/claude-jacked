"""Private serialization helpers for native supervisor artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def systemd_quote(value: str) -> str:
    if "\x00" in value or "\n" in value:
        raise ValueError("systemd argument contains an invalid character")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


def windows_quote(value: str) -> str:
    if '"' in value or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("Windows launcher path contains an unsupported character")
    return f'"{value}"'

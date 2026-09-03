"""Machine-local recovery key storage."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path


class FileInstallKeyProvider:
    """Create/read one private 256-bit install key without following links."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get_key(self) -> bytes | None:
        try:
            if self.path.exists():
                return self._read_existing()
            return self._create()
        except OSError:
            return None

    def _read_existing(self) -> bytes:
        path_stat = self.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
            raise OSError("recovery key is not a private regular file")
        if os.name != "nt" and path_stat.st_mode & 0o077:
            raise OSError("recovery key permissions are not private")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            value = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if len(value) != 32:
            raise OSError("recovery key has an invalid length")
        return value

    def _create(self) -> bytes:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        value = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, 0o600)
        try:
            written = os.write(descriptor, value)
            if written != len(value):
                raise OSError("recovery key write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(self.path, 0o600)
        return value


def machine_install_id(key: bytes) -> str:
    """Derive a stable nonsecret install identifier from the private key."""
    return hmac.new(
        key, b"jacked-machine-install-id-v1", hashlib.sha256
    ).hexdigest()

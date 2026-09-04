"""Safe cross-platform JSON credential file adapter."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tempfile
from pathlib import Path

from .canonical import CredentialFormatError, CredentialPayload
from .models import InteractionMode, StoreReadResult, StoreStatus, StoreWriteResult

logger = logging.getLogger(__name__)

_REPARSE_POINT = 0x400
# Stand-in stamp for "this adapter read the file and it was not there".
_SEEN_MISSING = ("missing",)


def _is_reparse_point(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return bool(attributes & _REPARSE_POINT)


def _validate_existing(path: Path) -> None:
    if path.is_symlink():
        raise OSError("refusing credential symlink")
    if not path.exists():
        return
    path_stat = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(path_stat.st_mode) or _is_reparse_point(path_stat):
        raise OSError("credential path is not a regular file")
    if path_stat.st_nlink != 1:
        raise OSError("refusing hard-linked credential file")


def _validate_parent(path: Path, trusted_root: Path) -> None:
    try:
        path.absolute().relative_to(trusted_root.absolute())
    except ValueError as exc:
        raise OSError("credential path escapes its trusted root") from exc
    current = path.parent
    while True:
        if current.exists() and current.is_symlink():
            raise OSError("credential parent contains a symlink")
        if current == trusted_root:
            break
        if current == current.parent:
            raise OSError("credential trusted root was not reached")
        current = current.parent


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_replace(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    replace_existing = 0x1
    write_through = 0x8
    if not move_file(str(source), str(target), replace_existing | write_through):
        raise ctypes.WinError(ctypes.get_last_error())


def _stamp(status: os.stat_result, raw: bytes) -> tuple:
    """Identify exact file content: metadata plus a digest of the bytes.

    The digest closes the coarse-mtime blind spot. Tokens are fixed length, so
    an in-place refresh inside one mtime tick keeps size and inode identical.
    """
    return (status.st_size, status.st_mtime_ns, status.st_ino, hashlib.sha256(raw).digest())


def _refuse_concurrent_write() -> StoreWriteResult:
    return StoreWriteResult(
        StoreStatus.CONCURRENT_WRITE, "credential file changed since it was read"
    )


class FileCredentialStore:
    """Strict JSON file store with private staging and durable replacement."""

    def __init__(self, path: Path, *, trusted_root: Path | None = None) -> None:
        self.path = path
        self.trusted_root = trusted_root or path.parent
        self._seen: tuple | None = None

    @property
    def locator(self) -> str:
        return str(self.path.resolve(strict=False))

    def read(self) -> StoreReadResult:
        try:
            _validate_parent(self.path, self.trusted_root)
            _validate_existing(self.path)
            if not self.path.exists():
                self._seen = _SEEN_MISSING
                return StoreReadResult(StoreStatus.MISSING)
            # Stat before the bytes. Stating afterwards would pair fresh
            # metadata with content read before a concurrent rewrite, so a
            # later compare-and-swap would see no change and overwrite it.
            status = self.path.stat()
            raw = self.path.read_bytes()
            payload = CredentialPayload.from_json(raw)
            self._seen = _stamp(status, raw)
            return StoreReadResult(StoreStatus.OK, payload)
        except CredentialFormatError as exc:
            self._seen = None
            return StoreReadResult(StoreStatus.UNUSABLE, reason=str(exc))
        except OSError as exc:
            self._seen = None
            return StoreReadResult(StoreStatus.UNUSABLE, reason=str(exc))

    def _changed_since_read(self) -> bool:
        """True when this adapter read the file and it changed afterwards.

        A never-read adapter never refuses. A file that appeared after a
        MISSING read counts as a change: Claude Code may have just logged in.
        """
        if self._seen is None:
            return False
        exists = self.path.exists()
        if self._seen == _SEEN_MISSING:
            return exists
        if not exists:
            return True
        status = self.path.stat()
        if (status.st_size, status.st_mtime_ns, status.st_ino) != self._seen[:3]:
            return True
        return hashlib.sha256(self.path.read_bytes()).digest() != self._seen[3]

    def _remember(self, raw: bytes) -> None:
        """Re-arm the compare-and-swap stamp after a committed replace.

        The bytes are already on disk, so a failure to stamp them must never
        report the write as failed; it only disarms the next refusal.
        """
        try:
            self._seen = _stamp(self.path.stat(), raw)
        except OSError as exc:
            self._seen = None
            logger.warning("Could not re-arm the credential file stamp: %s", exc)

    def write(
        self, payload: CredentialPayload, interaction: InteractionMode
    ) -> StoreWriteResult:
        del interaction
        raw = payload.to_bytes()
        stage_path: Path | None = None
        try:
            _validate_parent(self.path, self.trusted_root)
            _validate_existing(self.path)
            if self._changed_since_read():
                return _refuse_concurrent_write()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, stage_name = tempfile.mkstemp(
                prefix=".credentials-stage-", dir=self.path.parent
            )
            stage_path = Path(stage_name)
            os.chmod(stage_path, 0o600)
            with os.fdopen(descriptor, "wb") as stage:
                stage.write(raw)
                stage.flush()
                os.fsync(stage.fileno())
            _validate_existing(self.path)
            # Staging and fsync take real time on a slow disk, so re-check as
            # late as possible before the replace commits.
            if self._changed_since_read():
                return _refuse_concurrent_write()
            _durable_replace(stage_path, self.path)
            os.chmod(self.path, 0o600)
            _sync_directory(self.path.parent)
        except OSError as exc:
            return StoreWriteResult(StoreStatus.UNUSABLE, str(exc))
        finally:
            if stage_path is not None:
                try:
                    stage_path.unlink(missing_ok=True)
                except OSError:
                    pass
        self._remember(raw)
        return StoreWriteResult(StoreStatus.OK)

    def cleanup_stages(self) -> None:
        """Remove adapter-owned abandoned stages while the caller holds its lease."""
        for stage in self.path.parent.glob(".credentials-stage-*"):
            try:
                _validate_existing(stage)
                stage.unlink()
            except OSError:
                continue

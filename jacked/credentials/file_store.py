"""Safe cross-platform JSON credential file adapter."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .canonical import CredentialFormatError, CredentialPayload
from .models import InteractionMode, StoreReadResult, StoreStatus, StoreWriteResult

_REPARSE_POINT = 0x400


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


class FileCredentialStore:
    """Strict JSON file store with private staging and durable replacement."""

    def __init__(self, path: Path, *, trusted_root: Path | None = None) -> None:
        self.path = path
        self.trusted_root = trusted_root or path.parent

    @property
    def locator(self) -> str:
        return str(self.path.resolve(strict=False))

    def read(self) -> StoreReadResult:
        try:
            _validate_parent(self.path, self.trusted_root)
            _validate_existing(self.path)
            if not self.path.exists():
                return StoreReadResult(StoreStatus.MISSING)
            return StoreReadResult(
                StoreStatus.OK, CredentialPayload.from_json(self.path.read_bytes())
            )
        except CredentialFormatError as exc:
            return StoreReadResult(StoreStatus.UNUSABLE, reason=str(exc))
        except OSError as exc:
            return StoreReadResult(StoreStatus.UNUSABLE, reason=str(exc))

    def write(
        self, payload: CredentialPayload, interaction: InteractionMode
    ) -> StoreWriteResult:
        del interaction
        stage_path: Path | None = None
        try:
            _validate_parent(self.path, self.trusted_root)
            _validate_existing(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, stage_name = tempfile.mkstemp(
                prefix=".credentials-stage-", dir=self.path.parent
            )
            stage_path = Path(stage_name)
            os.chmod(stage_path, 0o600)
            with os.fdopen(descriptor, "wb") as stage:
                stage.write(payload.to_bytes())
                stage.flush()
                os.fsync(stage.fileno())
            _validate_existing(self.path)
            _durable_replace(stage_path, self.path)
            os.chmod(self.path, 0o600)
            _sync_directory(self.path.parent)
            return StoreWriteResult(StoreStatus.OK)
        except OSError as exc:
            return StoreWriteResult(StoreStatus.UNUSABLE, str(exc))
        finally:
            if stage_path is not None:
                try:
                    stage_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def cleanup_stages(self) -> None:
        """Remove adapter-owned abandoned stages while the caller holds its lease."""
        for stage in self.path.parent.glob(".credentials-stage-*"):
            try:
                _validate_existing(stage)
                stage.unlink()
            except OSError:
                continue

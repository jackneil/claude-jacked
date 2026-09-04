from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jacked.credentials.canonical import CredentialFormatError, CredentialPayload
from jacked.credentials.file_store import FileCredentialStore
from jacked.credentials.macos_store import (
    MacOSCredentialStore,
    NativeReadResult,
    NativeSecurityBackend,
    PyObjCSecurityBackend,
)
from jacked.credentials.models import InteractionMode, StoreStatus


def _payload(account_id: int = 1) -> CredentialPayload:
    return CredentialPayload.from_mapping(
        {"_jackedAccountId": account_id, "claudeAiOauth": {"accessToken": "secret"}}
    )


def test_file_store_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    path.write_text('{"x": 1, "x": 2}', encoding="utf-8")

    result = FileCredentialStore(path).read()

    assert result.status is StoreStatus.UNUSABLE
    assert "duplicate" in result.reason


def test_file_store_durable_replace_and_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    store = FileCredentialStore(path)

    result = store.write(_payload(), InteractionMode.BACKGROUND)

    assert result.status is StoreStatus.OK
    assert store.read().payload == _payload()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".credentials-stage-*")) == []


def test_file_store_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / ".credentials.json"
    path.symlink_to(target)

    result = FileCredentialStore(path).write(_payload(), InteractionMode.BACKGROUND)

    assert result.status is StoreStatus.UNUSABLE
    assert json.loads(target.read_text(encoding="utf-8")) == {}


def test_strict_payload_constructor_rejects_duplicates() -> None:
    with pytest.raises(CredentialFormatError):
        CredentialPayload.from_json('{"x":1,"x":2}')


class FakeSecurityBackend(NativeSecurityBackend):
    def __init__(self, read_result: NativeReadResult) -> None:
        self.read_result = read_result
        self.updated: bytes | None = None
        self.added: bytes | None = None

    def read(
        self, *, service: str, account: str, is_interactive: bool
    ) -> NativeReadResult:
        return self.read_result

    def update(self, *, service: str, account: str, data: bytes, is_interactive: bool):
        self.updated = data
        self.read_result = NativeReadResult(StoreStatus.OK, data)
        return StoreStatus.OK, ""

    def add(self, *, service: str, account: str, data: bytes, is_interactive: bool):
        self.added = data
        self.read_result = NativeReadResult(StoreStatus.OK, data)
        return StoreStatus.OK, ""


def test_macos_existing_item_uses_update_not_delete_or_add() -> None:
    backend = FakeSecurityBackend(
        NativeReadResult(StoreStatus.OK, _payload().to_bytes())
    )
    store = MacOSCredentialStore("alice", backend=backend)

    result = store.write(_payload(2), InteractionMode.FOREGROUND)

    assert result.status is StoreStatus.OK
    assert backend.updated == _payload(2).to_bytes()
    assert backend.added is None


def test_macos_missing_item_requires_foreground_before_add() -> None:
    backend = FakeSecurityBackend(NativeReadResult(StoreStatus.MISSING))
    store = MacOSCredentialStore("alice", backend=backend)

    result = store.write(_payload(), InteractionMode.BACKGROUND)

    assert result.status is StoreStatus.INTERACTIVE_REQUIRED
    assert backend.added is None


class BlockingSecurityBackend(FakeSecurityBackend):
    def __init__(self) -> None:
        super().__init__(NativeReadResult(StoreStatus.MISSING))
        self.release = threading.Event()
        self.calls = 0

    def read(
        self, *, service: str, account: str, is_interactive: bool
    ) -> NativeReadResult:
        self.calls += 1
        self.release.wait()
        return self.read_result


def test_macos_noninteractive_read_is_bounded_and_circuit_breaks() -> None:
    backend = BlockingSecurityBackend()
    store = MacOSCredentialStore(
        "bounded-timeout-test",
        backend=backend,
        noninteractive_timeout=0.01,
    )

    started = time.monotonic()
    first = store.read()
    second = store.read()
    elapsed = time.monotonic() - started
    backend.release.set()

    assert elapsed < 0.25
    assert first.status is StoreStatus.ERROR
    assert "timed out" in first.reason
    assert second.status is StoreStatus.ERROR
    assert "prior timeout" in second.reason
    assert backend.calls == 1


def test_macos_background_write_bounds_its_preflight_read() -> None:
    backend = BlockingSecurityBackend()
    store = MacOSCredentialStore(
        "bounded-write-test",
        backend=backend,
        noninteractive_timeout=0.01,
    )

    started = time.monotonic()
    result = store.write(_payload(), InteractionMode.BACKGROUND)
    elapsed = time.monotonic() - started
    backend.release.set()

    assert elapsed < 0.25
    assert result.status is StoreStatus.ERROR
    assert "timed out" in result.reason


def test_pyobjc_noninteractive_query_uses_ui_fail_without_auth_context() -> None:
    class Context:
        interaction_not_allowed = False

        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setInteractionNotAllowed_(self, value):
            self.interaction_not_allowed = value

    backend = PyObjCSecurityBackend.__new__(PyObjCSecurityBackend)
    backend._security = SimpleNamespace(
        kSecClass="class-key",
        kSecClassGenericPassword="generic",
        kSecAttrService="service-key",
        kSecAttrAccount="account-key",
        kSecUseAuthenticationContext="context-key",
        kSecUseAuthenticationUI="ui-key",
        kSecUseAuthenticationUIFail="ui-fail",
    )
    backend._local_auth = SimpleNamespace(LAContext=Context)

    query = backend._query("Claude Code-credentials", "alice", False)

    assert query["ui-key"] == "ui-fail"
    assert "context-key" not in query


def test_file_store_refuses_to_overwrite_a_file_changed_since_read(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    path.write_bytes(_payload(1).to_bytes())
    store = FileCredentialStore(path, trusted_root=tmp_path)
    assert store.read().status is StoreStatus.OK

    path.write_bytes(_payload(2).to_bytes())  # Claude Code refreshed a token
    os.utime(path, ns=(10**12, 10**12))

    result = store.write(_payload(3), InteractionMode.FOREGROUND)

    assert result.status is StoreStatus.CONCURRENT_WRITE
    assert CredentialPayload.from_json(path.read_bytes()).identity.account_id == 2


def test_file_store_refuses_when_a_file_appears_after_a_missing_read(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    store = FileCredentialStore(path, trusted_root=tmp_path)
    assert store.read().status is StoreStatus.MISSING

    path.write_bytes(_payload(9).to_bytes())  # Claude Code logged in meanwhile

    result = store.write(_payload(3), InteractionMode.FOREGROUND)

    assert result.status is StoreStatus.CONCURRENT_WRITE
    assert CredentialPayload.from_json(path.read_bytes()).identity.account_id == 9


def test_file_store_checks_again_right_before_replace(tmp_path: Path, monkeypatch) -> None:
    from jacked.credentials import file_store as file_store_module

    path = tmp_path / ".credentials.json"
    path.write_bytes(_payload(1).to_bytes())
    store = FileCredentialStore(path, trusted_root=tmp_path)
    assert store.read().status is StoreStatus.OK

    def rewrite_during_staging(fd):
        path.write_bytes(_payload(2).to_bytes())
        os.utime(path, ns=(10**12, 10**12))

    monkeypatch.setattr(file_store_module.os, "fsync", rewrite_during_staging)

    result = store.write(_payload(3), InteractionMode.FOREGROUND)

    assert result.status is StoreStatus.CONCURRENT_WRITE
    assert CredentialPayload.from_json(path.read_bytes()).identity.account_id == 2


def test_file_store_detects_same_size_rewrite_inside_one_mtime_tick(tmp_path: Path) -> None:
    path = tmp_path / ".credentials.json"
    path.write_bytes(_payload(1).to_bytes())
    store = FileCredentialStore(path, trusted_root=tmp_path)
    assert store.read().status is StoreStatus.OK
    original = path.stat()

    path.write_bytes(_payload(2).to_bytes())  # same length as _payload(1)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))

    result = store.write(_payload(3), InteractionMode.FOREGROUND)

    assert result.status is StoreStatus.CONCURRENT_WRITE

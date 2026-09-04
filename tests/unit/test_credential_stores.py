from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from jacked.credentials.canonical import CredentialFormatError, CredentialPayload
from jacked.credentials.file_store import FileCredentialStore
from jacked.credentials.macos_store import (
    MacOSCredentialStore,
    NativeReadResult,
    NativeSecurityBackend,
    SecurityCliBackend,
    keychain_is_locked,
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
    store = MacOSCredentialStore("alice", backend=backend, lock_probe=lambda: False)

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
        lock_probe=lambda: False,
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
        lock_probe=lambda: False,
    )

    started = time.monotonic()
    result = store.write(_payload(), InteractionMode.BACKGROUND)
    elapsed = time.monotonic() - started
    backend.release.set()

    assert elapsed < 0.25
    assert result.status is StoreStatus.ERROR
    assert "timed out" in result.reason


def _completed(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_security_cli_read_uses_argv_and_returns_payload_bytes() -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(0, b'{"x":1}\n')

    backend = SecurityCliBackend(run=run)
    result = backend.read(service="Claude Code-credentials", account="alice", is_interactive=False)

    assert result.status is StoreStatus.OK
    assert result.data == b'{"x":1}'
    assert calls[0][0] == [
        "/usr/bin/security", "find-generic-password",
        "-a", "alice", "-s", "Claude Code-credentials", "-w",
    ]
    assert calls[0][1]["timeout"] == 2.0
    assert calls[0][1]["text"] is False


@pytest.mark.parametrize(
    "returncode,stderr,status",
    [
        (44, b"security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.", StoreStatus.MISSING),
        (36, b"security: SecKeychainItemCopyContent: User interaction is not allowed.", StoreStatus.INTERACTIVE_REQUIRED),
        (128, b"security: SecKeychainItemCopyContent: User canceled the operation.", StoreStatus.DENIED),
        (1, b"security: something else entirely", StoreStatus.ERROR),
    ],
)
def test_security_cli_read_maps_failures_by_exit_code(returncode, stderr, status) -> None:
    backend = SecurityCliBackend(run=lambda *a, **k: _completed(returncode, b"", stderr))

    result = backend.read(service="s", account="a", is_interactive=False)

    assert result.status is status
    assert result.data is None
    assert "entirely" not in result.reason  # raw stderr never leaves the backend


def test_security_cli_noninteractive_timeout_is_reported_not_raised() -> None:
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    backend = SecurityCliBackend(run=run)
    result = backend.read(service="s", account="a", is_interactive=False)

    assert result.status is StoreStatus.ERROR
    assert "timed out" in result.reason


def test_security_cli_write_sends_command_on_stdin_never_in_argv() -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(0)

    backend = SecurityCliBackend(run=run)
    secret = b'{"a":"sk-ant-oat01-secret"}'
    status, _ = backend.update(
        service="Claude Code-credentials", account="alice", data=secret, is_interactive=True
    )

    assert status is StoreStatus.OK
    args, kwargs = calls[0]
    assert args == ["/usr/bin/security", "-i"]
    assert kwargs["timeout"] == 60.0
    assert kwargs["input"] == (
        b'add-generic-password -U -a "alice" -s "Claude Code-credentials" -X '
        + secret.hex().encode("ascii") + b"\n"
    )
    assert all(secret.hex() not in item and "sk-ant" not in item for item in args)

    status, _ = backend.add(service="s", account="a", data=b"{}", is_interactive=True)
    assert status is StoreStatus.OK
    assert calls[1][0] == ["/usr/bin/security", "-i"]


def test_security_cli_rejects_unquotable_locator_parts() -> None:
    backend = SecurityCliBackend(run=lambda *a, **k: _completed(0))

    status, reason = backend.update(
        service='bad"name', account="alice", data=b"{}", is_interactive=True
    )

    assert status is StoreStatus.ERROR
    assert "locator" in reason


class _ObjcLikeError(Exception):
    """Stands in for objc.error, which is an Exception but not an OSError."""


def test_keychain_probe_swallows_framework_errors() -> None:
    def explode(_none):
        raise _ObjcLikeError("bridge failure")

    with mock.patch("jacked.credentials.macos_store._PROBE_ERRORS", (_ObjcLikeError,)):
        fake = SimpleNamespace(SecKeychainCopyDefault=explode)
        assert keychain_is_locked(security_module=fake) is False


def test_security_cli_medium_payload_uses_escaped_json_on_stdin() -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(0)

    backend = SecurityCliBackend(run=run)
    medium = b'{"a":"' + b"x" * 3000 + b'","q":"say \\"hi\\""}'  # JSON-escaped quotes; hex would exceed 4095
    status, _ = backend.update(service="s", account="a", data=medium, is_interactive=True)

    assert status is StoreStatus.OK
    args, kwargs = calls[0]
    assert args == ["/usr/bin/security", "-i"]
    line = kwargs["input"]
    assert line.startswith(b'add-generic-password -U -a "a" -s "s" -w "')
    assert len(line) <= 4095
    prefix = b'add-generic-password -U -a "a" -s "s" -w "'
    quoted = line[len(prefix):-2]  # drop the closing quote and newline
    unescaped = re.sub(r"\\(.)", r"\1", quoted.decode("ascii"))  # the -i lexer's escape rule
    assert json.loads(unescaped) == json.loads(medium)
    assert medium.hex().encode() not in line


def test_security_cli_oversized_payload_fails_closed_unless_argv_opt_in(monkeypatch, caplog) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(0)

    backend = SecurityCliBackend(run=run)
    huge = b'{"a":"' + b"x" * 5000 + b'"}'

    monkeypatch.delenv("JACKED_KEYCHAIN_ARGV_FALLBACK", raising=False)
    status, reason = backend.update(service="s", account="a", data=huge, is_interactive=True)
    assert status is StoreStatus.UNUSABLE
    assert "stdin line limit" in reason and "5008 bytes" in reason
    assert calls == []

    monkeypatch.setenv("JACKED_KEYCHAIN_ARGV_FALLBACK", "1")
    with caplog.at_level("WARNING"):
        status, _ = backend.update(service="s", account="a", data=huge, is_interactive=True)
        backend.update(service="s", account="a", data=huge, is_interactive=True)
    assert status is StoreStatus.OK
    assert calls[0][0][:3] == ["/usr/bin/security", "add-generic-password", "-U"]
    assert "input" not in calls[0][1]
    assert caplog.text.count("process argument") == 1  # warned once per process


def test_security_cli_timeout_on_argv_path_never_exposes_the_command(monkeypatch, caplog) -> None:
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setenv("JACKED_KEYCHAIN_ARGV_FALLBACK", "1")
    huge = b'{"a":"' + b"x" * 5000 + b'"}'
    with caplog.at_level("DEBUG"):
        status, reason = SecurityCliBackend(run=run).update(
            service="s", account="a", data=huge, is_interactive=True
        )
    assert status is StoreStatus.ERROR
    assert huge.hex() not in caplog.text and huge.hex() not in reason


def test_security_cli_small_payload_uses_hex_on_stdin() -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _completed(0)

    backend = SecurityCliBackend(run=run)
    backend.update(service="s", account="a", data=b'{"a":1}', is_interactive=True)
    assert calls[0][0] == ["/usr/bin/security", "-i"]
    assert calls[0][1]["input"].endswith(b"\n")
    assert b" -X " in calls[0][1]["input"]
    assert len(calls[0][1]["input"]) <= 4095


def test_security_cli_write_failure_never_logs_stderr(caplog) -> None:
    secret_hex = b'{"a":"sk-ant-oat01-secret"}'.hex().encode()
    backend = SecurityCliBackend(
        run=lambda *a, **k: _completed(1, b"", b'security: unknown command "' + secret_hex + b'"')
    )
    with caplog.at_level("DEBUG"):
        status, reason = backend.update(service="s", account="a", data=b"{}", is_interactive=True)

    assert status is StoreStatus.ERROR
    assert secret_hex.decode() not in caplog.text
    assert secret_hex.decode() not in reason


def test_security_cli_read_decodes_hex_output_for_non_ascii_payloads() -> None:
    backend = SecurityCliBackend(run=lambda *a, **k: _completed(0, b"7b22c3a9223a317d\n"))
    assert backend.read(service="s", account="a", is_interactive=False).data == '{"é":1}'.encode()

    backend = SecurityCliBackend(run=lambda *a, **k: _completed(0, b"7B22C3A9223A317D\n"))
    assert backend.read(service="s", account="a", is_interactive=False).data == '{"é":1}'.encode()

    backend = SecurityCliBackend(run=lambda *a, **k: _completed(0, b'{"x":1}\n'))
    assert backend.read(service="s", account="a", is_interactive=False).data == b'{"x":1}'


def test_keychain_is_locked_reads_status_bit_without_touching_items() -> None:
    fake = SimpleNamespace(
        SecKeychainCopyDefault=lambda _none: (0, "kc"),
        SecKeychainGetStatus=lambda kc, _none: (0, 0b110),  # unlock bit clear
        kSecUnlockStateStatus=1,
    )
    assert keychain_is_locked(security_module=fake) is True
    fake.SecKeychainGetStatus = lambda kc, _none: (0, 0b111)
    assert keychain_is_locked(security_module=fake) is False


def test_locked_keychain_short_circuits_background_read_without_spawning() -> None:
    backend = FakeSecurityBackend(NativeReadResult(StoreStatus.OK, _payload().to_bytes()))
    store = MacOSCredentialStore("alice", backend=backend, lock_probe=lambda: True)

    result = store.read()

    assert result.status is StoreStatus.INTERACTIVE_REQUIRED


def test_timed_out_latch_is_shared_across_store_instances_and_expires() -> None:
    """Stores are rebuilt on every resolution, so the latch must outlive them."""
    backend = BlockingSecurityBackend()
    clock = [1000.0]

    def make():
        return MacOSCredentialStore(
            "alice",
            backend=backend,
            noninteractive_timeout=0.05,
            latch_cooldown=60.0,
            lock_probe=lambda: False,
            clock=lambda: clock[0],
        )

    assert make().read().status is StoreStatus.ERROR  # times out, latches
    backend.release.set()

    second = make()
    assert second.read().status is StoreStatus.ERROR  # latched, no backend call
    assert second.write(_payload(), InteractionMode.BACKGROUND).status is StoreStatus.INTERACTIVE_REQUIRED
    assert backend.calls == 1

    clock[0] += 61.0
    backend.read_result = NativeReadResult(StoreStatus.OK, _payload().to_bytes())
    assert make().read().status is StoreStatus.OK  # expired, backend consulted again
    assert backend.calls == 2


def test_tool_timeout_latches_although_the_bounded_read_returns_in_time() -> None:
    """The tool's own timeout lands inside the store's wait, so no queue.Empty."""
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    def make():
        return MacOSCredentialStore(
            "alice", backend=SecurityCliBackend(run=run), lock_probe=lambda: False
        )

    assert make().read().status is StoreStatus.ERROR
    assert len(calls) == 1

    second = make()
    assert second.read().status is StoreStatus.ERROR  # latched, tool not respawned
    assert (
        second.write(_payload(), InteractionMode.BACKGROUND).status
        is StoreStatus.INTERACTIVE_REQUIRED
    )
    assert len(calls) == 1


def test_successful_interactive_call_clears_the_latch() -> None:
    backend = BlockingSecurityBackend()
    store = MacOSCredentialStore(
        "alice", backend=backend, noninteractive_timeout=0.05, lock_probe=lambda: False
    )
    assert store.read().status is StoreStatus.ERROR
    backend.release.set()
    backend.read_result = NativeReadResult(StoreStatus.OK, _payload().to_bytes())

    assert store.write(_payload(2), InteractionMode.FOREGROUND).status is StoreStatus.OK
    assert store.read().status is StoreStatus.OK


def test_macos_store_defaults_to_security_cli_backend() -> None:
    store = MacOSCredentialStore("alice")
    assert isinstance(store._backend, SecurityCliBackend)


def test_credential_package_never_uses_security_framework_for_item_access() -> None:
    root = Path(__file__).resolve().parents[2] / "jacked" / "credentials"
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "SecItemCopyMatching" not in text, source
        assert "SecItemAdd" not in text, source
        assert "SecItemUpdate" not in text, source
        assert "LocalAuthentication" not in text, source


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


def test_file_store_rearms_the_stamp_when_post_replace_cleanup_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """A failure after the replace commits must not poison the next write.

    ``_durable_replace`` publishes the bytes; a later ``os.chmod`` /
    ``_sync_directory`` OSError makes ``write()`` report UNUSABLE while the new
    bytes are already on disk. If the compare-and-swap stamp still held the
    pre-write file, the next write would refuse with CONCURRENT_WRITE and the
    engine would report FAILED_PRESERVED naming the account we just wrote.
    """
    from jacked.credentials import file_store as file_store_module

    path = tmp_path / ".credentials.json"
    path.write_bytes(_payload(1).to_bytes())
    store = FileCredentialStore(path, trusted_root=tmp_path)
    assert store.read().status is StoreStatus.OK

    real_chmod = file_store_module.os.chmod
    failing = {"armed": False}

    def chmod_failing_on_the_committed_file(target, mode, *args, **kwargs):
        if failing["armed"] and Path(target) == path:
            raise OSError("chmod refused after the replace committed")
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(
        file_store_module.os, "chmod", chmod_failing_on_the_committed_file
    )
    failing["armed"] = True

    first = store.write(_payload(2), InteractionMode.FOREGROUND)

    assert first.status is StoreStatus.UNUSABLE
    assert "chmod refused" in first.reason
    assert CredentialPayload.from_json(path.read_bytes()).identity.account_id == 2

    failing["armed"] = False
    second = store.write(_payload(3), InteractionMode.FOREGROUND)

    assert second.status is not StoreStatus.CONCURRENT_WRITE
    assert second.status is StoreStatus.OK
    assert CredentialPayload.from_json(path.read_bytes()).identity.account_id == 3


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

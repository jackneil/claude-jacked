"""The one atomic writer (``jacked.atomic_io``) and its Windows-aware replace.

On Windows the replace fails with ``PermissionError`` while another handle
holds the target (a concurrent replace, a reader, an indexer, an antivirus
scan). The shared replace retries that refusal for a bounded time; POSIX
gets exactly one attempt. Every jacked file replace goes through this module,
which the AST guard at the bottom enforces.
"""

from __future__ import annotations

import ast
import errno
import logging
import os
from pathlib import Path

import pytest

from jacked import atomic_io

JACKED_ROOT = Path(__file__).resolve().parents[2] / "jacked"
ATOMIC_IO = JACKED_ROOT / "atomic_io.py"
# The exception text of a real refusal names the full path: no log line may
# print it.
REFUSAL_TEXT = r"Access is denied: C:\Users\someone\.claude\settings.json"


def _refusal(winerror: int) -> PermissionError:
    """A Windows sharing refusal (WinError 5 or 32) as the replace raises it."""
    refusal = PermissionError(errno.EACCES, REFUSAL_TEXT)
    refusal.winerror = winerror
    return refusal


def _flaky_replace(monkeypatch, failures: int, refusals: list | None = None) -> list:
    """Make the replace refuse ``failures`` times, then do the real replace.

    Each refusal is a new ``PermissionError`` with ``winerror`` 5 and 32 in
    turn, and ``refusals`` (when given) collects them in the raised order.
    """
    real_replace = os.replace
    calls: list = []

    def fake_replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            refusal = _refusal(5 if len(calls) % 2 else 32)
            if refusals is not None:
                refusals.append(refusal)
            raise refusal
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", fake_replace)
    return calls


def _record_pauses(monkeypatch) -> list:
    pauses: list = []
    monkeypatch.setattr(atomic_io.time, "sleep", pauses.append)
    return pauses


def _on_windows(monkeypatch) -> None:
    monkeypatch.setattr(atomic_io.sys, "platform", "win32")


def _stray_temps(directory: Path) -> list:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


# --------------------------------------------------------------------------- #
# The Windows retry
# --------------------------------------------------------------------------- #


def test_a_windows_sharing_refusal_is_retried_until_the_replace_lands(
    tmp_path, monkeypatch, caplog
):
    _on_windows(monkeypatch)
    _record_pauses(monkeypatch)
    refusals: list = []
    calls = _flaky_replace(monkeypatch, failures=9, refusals=refusals)
    target = tmp_path / "lessons.md"
    target.write_text("old\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        atomic_io.atomic_write_text(target, "new\n")

    assert target.read_text(encoding="utf-8") == "new\n"
    assert len(calls) == 10
    assert _stray_temps(tmp_path) == []
    records = [r for r in caplog.records if r.name == "jacked.atomic_io"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    message = records[0].getMessage()
    assert "lessons.md" in message and "9" in message
    # The last refusal by class and Windows code, never by its text.
    assert refusals[-1].winerror == 5
    assert "PermissionError" in message and "winerror=5" in message
    assert REFUSAL_TEXT not in message
    assert str(tmp_path) not in message  # the file name only, never the path


def test_the_retry_gives_up_after_ten_tries_and_the_writer_removes_its_temp(
    tmp_path, monkeypatch, caplog
):
    _on_windows(monkeypatch)
    _record_pauses(monkeypatch)
    refusals: list = []
    calls = _flaky_replace(monkeypatch, failures=11, refusals=refusals)
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        with pytest.raises(PermissionError) as excinfo:
            atomic_io.atomic_write_text(target, '{"a": 1}')

    assert len(calls) == 10
    # The caller gets the last refusal itself, with its Windows code intact.
    assert [r.winerror for r in refusals[-2:]] == [5, 32]
    assert excinfo.value is refusals[-1]
    assert excinfo.value.winerror == 32 and excinfo.value.errno == errno.EACCES
    assert target.read_text(encoding="utf-8") == "{}"
    assert _stray_temps(tmp_path) == []
    records = [r for r in caplog.records if r.name == "jacked.atomic_io"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "settings.json" in message
    assert "PermissionError" in message and "winerror=32" in message
    assert REFUSAL_TEXT not in message


def test_a_refusal_with_no_windows_code_is_named_by_its_errno(tmp_path, monkeypatch, caplog):
    _on_windows(monkeypatch)
    _record_pauses(monkeypatch)
    real_replace = os.replace
    calls: list = []

    def refuse_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, REFUSAL_TEXT)
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", refuse_once)
    src = tmp_path / "staged"
    src.write_text("x", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        atomic_io.replace_with_retry(src, tmp_path / "target")

    [record] = [r for r in caplog.records if r.name == "jacked.atomic_io"]
    message = record.getMessage()
    assert f"PermissionError errno={errno.EACCES}" in message
    assert REFUSAL_TEXT not in message


def test_posix_never_retries_a_permission_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(atomic_io.sys, "platform", "linux")
    pauses = _record_pauses(monkeypatch)
    calls = _flaky_replace(monkeypatch, failures=1)
    src = tmp_path / "staged"
    src.write_text("x", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        with pytest.raises(PermissionError):
            atomic_io.replace_with_retry(src, tmp_path / "target")

    assert len(calls) == 1
    assert pauses == []
    assert [r for r in caplog.records if r.name == "jacked.atomic_io"] == []


def test_the_pauses_double_from_ten_ms_and_stay_under_one_second(tmp_path, monkeypatch):
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    _flaky_replace(monkeypatch, failures=100)
    src = tmp_path / "staged"
    src.write_text("x", encoding="utf-8")

    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry(src, tmp_path / "target")

    assert pauses[:4] == [0.01, 0.02, 0.04, 0.08]
    assert len(pauses) == 9  # ten tries
    assert sum(pauses) < 1.0


def test_a_caller_can_widen_or_close_the_retry_budget(tmp_path, monkeypatch):
    """The Claude config store needs about 3 s (antivirus holds new files);
    the statusline cache must never wait (a lost cache write is a miss)."""
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    calls = _flaky_replace(monkeypatch, failures=1000)
    src = tmp_path / "staged"
    src.write_text("x", encoding="utf-8")

    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry(src, tmp_path / "target", retry_budget=3.0)
    assert 2.5 < sum(pauses) <= 3.0 + 1e-9

    pauses.clear()
    calls.clear()
    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry(src, tmp_path / "target", retry_budget=0)
    assert pauses == [] and len(calls) == 1


def test_write_through_uses_the_windows_move_only_on_windows(tmp_path, monkeypatch):
    moves: list = []

    def fake_move(src, dst):
        moves.append(dst)
        if len(moves) == 1:
            raise PermissionError(13, "Access is denied")
        os.replace(src, dst)

    monkeypatch.setattr(atomic_io, "_move_file_write_through", fake_move)
    _record_pauses(monkeypatch)
    src = tmp_path / "staged"
    target = tmp_path / ".credentials.json"

    src.write_text("posix", encoding="utf-8")
    monkeypatch.setattr(atomic_io.sys, "platform", "linux")
    atomic_io.replace_with_retry(src, target, write_through=True)
    assert moves == [] and target.read_text(encoding="utf-8") == "posix"

    src.write_text("windows", encoding="utf-8")
    _on_windows(monkeypatch)
    atomic_io.replace_with_retry(src, target, write_through=True)
    assert moves == [target, target]  # refused once, retried, landed
    assert target.read_text(encoding="utf-8") == "windows"


def test_before_retry_runs_after_each_pause_and_can_stop_the_replace(tmp_path, monkeypatch):
    """A compare-and-swap writer checks the target again before each retry."""
    _on_windows(monkeypatch)
    events: list = []
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _seconds: events.append("pause"))
    calls = _flaky_replace(monkeypatch, failures=100)
    src = tmp_path / "staged"
    src.write_text("x", encoding="utf-8")

    class TargetChanged(Exception):
        pass

    def before_retry():
        events.append("check")
        if events.count("check") == 2:
            raise TargetChanged

    with pytest.raises(TargetChanged):
        atomic_io.replace_with_retry(src, tmp_path / "target", before_retry=before_retry)

    assert events == ["pause", "check", "pause", "check"]
    assert len(calls) == 2
    assert src.exists()


def test_a_refused_recheck_is_one_more_refusal_of_the_same_budget(tmp_path, monkeypatch):
    """The re-read in ``before_retry`` can be refused like the replace (the
    same handle holds the file). That refusal pauses and checks again inside
    the same budget, and the replace then lands."""
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    calls = _flaky_replace(monkeypatch, failures=1)
    src = tmp_path / "staged"
    src.write_text("new", encoding="utf-8")
    target = tmp_path / "target"
    checks: list = []

    def before_retry():
        checks.append(len(pauses))
        if len(checks) <= 2:
            raise _refusal(32)

    atomic_io.replace_with_retry(src, target, before_retry=before_retry)

    assert target.read_text(encoding="utf-8") == "new"
    assert len(calls) == 2  # refused once, then landed
    assert checks == [1, 2, 3]  # every check runs after its own pause
    assert pauses == [0.01, 0.02, 0.04]


def test_a_recheck_refused_to_the_end_of_the_budget_raises_its_last_refusal(
    tmp_path, monkeypatch
):
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    calls = _flaky_replace(monkeypatch, failures=1)
    src = tmp_path / "staged"
    src.write_text("new", encoding="utf-8")
    raised: list = []

    def before_retry():
        raised.append(_refusal(5))
        raise raised[-1]

    with pytest.raises(PermissionError) as excinfo:
        atomic_io.replace_with_retry(src, tmp_path / "target", before_retry=before_retry)

    assert excinfo.value is raised[-1]
    assert len(pauses) == 9  # the one budget, shared with the replace
    assert len(calls) == 1
    assert src.exists()


def test_a_recheck_that_fails_another_way_stops_the_replace_at_once(tmp_path, monkeypatch):
    """Only a refusal is retried. Any other error from ``before_retry`` (the
    file store raises OSError for a target that is no longer a regular
    file) propagates unchanged, and the replace is not tried again."""
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    calls = _flaky_replace(monkeypatch, failures=1)
    src = tmp_path / "staged"
    src.write_text("new", encoding="utf-8")
    stop = OSError("credential path is not a regular file")

    def before_retry():
        raise stop

    with pytest.raises(OSError) as excinfo:
        atomic_io.replace_with_retry(src, tmp_path / "target", before_retry=before_retry)

    assert excinfo.value is stop
    assert pauses == [0.01]
    assert len(calls) == 1
    assert src.exists() and not (tmp_path / "target").exists()


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #


def test_fsync_runs_once_only_when_asked(tmp_path, monkeypatch):
    synced: list = []
    real_fsync = os.fsync
    monkeypatch.setattr(atomic_io.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))

    atomic_io.atomic_write_bytes(tmp_path / "a.bin", b"a")
    assert synced == []
    atomic_io.atomic_write_bytes(tmp_path / "a.bin", b"b", fsync=True)
    atomic_io.atomic_write_text(tmp_path / "a.txt", "c", fsync=True)
    assert len(synced) == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_the_mode_lands_on_the_result(tmp_path, mode):
    target = tmp_path / ".credentials.json"
    atomic_io.atomic_write_text(target, "{}", mode=mode)
    assert target.stat().st_mode & 0o777 == mode


def _chmod_not_supported(monkeypatch) -> list:
    """Storage that creates and replaces files but refuses chmod (ENOTSUP),
    by descriptor and by path."""
    refused: list = []

    def refuse(target, mode, *args, **kwargs):
        refused.append(target)
        raise OSError(errno.ENOTSUP, f"Operation not supported: {target}")

    monkeypatch.setattr(atomic_io.os, "chmod", refuse)
    monkeypatch.setattr(atomic_io.os, "fchmod", refuse, raising=False)
    return refused


class _OsView:
    """atomic_io's own view of ``os``: the real module, with some names
    replaced or removed. The real module is never changed, so pathlib and
    the rest of the process keep the real ``os.name``."""

    def __init__(self, missing=(), **replaced):
        self._missing = set(missing)
        self._replaced = replaced

    def __getattr__(self, name):
        if name in self._missing:
            raise AttributeError(name)
        if name in self._replaced:
            return self._replaced[name]
        return getattr(os, name)


@pytest.mark.parametrize(
    "write",
    [
        lambda target: atomic_io.atomic_write_text(target, "secret", mode=0o600),
        lambda target: atomic_io.atomic_write_bytes(target, b"secret", mode=0o600),
    ],
    ids=["text", "bytes"],
)
def test_a_refused_chmod_stops_a_strict_write_before_publication(tmp_path, monkeypatch, write):
    """The default: a credential write that cannot set its mode is not
    published, and its temp file is removed."""
    target = tmp_path / ".credentials.json"
    target.write_text("old", encoding="utf-8")
    refused = _chmod_not_supported(monkeypatch)

    with pytest.raises(OSError) as excinfo:
        write(target)

    assert excinfo.value.errno == errno.ENOTSUP
    assert len(refused) == 1
    assert target.read_text(encoding="utf-8") == "old"
    assert _stray_temps(tmp_path) == []


def test_a_tolerant_write_publishes_through_a_refused_chmod(tmp_path, monkeypatch, caplog):
    """``mode_strict=False`` (the launch's per-account files): the refused
    chmod is logged at DEBUG by file name and errno, and the write goes on.
    The temp file is created owner-only, so 0o600 is still what lands."""
    target = tmp_path / ".claude.json"
    refused = _chmod_not_supported(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        atomic_io.atomic_write_text(target, "secret", mode=0o600, mode_strict=False)
        atomic_io.atomic_write_bytes(target, b"secret2", mode=0o600, mode_strict=False)

    assert target.read_bytes() == b"secret2"
    assert len(refused) == 2
    assert _stray_temps(tmp_path) == []
    records = [r for r in caplog.records if r.name == "jacked.atomic_io"]
    assert len(records) == 2
    for record in records:
        message = record.getMessage()
        assert record.levelno == logging.DEBUG
        assert ".claude.json" in message and f"OSError errno={errno.ENOTSUP}" in message
        assert "Operation not supported" not in message and str(tmp_path) not in message
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


# --- the mode is set on the open temp file (S2) and privately on Windows (S3) --


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_the_mode_is_set_on_the_descriptor_so_a_planted_link_cannot_redirect_it(
    tmp_path, monkeypatch
):
    """Another process can swap the temp name for a link between mkstemp and
    the mode change. A chmod by path would then change the link's target."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep", encoding="utf-8")
    victim.chmod(0o644)
    real_mkstemp = atomic_io.tempfile.mkstemp

    def mkstemp_then_plant_a_link(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        os.rename(name, name + ".moved")  # the descriptor keeps the real temp file
        os.symlink(victim, name)
        return fd, name

    monkeypatch.setattr(atomic_io.tempfile, "mkstemp", mkstemp_then_plant_a_link)

    atomic_io.atomic_write_text(tmp_path / "secret.json", "{}", mode=0o600)

    assert victim.stat().st_mode & 0o777 == 0o644
    assert victim.read_text(encoding="utf-8") == "keep"


def test_the_descriptor_gets_the_mode_when_fchmod_exists(tmp_path, monkeypatch):
    calls: list = []

    def fchmod(fd, mode):
        calls.append((os.fstat(fd).st_ino, mode))

    def chmod(*args, **kwargs):
        raise AssertionError("a path chmod must not run when fchmod exists")

    monkeypatch.setattr(atomic_io, "os", _OsView(fchmod=fchmod, chmod=chmod))
    target = tmp_path / ".credentials.json"

    atomic_io.atomic_write_text(target, "{}", mode=0o600)

    assert calls == [(target.stat().st_ino, 0o600)]  # the temp file that became the target


def test_a_build_without_fchmod_sets_the_mode_by_path(tmp_path, monkeypatch):
    """Windows before Python 3.13 has no os.fchmod: the path is the fallback."""
    calls: list = []

    def chmod(path, mode):
        calls.append((Path(path).name, mode))
        os.chmod(path, mode)

    monkeypatch.setattr(atomic_io, "os", _OsView(missing={"fchmod"}, chmod=chmod))
    target = tmp_path / "state.json"

    atomic_io.atomic_write_text(target, "{}", mode=0o600)

    [(name, mode)] = calls
    assert name.startswith(".state.json.") and name.endswith(".tmp") and mode == 0o600
    assert target.read_text(encoding="utf-8") == "{}"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def _record_windows_acl(monkeypatch, *, fail: bool = False, **os_replaced) -> list:
    """Run atomic_io as on Windows and record each private-ACL request.

    ``os_replaced`` replaces more names in atomic_io's view of ``os`` (a
    refusing ``fchmod``, for example)."""
    from jacked.service import windows_security

    secured: list = []

    def secure_windows_path(path):
        # The bytes written so far tell when the ACL was set.
        secured.append((Path(path), Path(path).read_bytes()))
        if fail:
            raise OSError(errno.EACCES, f"Access is denied: {path}")

    monkeypatch.setattr(windows_security, "secure_windows_path", secure_windows_path)
    monkeypatch.setattr(atomic_io, "os", _OsView(name="nt", **os_replaced))
    return secured


def test_windows_makes_a_moded_temp_file_private_before_the_bytes_land(
    tmp_path, monkeypatch
):
    """A Windows mode bit does not change who can open the file (mkstemp
    gives it the folder's ACL), so a requested mode also sets a private ACL."""
    secured = _record_windows_acl(monkeypatch)
    target = tmp_path / ".credentials.json"

    atomic_io.atomic_write_text(target, "secret", mode=0o600)

    [(path, bytes_then)] = secured
    assert path.parent == tmp_path
    assert path.name.startswith("..credentials.json.") and path.name.endswith(".tmp")
    assert bytes_then == b""  # private before the secret is written
    assert target.read_text(encoding="utf-8") == "secret"


def test_windows_leaves_the_acl_alone_when_no_mode_is_asked(tmp_path, monkeypatch):
    secured = _record_windows_acl(monkeypatch)

    atomic_io.atomic_write_text(tmp_path / "lessons.md", "# Lessons\n")

    assert secured == []


def test_posix_never_asks_for_a_windows_acl(tmp_path, monkeypatch):
    from jacked.service import windows_security

    def secure_windows_path(path):
        raise AssertionError("POSIX must not set a Windows ACL")

    monkeypatch.setattr(windows_security, "secure_windows_path", secure_windows_path)
    monkeypatch.setattr(atomic_io, "os", _OsView(name="posix"))

    atomic_io.atomic_write_text(tmp_path / ".credentials.json", "{}", mode=0o600)


def test_a_refused_windows_acl_stops_a_strict_write(tmp_path, monkeypatch):
    secured = _record_windows_acl(monkeypatch, fail=True)
    target = tmp_path / ".credentials.json"

    with pytest.raises(OSError):
        atomic_io.atomic_write_text(target, "secret", mode=0o600)

    assert len(secured) == 1
    assert not target.exists()
    assert _stray_temps(tmp_path) == []


def test_a_refused_windows_acl_stops_a_tolerant_write_too(tmp_path, monkeypatch, caplog):
    """On Windows the ACL is the only protection the file has. The tolerance
    of ``mode_strict=False`` covers the mode bits only, so a temp file that
    cannot be made private is not published, and it is removed."""
    secured = _record_windows_acl(monkeypatch, fail=True)
    target = tmp_path / ".claude.json"

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        with pytest.raises(OSError) as excinfo:
            atomic_io.atomic_write_text(target, "{}", mode=0o600, mode_strict=False)

    [(temp, _bytes_then)] = secured
    # The ACL's own error, for the temp file, reaches the caller.
    assert excinfo.value.errno == errno.EACCES and str(temp) in str(excinfo.value)
    assert not target.exists()
    assert _stray_temps(tmp_path) == []
    # No log line says that the write went on.
    assert [r for r in caplog.records if r.name == "jacked.atomic_io"] == []


def test_a_refused_chmod_does_not_skip_the_windows_acl(tmp_path, monkeypatch, caplog):
    """A tolerant write goes on after a refused chmod, but the ACL step still
    runs: the temp file is private before the bytes land, then published."""
    refused: list = []

    def refuse(target, mode, *args, **kwargs):
        refused.append(target)
        raise OSError(errno.ENOTSUP, f"Operation not supported: {target}")

    secured = _record_windows_acl(monkeypatch, fchmod=refuse, chmod=refuse)
    target = tmp_path / ".credentials.json"

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        atomic_io.atomic_write_text(target, "secret", mode=0o600, mode_strict=False)

    assert len(refused) == 1
    [(temp, bytes_then)] = secured
    assert temp.parent == tmp_path
    assert temp.name.startswith("..credentials.json.") and temp.name.endswith(".tmp")
    assert bytes_then == b""  # private before the secret is written
    assert target.read_text(encoding="utf-8") == "secret"
    [record] = [r for r in caplog.records if r.name == "jacked.atomic_io"]
    assert f"OSError errno={errno.ENOTSUP}" in record.getMessage()


def test_the_writer_follows_a_symlink_only_when_asked(tmp_path):
    outside = tmp_path / "home" / ".bashrc"
    outside.parent.mkdir()
    outside.write_text("export SAFE=1\n", encoding="utf-8")
    link = tmp_path / "repo" / "lessons.md"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    atomic_io.atomic_write_bytes(link, b"# Lessons\n")
    assert not link.is_symlink() and link.read_bytes() == b"# Lessons\n"
    assert outside.read_text(encoding="utf-8") == "export SAFE=1\n"

    shared = tmp_path / "dotfiles" / "CLAUDE.md"
    shared.parent.mkdir()
    shared.write_text("old\n", encoding="utf-8")
    followed = tmp_path / "CLAUDE.md"
    followed.symlink_to(shared)
    atomic_io.atomic_write_text(followed, "new\n", follow_symlinks=True)
    assert followed.is_symlink() and shared.read_text(encoding="utf-8") == "new\n"


def test_atomic_write_bytes_writes_the_bytes_exactly(tmp_path):
    payload = b"\x00\r\n\xff\xfeline\n"
    target = tmp_path / "deep" / "nested" / "blob.bin"
    atomic_io.atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_atomic_write_text_encodes_utf8(tmp_path):
    target = tmp_path / "note.md"
    atomic_io.atomic_write_text(target, "café")
    assert target.read_text(encoding="utf-8") == "café"


def test_the_temp_file_is_writer_unique_and_beside_the_target(tmp_path, monkeypatch):
    seen: list = []
    real_mkstemp = atomic_io.tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(Path(name))
        return fd, name

    monkeypatch.setattr(atomic_io.tempfile, "mkstemp", spy_mkstemp)
    target = tmp_path / "state.json"
    atomic_io.atomic_write_text(target, "1")
    atomic_io.atomic_write_text(target, "2")

    assert len(seen) == 2 and seen[0] != seen[1]
    for name in seen:
        assert name.parent == tmp_path
        assert name.name.startswith(".state.json.") and name.name.endswith(".tmp")
        assert not name.exists()


def test_a_keyboard_interrupt_mid_write_removes_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "jacked-memory.json"
    target.write_text("old", encoding="utf-8")

    def interrupted(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(atomic_io.os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        atomic_io.atomic_write_text(target, "new", fsync=True)

    assert target.read_text(encoding="utf-8") == "old"
    assert _stray_temps(tmp_path) == []


def _failing_replace(monkeypatch) -> OSError:
    """A replace that fails for good (not a sharing refusal)."""
    failure = OSError(errno.EIO, "I/O error")

    def fail(src, dst):
        raise failure

    monkeypatch.setattr(atomic_io.os, "replace", fail)
    return failure


def test_a_locked_temp_file_is_removed_once_its_holder_lets_go(tmp_path, monkeypatch):
    """On Windows a scanner can hold the new temp file when the write fails.
    The cleanup retries the refused unlink with the replace's pauses."""
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    failure = _failing_replace(monkeypatch)
    real_unlink = os.unlink
    unlinks: list = []

    def held_twice(path, *args, **kwargs):
        unlinks.append(Path(path).name)
        if len(unlinks) <= 2:
            raise _refusal(32)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(atomic_io.os, "unlink", held_twice)
    target = tmp_path / "jacked-packs.json"

    with pytest.raises(OSError) as excinfo:
        atomic_io.atomic_write_text(target, "{}")

    assert excinfo.value is failure  # the cleanup never hides the real error
    assert len(unlinks) == 3
    assert pauses == [0.01, 0.02]
    assert _stray_temps(tmp_path) == []


def test_the_temp_cleanup_gives_up_quietly_after_the_pause_schedule(tmp_path, monkeypatch):
    """Best effort: a temp file held past the schedule stays, and the write
    still raises its own error, not the cleanup's."""
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    failure = _failing_replace(monkeypatch)

    def always_held(path, *args, **kwargs):
        raise _refusal(32)

    monkeypatch.setattr(atomic_io.os, "unlink", always_held)

    with pytest.raises(OSError) as excinfo:
        atomic_io.atomic_write_text(tmp_path / "jacked-packs.json", "{}")

    assert excinfo.value is failure
    assert pauses == list(atomic_io._pause_schedule(atomic_io.RETRY_BUDGET_SECONDS))
    assert len(_stray_temps(tmp_path)) == 1


def test_a_zero_budget_write_never_pauses_in_its_cleanup_either(tmp_path, monkeypatch):
    """``retry_budget=0`` (the statusline cache) is a write that must never
    wait. When a scanner holds the temp file, the replace and the cleanup
    unlink are both refused, and the cleanup follows the same budget: one
    unlink and no pause, on Windows as on POSIX."""
    _on_windows(monkeypatch)
    pauses = _record_pauses(monkeypatch)
    refusals: list = []
    calls = _flaky_replace(monkeypatch, failures=100, refusals=refusals)
    unlinks: list = []

    def held(path, *args, **kwargs):
        unlinks.append(Path(path).name)
        raise _refusal(32)

    monkeypatch.setattr(atomic_io.os, "unlink", held)

    with pytest.raises(PermissionError) as excinfo:
        atomic_io.atomic_write_text(tmp_path / "cache.json", "x", retry_budget=0)

    assert excinfo.value is refusals[-1]  # the replace's refusal, not the cleanup's
    assert pauses == []
    assert len(calls) == 1
    assert len(unlinks) == 1


def test_posix_removes_the_temp_file_with_one_unlink(tmp_path, monkeypatch):
    monkeypatch.setattr(atomic_io.sys, "platform", "linux")
    pauses = _record_pauses(monkeypatch)
    _failing_replace(monkeypatch)
    unlinks: list = []

    def refused(path, *args, **kwargs):
        unlinks.append(path)
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(atomic_io.os, "unlink", refused)

    with pytest.raises(OSError):
        atomic_io.atomic_write_text(tmp_path / "state.json", "{}")

    assert len(unlinks) == 1 and pauses == []


# --------------------------------------------------------------------------- #
# Guard: every replace goes through the shared one
# --------------------------------------------------------------------------- #


def _bare_replaces(path: Path) -> list[str]:
    """Replaces that bypass ``jacked.atomic_io``.

    Flags every use of the ``os.replace`` attribute through any alias of
    ``os``: a call, and a reference that a later call can use
    (``replace = os.replace``, a callback argument). Also flags
    ``getattr(os, "replace")``, ``from os import replace``, and the
    one-argument ``Path.replace(target)`` call. A string replace needs two
    arguments and a datetime or dataclass replace takes keywords, so neither
    is flagged.
    """
    label = path.relative_to(JACKED_ROOT.parent) if JACKED_ROOT in path.parents else path.name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    os_names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names.update(a.asname or a.name for a in node.names if a.name == "os")

    def is_os(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Name) and expr.id in os_names

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(alias.name == "replace" for alias in node.names):
                found.append(f"{label}:{node.lineno} from os import replace")
        elif isinstance(node, ast.Attribute) and node.attr == "replace" and is_os(node.value):
            found.append(f"{label}:{node.lineno} {node.value.id}.replace")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if (
                node.func.id == "getattr"
                and len(node.args) >= 2
                and is_os(node.args[0])
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "replace"
            ):
                found.append(f"{label}:{node.lineno} getattr({node.args[0].id}, 'replace')")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
            and not is_os(node.func.value)
            and len(node.args) == 1
            and not node.keywords
        ):
            found.append(f"{label}:{node.lineno} .replace(target)")
    return found


def test_no_bare_os_replace_outside_atomic_io():
    offenders = [
        hit
        for path in sorted(JACKED_ROOT.rglob("*.py"))
        if path != ATOMIC_IO
        for hit in _bare_replaces(path)
    ]
    assert not offenders, "use jacked.atomic_io (replace_with_retry or atomic_write_*): " + ", ".join(
        offenders
    )


def test_the_replace_guard_sees_a_synthetic_offender(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import os\nimport os as _os\nfrom os import replace\nfrom pathlib import Path\n"
        "os.replace('a', 'b')\n_os.replace('a', 'b')\nPath('a').replace('b')\n"
        "'abc'.replace('a', 'b')\nstamp.replace(hour=3)\n",
        encoding="utf-8",
    )
    hits = _bare_replaces(bad)
    assert len(hits) == 4, hits
    assert _bare_replaces(ATOMIC_IO), "the guard must see the one real replace"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import os\nreplace = os.replace\nreplace('a', 'b')\n", ["bad.py:2 os.replace"]),
        ("import os\nrun(os.replace, 'a', 'b')\n", ["bad.py:2 os.replace"]),
        (
            "import os\ngetattr(os, 'replace')('a', 'b')\n",
            ["bad.py:2 getattr(os, 'replace')"],
        ),
        ("import os as o\no.replace('a', 'b')\n", ["bad.py:2 o.replace"]),
    ],
    ids=["alias-assignment", "callback-argument", "getattr", "import-alias"],
)
def test_the_replace_guard_sees_an_indirect_os_replace(tmp_path, source, expected):
    """Each form alone: a reference kept for a later call, ``getattr`` on
    ``os``, and ``os`` imported under another name."""
    bad = tmp_path / "bad.py"
    bad.write_text(source, encoding="utf-8")
    assert _bare_replaces(bad) == expected


def test_the_replace_guard_ignores_other_replace_names(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text(
        "import os\nfrom dataclasses import replace\n"
        "replace(record, name='x')\ngetattr(text, 'replace')('a', 'b')\n"
        "getattr(os, 'rename')\nos.environ.replace\n",
        encoding="utf-8",
    )
    assert _bare_replaces(clean) == []

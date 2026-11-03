"""The one atomic writer (``jacked.atomic_io``) and its Windows-aware replace.

On Windows the replace fails with ``PermissionError`` while another handle
holds the target (a concurrent replace, a reader, an indexer, an antivirus
scan). The shared replace retries that refusal for a bounded time; POSIX
gets exactly one attempt. Every jacked file replace goes through this module,
which the AST guard at the bottom enforces.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

import pytest

from jacked import atomic_io

JACKED_ROOT = Path(__file__).resolve().parents[2] / "jacked"
ATOMIC_IO = JACKED_ROOT / "atomic_io.py"


def _flaky_replace(monkeypatch, failures: int) -> list:
    """Make the replace refuse ``failures`` times, then do the real replace."""
    real_replace = os.replace
    calls: list = []

    def fake_replace(src, dst):
        calls.append(dst)
        if len(calls) <= failures:
            raise PermissionError(13, "Access is denied")
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
    calls = _flaky_replace(monkeypatch, failures=9)
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
    assert str(tmp_path) not in message  # the file name only, never the path


def test_the_retry_gives_up_after_ten_tries_and_the_writer_removes_its_temp(
    tmp_path, monkeypatch, caplog
):
    _on_windows(monkeypatch)
    _record_pauses(monkeypatch)
    calls = _flaky_replace(monkeypatch, failures=11)
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="jacked.atomic_io"):
        with pytest.raises(PermissionError):
            atomic_io.atomic_write_text(target, '{"a": 1}')

    assert len(calls) == 10
    assert target.read_text(encoding="utf-8") == "{}"
    assert _stray_temps(tmp_path) == []
    records = [r for r in caplog.records if r.name == "jacked.atomic_io"]
    assert len(records) == 1 and "settings.json" in records[0].getMessage()


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


# --------------------------------------------------------------------------- #
# Guard: every replace goes through the shared one
# --------------------------------------------------------------------------- #


def _bare_replaces(path: Path) -> list[str]:
    """Replace calls that bypass ``jacked.atomic_io``.

    Flags ``os.replace(...)`` (any alias of ``os``), ``from os import
    replace``, and the one-argument ``Path.replace(target)`` form. A string
    replace needs two arguments and a datetime or dataclass replace takes
    keywords, so neither is flagged.
    """
    label = path.relative_to(JACKED_ROOT.parent) if JACKED_ROOT in path.parents else path.name
    tree = ast.parse(path.read_text(encoding="utf-8"))
    os_names = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            os_names.update(a.asname or a.name for a in node.names if a.name == "os")
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            if any(alias.name == "replace" for alias in node.names):
                found.append(f"{label}:{node.lineno} from os import replace")
            continue
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "replace":
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in os_names:
            found.append(f"{label}:{node.lineno} {receiver.id}.replace()")
        elif len(node.args) == 1 and not node.keywords:
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

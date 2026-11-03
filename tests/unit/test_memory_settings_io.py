"""Tests for jacked.memory.settings_io (F1): the single corruption-safe
settings.json reader/writer.

The load-bearing behavior is that an EXISTING-but-unreadable file raises rather
than reading as ``{}`` (which a later write would then clobber into oblivion),
while a genuinely absent file is the benign fresh-install case.
"""
import json
import logging
import os

import pytest

from jacked.memory import settings_io
from jacked.memory.settings_io import SettingsUnreadableError
from tests._platform import requires_fifo, returns_without_blocking, symlinks_supported


def test_read_missing_returns_empty(tmp_path):
    assert settings_io.read_settings(tmp_path / "nope.json") == {}


def test_read_valid_object(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"hooks": {"SessionEnd": []}}), encoding="utf-8")
    assert settings_io.read_settings(p) == {"hooks": {"SessionEnd": []}}


def test_read_corrupt_raises(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(SettingsUnreadableError):
        settings_io.read_settings(p)


def test_read_non_object_raises(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(SettingsUnreadableError):
        settings_io.read_settings(p)


def test_write_round_trip_and_no_leftover_tmp(tmp_path):
    p = tmp_path / "settings.json"
    settings_io.write_settings(p, {"a": 1, "nested": {"b": 2}})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "nested": {"b": 2}}
    # Writer-unique temp is cleaned up (nothing matching the tmp prefix remains).
    assert not list(tmp_path.glob(".settings.json.*.tmp"))


def test_write_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "settings.json"
    settings_io.write_settings(p, {"ok": True})
    assert p.exists()


def test_settings_path_shape(tmp_path):
    assert settings_io.settings_path(tmp_path) == tmp_path / ".claude" / "settings.json"


# --------------------------------------------------------------------------- #
# Only a regular file is read, and only up to SETTINGS_MAX_BYTES
# --------------------------------------------------------------------------- #

@requires_fifo
def test_a_fifo_raises_at_once(tmp_path):
    """A FIFO with no writer blocks a plain read forever (SessionStart reads these)."""
    fifo = tmp_path / "settings.json"
    os.mkfifo(fifo)

    finished, _ = returns_without_blocking(
        lambda: pytest.raises(SettingsUnreadableError, settings_io.read_settings, fifo), [fifo]
    )

    assert finished, "read_settings blocked on a FIFO"


@requires_fifo
@pytest.mark.skipif(not symlinks_supported(), reason="needs symlinks")
def test_a_symlink_to_a_fifo_raises_at_once(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    link = tmp_path / "settings.json"
    link.symlink_to(fifo)

    finished, _ = returns_without_blocking(
        lambda: pytest.raises(SettingsUnreadableError, settings_io.read_settings, link), [fifo]
    )

    assert finished, "read_settings blocked on a symlink to a FIFO"


def test_a_directory_raises(tmp_path):
    (tmp_path / "settings.json").mkdir()
    with pytest.raises(SettingsUnreadableError):
        settings_io.read_settings(tmp_path / "settings.json")


def test_a_file_over_the_limit_raises_and_logs_its_size(tmp_path, caplog):
    p = tmp_path / "settings.json"
    with open(p, "wb") as f:
        f.truncate(settings_io.SETTINGS_MAX_BYTES + 1)  # sparse: no real bytes
    caplog.set_level(logging.WARNING, logger="jacked.memory.settings_io")

    with pytest.raises(SettingsUnreadableError, match="larger than"):
        settings_io.read_settings(p)

    assert any(str(settings_io.SETTINGS_MAX_BYTES + 1) in r.getMessage() for r in caplog.records)


def test_the_limit_is_4_mib():
    assert settings_io.SETTINGS_MAX_BYTES == 4 * 1024 * 1024


def test_a_file_at_the_limit_is_read(tmp_path):
    p = tmp_path / "settings.json"
    body = json.dumps({"k": ""})
    pad = settings_io.SETTINGS_MAX_BYTES - len(body)
    p.write_text(body[:-1] + " " * pad + body[-1:], encoding="utf-8")
    assert p.stat().st_size == settings_io.SETTINGS_MAX_BYTES
    assert settings_io.read_settings(p) == {"k": ""}


def test_invalid_utf8_raises_the_settings_error(tmp_path):
    p = tmp_path / "settings.json"
    p.write_bytes(b'{"k": "\xff"}')
    with pytest.raises(SettingsUnreadableError):
        settings_io.read_settings(p)


def test_json_nested_too_deep_raises_the_settings_error(tmp_path):
    """K8: json.loads raises RecursionError on deep nesting; that is unreadable too."""
    p = tmp_path / "settings.local.json"
    p.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(SettingsUnreadableError):
        settings_io.read_settings(p)


# --------------------------------------------------------------------------- #
# settings_io.write_settings is the ONE settings.json writer (#99i)
# --------------------------------------------------------------------------- #

import ast  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

_PACKAGE = Path(__file__).resolve().parents[2] / "jacked"
# A write whose target names the user settings file. ``settings.local.json``
# (a project file) and the integrity baseline beside it are other files.
_SETTINGS_TARGET = re.compile(r"settings_path|SETTINGS_JSON|settings\.json|settings_file")
_WRITE_CALLS = {
    "write_text", "write_bytes", "atomic_write_text", "atomic_write_bytes", "dump",
    "open", "replace", "rename", "copy", "copy2", "copyfile", "move",
}
_DEST_LAST = {"copy", "copy2", "copyfile", "move", "replace", "rename"}


#: Modules whose ``open`` takes the path first and a mode string second.
_OPEN_MODULES = {"io", "builtins", "codecs"}
_WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC")


def _open_write(node: ast.Call, source: str) -> tuple[bool, ast.expr | None]:
    """``(writes, target)`` for an ``open`` call, in its three forms.

    ``open(path, mode)`` and ``io.open(path, mode)``; ``os.open(path, flags)``;
    ``path.open(mode)`` (``pathlib.Path.open``), where the receiver is the target.
    """
    fn = node.func
    receiver = fn.value if isinstance(fn, ast.Attribute) else None
    receiver_name = receiver.id if isinstance(receiver, ast.Name) else None
    if receiver_name == "os":
        flags = ast.get_source_segment(source, node.args[1]) if len(node.args) > 1 else ""
        return any(f in (flags or "") for f in _WRITE_FLAGS), node.args[0] if node.args else None
    if receiver is None or receiver_name in _OPEN_MODULES:
        modes, target = node.args[1:2], node.args[0] if node.args else None
    else:
        modes, target = node.args[0:1], receiver
    modes = [*modes, *(k.value for k in node.keywords if k.arg == "mode")]
    writes = any(isinstance(m, ast.Constant) and set(str(m.value)) & set("wax+") for m in modes)
    return writes, target


def _settings_writes(source: str) -> list[str]:
    """Each call in ``source`` that writes to a settings.json target, as text."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name not in _WRITE_CALLS:
            continue
        parts = []
        if name == "open":
            writes, target = _open_write(node, source)
            if not writes:
                continue
            parts.append(ast.get_source_segment(source, target) if target is not None else "")
        else:
            if isinstance(fn, ast.Attribute) and name in ("write_text", "write_bytes", "replace", "rename"):
                parts.append(ast.get_source_segment(source, fn.value) or "")
            if node.args:
                target = node.args[-1] if name in _DEST_LAST else node.args[0]
                parts.append(ast.get_source_segment(source, target) or "")
        text = " ".join(p or "" for p in parts)
        if _SETTINGS_TARGET.search(text) and "local" not in text and "baseline_path" not in text:
            found.append(f"{name}({text})")
    return found


def test_the_scanner_finds_each_writer_shape():
    planted = (
        "settings_path.write_text(json.dumps(x))\n"
        "atomic_write_text(SETTINGS_JSON, '{}')\n"
        "with open(home / '.claude' / 'settings.json', 'w') as fh:\n    json.dump(x, fh)\n"
        "shutil.copy2(tmp, settings_file)\n"
        "with settings_path.open('w') as fh:\n    fh.write('{}')\n"
        "settings_file.open(mode='a').write('x')\n"
        "io.open(SETTINGS_JSON, 'w')\n"
        "os.open(settings_path, os.O_WRONLY | os.O_CREAT)\n"
    )
    assert len(_settings_writes(planted)) == 8
    assert _settings_writes(
        "settings_path.read_text()\nopen(settings_path)\nsettings_path.open()\n"
        "settings_file.open('r', encoding='utf-8')\nsettings_path.open(mode='rb')\n"
        "os.open(settings_path, os.O_RDONLY)\n"
    ) == []


def test_no_module_other_than_settings_io_writes_settings_json():
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == "settings_io.py" and path.parent.name == "memory":
            continue
        for call in _settings_writes(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(_PACKAGE.parent)}: {call}")
    assert offenders == [], (
        "settings.json must be written only through jacked.memory.settings_io.write_settings, "
        "which keeps the integrity baseline current:\n" + "\n".join(offenders)
    )


def test_write_settings_rebaselines_with_its_reason(tmp_path):
    from jacked import integrity

    p = tmp_path / ".claude" / "settings.json"
    settings_io.write_settings(p, {"model": "opus"}, reason="unit test")

    assert integrity.check(p) == []
    assert integrity.load_baseline(p)["log"][-1]["reason"] == "unit test"


# --------------------------------------------------------------------------- #
# A symlinked settings.json stays a symlink (H1)
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not symlinks_supported(), reason="needs symlinks")
def test_a_symlinked_settings_file_stays_a_symlink_after_a_write(tmp_path):
    """A dotfiles-managed settings.json is a link: the write goes to its target."""
    dotfiles = tmp_path / "dotfiles" / "claude-settings.json"
    dotfiles.parent.mkdir()
    dotfiles.write_text('{"model": "opus"}', encoding="utf-8")
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(dotfiles)
    pointer = os.readlink(link)

    settings_io.write_settings(link, {"model": "sonnet"})

    assert link.is_symlink()
    assert os.readlink(link) == pointer
    assert json.loads(dotfiles.read_text(encoding="utf-8")) == {"model": "sonnet"}
    assert sorted(p.name for p in dotfiles.parent.iterdir()) == ["claude-settings.json"]


@pytest.mark.skipif(not symlinks_supported(), reason="needs symlinks")
def test_a_dangling_settings_symlink_is_refused_and_nothing_is_created(tmp_path, caplog):
    target = tmp_path / "dotfiles" / "gone.json"
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(target)

    with caplog.at_level(logging.WARNING), pytest.raises(SettingsUnreadableError):
        settings_io.write_settings(link, {"model": "sonnet"})

    assert link.is_symlink() and not target.exists()
    assert not target.parent.exists()
    assert "symlink" in caplog.text and "gone.json" not in caplog.text
    assert sorted(p.name for p in link.parent.iterdir()) == ["settings.json"]


@pytest.mark.skipif(not symlinks_supported(), reason="needs symlinks")
def test_a_settings_symlink_to_a_directory_is_refused(tmp_path, caplog):
    folder = tmp_path / "a-folder"
    folder.mkdir()
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(folder)

    with caplog.at_level(logging.WARNING), pytest.raises(SettingsUnreadableError):
        settings_io.write_settings(link, {})

    assert link.is_symlink() and list(folder.iterdir()) == []
    assert "not a regular file" in caplog.text and "a-folder" not in caplog.text


@requires_fifo
@pytest.mark.skipif(not symlinks_supported(), reason="needs symlinks")
def test_a_settings_symlink_to_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    link = tmp_path / ".claude" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(fifo)

    finished, _ = returns_without_blocking(
        lambda: pytest.raises(SettingsUnreadableError, settings_io.write_settings, link, {}), [fifo]
    )

    assert finished, "write_settings blocked on a symlink to a FIFO"
    assert link.is_symlink()


# --------------------------------------------------------------------------- #
# W4: a write never makes a settings file that the reader refuses
# --------------------------------------------------------------------------- #

# 800,000 "é" are 1.6 MB as UTF-8, but json.dumps writes each one as the
# 6-byte escape é, so the file would be about 4.8 MB.
_TOO_LARGE = {"k": "é" * 800_000}


def _writers(tmp_path, monkeypatch):
    from jacked import cli
    from jacked.api.routes import features

    monkeypatch.setattr(features, "SETTINGS_JSON", tmp_path / "settings.json")
    return {
        "settings_io": lambda data: settings_io.write_settings(tmp_path / "settings.json", data),
        "cli": lambda data: cli._write_settings_atomic(tmp_path / "settings.json", data),
        "features": features._write_settings_json,
    }


@pytest.mark.parametrize("writer", ["settings_io", "cli", "features"])
def test_settings_over_the_limit_are_refused_before_the_file_changes(tmp_path, monkeypatch, writer):
    path = tmp_path / "settings.json"
    original = b'{\n  "hooks": {}\n}\n'
    path.write_bytes(original)

    with pytest.raises(settings_io.SettingsTooLargeError) as caught:
        _writers(tmp_path, monkeypatch)[writer](_TOO_LARGE)

    assert path.read_bytes() == original
    assert str(settings_io.SETTINGS_MAX_BYTES) in str(caught.value)
    assert str(len(json.dumps(_TOO_LARGE, indent=2).encode("utf-8"))) in str(caught.value)
    assert isinstance(caught.value, SettingsUnreadableError)
    assert not list(tmp_path.glob("*.tmp"))


def test_settings_text_counts_the_text_mode_line_endings(monkeypatch):
    data = {"k": "x" * 100}
    size = len(json.dumps(data, indent=2).encode("utf-8"))
    monkeypatch.setattr(settings_io, "SETTINGS_MAX_BYTES", size)
    assert settings_io.settings_text(data) == json.dumps(data, indent=2)
    monkeypatch.setattr(settings_io.os, "linesep", "\r\n")
    with pytest.raises(settings_io.SettingsTooLargeError):
        settings_io.settings_text(data)


def test_settings_at_the_limit_are_written_and_read_back(tmp_path, monkeypatch):
    data = {"k": "x" * 100}
    monkeypatch.setattr(settings_io, "SETTINGS_MAX_BYTES",
                        len(json.dumps(data, indent=2).encode("utf-8")))
    path = tmp_path / "settings.json"
    settings_io.write_settings(path, data)
    assert settings_io.read_settings(path) == data


def test_the_raw_settings_route_answers_413_for_settings_over_the_limit(tmp_path, monkeypatch):
    import asyncio

    from jacked.api.routes import features

    path = tmp_path / "settings.json"
    path.write_bytes(b"{}\n")
    monkeypatch.setattr(features, "SETTINGS_JSON", path)
    response = asyncio.run(features.set_raw_settings(
        features.RawSettingsRequest(content=_TOO_LARGE, confirm_overwrite=True)
    ))
    assert response.status_code == 413
    assert json.loads(response.body)["error"]["code"] == "SETTINGS_TOO_LARGE"
    assert path.read_bytes() == b"{}\n"

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


@pytest.mark.parametrize("linesep", ["\n", "\r\n"])
def test_the_settings_size_is_the_utf8_byte_count_on_every_platform(monkeypatch, linesep):
    """The size is the UTF-8 bytes of the text, whatever ``os.linesep`` is.

    The writers write those bytes with LF, so no platform adds a byte.
    """
    data = {"k": "é" * 10 + "x" * 90}
    text = json.dumps(data, indent=2)
    monkeypatch.setattr(os, "linesep", linesep)
    monkeypatch.setattr(settings_io, "SETTINGS_MAX_BYTES", len(text.encode("utf-8")))
    assert settings_io.settings_text(data) == text
    monkeypatch.setattr(settings_io, "SETTINGS_MAX_BYTES", len(text.encode("utf-8")) - 1)
    with pytest.raises(settings_io.SettingsTooLargeError):
        settings_io.settings_text(data)


def _windows_text_mode(path, text, **kwargs):
    """Write ``text`` the way a text-mode write does on Windows: CRLF."""
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


@pytest.mark.parametrize("writer", ["settings_io", "cli", "features"])
def test_every_writer_writes_the_checked_bytes_with_lf(tmp_path, monkeypatch, writer):
    """Each writer writes exactly the UTF-8 bytes that ``settings_text`` checked.

    A text-mode write stands in for Windows here: it adds a CR to each line.
    A writer that uses it makes a file larger than the checked size.
    """
    from jacked import atomic_io
    from jacked.api.routes import features
    from jacked.memory import vault

    for module in (atomic_io, vault, features):
        monkeypatch.setattr(module, "atomic_write_text", _windows_text_mode)
    data = {"hooks": {"Stop": []}, "k": "é"}
    _writers(tmp_path, monkeypatch)[writer](data)

    on_disk = (tmp_path / "settings.json").read_bytes()
    assert on_disk == settings_io.settings_text(data).encode("utf-8")
    assert b"\r" not in on_disk


def test_settings_at_the_limit_are_written_and_read_back(tmp_path, monkeypatch):
    data = {"k": "x" * 100}
    assert len(json.dumps(data, indent=2).encode("utf-8")) == 113
    monkeypatch.setattr(settings_io, "SETTINGS_MAX_BYTES", 113)
    path = tmp_path / "settings.json"
    settings_io.write_settings(path, data)
    assert path.stat().st_size == 113
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

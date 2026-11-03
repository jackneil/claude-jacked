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

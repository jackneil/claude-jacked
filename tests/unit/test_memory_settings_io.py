"""Tests for jacked.memory.settings_io (F1): the single corruption-safe
settings.json reader/writer.

The load-bearing behavior is that an EXISTING-but-unreadable file raises rather
than reading as ``{}`` (which a later write would then clobber into oblivion),
while a genuinely absent file is the benign fresh-install case.
"""
import json

import pytest

from jacked.memory import settings_io
from jacked.memory.settings_io import SettingsUnreadableError


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

"""Tests for ensure_native_lifecycle() — exercises the real install_autostart
path (writes a plist to a tmp location).  No fake_run lambdas that synthesize
the plist — those give false positives (/dc round-1 CRITICAL)."""
import sys
from unittest.mock import MagicMock, patch


class TestMacOS:
    def test_returns_already_installed_when_plist_exists(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        fake_plist.write_text("<plist/>")
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is True
        assert state == "already_installed"

    def test_calls_install_autostart_when_plist_missing(self, tmp_path, monkeypatch):
        """Missing plist → install_autostart writes it + bootstraps via
        launchctl. We mock launchctl so no real launchd interaction, but the
        plist file IS actually written by the real install_autostart code — no
        self-mocking side effects.

        CRITICAL: mock stop_process_graceful so this test can't kill the
        developer's real running jacked service (/dc round-2 v2.1 fix).
        _service_is_running is pinned False for the same reason: the real check
        reads the developer's live PID file, and a running dev service would
        flip install_autostart into its file-only branch."""
        import os
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)
        monkeypatch.setattr(plat, "_service_is_running", lambda: False)
        monkeypatch.setattr("jacked.findbin.find_bin", lambda name: "/fake/jacked")
        monkeypatch.setattr(
            "jacked.service.process.stop_process_graceful",
            lambda *a, **kw: {"was_running": False, "died": False, "killed": False},
        )
        with patch.object(plat.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")):
            ok, state, reason = plat.ensure_native_lifecycle()

        assert ok is True
        assert state == "just_installed"
        assert fake_plist.exists()
        assert "Label" in fake_plist.read_text()
        assert "ai.hank.jacked" in fake_plist.read_text()
        assert "--host" not in fake_plist.read_text()

    def test_returns_false_when_install_autostart_cannot_find_jacked(
        self, tmp_path, monkeypatch,
    ):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)
        monkeypatch.setattr("jacked.findbin.find_bin", lambda name: None)
        monkeypatch.setattr(
            "jacked.service.process.stop_process_graceful",
            lambda *a, **kw: {"was_running": False, "died": False, "killed": False},
        )

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is False
        assert state == "unavailable"
        assert "jacked" in reason.lower() and "binary" in reason.lower()


class TestLinux:
    def test_returns_already_installed_when_unit_exists(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "linux")

        fake_unit = tmp_path / "jacked.service"
        fake_unit.write_text("[Unit]\n")
        monkeypatch.setattr(plat, "_get_systemd_user_unit_path", lambda: fake_unit)

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is True
        assert state == "already_installed"

    def test_returns_unavailable_when_unit_missing(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "linux")

        unit_path = tmp_path / "jacked.service"
        monkeypatch.setattr(plat, "_get_systemd_user_unit_path", lambda: unit_path)

        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is False
        assert state == "unavailable"
        assert "systemd" in reason.lower() or "unit" in reason.lower()


class TestWindows:
    def test_windows_returns_unavailable(self, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "win32")
        ok, state, reason = plat.ensure_native_lifecycle()
        assert ok is False
        assert state == "unavailable"
        assert "no native lifecycle" in reason.lower()

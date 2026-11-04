"""Tests for jacked.upgrade_guard: no upgrade path installs an older version (#295i)."""

from unittest.mock import patch

import pytest


class TestDowngradeRefusal:
    def test_the_295i_case_is_refused_with_the_exact_message(self):
        from jacked.upgrade_guard import downgrade_refusal

        assert downgrade_refusal("0.111.2", "0.111.1") == (
            "Nothing to upgrade. The package index serves v0.111.1. "
            "The installed version is v0.111.2."
        )

    @pytest.mark.parametrize(
        ("installed", "installable"),
        [
            ("0.111.2", "0.111.2"),  # same version: today's reinstall
            ("0.111.1", "0.111.2"),  # a real upgrade
            ("0.111.2", None),  # the index did not answer
            ("", "0.111.1"),  # the installed version is not known
        ],
    )
    def test_an_install_that_does_not_go_back_is_allowed(self, installed, installable):
        from jacked.upgrade_guard import downgrade_refusal

        assert downgrade_refusal(installed, installable) is None

    def test_a_local_build_is_newer_than_the_same_public_release(self):
        from jacked.upgrade_guard import downgrade_refusal

        assert downgrade_refusal("0.111.2+local", "0.111.2") is not None

    def test_the_message_has_no_em_dash(self):
        from jacked.upgrade_guard import downgrade_refusal

        assert "\u2014" not in downgrade_refusal("1.0.0", "0.9.0")


class TestGuardedUpgradeCommand:
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    def test_uv_requirement_carries_the_installed_version_as_a_floor(self, _method):
        from jacked.upgrade_guard import guarded_upgrade_command, guarded_upgrade_command_label

        assert guarded_upgrade_command("tray", "0.111.2") == [
            "uv", "tool", "install", "claude-jacked[tray]>=0.111.2", "--force", "--refresh",
        ]
        assert guarded_upgrade_command_label("tray", "0.111.2") == (
            'uv tool install "claude-jacked[tray]>=0.111.2" --force --refresh'
        )

    @patch("jacked.install_method.detect_install_method", return_value="pipx")
    def test_pipx_requirement_carries_the_floor_too(self, _method):
        from jacked.upgrade_guard import guarded_upgrade_command, guarded_upgrade_command_label

        assert guarded_upgrade_command("tray", "0.111.2") == [
            "pipx", "install", "claude-jacked[tray]>=0.111.2", "--force",
        ]
        assert guarded_upgrade_command_label("tray", "0.111.2") == (
            'pipx install "claude-jacked[tray]>=0.111.2" --force'
        )

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    def test_a_local_label_is_dropped_from_the_floor(self, _method):
        from jacked.upgrade_guard import guarded_upgrade_command

        assert "claude-jacked[tray]>=0.111.2" in guarded_upgrade_command("tray", "0.111.2+abc")

    @pytest.mark.parametrize("installed", ["", None, "garbage", "1!0.1"])
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    def test_an_unknown_installed_version_keeps_the_plain_command(self, _method, installed):
        from jacked.install_method import upgrade_command, upgrade_command_label
        from jacked.upgrade_guard import guarded_upgrade_command, guarded_upgrade_command_label

        assert guarded_upgrade_command("tray", installed) == upgrade_command("tray")
        assert guarded_upgrade_command_label("tray", installed) == upgrade_command_label("tray")

    @patch("jacked.install_method.detect_install_method", return_value="pip")
    def test_a_method_that_cannot_upgrade_still_raises(self, _method):
        from jacked.upgrade_guard import guarded_upgrade_command, guarded_upgrade_command_label

        with pytest.raises(ValueError):
            guarded_upgrade_command("tray", "0.111.2")
        with pytest.raises(ValueError):
            guarded_upgrade_command_label("tray", "0.111.2")

    @patch("jacked.install_method.detect_install_method", return_value="uv")
    def test_a_command_with_no_requirement_fails_loud(self, _method):
        """The floor must never vanish in silence when the command shape drifts."""
        from jacked.upgrade_guard import guarded_upgrade_command

        with patch("jacked.install_method.upgrade_command", return_value=["uv", "tool", "upgrade"]):
            with pytest.raises(ValueError, match="version floor"):
                guarded_upgrade_command("tray", "0.111.2")


class TestPreinstallCheck:
    """The REAL probe (the autouse stub is bypassed): only the two index
    probes are replaced, so no test reaches PyPI."""

    @staticmethod
    def _check(installed, json_api, simple_index):
        from jacked import upgrade_guard

        with (
            patch.object(upgrade_guard, "probe_index_versions",
                         upgrade_guard._probe_index_versions_impl),
            patch("jacked.version_check.get_latest_pypi_version", return_value=json_api),
            patch("jacked.version_check.get_latest_from_simple_index",
                  return_value=simple_index),
        ):
            return upgrade_guard.preinstall_check(installed)

    def test_pin_295i_the_guard_reads_what_uv_installs_not_the_json_api(self):
        """2026-11-04: the JSON API already named 0.111.2, the simple index
        still served 0.111.1, and uv installed 0.111.1 over 0.111.2."""
        versions, refusal = self._check("0.111.2", json_api="0.111.2", simple_index="0.111.1")
        assert versions.target == "0.111.2"
        assert versions.installable == "0.111.1"
        assert refusal == (
            "Nothing to upgrade. The package index serves v0.111.1. "
            "The installed version is v0.111.2."
        )

    def test_a_newer_simple_index_is_an_upgrade(self):
        versions, refusal = self._check("0.111.1", json_api="0.111.1", simple_index="0.111.2")
        assert versions.installable == "0.111.2"
        assert refusal is None

    def test_an_offline_index_never_blocks_the_install(self):
        versions, refusal = self._check("0.111.2", json_api=None, simple_index=None)
        assert versions.target is None and versions.installable is None
        assert refusal is None

    def test_an_unsafe_simple_index_answer_is_no_answer(self):
        versions, refusal = self._check("0.111.2", None, '1.0" & del C:\\x & "')
        assert versions.installable is None
        assert refusal is None


def test_the_autouse_stub_keeps_every_test_off_pypi():
    from jacked import upgrade_guard

    assert upgrade_guard.probe_index_versions() == upgrade_guard.IndexVersions(None, None)

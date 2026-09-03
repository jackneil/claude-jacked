"""Compatibility adapters route through exact lifecycle ownership APIs."""

from unittest.mock import MagicMock, patch


def test_native_restart_uses_exact_lifecycle_contract():
    from jacked.service import platform as plat

    spec = MagicMock()
    with (
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(spec, {"PATH": "safe"}),
        ),
        patch(
            "jacked.service.lifecycle.native_artifact_path", return_value="artifact"
        ) as artifact,
        patch(
            "jacked.service.lifecycle.restart_native_owned",
            return_value=MagicMock(ok=True, reason="exact"),
        ) as restart,
    ):
        assert plat.native_restart() == (True, "exact")
    artifact.assert_called_once_with(spec)
    restart.assert_called_once_with(spec, "artifact", environment={"PATH": "safe"})


class TestUpdaterUsesNativeRestart:
    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(True, "already_installed", "plist installed"),
    )
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch(
        "jacked.service.platform.native_restart",
        return_value=(True, "launchctl kickstart"),
    )
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_starting_service_uses_native_when_available(
        self,
        mock_popen,
        mock_run,
        mock_find,
        mock_port_avail,
        mock_native,
        mock_gate,
        mock_method,
        mock_ensure,
        tmp_path,
        monkeypatch,
    ):
        """When native_restart succeeds, the updater does NOT spawn its own
        detached `jacked service start`. That's the whole point — no race."""
        from jacked.service import updater, update_status as us_mod

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(
                parent_pid=12345, extras="tray", target_version="0.41.22"
            )

        # native_restart was called
        mock_native.assert_called()
        # NO detached Popen for `jacked service start` — launchd is handling it
        for call in mock_popen.call_args_list:
            argv = call[0][0]
            if isinstance(argv, list):
                cmd_str = " ".join(str(a) for a in argv)
                assert "service start" not in cmd_str, (
                    f"expected no detached service start when native restart succeeded, got: {cmd_str}"
                )

    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(False, "unavailable", "no native lifecycle manager"),
    )
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.platform.native_restart", return_value=(False, "no plist"))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_starting_service_falls_back_when_no_native(
        self,
        mock_popen,
        mock_run,
        mock_find,
        mock_port_avail,
        mock_native,
        mock_gate,
        mock_method,
        mock_ensure,
        tmp_path,
        monkeypatch,
    ):
        """When no native manager, updater uses its detached spawn (Windows
        and unmanaged-Linux behavior)."""
        from jacked.service import updater, update_status as us_mod

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(
                parent_pid=12345, extras="tray", target_version="0.41.22"
            )

        # Detached Popen for `jacked service start` fired (fallback path)
        spawn_calls = [
            c
            for c in mock_popen.call_args_list
            if isinstance(c[0][0], list)
            and "service" in " ".join(str(a) for a in c[0][0])
            and "start" in " ".join(str(a) for a in c[0][0])
        ]
        assert len(spawn_calls) == 1

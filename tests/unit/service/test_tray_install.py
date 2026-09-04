"""Tests for tray auto-install during `jacked install` (and `jacked service install`)."""

import builtins
import sys
from unittest import mock


# ---------------------------------------------------------------------------
# _tray_extra_installed
# ---------------------------------------------------------------------------


class TestTrayExtraInstalled:
    def test_true_when_pystray_importable(self, monkeypatch):
        import jacked.cli as cli

        monkeypatch.setitem(sys.modules, "pystray", mock.MagicMock())
        assert cli._tray_extra_installed() is True

    def test_false_when_pystray_missing(self, monkeypatch):
        import jacked.cli as cli

        monkeypatch.delitem(sys.modules, "pystray", raising=False)
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "pystray":
                raise ImportError("no pystray here")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert cli._tray_extra_installed() is False


# ---------------------------------------------------------------------------
# _setup_tray_autostart
# ---------------------------------------------------------------------------


class TestSetupTrayAutostart:
    def test_noop_without_tray_extra(self, monkeypatch):
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_tray_extra_installed", lambda: False)
        calls = []
        monkeypatch.setattr(
            cli, "_ensure_autostart_and_running", lambda *a, **k: calls.append(a)
        )
        cli._setup_tray_autostart()
        assert calls == []  # base/headless install -> nothing happens

    def test_registers_and_starts_with_tray_extra(self, monkeypatch):
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_tray_extra_installed", lambda: True)
        # Pin the display gate too - on a headless runner (Linux CI, no
        # $DISPLAY) the real _is_headless() short-circuits this branch.
        monkeypatch.setattr(cli, "_is_headless", lambda: False)
        captured = {}

        def fake_ensure(port, *, one_shot_host=None, label="Service"):
            captured.update(port=port, one_shot_host=one_shot_host, label=label)

        monkeypatch.setattr(cli, "_ensure_autostart_and_running", fake_ensure)
        cli._setup_tray_autostart()
        assert captured["label"] == "Tray"
        assert captured["port"]  # DEFAULT_PORT threaded through
        # No host is ever passed: the artifact is host-free and the bind is
        # resolved from the settings DB at boot.
        assert captured["one_shot_host"] is None

    def test_skips_on_headless_environment(self, monkeypatch):
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_tray_extra_installed", lambda: True)
        monkeypatch.setattr(cli, "_is_headless", lambda: True)
        calls = []
        monkeypatch.setattr(
            cli, "_ensure_autostart_and_running", lambda *a, **k: calls.append(a)
        )
        cli._setup_tray_autostart()
        assert calls == []  # headless -> no autostart, no tray spawn


# ---------------------------------------------------------------------------
# _ensure_autostart_and_running
# ---------------------------------------------------------------------------


def _patch_autostart(monkeypatch, *, result, pid=None, alive=True, platform="win32"):
    import jacked.cli as cli
    from jacked.service.spec import SupervisorKind

    spec = mock.MagicMock(supervisor=SupervisorKind.TASK_SCHEDULER)
    monkeypatch.setattr(
        "jacked.service.lifecycle.provision_service_contract", lambda **k: (spec, {})
    )
    monkeypatch.setattr(
        "jacked.service.lifecycle.install_native_owned",
        lambda *a, **k: mock.MagicMock(
            ok=not result.startswith("Could"), reason=result
        ),
    )
    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(
        cli,
        "_wait_owned_service_ready",
        lambda *_a, **_k: {"state": "running", "port": 8321},
    )
    spawned = []
    monkeypatch.setattr(
        cli, "_spawn_service_detached", lambda h, p: spawned.append((h, p))
    )
    return cli, spawned


class TestEnsureAutostartAndRunning:
    def test_verified_manual_instance_hands_off_before_native_activation(
        self, monkeypatch, tmp_path
    ):
        import jacked.cli as cli
        from jacked.service.spec import SupervisorKind
        from jacked.service.supervisors import ArtifactDisposition, ArtifactInspection

        manifest = tmp_path / "manifest"
        manifest.write_text("owned")
        paths = mock.MagicMock(manifest=manifest)
        spec = mock.MagicMock(supervisor=SupervisorKind.SYSTEMD_USER)
        order = []
        monkeypatch.setattr(
            "jacked.service.lifecycle.default_service_paths", lambda: paths
        )
        monkeypatch.setattr(
            "jacked.service.lifecycle.provision_service_contract",
            lambda **k: (spec, {}),
        )
        monkeypatch.setattr(
            "jacked.service.lifecycle.discover_service",
            lambda _p: mock.MagicMock(source="manifest"),
        )
        monkeypatch.setattr(
            "jacked.service.instance.read_manifest",
            lambda _p: mock.MagicMock(supervisor="manual"),
        )
        monkeypatch.setattr(
            "jacked.service.lifecycle.inspect_native_artifact",
            lambda *a, **k: (
                order.append("inspect")
                or mock.MagicMock(
                    artifact=ArtifactInspection(ArtifactDisposition.MATCHING)
                )
            ),
        )
        monkeypatch.setattr(
            "jacked.service.lifecycle.native_artifact_path",
            lambda *a, **k: tmp_path / "unit",
        )

        def shutdown(*_a, **_k):
            order.append("shutdown")
            manifest.unlink()
            return {"ok": True}

        monkeypatch.setattr("jacked.service.ipc.send_native_control", shutdown)
        monkeypatch.setattr(
            "jacked.service.lifecycle.install_native_owned",
            lambda *a, **k: (
                order.append("activate") or mock.MagicMock(ok=True, reason="ok")
            ),
        )
        monkeypatch.setattr(
            cli,
            "_wait_owned_service_ready",
            lambda *_a, **_k: {"state": "running", "port": 8321},
        )

        cli._ensure_autostart_and_running(8321)
        assert order == ["inspect", "shutdown", "activate"]

    def test_never_starts_if_autostart_registration_failed(self, monkeypatch):
        cli, spawned = _patch_autostart(
            monkeypatch, result="Could not find 'jacked' binary on PATH."
        )
        cli._ensure_autostart_and_running(8321)
        assert spawned == []

    def test_skips_spawn_when_already_running(self, monkeypatch):
        cli, spawned = _patch_autostart(
            monkeypatch,
            result="Installed startup script: x",
            pid={"pid": 999, "port": 8321},
            alive=True,
        )
        cli._ensure_autostart_and_running(8321)
        assert spawned == []  # no double-start

    def test_spawns_hostfree_when_stopped_on_windows(self, monkeypatch):
        cli, spawned = _patch_autostart(
            monkeypatch,
            result="Installed startup script: x",
            pid=None,
            platform="win32",
        )
        cli._ensure_autostart_and_running(8321, label="Tray")
        # No one-shot host -> the spawn passes None so the child resolves its
        # bind from the settings DB.
        assert spawned == []

    def test_one_shot_host_passes_through_to_spawn(self, monkeypatch):
        """An unmapped `service install --host X` stays a one-shot for the
        immediate spawn only — never baked into the artifact."""
        cli, spawned = _patch_autostart(
            monkeypatch,
            result="Installed startup script: x",
            pid=None,
            platform="win32",
        )
        cli._ensure_autostart_and_running(8321, one_shot_host="192.168.1.5")
        assert spawned == []

    def test_no_self_spawn_on_macos(self, monkeypatch):
        # launchd already started it via install_autostart's bootstrap
        cli, spawned = _patch_autostart(
            monkeypatch,
            result="Installed and started launchd agent",
            pid=None,
            platform="darwin",
        )
        cli._ensure_autostart_and_running(8321)
        assert spawned == []

    def test_spawn_failure_is_non_fatal(self, monkeypatch):
        import jacked.cli as cli
        from jacked.service.spec import SupervisorKind

        spec = mock.MagicMock(supervisor=SupervisorKind.MANUAL)
        monkeypatch.setattr(
            "jacked.service.lifecycle.provision_service_contract",
            lambda **k: (spec, {}),
        )

        def boom(*a, **k):
            raise OSError("spawn blew up")

        monkeypatch.setattr("jacked.service.lifecycle.spawn_exact_service", boom)
        # Must not propagate — install should never die over the tray.
        cli._ensure_autostart_and_running(8321)

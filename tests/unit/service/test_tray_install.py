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
# `jacked install` end to end: each tray branch says what it did
# ---------------------------------------------------------------------------

_HEADLESS_NOTICE = (
    "[-] No display (DISPLAY and WAYLAND_DISPLAY are not set). Skipping the "
    "tray icon: the tray service needs a display. To use the dashboard here, "
    "run `jacked webux --no-browser`."
)
_BACKEND_NOTICE = (
    "[WARN] Skipping the tray icon: pystray could not load (ValueError: "
    "Namespace AyatanaAppIndicator3 not available). See \"Linux tray notes\" "
    "in the jacked README: "
    "https://github.com/jackneil/claude-jacked#linux-tray-notes"
)
_MISSING_NOTICE = (
    "[WARN] Skipping the tray icon: jacked could not import pystray. "
    "Reinstall jacked with: uv tool install claude-jacked --force"
)


def _invoke_with_tray_probes(monkeypatch, tmp_path, argv, *, headless, importable, failure):
    """Run a jacked command with the tray probes fixed.

    Return the exit code, the output (ANSI codes removed, whitespace runs
    joined, so a console line wrap cannot split a phrase), the calls that
    would start the service, and the calls that would write the Remote
    access setting. ``failure`` is the backend error that the tray module
    recorded when pystray could not load. The conftest guard keeps the real
    Claude Code CLI out of reach.
    """
    import re

    from click.testing import CliRunner

    import jacked.cli as cli
    from jacked.service import tray

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setattr(cli, "_is_headless", lambda: headless)
    monkeypatch.setattr(cli, "_tray_extra_installed", lambda: importable)
    monkeypatch.setattr(tray, "_PYSTRAY_LOAD_FAILURE", failure)
    starts, settings = [], []
    monkeypatch.setattr(
        cli,
        "_ensure_autostart_and_running",
        lambda port, **kw: starts.append((port, kw)) or True,  # the service is running
    )
    monkeypatch.setattr(
        cli,
        "_persist_remote_access_from_host",
        lambda *args, **kw: settings.append((args, kw)),
    )

    result = CliRunner().invoke(cli.main, argv)

    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    return result.exit_code, " ".join(plain.split()), starts, settings


def _install_with_tray_probes(monkeypatch, tmp_path, **probes):
    """Run `jacked install` with the tray probes fixed. See _invoke_with_tray_probes."""
    code, output, starts, _settings = _invoke_with_tray_probes(
        monkeypatch, tmp_path, ["install", "--no-rules", "--no-packs"], **probes
    )
    assert code == 0, output
    return output, starts


class TestInstallTrayNotices:
    """The tray service needs a display: pystray opens the display when it
    loads. So `jacked install` checks for a display first, and each branch
    that skips the tray prints why."""

    def test_a_headless_box_recommends_webux(self, monkeypatch, tmp_path):
        # With no display, pystray cannot load either (Xlib raises
        # DisplayNameError), so the import probe fails too. The display
        # check must answer first, or the tray is skipped for the wrong
        # reason.
        output, starts = _install_with_tray_probes(
            monkeypatch, tmp_path, headless=True, importable=False,
            failure="DisplayNameError: Can't connect to display \":0\"",
        )

        assert _HEADLESS_NOTICE in output, output
        assert "Skipping the tray icon: pystray" not in output, output
        assert "could not import pystray" not in output, output
        assert starts == []

    def test_a_backend_error_is_named_with_the_tray_notes(self, monkeypatch, tmp_path):
        output, starts = _install_with_tray_probes(
            monkeypatch, tmp_path, headless=False, importable=False,
            failure="ValueError: Namespace AyatanaAppIndicator3 not available",
        )

        assert _BACKEND_NOTICE in output, output
        assert "No display" not in output, output
        assert starts == []

    def test_a_backend_error_with_brackets_is_printed_as_it_is(
        self, monkeypatch, tmp_path
    ):
        # The error text comes from an exception message. Unescaped, "[/...]"
        # raises rich.errors.MarkupError and ends `jacked install`.
        output, starts = _install_with_tray_probes(
            monkeypatch, tmp_path, headless=False, importable=False,
            failure="ValueError: no typelib in [/usr/lib/girepository-1.0]",
        )

        assert (
            "pystray could not load (ValueError: no typelib in "
            "[/usr/lib/girepository-1.0])." in output
        ), output
        assert starts == []

    def test_a_missing_pystray_package_says_to_reinstall(self, monkeypatch, tmp_path):
        output, starts = _install_with_tray_probes(
            monkeypatch, tmp_path, headless=False, importable=False, failure=None,
        )

        assert _MISSING_NOTICE in output, output
        assert "No display" not in output, output
        assert starts == []

    def test_a_box_with_a_display_starts_the_tray_as_before(self, monkeypatch, tmp_path):
        from jacked.service import DEFAULT_PORT

        output, starts = _install_with_tray_probes(
            monkeypatch, tmp_path, headless=False, importable=True, failure=None,
        )

        assert starts == [(DEFAULT_PORT, {"label": "Tray"})]
        assert "Skipping the tray icon" not in output, output
        assert "No display" not in output, output


class TestServiceInstallTrayCheck:
    """`jacked service install` makes the same check as `jacked install`
    before it registers autostart, with the same lines. The service is the
    command's only job, so a refusal exits 1, and it writes no setting."""

    ARGV = ["service", "install", "--host", "0.0.0.0"]

    def test_a_headless_box_is_refused_with_the_webux_line(self, monkeypatch, tmp_path):
        code, output, starts, settings = _invoke_with_tray_probes(
            monkeypatch, tmp_path, self.ARGV, headless=True, importable=False,
            failure="DisplayNameError: Can't connect to display \":0\"",
        )

        assert code == 1, output
        assert _HEADLESS_NOTICE in output, output
        assert "Skipping the tray icon: pystray" not in output, output
        assert (starts, settings) == ([], [])

    def test_a_backend_error_is_refused_with_the_tray_notes(self, monkeypatch, tmp_path):
        code, output, starts, settings = _invoke_with_tray_probes(
            monkeypatch, tmp_path, self.ARGV, headless=False, importable=False,
            failure="ValueError: Namespace AyatanaAppIndicator3 not available",
        )

        assert code == 1, output
        assert _BACKEND_NOTICE in output, output
        assert (starts, settings) == ([], [])

    def test_a_missing_pystray_package_is_refused_with_the_reinstall_line(
        self, monkeypatch, tmp_path
    ):
        code, output, starts, settings = _invoke_with_tray_probes(
            monkeypatch, tmp_path, self.ARGV, headless=False, importable=False,
            failure=None,
        )

        assert code == 1, output
        assert _MISSING_NOTICE in output, output
        assert (starts, settings) == ([], [])

    def test_a_box_with_a_display_installs_the_service_as_before(
        self, monkeypatch, tmp_path
    ):
        from jacked.service import DEFAULT_PORT

        code, output, starts, settings = _invoke_with_tray_probes(
            monkeypatch, tmp_path, self.ARGV, headless=False, importable=True,
            failure=None,
        )

        assert code == 0, output
        assert starts == [(DEFAULT_PORT, {"label": "Service"})]
        assert settings == [(("0.0.0.0",), {"allow_one_shot": False})]
        assert "Skipping the tray icon" not in output, output


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
        # A live owner: install applies the shared liveness rule (#161i).
        monkeypatch.setattr(
            "jacked.service.instance.read_manifest",
            lambda _p: mock.MagicMock(supervisor="manual"),
        )
        monkeypatch.setattr(
            "jacked.service.instance.manifest_is_proven_stale", lambda _p: False
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


# ---------------------------------------------------------------------------
# _ensure_autostart_and_running: legacy vs foreign artifacts
# ---------------------------------------------------------------------------


def _legacy_launchd_plist(path):
    """Write the exact pre-v2 launchd definition jacked used to install."""
    import plistlib

    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hank.jacked",
                "ProgramArguments": [
                    "/Users/test/.local/bin/jacked",
                    "service",
                    "start",
                    "--port",
                    "8321",
                ],
                "RunAtLoad": True,
            }
        )
    )
    path.chmod(0o600)
    return path


def _patch_manual_owner(monkeypatch, tmp_path, artifact_path):
    """Wire a live manual instance whose artifact inspects as FOREIGN."""
    import jacked.cli as cli
    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors import ArtifactDisposition, ArtifactInspection

    manifest = tmp_path / "manifest"
    manifest.write_text("owned")
    paths = mock.MagicMock(manifest=manifest)
    spec = mock.MagicMock(
        supervisor=SupervisorKind.LAUNCHD, service_id="ai.hank.jacked"
    )
    order = []
    monkeypatch.setattr("jacked.service.lifecycle.default_service_paths", lambda: paths)
    monkeypatch.setattr(
        "jacked.service.lifecycle.provision_service_contract", lambda **k: (spec, {})
    )
    # A live owner: install applies the shared liveness rule (#161i).
    monkeypatch.setattr(
        "jacked.service.instance.read_manifest",
        lambda _p: mock.MagicMock(supervisor="manual"),
    )
    monkeypatch.setattr(
        "jacked.service.instance.manifest_is_proven_stale", lambda _p: False
    )
    monkeypatch.setattr(
        "jacked.service.lifecycle.inspect_native_artifact",
        lambda *a, **k: mock.MagicMock(
            artifact=ArtifactInspection(ArtifactDisposition.FOREIGN, "unowned")
        ),
    )
    monkeypatch.setattr(
        "jacked.service.lifecycle.native_artifact_path", lambda *a, **k: artifact_path
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
    return cli, order


class TestEnsureAutostartLegacyHandoff:
    def test_known_legacy_launchd_artifact_is_replaced_not_refused(
        self, monkeypatch, tmp_path, capsys
    ):
        artifact = _legacy_launchd_plist(tmp_path / "ai.hank.jacked.plist")
        cli, order = _patch_manual_owner(monkeypatch, tmp_path, artifact)

        cli._ensure_autostart_and_running(8321)

        assert order == ["shutdown", "activate"]
        assert "foreign" not in capsys.readouterr().out.lower()

    def test_truly_foreign_artifact_still_refuses(
        self, monkeypatch, tmp_path, capsys
    ):
        import plistlib

        artifact = tmp_path / "ai.hank.jacked.plist"
        artifact.write_bytes(
            plistlib.dumps(
                {
                    "Label": "ai.hank.jacked",
                    "ProgramArguments": ["/usr/bin/curl", "https://example.invalid"],
                }
            )
        )
        artifact.chmod(0o600)
        cli, order = _patch_manual_owner(monkeypatch, tmp_path, artifact)

        cli._ensure_autostart_and_running(8321)

        assert order == []  # nothing shut down, nothing activated
        output = capsys.readouterr().out.lower()
        assert "foreign" in output
        assert "service recover" in output

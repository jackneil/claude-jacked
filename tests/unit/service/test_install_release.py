"""Tests for jacked.service.install_release and `jacked _release_install`.

The Windows update batches run this step with the OLD build, before the
package step. A live service locks the tool environment, so uv cannot replace
it. These tests use fakes only: no real process, socket, or control channel.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from jacked.service import install_release as ir
from jacked.service import updater
from jacked.service.instance_models import ServicePaths

# The real listener identification, before World replaces it.
_REAL_IDENTIFY = ir._identify_listener


class FakeClock:
    """A monotonic clock that advances only when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


def _manifest(instance_id: str = "inst-1", pid: int = 4242, port: int = 8321):
    return SimpleNamespace(
        instance_id=instance_id,
        process=SimpleNamespace(pid=pid),
        bind=SimpleNamespace(port=port),
    )


class World:
    """Fake service state that the module reads through its helpers."""

    def __init__(self, tmp_path, monkeypatch, *, manifest=None):
        self.paths = ServicePaths.in_directory(tmp_path)
        self.manifest = manifest
        self.dead_pids: set[int] = set()
        self.port_free = True
        self.legacy = None
        self.stop_calls = 0
        self.stop_response = {"ok": True, "result": {"accepted": True}}
        self.stop_error: Exception | None = None
        # Called after each accepted stop, to model the service exiting.
        self.on_stop = None
        # Called when a stop request arrives, before any stop error.
        self.on_send = None
        self._write()
        monkeypatch.setattr(ir, "read_manifest", self._read_manifest)
        monkeypatch.setattr(ir, "process_is_stale", self._is_stale)
        monkeypatch.setattr(ir, "send_native_control", self._send)
        monkeypatch.setattr(
            ir, "resolve_active_legacy_service", lambda _path: self.legacy
        )
        self.probed_ports: list[int] = []
        monkeypatch.setattr(ir, "is_port_available", self._port_available)
        # What /api/health says about a listener on a port that is in use.
        # The default is a jacked service, so a held port stays held.
        self.listener = updater.LISTENER_JACKED
        self.identified_ports: list[int] = []
        monkeypatch.setattr(ir, "_identify_listener", self._identify_listener)
        # Live processes for --wait-process, by PID: their creation identity.
        self.live: dict[int, str] = {}
        self.identity_probe_error: Exception | None = None
        monkeypatch.setattr(ir, "process_identity", self._identity)

    def _write(self) -> None:
        if self.manifest is None:
            self.paths.manifest.unlink(missing_ok=True)
        else:
            self.paths.manifest.write_text("{}", encoding="utf-8")

    def set_manifest(self, manifest) -> None:
        self.manifest = manifest
        self._write()

    def _read_manifest(self, path):
        assert path == self.paths.manifest
        if self.manifest is None:
            raise FileNotFoundError(path)
        if isinstance(self.manifest, Exception):
            raise self.manifest
        return self.manifest

    def _port_available(self, _host, port) -> bool:
        self.probed_ports.append(port)
        return self.port_free

    def _identify_listener(self, port) -> str:
        self.identified_ports.append(port)
        return self.listener

    def _identity(self, pid):
        if self.identity_probe_error is not None:
            raise self.identity_probe_error
        if pid not in self.live:
            raise ProcessLookupError(pid)
        return SimpleNamespace(pid=pid, creation_id=self.live[pid])

    def _is_stale(self, process) -> bool:
        return process is not None and process.pid in self.dead_pids

    def _send(self, path, action):
        assert path == self.paths.manifest
        assert action is ir.ControlAction.SHUTDOWN
        self.stop_calls += 1
        if self.on_send is not None:
            self.on_send()
        if self.stop_error is not None:
            raise self.stop_error
        if self.stop_response.get("ok") and self.on_stop is not None:
            self.on_stop()
        return self.stop_response

    def release(self, **kwargs):
        clock = FakeClock()
        kwargs.setdefault("timeout", 5)
        result = ir.release_install(
            self.paths, clock=clock, sleep=clock.sleep, **kwargs
        )
        return result, clock


def _assert_values_free(message: str) -> None:
    assert "4242" not in message
    assert "8321" not in message
    assert "\\" not in message and "/" not in message


class TestNothingHoldsTheInstall:
    def test_no_manifest_and_no_legacy_service_is_released(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch)
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_RELEASED
        assert result.released
        assert world.stop_calls == 0
        assert "No jacked service is running" in result.message

    def test_a_stale_manifest_with_a_free_port_is_released_without_a_stop(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.dead_pids.add(4242)
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_RELEASED
        assert world.stop_calls == 0

    def test_a_live_legacy_service_is_refused_and_never_signalled(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch)
        world.legacy = object()
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert world.stop_calls == 0
        assert "legacy" in result.message

    def test_an_invalid_manifest_is_refused_before_any_stop(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=ValueError("bad"))
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert world.stop_calls == 0
        assert "ValueError" in result.message
        assert "bad" not in result.message


class TestStopAndWait:
    def test_a_running_service_is_stopped_then_awaited(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.port_free = False
        polls = {"n": 0}
        real_stale = world._is_stale

        def exits_after_two_polls(process):
            # The service exits, and frees its port, two polls after the stop.
            if world.stop_calls:
                polls["n"] += 1
                if polls["n"] >= 3:
                    world.dead_pids.add(4242)
                    world.port_free = True
            return real_stale(process)

        monkeypatch.setattr(ir, "process_is_stale", exits_after_two_polls)
        result, clock = world.release()
        assert world.stop_calls == 1
        assert result.exit_code == ir.EXIT_RELEASED
        assert clock.sleeps >= 1, "the command returned before the service exited"
        assert "exited" in result.message

    def test_a_dead_process_that_still_holds_the_port_is_awaited(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.on_stop = lambda: world.dead_pids.add(4242)
        world.port_free = False
        result, _clock = world.release(timeout=2)
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert "port" in result.message
        assert "Nothing was installed" in result.message
        _assert_values_free(result.message)

    def test_a_service_that_never_exits_times_out_with_exit_3(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        result, clock = world.release(timeout=3)
        assert world.stop_calls == 1
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert not result.released
        assert clock.now >= 3, "the wait ended before the time limit"
        assert "did not exit" in result.message
        assert "3 seconds" in result.message
        _assert_values_free(result.message)

    def test_a_different_live_owner_keeps_the_install_held(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())

        def replaced():
            world.dead_pids.add(4242)
            world.set_manifest(_manifest(instance_id="inst-2", pid=5151))

        world.on_stop = replaced
        result, _clock = world.release(timeout=1)
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert "different jacked service" in result.message

    def test_the_owner_removing_its_manifest_counts_as_released(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())

        def gone():
            world.dead_pids.add(4242)
            world.set_manifest(None)

        world.on_stop = gone
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_RELEASED

    def test_a_wait_that_raises_still_returns_an_exit_code(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.on_stop = lambda: world.dead_pids.add(4242)

        def boom(_host, _port):
            raise RuntimeError("probe broke")

        monkeypatch.setattr(ir, "is_port_available", boom)
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert "RuntimeError" in result.message
        assert "probe broke" not in result.message


class TestRefusedStop:
    def test_a_rejected_stop_is_exit_1_and_waits_for_nothing(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.stop_response = {"ok": False, "error": "PermissionError"}
        result, clock = world.release()
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert "rejected" in result.message
        assert clock.sleeps == 0

    def test_an_unreachable_control_channel_is_exit_1(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.stop_error = OSError("pipe busy")
        result, _clock = world.release()
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert "OSError" in result.message
        assert "pipe busy" not in result.message

    def test_a_service_that_exited_before_the_request_is_not_a_refusal(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        calls = {"n": 0}

        def died_meanwhile(_path, _action):
            calls["n"] += 1
            world.dead_pids.add(4242)
            raise OSError("no pipe")

        monkeypatch.setattr(ir, "send_native_control", died_meanwhile)
        result, _clock = world.release()
        assert calls["n"] == 1
        assert result.exit_code == ir.EXIT_RELEASED


class TestCheckOnly:
    def test_check_only_refuses_a_live_service_without_a_stop(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        result, _clock = world.release(stop=False)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert world.stop_calls == 0

    def test_check_only_timeout_never_claims_a_stop(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.dead_pids.add(4242)
        world.port_free = False
        result, _clock = world.release(stop=False, timeout=1)
        # Nothing was stopped, so the batch must not restart anything.
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert world.stop_calls == 0


class TestReleaseInstallCommand:
    """`jacked _release_install` maps each outcome to the batch exit code."""

    @staticmethod
    def _invoke(monkeypatch, tmp_path, *args):
        from jacked.cli import main
        from jacked.service import lifecycle

        monkeypatch.setattr(
            lifecycle,
            "default_service_paths",
            lambda: ServicePaths.in_directory(tmp_path),
        )
        return CliRunner().invoke(main, ["_release_install", *args])

    def test_the_command_is_hidden(self):
        from jacked.cli import main

        assert main.commands["_release_install"].hidden

    def test_not_running_exits_0(self, tmp_path, monkeypatch):
        World(tmp_path, monkeypatch)
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert "[release-install]" in result.output
        assert "No jacked service is running" in result.output

    def test_running_service_is_stopped_and_awaited(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.on_stop = lambda: world.dead_pids.add(4242)
        result = self._invoke(monkeypatch, tmp_path, "--timeout", "5")
        assert result.exit_code == 0, result.output
        assert world.stop_calls == 1
        assert "exited" in result.output

    def test_timeout_exits_3(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        result = self._invoke(monkeypatch, tmp_path, "--timeout", "0")
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED, result.output
        assert world.stop_calls == 1
        assert "Nothing was installed" in result.output

    def test_refused_stop_exits_1(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.stop_response = {"ok": False, "error": "PermissionError"}
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == ir.EXIT_NOT_STOPPED, result.output
        assert "rejected" in result.output

    def test_check_only_sends_no_stop(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        result = self._invoke(monkeypatch, tmp_path, "--check-only")
        assert result.exit_code == ir.EXIT_NOT_STOPPED, result.output
        assert world.stop_calls == 0

    def test_an_unexpected_error_exits_1_without_a_traceback(
        self, tmp_path, monkeypatch
    ):
        def explode(**_kwargs):
            raise RuntimeError("secret detail")

        monkeypatch.setattr(ir, "release_install", explode)
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert "RuntimeError" in result.output
        assert "secret detail" not in result.output

    @pytest.mark.parametrize("bad", ["-1", "soon"])
    def test_a_bad_timeout_is_a_usage_error(self, tmp_path, monkeypatch, bad):
        World(tmp_path, monkeypatch)
        result = self._invoke(monkeypatch, tmp_path, "--timeout", bad)
        assert result.exit_code == 2


TRAY = (7070, "windows-filetime:111")


class TestNoManifestIsNotProofOfRelease:
    """A missing manifest alone never releases the install (finding 1)."""

    def test_no_manifest_and_a_held_port_is_refused(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        result, clock = world.release(timeout=4, port=8999)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert world.stop_calls == 0
        assert clock.now >= 4, "the wait ended before the time limit"
        assert set(world.probed_ports) == {8999}
        assert "port is still in use" in result.message
        assert "Nothing was installed" in result.message
        assert "8999" not in result.message
        _assert_values_free(result.message)

    def test_no_manifest_waits_until_the_port_is_free(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        polls = {"n": 0}

        def frees_on_third_probe(_host, port):
            polls["n"] += 1
            world.port_free = polls["n"] >= 3
            return world.port_free

        monkeypatch.setattr(ir, "is_port_available", frees_on_third_probe)
        result, clock = world.release(timeout=5)
        assert result.exit_code == ir.EXIT_RELEASED
        assert clock.sleeps == 2

    def test_the_default_port_is_the_service_default(self, tmp_path, monkeypatch):
        from jacked.service import DEFAULT_PORT

        world = World(tmp_path, monkeypatch)
        result, _clock = world.release()
        assert result.released
        assert world.probed_ports == [DEFAULT_PORT]

    def test_a_live_manifest_that_appears_during_the_wait_is_refused(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        # The first probe sees no owner; then a new service writes a manifest.
        world.set_manifest(None)

        def appears(_host, _port):
            world.set_manifest(_manifest(instance_id="inst-9", pid=9191))
            return False

        monkeypatch.setattr(ir, "is_port_available", appears)
        result, _clock = world.release(timeout=1)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert "started from this install" in result.message


class TestTheTrayProcessIsAwaited:
    """The tray deletes its manifest before its interpreter exits."""

    def test_a_live_tray_with_no_manifest_is_refused(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.live[TRAY[0]] = TRAY[1]
        result, clock = world.release(timeout=3, wait_process=TRAY)
        # This command stopped nothing, so exit 1: the tray batch restarts
        # the previous service on any non-zero exit.
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert clock.now >= 3
        assert "did not exit" in result.message
        assert "7070" not in result.message
        _assert_values_free(result.message)

    def test_the_install_is_free_after_the_tray_exits(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.live[TRAY[0]] = TRAY[1]
        polls = {"n": 0}
        real_identity = world._identity

        def exits_after_two_polls(pid):
            polls["n"] += 1
            if polls["n"] >= 3:
                world.live.pop(TRAY[0], None)
            return real_identity(pid)

        monkeypatch.setattr(ir, "process_identity", exits_after_two_polls)
        result, clock = world.release(timeout=5, wait_process=TRAY)
        assert result.exit_code == ir.EXIT_RELEASED
        assert clock.sleeps == 2

    def test_a_reused_pid_does_not_look_like_the_tray(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        # A different process now owns the PID: other creation identity.
        world.live[TRAY[0]] = "windows-filetime:999"
        result, clock = world.release(timeout=5, wait_process=TRAY)
        assert result.exit_code == ir.EXIT_RELEASED
        assert clock.sleeps == 0

    def test_a_probe_that_times_out_proves_nothing(self, tmp_path, monkeypatch):
        import subprocess

        world = World(tmp_path, monkeypatch)
        world.identity_probe_error = subprocess.TimeoutExpired("probe", 2)
        result, _clock = world.release(timeout=1, wait_process=TRAY)
        assert result.exit_code == ir.EXIT_NOT_STOPPED

    def test_a_live_tray_also_holds_a_manifest_path_release(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.dead_pids.add(4242)
        world.live[TRAY[0]] = TRAY[1]
        result, _clock = world.release(timeout=1, wait_process=TRAY)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert "did not exit" in result.message


class TestADeletedManifestIsNotAnExit:
    """The owner deleting its manifest proves nothing while its PID lives."""

    def test_old_pid_alive_after_the_manifest_is_gone_is_not_released(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        # The stop is accepted and the manifest disappears, but the process
        # (with its original identity) keeps running.
        world.on_stop = lambda: world.set_manifest(None)
        result, clock = world.release(timeout=3)
        assert world.stop_calls == 1
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert clock.now >= 3
        assert "service process did not exit" in result.message
        _assert_values_free(result.message)


class TestAnOwnerThatIsAlreadyStopping:
    """A closed control channel means the stop is in progress (finding 3)."""

    @pytest.mark.parametrize(
        "error",
        [
            FileNotFoundError("manifest gone"),
            BrokenPipeError("pipe broke"),
            ConnectionResetError("reset"),
        ],
        ids=["no-manifest", "broken-pipe", "reset"],
    )
    def test_a_closing_channel_that_never_exits_is_exit_3(
        self, tmp_path, monkeypatch, error
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.stop_error = error
        if isinstance(error, FileNotFoundError):
            # The owner removed its manifest as it began to stop, but the
            # stopped process is still alive.
            world.on_send = lambda: world.paths.manifest.unlink()
        result, clock = world.release(timeout=2)
        assert world.stop_calls == 1
        # Exit 3, so the CLI batch restarts the service it saw stopping.
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert clock.now >= 2
        assert "already stopping" in result.message
        assert type(error).__name__ not in result.message
        _assert_values_free(result.message)

    @pytest.mark.parametrize("winerror", [109, 232, 233])
    def test_windows_pipe_closing_codes_count_as_stopping(
        self, tmp_path, monkeypatch, winerror
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        exc = OSError("pipe closing")
        exc.winerror = winerror
        world.stop_error = exc
        result, _clock = world.release(timeout=1)
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED

    def test_a_closing_channel_then_an_exit_is_released(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        world.stop_error = BrokenPipeError("pipe broke")
        polls = {"n": 0}
        real_stale = world._is_stale

        def exits_after_a_poll(process):
            if world.stop_calls:
                polls["n"] += 1
                if polls["n"] >= 2:
                    world.dead_pids.add(4242)
            return real_stale(process)

        monkeypatch.setattr(ir, "process_is_stale", exits_after_a_poll)
        result, _clock = world.release(timeout=5)
        assert result.exit_code == ir.EXIT_RELEASED

    def test_a_busy_pipe_is_still_a_refusal(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        exc = OSError("all pipe instances are busy")
        exc.winerror = 231
        world.stop_error = exc
        result, clock = world.release()
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert clock.sleeps == 0


class TestWaitProcessText:
    def test_round_trip(self):
        text = ir.format_wait_process(*TRAY)
        assert ir.parse_wait_process(text) == TRAY

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "7070",
            "x:windows-filetime:1",
            "-1:windows-filetime:1",
            "7070:",
            '7070:a"b',
            "7070:a&b",
            "7070:a b",
        ],
    )
    def test_unsafe_or_invalid_text_is_refused(self, bad):
        with pytest.raises(ValueError):
            ir.parse_wait_process(bad)


class TestReleaseInstallCommandOptions:
    _invoke = staticmethod(TestReleaseInstallCommand._invoke)

    def test_port_and_wait_process_reach_the_function(self, tmp_path, monkeypatch):
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return ir.ReleaseResult(ir.EXIT_RELEASED, "free")

        monkeypatch.setattr(ir, "release_install", capture)
        result = self._invoke(
            monkeypatch,
            tmp_path,
            "--port",
            "9123",
            "--wait-process",
            "7070:windows-filetime:111",
        )
        assert result.exit_code == 0, result.output
        assert seen["port"] == 9123
        assert seen["wait_process"] == TRAY

    def test_the_default_port_is_the_service_default(self, tmp_path, monkeypatch):
        from jacked.service import DEFAULT_PORT

        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return ir.ReleaseResult(ir.EXIT_RELEASED, "free")

        monkeypatch.setattr(ir, "release_install", capture)
        result = self._invoke(monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert seen["port"] == DEFAULT_PORT
        assert seen["wait_process"] is None

    def test_a_held_port_with_no_manifest_exits_1(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        result = self._invoke(monkeypatch, tmp_path, "--timeout", "0")
        assert result.exit_code == ir.EXIT_NOT_STOPPED, result.output
        assert "port is still in use" in result.output

    @pytest.mark.parametrize("bad", ["7070", "7070:a&b"])
    def test_a_bad_wait_process_is_a_usage_error(self, tmp_path, monkeypatch, bad):
        World(tmp_path, monkeypatch)
        result = self._invoke(monkeypatch, tmp_path, "--wait-process", bad)
        assert result.exit_code == 2
        assert "not valid" in result.output
        assert "a&b" not in result.output


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, amount=None) -> bytes:
        return self._body if amount is None else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _fake_opener(monkeypatch, answer):
    """Replace the HTTP opener of the /api/health probe. No real socket."""
    calls: list[str] = []

    class _Opener:
        def open(self, url, timeout=None):
            calls.append(url)
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(
        updater.urllib.request, "build_opener", lambda *_a: _Opener()
    )
    return calls


class TestADifferentProgramOnThePort:
    """A program that is clearly not jacked does not lock the install."""

    @staticmethod
    def _world(tmp_path, monkeypatch, **kwargs):
        world = World(tmp_path, monkeypatch, **kwargs)
        world.port_free = False
        # Use the real identification over a fake HTTP opener.
        monkeypatch.setattr(ir, "_identify_listener", _REAL_IDENTIFY)
        return world

    def test_a_foreign_http_listener_is_released_at_once(
        self, tmp_path, monkeypatch
    ):
        world = self._world(tmp_path, monkeypatch)
        calls = _fake_opener(monkeypatch, _FakeResponse(b"<html>hi</html>"))
        result, clock = world.release(timeout=60)
        assert result.exit_code == ir.EXIT_RELEASED
        assert clock.sleeps == 0
        assert calls == ["http://127.0.0.1:8321/api/health"]
        assert ir.FOREIGN_PORT_NOTE in result.message
        _assert_values_free(result.message)

    @pytest.mark.parametrize(
        "answer",
        [
            _FakeResponse(b'{"status": "ok"}'),
            _FakeResponse(b"not json"),
            _FakeResponse(b"", status=204),
            updater.urllib.error.HTTPError(
                "http://127.0.0.1:8321/api/health", 404, "nf", {}, None
            ),
            updater.urllib.error.URLError(ConnectionRefusedError()),
            ConnectionResetError(),
        ],
        ids=["wrong-shape", "not-json", "status-204", "http-404", "refused", "reset"],
    )
    def test_every_clear_stranger_is_released(
        self, tmp_path, monkeypatch, answer
    ):
        world = self._world(tmp_path, monkeypatch)
        _fake_opener(monkeypatch, answer)
        result, _clock = world.release(timeout=5)
        assert result.exit_code == ir.EXIT_RELEASED
        assert ir.FOREIGN_PORT_NOTE in result.message

    def test_a_jacked_health_listener_still_holds_the_install(
        self, tmp_path, monkeypatch
    ):
        world = self._world(tmp_path, monkeypatch)
        _fake_opener(monkeypatch, _FakeResponse(b'{"status": "ok", "db": true}'))
        result, clock = world.release(timeout=4)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert clock.now >= 4
        assert "in use by a jacked service" in result.message
        assert ir.FOREIGN_PORT_NOTE not in result.message
        _assert_values_free(result.message)

    @pytest.mark.parametrize(
        "answer",
        [
            TimeoutError(),
            updater.urllib.error.URLError(TimeoutError()),
            OSError("something else"),
        ],
        ids=["read-timeout", "connect-timeout", "other-error"],
    )
    def test_a_probe_that_proves_nothing_holds_the_install(
        self, tmp_path, monkeypatch, answer
    ):
        world = self._world(tmp_path, monkeypatch)
        _fake_opener(monkeypatch, answer)
        result, clock = world.release(timeout=4)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert clock.now >= 4
        assert "port is still in use" in result.message
        assert ir.FOREIGN_PORT_NOTE not in result.message

    def test_a_stopped_service_and_a_foreign_listener_is_released(
        self, tmp_path, monkeypatch
    ):
        world = self._world(tmp_path, monkeypatch, manifest=_manifest())
        world.on_stop = lambda: world.dead_pids.add(4242)
        _fake_opener(monkeypatch, _FakeResponse(b"{}"))
        result, _clock = world.release(timeout=5)
        assert world.stop_calls == 1
        assert result.exit_code == ir.EXIT_RELEASED
        assert ir.FOREIGN_PORT_NOTE in result.message

    def test_a_free_port_is_never_probed(self, tmp_path, monkeypatch):
        world = self._world(tmp_path, monkeypatch)
        world.port_free = True
        calls = _fake_opener(monkeypatch, _FakeResponse(b"{}"))
        result, _clock = world.release()
        assert result.released
        assert calls == []
        assert ir.FOREIGN_PORT_NOTE not in result.message

    def test_the_identification_uses_the_updater_time_limit(self, monkeypatch):
        seen = {}

        def classify(port, budget):
            seen.update(port=port, budget=budget)
            return updater.LISTENER_FOREIGN, "why"

        monkeypatch.setattr(updater, "classify_jacked_listener", classify)
        assert _REAL_IDENTIFY(8321) == updater.LISTENER_FOREIGN
        assert seen == {"port": 8321, "budget": updater.IDENTIFY_TIMEOUT_SECONDS}


class TestCheckOnlyWithAForeignListener:
    """`jacked upgrade --skip-service` runs the check with a time limit of 0."""

    def test_the_check_only_command_exits_0(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        world.listener = updater.LISTENER_FOREIGN
        result = TestReleaseInstallCommand._invoke(
            monkeypatch, tmp_path, "--check-only", "--timeout", "0"
        )
        assert result.exit_code == ir.EXIT_RELEASED, result.output
        assert ir.FOREIGN_PORT_NOTE in result.output
        assert world.stop_calls == 0

    def test_the_check_only_command_still_refuses_a_jacked_listener(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        result = TestReleaseInstallCommand._invoke(
            monkeypatch, tmp_path, "--check-only", "--timeout", "0"
        )
        assert result.exit_code == ir.EXIT_NOT_STOPPED, result.output


class TestTheTimeLimitClause:
    """A time limit of 0 is a check, so the message names no time limit."""

    def test_a_zero_time_limit_leaves_out_the_clause(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        result, _clock = world.release(timeout=0, stop=False)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert "within" not in result.message
        assert result.message == (
            "No live service was stopped, but the service port is still in "
            "use by a jacked service. Nothing was installed."
        )

    def test_a_zero_time_limit_after_a_stop_leaves_out_the_clause(
        self, tmp_path, monkeypatch
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        result, _clock = world.release(timeout=0)
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert "within" not in result.message
        assert result.message.endswith("did not exit. Nothing was installed.")

    def test_a_positive_time_limit_keeps_the_clause(self, tmp_path, monkeypatch):
        world = World(tmp_path, monkeypatch)
        world.port_free = False
        result, _clock = world.release(timeout=2)
        assert "within 2 seconds" in result.message


class TestAMissingPipe:
    """"File not found" is a stop in progress only when the manifest is gone."""

    @pytest.mark.parametrize("kind", ["exception", "winerror"])
    def test_a_missing_pipe_of_a_live_owner_is_a_refusal(
        self, tmp_path, monkeypatch, kind
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        if kind == "exception":
            world.stop_error = FileNotFoundError("no pipe")
        else:
            exc = OSError("no pipe")
            exc.winerror = 2
            world.stop_error = exc
        result, clock = world.release(timeout=60)
        assert result.exit_code == ir.EXIT_NOT_STOPPED
        assert clock.sleeps == 0, "a refusal must not wait"
        assert "already stopping" not in result.message
        assert "No process was signalled" in result.message
        _assert_values_free(result.message)

    @pytest.mark.parametrize("kind", ["exception", "winerror"])
    def test_a_missing_pipe_after_the_manifest_is_gone_is_in_progress(
        self, tmp_path, monkeypatch, kind
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        if kind == "exception":
            world.stop_error = FileNotFoundError("no pipe")
        else:
            exc = OSError("no pipe")
            exc.winerror = 2
            world.stop_error = exc
        world.on_send = lambda: world.paths.manifest.unlink()
        result, _clock = world.release(timeout=2)
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert "already stopping" in result.message

    @pytest.mark.parametrize("winerror", [109, 232, 233])
    def test_pipe_closing_codes_are_in_progress_with_the_manifest(
        self, tmp_path, monkeypatch, winerror
    ):
        world = World(tmp_path, monkeypatch, manifest=_manifest())
        exc = OSError("pipe closing")
        exc.winerror = winerror
        world.stop_error = exc
        result, _clock = world.release(timeout=1)
        assert result.exit_code == ir.EXIT_STOPPED_NOT_RELEASED
        assert "already stopping" in result.message

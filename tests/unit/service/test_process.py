"""Tests for jacked.service.process module."""

import os
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest


class TestWritePid:
    def test_writes_pid_to_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        from jacked.service.process import write_pid
        assert write_pid(pid_file, port=8321) is True
        content = pid_file.read_text().strip()
        lines = content.split("\n")
        assert lines[0] == str(os.getpid())
        assert lines[1] == "8321"

    def test_creates_parent_dirs(self, tmp_path):
        pid_file = tmp_path / "sub" / "dir" / "test.pid"
        from jacked.service.process import write_pid
        assert write_pid(pid_file, port=8321) is True
        assert pid_file.exists()

    def test_preserves_stale_existing_pid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n1234")
        from jacked.service.process import write_pid
        with patch("jacked.service.process.is_process_alive", return_value=False):
            assert write_pid(pid_file, port=5555) is True
        assert pid_file.read_text() == "12345\n1234"

    def test_refuses_to_overwrite_different_live_pid(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        other_pid = os.getpid() + 1
        original = f"{other_pid}\n1234"
        pid_file.write_text(original)
        from jacked.service.process import write_pid
        with patch("jacked.service.process.is_process_alive", return_value=True):
            assert write_pid(pid_file, port=5555) is False
        assert pid_file.read_text() == original

    def test_preserves_same_process_without_liveness_probe(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text(f"{os.getpid()}\n1234")
        from jacked.service.process import write_pid
        with patch("jacked.service.process.is_process_alive") as is_alive:
            assert write_pid(pid_file, port=5555) is True
        is_alive.assert_not_called()
        assert pid_file.read_text() == f"{os.getpid()}\n1234"

    def test_atomic_publish_refuses_live_race_winner(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        other_pid = os.getpid() + 1

        def racing_link(_source, target):
            target.write_text(f"{other_pid}\n4321")
            raise FileExistsError

        from jacked.service.process import write_pid

        with (
            patch("jacked.service.process.os.link", side_effect=racing_link),
            patch("jacked.service.process.is_process_alive", return_value=True),
        ):
            assert write_pid(pid_file, port=5555) is False
        assert pid_file.read_text() == f"{other_pid}\n4321"

    def test_v2_marker_never_treats_reused_pid_as_legacy_authority(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n8321\njacked-v2\n")
        from jacked.service.process import write_pid

        with patch("jacked.service.process.is_process_alive") as alive:
            assert write_pid(pid_file, port=5555) is True
        alive.assert_not_called()
        assert pid_file.read_text() == "12345\n8321\njacked-v2\n"


class TestReadPid:
    def test_reads_valid_pid_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n8321")
        from jacked.service.process import read_pid
        result = read_pid(pid_file)
        assert result == {"pid": 12345, "port": 8321}

    def test_returns_none_for_missing_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import read_pid
        assert read_pid(pid_file) is None

    def test_returns_none_for_corrupt_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not a number")
        from jacked.service.process import read_pid
        assert read_pid(pid_file) is None

    def test_returns_none_for_empty_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("")
        from jacked.service.process import read_pid
        assert read_pid(pid_file) is None

    def test_handles_pid_only_no_port(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        from jacked.service.process import read_pid
        result = read_pid(pid_file)
        assert result == {"pid": 12345, "port": 8321}


class TestRemovePid:
    def test_removes_existing_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n8321")
        from jacked.service.process import remove_pid
        assert remove_pid(pid_file) is True
        assert not pid_file.exists()

    def test_no_error_on_missing_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import remove_pid
        assert remove_pid(pid_file) is False

    def test_expected_pid_removes_matching_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n8321")
        from jacked.service.process import remove_pid
        assert remove_pid(pid_file, expected_pid=12345) is True
        assert not pid_file.exists()

    def test_expected_pid_preserves_mismatched_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        original = "12345\n8321"
        pid_file.write_text(original)
        from jacked.service.process import remove_pid
        assert remove_pid(pid_file, expected_pid=54321) is False
        assert pid_file.read_text() == original

    def test_expected_pid_preserves_corrupt_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not a pid")
        from jacked.service.process import remove_pid
        assert remove_pid(pid_file, expected_pid=12345) is False
        assert pid_file.read_text() == "not a pid"


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        from jacked.service.process import is_process_alive
        assert is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self):
        from jacked.service.process import is_process_alive
        assert is_process_alive(999999999) is False

    def test_negative_or_zero_pid_is_not_alive(self):
        from jacked.service.process import is_process_alive
        assert is_process_alive(0) is False
        assert is_process_alive(-1) is False

    def test_exited_subprocess_reported_dead(self):
        from jacked.service.process import is_process_alive
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        # Small delay so parent can reap on some platforms
        time.sleep(0.1)
        assert is_process_alive(p.pid) is False

    def test_running_subprocess_reported_alive(self):
        from jacked.service.process import is_process_alive
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            time.sleep(0.2)
            assert is_process_alive(p.pid) is True
        finally:
            p.terminate()
            p.wait(timeout=5)


class TestCheckPort:
    def test_unused_port_is_available(self):
        from jacked.service.process import is_port_available
        assert is_port_available("127.0.0.1", 59999) is True

    def test_used_port_is_not_available(self):
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
        try:
            from jacked.service.process import is_port_available
            assert is_port_available("127.0.0.1", port) is False
        finally:
            sock.close()


class TestStopProcess:
    def test_returns_false_for_no_pid_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import stop_process
        assert stop_process(pid_file) is False

    def test_removes_stale_pid_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("999999999\n8321")
        from jacked.service.process import stop_process
        result = stop_process(pid_file)
        assert result is False
        assert not pid_file.exists()

    @patch("os.kill")
    def test_sends_sigterm_on_unix(self, mock_kill, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text(f"{os.getpid()}\n8321")
        from jacked.service.process import stop_process
        with patch("jacked.service.process.is_process_alive", return_value=True):
            with patch("sys.platform", "darwin"):
                result = stop_process(pid_file)
        mock_kill.assert_called_once_with(os.getpid(), signal.SIGTERM)
        assert result is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
class TestStopProcessGraceful:
    """Tests for stop_process_graceful() — SIGTERM with SIGKILL escalation."""

    def test_no_pid_file_returns_not_running(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import stop_process_graceful
        result = stop_process_graceful(pid_file)
        assert result == {"was_running": False, "died": False, "killed": False}

    def test_stale_pid_is_cleaned_up(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("999999999\n8321")
        from jacked.service.process import stop_process_graceful
        result = stop_process_graceful(pid_file)
        assert result["was_running"] is False
        assert result["died"] is True
        assert not pid_file.exists()

    def test_sigterm_kills_cooperative_process(self, tmp_path):
        """Subprocess that honors SIGTERM dies gracefully — no SIGKILL.

        Pytest owns the subprocess, so without a reaper the PID would linger
        as a zombie and is_process_alive would report it alive. A background
        thread calling p.wait() lets the kernel clear the zombie.
        """
        import threading
        pid_file = tmp_path / "test.pid"
        p = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        pid_file.write_text(f"{p.pid}\n8321")
        reaper = threading.Thread(target=p.wait, daemon=True)
        reaper.start()
        try:
            from jacked.service.process import stop_process_graceful
            result = stop_process_graceful(pid_file, term_timeout=5.0)
            assert result["was_running"] is True
            assert result["died"] is True
            assert result["killed"] is False
            assert not pid_file.exists()
        finally:
            try:
                p.kill()
            except OSError:
                pass
            reaper.join(timeout=5)

    def test_sigkill_escalates_when_sigterm_ignored(self, tmp_path):
        """Subprocess that ignores SIGTERM must be SIGKILLed.

        We wait for the child to write a ready file before sending signals —
        otherwise SIGTERM can arrive during interpreter startup, before
        SIG_IGN is installed, and the default handler terminates the process.
        """
        import threading
        pid_file = tmp_path / "test.pid"
        ready_file = tmp_path / "ready"
        script = (
            "import signal, time, sys\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(ready_file)!r}, 'w').close()\n"
            "time.sleep(30)\n"
        )
        p = subprocess.Popen([sys.executable, "-c", script])
        pid_file.write_text(f"{p.pid}\n8321")
        reaper = threading.Thread(target=p.wait, daemon=True)
        reaper.start()
        try:
            # Wait for the SIG_IGN handler to be installed.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not ready_file.exists():
                time.sleep(0.05)
            assert ready_file.exists(), "child never became ready"

            from jacked.service.process import stop_process_graceful
            # Short term_timeout forces the escalation path.
            result = stop_process_graceful(
                pid_file, term_timeout=1.0, kill_timeout=5.0
            )
            assert result["was_running"] is True
            assert result["killed"] is True
            assert result["died"] is True
            assert not pid_file.exists()
        finally:
            try:
                p.kill()
            except OSError:
                pass
            reaper.join(timeout=5)


class TestWaitForPortFree:
    def test_returns_true_immediately_if_free(self):
        from jacked.service.process import wait_for_port_free
        assert wait_for_port_free("127.0.0.1", 59998, timeout=1.0) is True

    def test_returns_false_when_port_stays_bound(self):
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        _, port = sock.getsockname()
        try:
            from jacked.service.process import wait_for_port_free
            t0 = time.monotonic()
            result = wait_for_port_free("127.0.0.1", port, timeout=0.5)
            elapsed = time.monotonic() - t0
            assert result is False
            assert elapsed >= 0.5
        finally:
            sock.close()

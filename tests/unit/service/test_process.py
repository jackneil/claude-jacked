"""Tests for jacked.service.process module."""

import os
import signal
from unittest.mock import patch

import pytest


class TestWritePid:
    def test_writes_pid_to_file(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        from jacked.service.process import write_pid
        write_pid(pid_file, port=8321)
        content = pid_file.read_text().strip()
        lines = content.split("\n")
        assert lines[0] == str(os.getpid())
        assert lines[1] == "8321"

    def test_creates_parent_dirs(self, tmp_path):
        pid_file = tmp_path / "sub" / "dir" / "test.pid"
        from jacked.service.process import write_pid
        write_pid(pid_file, port=8321)
        assert pid_file.exists()

    def test_overwrites_existing(self, tmp_path):
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("99999\n1234")
        from jacked.service.process import write_pid
        write_pid(pid_file, port=5555)
        lines = pid_file.read_text().strip().split("\n")
        assert lines[0] == str(os.getpid())
        assert lines[1] == "5555"


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
        remove_pid(pid_file)
        assert not pid_file.exists()

    def test_no_error_on_missing_file(self, tmp_path):
        pid_file = tmp_path / "nope.pid"
        from jacked.service.process import remove_pid
        remove_pid(pid_file)


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        from jacked.service.process import is_process_alive
        assert is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self):
        from jacked.service.process import is_process_alive
        assert is_process_alive(999999999) is False


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

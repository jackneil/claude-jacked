"""Tests for console-less (pythonw.exe) std-stream repair.

Regression cover for the silent Windows autostart failure: the login VBS runs
``pythonw.exe -m jacked service start``, pythonw gives the interpreter
``None`` for all three std streams, and two separate ``isatty()`` calls then
killed the tray with no console to report it to.

Note the std-stream patching happens INSIDE each test body, never in a fixture:
pytest's capture plugin re-installs ``sys.stdout``/``sys.stderr`` between
fixture setup and the call phase, which would silently undo it.
"""
import io
import os
import sys

from jacked.headless import ensure_std_streams


def _simulate_pythonw(monkeypatch):
    """Every std stream is None, exactly as pythonw.exe leaves them."""
    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)


def _log_at(monkeypatch, tmp_path):
    from jacked import headless

    log = tmp_path / ".claude" / "jacked-headless.log"
    monkeypatch.setattr(headless, "_headless_log_path", lambda: log)
    return log


def test_repairs_all_three_none_streams(monkeypatch, tmp_path):
    _log_at(monkeypatch, tmp_path)
    _simulate_pythonw(monkeypatch)

    repaired = ensure_std_streams()

    assert set(repaired) == {"stdin", "stdout", "stderr"}
    assert sys.stdin is not None
    assert sys.stdout is not None
    assert sys.stderr is not None


def test_repaired_streams_survive_isatty(monkeypatch, tmp_path):
    """The exact call that crashed us — ours and uvicorn's — must now work."""
    _log_at(monkeypatch, tmp_path)
    _simulate_pythonw(monkeypatch)

    ensure_std_streams()

    # No AttributeError, and a file is honestly not a tty.
    assert sys.stdout.isatty() is False
    assert sys.stderr.isatty() is False
    assert sys.stdin.isatty() is False


def test_repaired_stdout_actually_writes(monkeypatch, tmp_path):
    log = _log_at(monkeypatch, tmp_path)
    _simulate_pythonw(monkeypatch)

    ensure_std_streams()
    print("crash details land somewhere readable")
    sys.stdout.flush()

    assert "crash details land somewhere readable" in log.read_text(encoding="utf-8")


def test_live_streams_are_left_alone(monkeypatch, tmp_path):
    """A real console, a pytest capture, or a deliberate redirect is untouched."""
    _log_at(monkeypatch, tmp_path)
    sentinel_out, sentinel_err, sentinel_in = io.StringIO(), io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", sentinel_out)
    monkeypatch.setattr(sys, "stderr", sentinel_err)
    monkeypatch.setattr(sys, "stdin", sentinel_in)

    assert ensure_std_streams() == []
    assert sys.stdout is sentinel_out
    assert sys.stderr is sentinel_err
    assert sys.stdin is sentinel_in


def test_only_the_none_stream_is_replaced(monkeypatch, tmp_path):
    """Partial breakage is repaired without disturbing the working stream."""
    _log_at(monkeypatch, tmp_path)
    sentinel_out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys, "stdout", sentinel_out)
    monkeypatch.setattr(sys, "stderr", None)

    assert ensure_std_streams() == ["stderr"]
    assert sys.stdout is sentinel_out
    assert sys.stderr is not None


def test_is_idempotent(monkeypatch, tmp_path):
    _log_at(monkeypatch, tmp_path)
    _simulate_pythonw(monkeypatch)

    assert set(ensure_std_streams()) == {"stdin", "stdout", "stderr"}
    assert ensure_std_streams() == []  # second call has nothing left to fix


def test_unwritable_log_falls_back_instead_of_raising(monkeypatch, tmp_path):
    """Losing output is survivable; killing the process we're rescuing is not."""
    from jacked import headless

    monkeypatch.setattr(headless, "_headless_log_path",
                        lambda: (_ for _ in ()).throw(OSError("no home")))
    _simulate_pythonw(monkeypatch)

    repaired = ensure_std_streams()  # must not raise

    assert "stdout" in repaired
    sys.stdout.write("swallowed, but no crash")


def test_log_rotates_instead_of_growing_forever(monkeypatch, tmp_path):
    from jacked import headless

    log = _log_at(monkeypatch, tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("x" * 500, encoding="utf-8")
    monkeypatch.setattr(headless, "_MAX_LOG_BYTES", 100)  # already over budget
    _simulate_pythonw(monkeypatch)

    ensure_std_streams()
    sys.stdout.write("fresh")
    sys.stdout.flush()

    assert "x" not in log.read_text(encoding="utf-8")


def test_log_appends_when_under_the_cap(monkeypatch, tmp_path):
    log = _log_at(monkeypatch, tmp_path)
    log.parent.mkdir(parents=True)
    log.write_text("earlier boot\n", encoding="utf-8")
    _simulate_pythonw(monkeypatch)

    ensure_std_streams()
    sys.stdout.write("later boot\n")
    sys.stdout.flush()

    body = log.read_text(encoding="utf-8")
    assert "earlier boot" in body and "later boot" in body


def test_sink_fallback_is_usable_and_not_a_tty(monkeypatch, tmp_path):
    """When the log dir is unusable we still hand back a working writer.

    It must also report ``isatty() is False``: a raw ``os.devnull`` handle
    reports True on Windows (NUL is a character device), which would make rich
    and uvicorn emit ANSI colour for a terminal that does not exist.
    """
    from jacked import headless

    monkeypatch.setattr(headless, "_headless_log_path",
                        lambda: (_ for _ in ()).throw(OSError("boom")))
    _simulate_pythonw(monkeypatch)

    ensure_std_streams()

    assert sys.stdout.isatty() is False
    assert sys.stdout.write("goes nowhere without exploding") > 0


def test_raw_devnull_would_have_been_wrong_on_windows():
    """Documents *why* the sink exists rather than a devnull handle."""
    if sys.platform != "win32":
        import pytest
        pytest.skip("NUL character-device behaviour is Windows-specific")

    with open(os.devnull, "w", encoding="utf-8") as fh:
        assert fh.isatty() is True  # the trap we are avoiding


def test_main_module_repairs_before_importing_cli():
    """__main__.py must fix the streams BEFORE `from jacked.cli import main`.

    cli.py builds a rich Console at module scope, so importing it first would
    reintroduce the crash this module exists to prevent.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "jacked" / "__main__.py"
    text = src.read_text(encoding="utf-8")

    assert text.index("ensure_std_streams()") < text.index("from jacked.cli import main")

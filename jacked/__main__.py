"""Allow running jacked via `python -m jacked`."""
# MUST come before importing jacked.cli: under Windows' console-less
# pythonw.exe (what the login autostart VBS uses) sys.stdout/stderr are None,
# and cli.py builds a rich Console at module scope. Repair the streams first
# so nothing downstream — ours or a dependency's — trips over a None stream.
from jacked.headless import ensure_std_streams

ensure_std_streams()

try:
    from jacked.cli import main
except Exception:
    # Under a supervisor or pythonw.exe this traceback goes nowhere. Keep a
    # trace in ~/.claude/jacked-boot-failure.log, then fail as before.
    try:
        from jacked.boot_failure import record_current

        record_current()
    except Exception:  # noqa: BLE001 - the original error matters more
        pass
    raise

main()

"""Static guard: every phase name in update_phases.PHASES must appear in
both the POSIX updater (as _begin/_end calls) AND the Windows batch body.
Prevents the 'new dev adds phase N+1, forgets writer X' regression."""

import ast
from pathlib import Path

from jacked.service.update_phases import PHASE_NAMES


def _collect_phase_args(source: str) -> set[str]:
    """Parse `source` and return every string literal passed as the FIRST arg
    to a call whose func name ends with 'begin_phase', 'end_phase', '_begin',
    or '_end'."""
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = None
        if isinstance(node.func, ast.Name):
            fname = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fname = node.func.attr
        if fname not in ("begin_phase", "end_phase", "_begin", "_end"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def test_updater_py_covers_all_phases():
    import jacked.service.updater as _u_mod
    src = Path(_u_mod.__file__).read_text()
    found = _collect_phase_args(src)
    for name in PHASE_NAMES:
        assert name in found, (
            f"updater.py has no _begin/_end call for {name!r} — "
            "every PHASE_NAMES entry must be opened AND closed"
        )


def test_windows_batch_body_contains_every_phase():
    """Generate the Windows batch and assert every phase appears with both
    in_progress and ok/failed transitions."""
    import subprocess as _sp
    from unittest.mock import patch
    from jacked.service import updater
    from pathlib import Path as _P

    with patch("jacked.install_method.detect_install_method", return_value="uv"):
        with patch("jacked.service.updater.find_bin", return_value=r"C:\uv\uv.exe"):
            with patch("subprocess.Popen") as mock_popen:
                with patch.object(_sp, "DETACHED_PROCESS", 0x8, create=True):
                    updater._spawn_windows_tray_updater(
                        parent_pid=12345, extras="tray", target_version="0.41.20",
                    )

    batch_path = mock_popen.call_args[0][0][2]
    try:
        body = _P(batch_path).read_text()
        for name in PHASE_NAMES:
            assert f"_update_status {name} in_progress" in body, (
                f"Windows batch missing begin for {name!r}"
            )
            assert (
                f"_update_status {name} ok" in body
                or f"_update_status {name} failed" in body
            ), f"Windows batch missing close for {name!r}"
    finally:
        import os as _os
        try:
            _os.unlink(batch_path)
        except OSError:
            pass


def test_update_html_still_embeds_every_phase():
    """Belt-and-suspenders: the browser UI must have every phase too."""
    import jacked
    repo_root = Path(jacked.__file__).resolve().parent
    html = (repo_root / "data" / "web" / "update.html").read_text()
    for name in PHASE_NAMES:
        assert name in html, f"update.html missing phase {name!r}"

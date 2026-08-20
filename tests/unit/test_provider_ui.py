"""M5: provider indicator — backend bits (menubar summary + pill icon).

The dashboard card + panel marks are covered by the node harness in
test_web_js_panel.py / test_web_js_provider.py; this file covers the Python
surfaces: summarize_account carrying `provider`, and the menu-bar pill icon
drawing a distinct (cached) glyph per provider.
"""

from pathlib import Path


def test_summarize_account_includes_provider():
    from jacked.service.menubar_summary import summarize_account

    claude = summarize_account(
        {"id": 1, "email": "a@x.com", "cached_usage_5h": 10, "cached_usage_7d": 5}
    )
    assert claude["provider"] == "claude"  # default when absent

    codex = summarize_account(
        {
            "id": 2,
            "email": "c@x.com",
            "provider": "codex",
            "cached_usage_5h": 1,
            "cached_usage_7d": 2,
        }
    )
    assert codex["provider"] == "codex"


def test_render_status_icon_distinct_per_provider(tmp_path, monkeypatch):
    import jacked.service as svc

    monkeypatch.setattr(svc, "CLAUDE_DIR", tmp_path)
    from jacked.service.menubar_mac import _render_status_icon

    claude = _render_status_icon("green", False, "claude")
    codex = _render_status_icon("green", False, "codex")
    assert claude is not None and codex is not None
    # Claude path is unchanged (back-compat); Codex gets its own cache file.
    assert claude.endswith("jacked-menubar-arm-green.png")
    assert codex.endswith("jacked-menubar-arm-green-codex.png")
    assert claude != codex
    # The Codex icon draws a brand dot, so its bytes differ from Claude's.
    assert Path(claude).read_bytes() != Path(codex).read_bytes()


def test_render_status_icon_unknown_provider_falls_back_to_claude(tmp_path, monkeypatch):
    import jacked.service as svc

    monkeypatch.setattr(svc, "CLAUDE_DIR", tmp_path)
    from jacked.service.menubar_mac import _render_status_icon

    # None/empty provider → the plain Claude icon path (no suffix).
    assert _render_status_icon("yellow", False, "").endswith("jacked-menubar-arm-yellow.png")


def test_legacy_icon_cache_swept_once(tmp_path, monkeypatch):
    """Old pre-arm cache PNGs (the rounded-rect "J" era) get deleted on the
    first render — but only stale ones, so a still-running pre-upgrade
    process's freshly-written file survives."""
    import os
    import time

    import jacked.service as svc
    from jacked.service import menubar_mac

    monkeypatch.setattr(svc, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(menubar_mac, "_legacy_sweep_done", False)

    old = time.time() - 3600
    stale = tmp_path / "jacked-menubar-green.png"
    stale.write_bytes(b"old")
    os.utime(stale, (old, old))
    fresh = tmp_path / "jacked-menubar-red-upd.png"
    fresh.write_bytes(b"live writer")
    old_arm = tmp_path / "jacked-menubar-arm-red.png"
    old_arm.write_bytes(b"current-scheme cache, old but valid")
    os.utime(old_arm, (old, old))

    assert menubar_mac._render_status_icon("green") is not None
    assert not stale.exists(), "stale legacy cache file should be swept"
    assert fresh.exists(), "recently-written legacy file must survive (live writer)"
    assert old_arm.exists(), "'-arm-' files are the CURRENT scheme - never swept, even old"
    assert (tmp_path / "jacked-menubar-arm-green.png").exists()

    # Once-per-process guard: a legacy file appearing AFTER the first sweep
    # survives later renders in this process.
    late = tmp_path / "jacked-menubar-gray.png"
    late.write_bytes(b"late legacy")
    os.utime(late, (old, old))
    assert menubar_mac._render_status_icon("red") is not None
    assert late.exists(), "second render must not sweep again (once per process)"

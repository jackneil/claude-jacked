import json
from jacked import install_summary as s
from jacked.install_manifest import ManifestDiff, CategoryDiff


def _diff(**kw):
    cats = {k: CategoryDiff() for k in ("skills", "commands", "agents", "lenses", "templates")}
    for k, cd in kw.items():
        cats[k] = cd
    return ManifestDiff(cats)


def test_build_record_shape():
    d = _diff(skills=CategoryDiff(added=["recover"], unchanged=["whats-next"]))
    rec = s.build_record(d, "0.50.0", "0.51.0", "2026-06-17T00:00:00Z")
    assert rec["from_version"] == "0.50.0"
    assert rec["to_version"] == "0.51.0"
    assert rec["changes"]["skills"]["added"] == ["recover"]
    assert rec["unchanged_count"] == 1


def test_render_upgrade_shows_arrow_and_changes():
    d = _diff(skills=CategoryDiff(added=["recover"]),
              commands=CategoryDiff(changed=["whats-next.md"]),
              agents=CategoryDiff(removed=["legacy.md"], unchanged=["a.md", "b.md"]))
    rec = s.build_record(d, "0.50.0", "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "0.50.0" in out and "0.51.0" in out and "→" in out
    assert "recover" in out and "whats-next.md" in out and "legacy.md" in out
    assert "Restart Claude Code" in out


def test_render_first_install_no_from_version():
    d = _diff(skills=CategoryDiff(added=["recover"]))
    rec = s.build_record(d, None, "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "installed" in out.lower()
    assert "0.51.0" in out


def test_render_no_changes_says_up_to_date():
    d = _diff(skills=CategoryDiff(unchanged=["recover", "whats-next"]))
    rec = s.build_record(d, "0.51.0", "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "up to date" in out.lower()


def test_render_files_refreshed_branch():
    # Same version (from == to) but real changes -> "files refreshed" header.
    d = _diff(skills=CategoryDiff(added=["recover"], unchanged=["whats-next"]))
    rec = s.build_record(d, "0.51.0", "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "files refreshed" in out
    assert "recover" in out


def test_render_removed_only():
    # Only a removed entry — the removed name and the "removed" marker show.
    d = _diff(agents=CategoryDiff(removed=["legacy.md"]))
    rec = s.build_record(d, "0.50.0", "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "legacy.md" in out
    assert "removed" in out


def test_write_last_install_roundtrip(tmp_path):
    rec = {"at": "x", "from_version": None, "to_version": "0.51.0",
           "changes": {}, "unchanged_count": 0}
    p = tmp_path / "last.json"
    s.write_last_install(rec, p)
    assert json.loads(p.read_text(encoding="utf-8"))["to_version"] == "0.51.0"


# --- CLI integration: install wires the manifest + summary (Task A3) ---
from click.testing import CliRunner  # noqa: E402
from jacked.cli import main  # noqa: E402


def test_cli_install_writes_manifest_and_summary(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    # Don't let the install integration test register autostart or spawn a tray.
    monkeypatch.setattr("jacked.cli._setup_tray_autostart", lambda: None)
    runner = CliRunner()
    # First install: everything new, manifest + last-install written.
    r1 = runner.invoke(main, ["install", "--no-security", "--no-rules"])
    assert r1.exit_code == 0, r1.output
    assert (fake_home / ".claude" / "jacked-manifest.json").exists()
    last = fake_home / ".claude" / "jacked-last-install.json"
    assert last.exists()
    import json
    rec = json.loads(last.read_text(encoding="utf-8"))
    assert rec["from_version"] is None
    assert "recover" in rec["changes"]["skills"]["added"]
    assert "installed" in r1.output.lower()
    # Old banner must be gone.
    assert "What you get" not in r1.output

    # Second install (no version change): up to date, no spurious changes.
    r2 = runner.invoke(main, ["install", "--no-security", "--no-rules"])
    assert r2.exit_code == 0, r2.output
    assert "up to date" in r2.output.lower()


def test_cli_install_json_emits_record(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    # Don't let the install integration test register autostart or spawn a tray.
    monkeypatch.setattr("jacked.cli._setup_tray_autostart", lambda: None)
    runner = CliRunner()
    r = runner.invoke(main, ["install", "--no-security", "--no-rules", "--json"])
    assert r.exit_code == 0, r.output
    import json
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["to_version"]
    assert "skills" in payload["changes"]

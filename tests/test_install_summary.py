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
    # ASCII "->" not the U+2192 arrow: the Windows legacy console (cp1252/cp437)
    # can't encode →, and it crashed `jacked install` + silently aborted the
    # tray-update batch. Keep the summary ASCII-only.
    assert "0.50.0" in out and "0.51.0" in out and "->" in out
    assert "recover" in out and "whats-next.md" in out and "legacy.md" in out
    assert "Restart Claude Code" in out
    # Regression lock: the whole rendered summary must encode on a legacy
    # Windows console (removed-only branch exercised separately below).
    out.encode("cp1252")
    assert all(ord(c) < 128 for c in out), "install summary must stay ASCII-only"


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


def test_write_last_install_does_not_use_a_fixed_temp_name(tmp_path):
    """A blocker at the old fixed ``<name>.tmp`` proves each write stages its
    own temp file (two writers can never share one) and leaves none behind."""
    p = tmp_path / "last.json"
    (tmp_path / "last.json.tmp").mkdir()
    s.write_last_install({"to_version": "1.0"}, p)
    assert json.loads(p.read_text(encoding="utf-8"))["to_version"] == "1.0"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["last.json", "last.json.tmp"]


def test_read_last_install_returns_the_written_record(tmp_path):
    rec = {"at": "2026-11-02T10:00:25+00:00", "from_version": "0.110.1",
           "to_version": "0.110.2", "changes": {}, "unchanged_count": 0}
    p = tmp_path / "last.json"
    s.write_last_install(rec, p)
    assert s.read_last_install(p) == rec


def test_read_last_install_defaults_to_the_path_at_call_time(tmp_path, monkeypatch):
    """The default resolves at call time under jacked's home, so a
    $JACKED_HOME set after import is honored: the reader looks where
    `jacked install` writes the record."""
    record = tmp_path / ".claude" / "jacked-last-install.json"
    s.write_last_install({"to_version": "0.110.2"}, record)
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    assert s.last_install_path() == record
    assert s.read_last_install() == {"to_version": "0.110.2"}


def test_write_last_install_defaults_to_the_path_at_call_time(tmp_path, monkeypatch):
    """The writer resolves the same default at call time, so a record written
    with no path lands under the $JACKED_HOME set after import."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    s.write_last_install({"to_version": "0.110.2"})
    record = tmp_path / ".claude" / "jacked-last-install.json"
    assert json.loads(record.read_text(encoding="utf-8")) == {"to_version": "0.110.2"}


def test_read_last_install_without_a_record_is_none_and_quiet(tmp_path, caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="jacked.install_summary"):
        assert s.read_last_install(tmp_path / "missing.json") is None
    assert not [r for r in caplog.records if r.name == "jacked.install_summary"]


def test_read_last_install_corrupt_is_none_and_logs_the_class_only(tmp_path, caplog):
    import logging

    p = tmp_path / "last.json"
    p.write_text('{"to_version": "0.110.2", "secret-ish": "TOKEN-VALUE"', encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="jacked.install_summary"):
        assert s.read_last_install(p) is None
    lines = [r.getMessage() for r in caplog.records if r.name == "jacked.install_summary"]
    assert len(lines) == 1
    assert "JSONDecodeError" in lines[0]
    assert "TOKEN-VALUE" not in lines[0] and str(tmp_path) not in lines[0]


def test_read_last_install_unreadable_file_is_none_and_logged(tmp_path, caplog):
    import logging

    p = tmp_path / "last.json"
    p.mkdir()  # a directory where the file belongs: reading it raises OSError
    with caplog.at_level(logging.DEBUG, logger="jacked.install_summary"):
        assert s.read_last_install(p) is None
    lines = [r for r in caplog.records if r.name == "jacked.install_summary"]
    assert len(lines) == 1 and lines[0].levelno == logging.DEBUG
    assert str(tmp_path) not in lines[0].getMessage()


def test_read_last_install_that_is_not_an_object_is_none(tmp_path, caplog):
    import logging

    p = tmp_path / "last.json"
    p.write_text(json.dumps(["0.110.2"]), encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="jacked.install_summary"):
        assert s.read_last_install(p) is None
    lines = [r.getMessage() for r in caplog.records if r.name == "jacked.install_summary"]
    assert len(lines) == 1 and "list" in lines[0]


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
    r1 = runner.invoke(main, ["install", "--no-rules", "--no-packs"])
    assert r1.exit_code == 0, r1.output
    assert (fake_home / ".claude" / "jacked-manifest.json").exists()
    last = fake_home / ".claude" / "jacked-last-install.json"
    assert last.exists()
    import json
    rec = json.loads(last.read_text(encoding="utf-8"))
    assert rec["from_version"] is None
    assert "recover" in rec["changes"]["skills"]["added"]
    # Outcome reporting: the record carries what the prune/sweep actually did,
    # not just the diff's intent (all empty on a fresh install).
    assert rec["pruned"] == [] and rec["preserved"] == []
    assert rec["stale_commands"] == []
    assert "installed" in r1.output.lower()
    # Old banner must be gone.
    assert "What you get" not in r1.output

    # Second install (no version change): up to date, no spurious changes.
    r2 = runner.invoke(main, ["install", "--no-rules", "--no-packs"])
    assert r2.exit_code == 0, r2.output
    assert "up to date" in r2.output.lower()


def test_cli_install_json_emits_record(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    # Don't let the install integration test register autostart or spawn a tray.
    monkeypatch.setattr("jacked.cli._setup_tray_autostart", lambda: None)
    runner = CliRunner()
    r = runner.invoke(main, ["install", "--no-rules", "--no-packs", "--json"])
    assert r.exit_code == 0, r.output
    import json
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["to_version"]
    assert "skills" in payload["changes"]

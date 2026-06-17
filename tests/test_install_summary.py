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


def test_write_last_install_roundtrip(tmp_path):
    rec = {"at": "x", "from_version": None, "to_version": "0.51.0",
           "changes": {}, "unchanged_count": 0}
    p = tmp_path / "last.json"
    s.write_last_install(rec, p)
    assert json.loads(p.read_text(encoding="utf-8"))["to_version"] == "0.51.0"

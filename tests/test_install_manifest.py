# tests/test_install_manifest.py
import json
from pathlib import Path

from jacked import install_manifest as m


def _make_source(root: Path):
    """Build a fake data_root with one of each artifact type."""
    (root / "skills" / "recover").mkdir(parents=True)
    (root / "skills" / "recover" / "SKILL.md").write_text("recover v1", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "dc.md").write_text("dc cmd", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents" / "readme.md").write_text("agent", encoding="utf-8")
    (root / "lenses").mkdir()
    (root / "lenses" / "lens.md").write_text("lens", encoding="utf-8")
    (root / "templates").mkdir()
    (root / "templates" / "plan.html").write_text("<html>", encoding="utf-8")


def test_hash_source_keys_by_name(tmp_path):
    _make_source(tmp_path)
    h = m.hash_source(tmp_path)
    assert set(h) == {"skills", "commands", "agents", "lenses", "templates"}
    assert "recover" in h["skills"]            # skill keyed by dir name
    assert "dc.md" in h["commands"]            # file keyed by filename
    assert h["templates"]["plan.html"].startswith("sha256:")


def test_diff_added_changed_removed_unchanged(tmp_path):
    _make_source(tmp_path)
    current = m.hash_source(tmp_path)
    prior = {"version": "0.50.0", "artifacts": {
        "skills": {"recover": "sha256:OLD", "gone": "sha256:x"},  # recover changed, gone removed
        "commands": {"dc.md": current["commands"]["dc.md"]},      # unchanged
        "agents": {}, "lenses": {}, "templates": {},              # readme/lens/plan.html added
    }}
    d = m.diff(prior, current)
    assert d.by_category["skills"].changed == ["recover"]
    assert d.by_category["skills"].removed == ["gone"]
    assert d.by_category["commands"].unchanged == ["dc.md"]
    assert d.by_category["agents"].added == ["readme.md"]
    assert d.unchanged_count() == 1
    assert d.has_changes() is True


def test_diff_first_install_all_added(tmp_path):
    _make_source(tmp_path)
    d = m.diff(None, m.hash_source(tmp_path))
    assert d.by_category["skills"].added == ["recover"]
    assert d.by_category["skills"].removed == []


def test_load_missing_and_corrupt(tmp_path):
    assert m.load(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert m.load(bad) is None


def test_write_roundtrip(tmp_path):
    p = tmp_path / "manifest.json"
    m.write(p, "0.51.0", {"skills": {"recover": "sha256:a"}}, "2026-06-17T00:00:00Z")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == "0.51.0"
    assert data["artifacts"]["skills"]["recover"] == "sha256:a"


def test_prune_removed_deletes_only_listed(tmp_path):
    home = tmp_path
    # a jacked skill dir to be pruned, and a user's own skill that must survive
    (home / ".claude" / "skills" / "gone").mkdir(parents=True)
    (home / ".claude" / "skills" / "gone" / "SKILL.md").write_text("x", encoding="utf-8")
    (home / ".claude" / "skills" / "mine").mkdir(parents=True)
    (home / ".claude" / "skills" / "mine" / "SKILL.md").write_text("keep", encoding="utf-8")
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "old.md").write_text("x", encoding="utf-8")
    d = m.ManifestDiff({
        "skills": m.CategoryDiff(removed=["gone"]),
        "commands": m.CategoryDiff(removed=["old.md"]),
        "agents": m.CategoryDiff(), "lenses": m.CategoryDiff(), "templates": m.CategoryDiff(),
    })
    pruned = m.prune_removed(d, home)
    assert not (home / ".claude" / "skills" / "gone").exists()
    assert not (home / ".claude" / "commands" / "old.md").exists()
    assert (home / ".claude" / "skills" / "mine").exists()   # untouched
    assert set(pruned) == {"skills/gone", "commands/old.md"}

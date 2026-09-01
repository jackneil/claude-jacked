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
    pruned, preserved = m.prune_removed(d, home)
    assert not (home / ".claude" / "skills" / "gone").exists()
    # Positive proof only: with no prior manifest hash to match, the flat file
    # is moved aside rather than unlinked.
    assert not (home / ".claude" / "commands" / "old.md").exists()
    assert list((home / ".claude" / "jacked-backups" / "commands").glob("old-*.md"))
    assert (home / ".claude" / "skills" / "mine").exists()   # untouched
    assert pruned == ["skills/gone"]
    assert preserved == ["commands/old.md"]


def test_migrated_command_pruned_from_installed_tree(tmp_path):
    """A command that became a skill must not linger in ~/.claude/commands.

    The 2026-09-01 migration moved /dcr (and nine others) out of data/commands
    into data/skills. On an upgrade, the prior manifest still lists the command
    and the current source no longer has it, so the diff must report it removed
    and prune_removed must delete the stale installed copy — otherwise it sits
    there forever as cruft no later diff (or uninstall) can reach.
    """
    home = tmp_path / "home"
    source = tmp_path / "data"
    _make_source(source)
    (source / "skills" / "dcr").mkdir()
    (source / "skills" / "dcr" / "SKILL.md").write_text("dcr engine", encoding="utf-8")

    (home / ".claude" / "commands").mkdir(parents=True)
    stale = home / ".claude" / "commands" / "dcr.md"
    stale.write_text("the 0.95 dcr command", encoding="utf-8")

    prior = {"version": "0.95.0", "artifacts": {
        "skills": {"recover": "sha256:x"},
        # the recorded hash matches the installed copy: it is jacked's own file
        "commands": {"dc.md": "sha256:x", "dcr.md": m._sha256_file(stale)},
        "agents": {}, "lenses": {}, "templates": {},
    }}
    d = m.diff(prior, m.hash_source(source))
    assert d.by_category["commands"].removed == ["dcr.md"]
    assert "dcr" in d.by_category["skills"].added

    pruned, preserved = m.prune_removed(d, home, prior)
    assert not stale.exists()
    assert "commands/dcr.md" in pruned
    assert preserved == []


def test_prune_preserves_user_modified_flat_file(tmp_path):
    """A flat artifact whose bytes no longer match the recorded hash was edited
    by the user: it moves into ~/.claude/jacked-backups/<category>/ instead of
    being unlinked, and is reported in `preserved` — never a silent delete."""
    home = tmp_path
    (home / ".claude" / "commands").mkdir(parents=True)
    edited = home / ".claude" / "commands" / "dcr.md"
    edited.write_text("my hand-tuned dcr", encoding="utf-8")
    prior = {"version": "0.95.0", "artifacts": {
        "skills": {}, "commands": {"dcr.md": "sha256:what-jacked-shipped"},
        "agents": {}, "lenses": {}, "templates": {},
    }}
    d = m.ManifestDiff({
        "skills": m.CategoryDiff(), "commands": m.CategoryDiff(removed=["dcr.md"]),
        "agents": m.CategoryDiff(), "lenses": m.CategoryDiff(), "templates": m.CategoryDiff(),
    })
    pruned, preserved = m.prune_removed(d, home, prior)
    assert not edited.exists()
    backups = list((home / ".claude" / "jacked-backups" / "commands").glob("dcr-*.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "my hand-tuned dcr"
    assert preserved == ["commands/dcr.md"]
    assert pruned == []


def test_sweep_migrated_commands_without_manifest_reports_stale(tmp_path):
    """The belt-and-braces sweep: with NO usable prior manifest, provenance is
    unknown, so the colliding file is LEFT IN PLACE (it is inert — the shipped
    skill shadows it) and reported as stale so install can tell the user."""
    home = tmp_path
    (home / ".claude" / "commands").mkdir(parents=True)
    orphan = home / ".claude" / "commands" / "dcr.md"
    orphan.write_text("pre-manifest 0.95 dcr", encoding="utf-8")
    (home / ".claude" / "commands" / "dc.md").write_text("still shipped", encoding="utf-8")

    removed, stale = m.sweep_migrated_commands(home, {"dcr", "qa"}, None)
    assert orphan.exists(), "unproven files must never be touched"
    assert stale == ["commands/dcr.md"]
    assert removed == []
    assert not (home / ".claude" / "jacked-backups").exists()
    # a command whose name is NOT a migrated skill is not even reported
    assert (home / ".claude" / "commands" / "dc.md").exists()


def test_sweep_migrated_commands_with_manifest_deletes_jacked_copy(tmp_path):
    """With a prior manifest proving the colliding file is jacked's unmodified
    copy, the sweep deletes it outright."""
    home = tmp_path
    (home / ".claude" / "commands").mkdir(parents=True)
    stale_f = home / ".claude" / "commands" / "dcr.md"
    stale_f.write_text("the 0.95 dcr command", encoding="utf-8")
    prior = {"version": "0.95.0", "artifacts": {
        "skills": {}, "commands": {"dcr.md": m._sha256_file(stale_f)},
        "agents": {}, "lenses": {}, "templates": {},
    }}
    removed, stale = m.sweep_migrated_commands(home, {"dcr"}, prior)
    assert not stale_f.exists()
    assert removed == ["commands/dcr.md"]
    assert stale == []
    assert not (home / ".claude" / "jacked-backups").exists()


def test_sweep_leaves_user_edited_file_as_stale(tmp_path):
    """A manifest hash that does NOT match means the user edited the file (or it
    was never jacked's): left in place, reported stale, never moved or deleted."""
    home = tmp_path
    (home / ".claude" / "commands").mkdir(parents=True)
    edited = home / ".claude" / "commands" / "dcr.md"
    edited.write_text("my own dcr", encoding="utf-8")
    prior = {"version": "0.95.0", "artifacts": {
        "skills": {}, "commands": {"dcr.md": "sha256:not-this"},
        "agents": {}, "lenses": {}, "templates": {},
    }}
    removed, stale = m.sweep_migrated_commands(home, {"dcr"}, prior)
    assert edited.exists() and edited.read_text(encoding="utf-8") == "my own dcr"
    assert removed == [] and stale == ["commands/dcr.md"]


def test_migrated_skill_names_derived_from_marker(tmp_path):
    """The migrated set comes from the `<!-- ENGINE -->` marker in the shipped
    tree, not a hardcoded list."""
    (tmp_path / "skills" / "withmarker").mkdir(parents=True)
    (tmp_path / "skills" / "withmarker" / "SKILL.md").write_text(
        "---\nname: withmarker\n---\npreamble\n<!-- ENGINE -->\nengine\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "plain").mkdir(parents=True)
    (tmp_path / "skills" / "plain" / "SKILL.md").write_text(
        "---\nname: plain\n---\njust a skill\n", encoding="utf-8",
    )
    assert m.migrated_skill_names(tmp_path) == {"withmarker"}


def test_move_aside_flat_suffixes_on_collision(tmp_path):
    """Two same-second backups of one name must not clobber each other."""
    from datetime import datetime, timezone

    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    for content in ("first", "second"):
        f = tmp_path / ".claude" / "commands" / "dcr.md"
        f.write_text(content, encoding="utf-8")
        m._move_aside_flat(f, "commands", now=now)
    backups = sorted((tmp_path / ".claude" / "jacked-backups" / "commands").glob("dcr-*.md"))
    assert len(backups) == 2
    assert {b.read_text(encoding="utf-8") for b in backups} == {"first", "second"}


def test_remove_or_preserve_flat_gates_on_hash_or_source(tmp_path):
    """Uninstall's flat gate: manifest-hash match removes, source-byte match
    removes (pre-manifest installs), anything else is preserved to backups."""
    (tmp_path / ".claude" / "commands").mkdir(parents=True)
    src = tmp_path / "src.md"
    src.write_text("shipped body", encoding="utf-8")

    target = tmp_path / ".claude" / "commands" / "dc.md"
    target.write_text("shipped body", encoding="utf-8")
    prior = {"artifacts": {"commands": {"dc.md": m._sha256_file(target)}}}
    assert m.remove_or_preserve_flat(target, "commands", "dc.md", prior, src=src) == "removed"
    assert not target.exists()

    # no manifest, but bytes match the packaged source -> still jacked's copy
    target.write_text("shipped body", encoding="utf-8")
    assert m.remove_or_preserve_flat(target, "commands", "dc.md", None, src=src) == "removed"

    # user-edited copy -> preserved to backups, never unlinked
    target.write_text("my edits", encoding="utf-8")
    assert m.remove_or_preserve_flat(target, "commands", "dc.md", None, src=src) == "preserved"
    assert not target.exists()
    backups = list((tmp_path / ".claude" / "jacked-backups" / "commands").glob("dc-*.md"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == "my edits"

    # nothing at the path
    assert m.remove_or_preserve_flat(target, "commands", "dc.md", None, src=src) is None

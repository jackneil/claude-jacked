"""Claude-side install/uninstall must never destroy a user's own skill dir.

Ports the Codex installer's guards to ~/.claude/skills:
  1. install moves a colliding NON-jacked dir into
     ~/.claude/jacked-backups/skills/<name>-<timestamp> before writing jacked's
     copy, and reports the move the moment it happens;
  2. uninstall deletes only a dir jacked owns (manifest hash, or every file
     still matching the packaged source) and keeps the rest with an honest note;
  3. a full-dir hash (`skills_dirs`, manifest format 2) catches sidecar edits
     that the SKILL.md hash alone cannot see;
  4. a copy that fails part-way removes its partial copy and puts the user's
     own dir back at its path (#145i), through one shared transaction helper.
"""

import errno
import json
import logging
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked import install_manifest as m
from tests._platform import requires_symlinks


def _skills_base(tmp_path: Path) -> Path:
    """The real layout, so backups resolve to <home>/.claude/jacked-backups."""
    base = tmp_path / ".claude" / "skills"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _backups_of(skills_base: Path, name: str) -> list:
    root = skills_base.parent / "jacked-backups" / "skills"
    return sorted(root.glob(f"{name}-*")) if root.is_dir() else []


def _manifest(skills: dict, dirs: dict = None) -> dict:
    """An OLD-format manifest by default (format 1, no skills_dirs)."""
    artifacts = {"skills": skills}
    if dirs is None:
        return {"version": "0.83.0", "artifacts": artifacts}
    artifacts[m.SKILLS_DIRS_KEY] = dirs
    return {"version": "0.83.0", "format": 2, "artifacts": artifacts}


def _owning_manifest(skill_dir: Path, name: str) -> dict:
    """A format-2 manifest that records `skill_dir` exactly as jacked's own."""
    return _manifest(
        {name: m.skill_dir_hash(skill_dir)},
        {name: m.skill_content_hash(skill_dir)},
    )


def _skill(root: Path, name: str, body: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def _with_sidecar(root: Path, name: str, body: str, sidecar: str) -> Path:
    d = _skill(root, name, body)
    (d / "measure.js").write_text(sidecar, encoding="utf-8")
    return d


# --- unit: hash + ownership helpers ---------------------------------------

def test_skill_dir_hash_matches_manifest_entry(tmp_path):
    d = _skill(_skills_base(tmp_path), "dcr", "jacked body")
    src = _skill(tmp_path / "src", "dcr", "jacked body")
    assert m.skill_dir_hash(d) == m.skill_dir_hash(src)
    assert m.skill_dir_hash(tmp_path / "nope") is None


def test_is_jacked_skill_dir_true_only_on_hash_match(tmp_path):
    d = _skill(_skills_base(tmp_path), "dcr", "jacked body")
    h = m.skill_dir_hash(d)
    assert m.is_jacked_skill_dir(d, "dcr", _manifest({"dcr": h})) is True
    assert m.is_jacked_skill_dir(d, "dcr", _manifest({"dcr": "sha256:OTHER"})) is False
    assert m.is_jacked_skill_dir(d, "dcr", _manifest({})) is False   # no entry
    assert m.is_jacked_skill_dir(d, "dcr", None) is False            # no manifest


def test_format_2_missing_dir_entry_is_not_owned(tmp_path):
    """format>=2 records dir hashes, so a MISSING entry means unknown provenance
    - unlike format 1, where the absence just means the feature didn't exist."""
    d = _skill(_skills_base(tmp_path), "dcr", "jacked body")
    md = {"dcr": m.skill_dir_hash(d)}
    assert m.is_jacked_skill_dir(d, "dcr", _manifest(md)) is True          # format 1
    assert m.is_jacked_skill_dir(d, "dcr", _manifest(md, {})) is False     # format 2
    assert m.is_jacked_skill_dir(d, "dcr", _manifest(md, {"other": "sha256:x"})) is False


# --- unit: install-time preservation ---------------------------------------

def test_preserve_moves_user_dir_into_backups_root(tmp_path):
    base = _skills_base(tmp_path)
    d = _skill(base, "dcr", "MY OWN skill")
    src = _skill(tmp_path / "src", "dcr", "jacked body")
    backup = m.preserve_user_skill_dir(d, "dcr", src, _manifest({}))
    assert backup.parent == base.parent / "jacked-backups" / "skills"
    assert backup.name.startswith("dcr-")
    assert (backup / "SKILL.md").read_text() == "MY OWN skill"
    assert not d.exists()
    assert not list(base.glob("dcr.pre-jacked*"))   # never beside the live skills


def test_preserve_logs_the_move_as_it_happens(tmp_path, caplog):
    """The notice comes from the helper, so an interrupted install can't strand a
    user's dir silently."""
    d = _skill(_skills_base(tmp_path), "dcr", "MY OWN skill")
    src = _skill(tmp_path / "src", "dcr", "jacked body")
    with caplog.at_level(logging.WARNING, logger="jacked.install_manifest"):
        backup = m.preserve_user_skill_dir(d, "dcr", src, _manifest({}))
    assert str(backup) in caplog.text


def test_preserve_leaves_jacked_owned_dir_in_place(tmp_path):
    base = _skills_base(tmp_path)
    d = _skill(base, "dcr", "jacked v1")
    src = _skill(tmp_path / "src", "dcr", "jacked v2")
    assert m.preserve_user_skill_dir(d, "dcr", src, _owning_manifest(d, "dcr")) is None
    assert (d / "SKILL.md").read_text() == "jacked v1"   # overwritten in place
    assert _backups_of(base, "dcr") == []


def test_preserve_skips_when_already_identical(tmp_path):
    """No manifest entry (older install) but content == what we'd install."""
    base = _skills_base(tmp_path)
    d = _skill(base, "dcr", "jacked v1")
    src = _skill(tmp_path / "src", "dcr", "jacked v1")
    assert m.preserve_user_skill_dir(d, "dcr", src, None) is None
    assert _backups_of(base, "dcr") == []


def test_preserve_no_manifest_entry_backs_up_modified_dir(tmp_path):
    base = _skills_base(tmp_path)
    d = _skill(base, "dcr", "user edited this")
    src = _skill(tmp_path / "src", "dcr", "jacked body")
    backup = m.preserve_user_skill_dir(d, "dcr", src, None)
    assert (backup / "SKILL.md").read_text() == "user edited this"


def test_preserve_never_clobbers_an_earlier_backup(tmp_path):
    base = _skills_base(tmp_path)
    src = _skill(tmp_path / "src", "dcr", "jacked body")
    _skill(base, "dcr", "first user dir")
    first = m.preserve_user_skill_dir(base / "dcr", "dcr", src, _manifest({}))
    _skill(base, "dcr", "second user dir")
    second = m.preserve_user_skill_dir(base / "dcr", "dcr", src, _manifest({}))
    assert first != second
    assert (first / "SKILL.md").read_text() == "first user dir"
    assert (second / "SKILL.md").read_text() == "second user dir"


def test_preserve_missing_dir_is_a_noop(tmp_path):
    assert m.preserve_user_skill_dir(tmp_path / "gone", "gone", None, None) is None


def test_backup_dir_for_is_timestamped_and_unique(tmp_path):
    base = _skills_base(tmp_path)
    target = base / "dcr"
    first = m.backup_dir_for(target, "dcr")
    first.mkdir(parents=True)
    second = m.backup_dir_for(target, "dcr")
    assert first.parent == m.backups_root(target) == base.parent / "jacked-backups" / "skills"
    assert second != first and second.name.startswith(first.name)


# --- unit: source-consistency (the editable / source-moved case) ------------

def test_is_source_subset_rules(tmp_path):
    src = _with_sidecar(tmp_path / "src", "qa", "body", "// v1")
    dest = _with_sidecar(_skills_base(tmp_path), "qa", "body", "// v1")
    assert m.is_source_subset(dest, src) is True
    (dest / "mine.md").write_text("user file", encoding="utf-8")
    assert m.is_source_subset(dest, src) is False       # extra file here
    (dest / "mine.md").unlink()
    (src / "extra.md").write_text("new in source", encoding="utf-8")
    assert m.is_source_subset(dest, src) is True        # extra file in SOURCE is fine
    (dest / "measure.js").write_text("// edited", encoding="utf-8")
    assert m.is_source_subset(dest, src) is False       # edited file
    assert m.is_source_subset(tmp_path / "nope", src) is False


@requires_symlinks
def test_editable_install_source_moved_does_not_back_up_jacked_dir(tmp_path):
    """Reviewer's repro: an editable install symlinks the dest at the source, then
    a `git pull` changes SKILL.md AND the file set. Both recorded hashes miss, but
    the dir is still jacked's - it must NOT be moved aside."""
    base = _skills_base(tmp_path)
    src = _with_sidecar(tmp_path / "src", "qa", "body v1", "// v1")
    dest = base / "qa"
    dest.mkdir()
    for f in ("SKILL.md", "measure.js"):
        (dest / f).symlink_to(src / f)
    manifest = _owning_manifest(dest, "qa")

    # Source moves: SKILL.md changes, a sidecar is dropped, another is added.
    (src / "SKILL.md").write_text("body v2", encoding="utf-8")
    (src / "measure.js").unlink()
    (src / "helper.js").write_text("// new", encoding="utf-8")

    assert m.is_jacked_skill_dir(dest, "qa", manifest) is False   # hashes both miss
    assert m.preserve_user_skill_dir(dest, "qa", src, manifest) is None
    assert _backups_of(base, "qa") == []
    # ... but a genuine user dir is still preserved.
    _skill(base, "dcr", "MY OWN")
    assert m.preserve_user_skill_dir(base / "dcr", "dcr", src, manifest) is not None


# --- unit: sidecar protection (skills_dirs, manifest format 2) --------------

def test_skill_content_hash_tracks_sidecars(tmp_path):
    d = _with_sidecar(_skills_base(tmp_path), "qa", "body", "// v1")
    before = m.skill_content_hash(d)
    (d / "measure.js").write_text("// v2", encoding="utf-8")
    assert m.skill_content_hash(d) != before
    assert m.skill_content_hash(tmp_path / "nope") is None


def test_skill_content_hash_ignores_os_droppings(tmp_path):
    d = _with_sidecar(_skills_base(tmp_path), "qa", "body", "// v1")
    before = m.skill_content_hash(d)
    (d / ".DS_Store").write_bytes(b"\x00junk")
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_bytes(b"junk")
    assert m.skill_content_hash(d) == before


def test_sidecar_edit_makes_dir_user_owned_under_new_manifest(tmp_path):
    d = _with_sidecar(_skills_base(tmp_path), "qa", "body", "// jacked")
    manifest = _owning_manifest(d, "qa")
    assert m.is_jacked_skill_dir(d, "qa", manifest) is True
    (d / "measure.js").write_text("// I tuned this", encoding="utf-8")
    assert m.is_jacked_skill_dir(d, "qa", manifest) is False


def test_sidecar_edit_under_old_manifest_behaves_as_before(tmp_path):
    """No skills_dirs entry and format 1 -> SKILL.md check alone, i.e. today."""
    d = _with_sidecar(_skills_base(tmp_path), "qa", "body", "// jacked")
    old = _manifest({"qa": m.skill_dir_hash(d)})          # format 1
    (d / "measure.js").write_text("// I tuned this", encoding="utf-8")
    assert m.is_jacked_skill_dir(d, "qa", old) is True
    src = _with_sidecar(tmp_path / "src", "qa", "body", "// jacked v2")
    assert m.preserve_user_skill_dir(d, "qa", src, old) is None


def test_sidecar_edited_dir_is_preserved_on_install_and_kept_on_uninstall(tmp_path):
    base = _skills_base(tmp_path)
    d = _with_sidecar(base, "qa", "body", "// jacked")
    manifest = _owning_manifest(d, "qa")
    (d / "measure.js").write_text("// I tuned this", encoding="utf-8")
    src = _with_sidecar(tmp_path / "src", "qa", "body", "// jacked v2")

    backup = m.preserve_user_skill_dir(d, "qa", src, manifest)
    assert (backup / "measure.js").read_text() == "// I tuned this"
    # uninstall-side gate on the same content: kept, not deleted
    remove, why = m.skill_removal_decision(backup, "qa", manifest, src)
    assert remove is False and "no longer matches" in why


def test_hash_installed_skill_dirs_reads_the_installed_tree(tmp_path):
    d = _with_sidecar(_skills_base(tmp_path), "qa", "body", "// jacked")
    out = m.hash_installed_skill_dirs(tmp_path, {"qa": "sha256:x", "absent": "sha256:y"})
    assert out == {"qa": m.skill_content_hash(d)}
    assert m.hash_installed_skill_dirs(tmp_path, {"../evil": "sha256:z"}) == {}


# --- unit: uninstall decision + manifest status -----------------------------

def test_skill_removal_decision_paths(tmp_path):
    base = _skills_base(tmp_path)
    src = _with_sidecar(tmp_path / "src", "qa", "body", "// jacked")
    pristine = _with_sidecar(base, "qa", "body", "// jacked")
    manifest = _owning_manifest(pristine, "qa")
    assert m.skill_removal_decision(pristine, "qa", manifest, src) == (True, "")
    # No manifest at all: content still matches the packaged source -> ours.
    assert m.skill_removal_decision(pristine, "qa", None, src) == (True, "")
    # No manifest and modified -> kept, and the message says WHY honestly.
    (pristine / "measure.js").write_text("// mine", encoding="utf-8")
    remove, why = m.skill_removal_decision(pristine, "qa", None, src)
    assert remove is False
    assert "no install manifest found for qa" in why and "manually" in why
    remove, why = m.skill_removal_decision(pristine, "qa", manifest, src)
    assert remove is False and why == "it no longer matches what jacked installed"


def test_load_with_status_distinguishes_missing_from_corrupt(tmp_path):
    assert m.load_with_status(tmp_path / "nope.json") == (None, "missing")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert m.load_with_status(bad) == (None, "corrupt")
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"version": "1"}), encoding="utf-8")
    assert m.load_with_status(good) == ({"version": "1"}, "ok")


# --- unit: prune hash-gate --------------------------------------------------

def _removed_diff(name: str) -> m.ManifestDiff:
    return m.ManifestDiff({
        "skills": m.CategoryDiff(removed=[name]),
        "commands": m.CategoryDiff(), "agents": m.CategoryDiff(),
        "lenses": m.CategoryDiff(), "templates": m.CategoryDiff(),
    })


def test_prune_removed_deletes_hash_matching_skill(tmp_path):
    d = _skill(_skills_base(tmp_path), "gone", "jacked body")
    prior = _manifest({"gone": m.skill_dir_hash(d)})
    assert m.prune_removed(_removed_diff("gone"), tmp_path, prior) == (["skills/gone"], [])
    assert not d.exists()


def test_prune_removed_keeps_modified_skill(tmp_path):
    d = _skill(_skills_base(tmp_path), "gone", "user rewrote this")
    prior = _manifest({"gone": "sha256:whatever-jacked-shipped"})
    assert m.prune_removed(_removed_diff("gone"), tmp_path, prior) == ([], [])
    assert (d / "SKILL.md").read_text() == "user rewrote this"


def test_prune_removed_ignores_unsafe_names(tmp_path):
    victim = _skill(tmp_path, "outside", "not yours")
    d = _removed_diff("../outside")
    assert m.prune_removed(d, tmp_path / ".claude", _manifest({})) == ([], [])
    assert victim.exists()


# --- unit: the per-skill copy helper ---------------------------------------

def test_copy_skill_tree_copies_sidecars_and_raises_on_bad_source(tmp_path):
    from jacked.cli import _copy_skill_tree

    src = _with_sidecar(tmp_path / "src", "qa", "body", "// side")
    (src / "references").mkdir()
    (src / "references" / "n.md").write_text("notes", encoding="utf-8")
    dest = _skills_base(tmp_path) / "qa"
    assert _copy_skill_tree(src, dest) == 3
    assert (dest / "references" / "n.md").read_text() == "notes"
    with pytest.raises(OSError):
        _copy_skill_tree(src, dest / "SKILL.md" / "nested")


@requires_symlinks
def test_copy_skill_tree_replaces_a_whole_dir_symlink_without_touching_source(tmp_path):
    """A skill dir that is itself a link to the source must be replaced, never
    written through: `_link_or_copy` unlinks each dst first, so writing through
    the link deleted the packaged source files it pointed at."""
    from jacked.cli import _copy_skill_tree, _remove_skill_dir

    src = _with_sidecar(tmp_path / "src", "qa", "body", "// side")
    dest = _skills_base(tmp_path) / "qa"
    dest.symlink_to(src, target_is_directory=True)
    _copy_skill_tree(src, dest)
    assert not dest.is_symlink() and dest.is_dir()
    assert (src / "SKILL.md").read_text() == "body"
    assert not (src / "SKILL.md").is_symlink()

    # The shared removal helper unlinks a link and rmtrees a dir, and neither
    # follows into the source.
    _remove_skill_dir(dest)
    assert not dest.exists()
    dest.symlink_to(src, target_is_directory=True)
    _remove_skill_dir(dest)
    assert not dest.is_symlink()
    assert (src / "SKILL.md").read_text() == "body"


@requires_symlinks
@pytest.mark.parametrize("editable", [True, False])
def test_copy_skill_tree_over_a_symlinked_subdir_keeps_source_bytes(tmp_path, monkeypatch, editable):
    from jacked import cli as _cli

    monkeypatch.setattr(_cli, "_is_editable_install", lambda: editable)
    src = _with_sidecar(tmp_path / "src", "qa", "body", "// side")
    (src / "references").mkdir()
    (src / "references" / "n.md").write_text("notes", encoding="utf-8")
    before = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}
    dest = _skills_base(tmp_path) / "qa"
    dest.mkdir()
    (dest / "references").symlink_to(src / "references", target_is_directory=True)

    assert _cli._copy_skill_tree(src, dest) == 3
    for rel, data in before.items():
        assert (src / rel).is_file() and not (src / rel).is_symlink(), rel
        assert (src / rel).read_bytes() == data, rel
    assert not (dest / "references").is_symlink()
    assert (dest / "references" / "n.md").read_text() == "notes"


@requires_symlinks
def test_link_or_copy_never_unlinks_the_source_it_would_link(tmp_path, monkeypatch):
    """dst reached through a linked parent IS the source file: unlinking it
    deletes the source. Same file means already installed."""
    from jacked import cli as _cli

    monkeypatch.setattr(_cli, "_is_editable_install", lambda: True)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "f.md"
    src.write_text("keep", encoding="utf-8")
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(src_dir, target_is_directory=True)
    assert _cli._link_or_copy(src, linked_parent / "f.md") == "linked-already"
    assert src.is_file() and not src.is_symlink() and src.read_text() == "keep"

    per_file = tmp_path / "per-file.md"
    per_file.symlink_to(src)
    assert _cli._link_or_copy(src, per_file) == "linked-already"
    assert per_file.is_symlink() and src.read_text() == "keep"


@pytest.mark.parametrize("git_entry", ["file", "dir"])
def test_is_editable_install_accepts_a_git_file_or_dir(tmp_path, monkeypatch, git_entry):
    """A normal clone has a `.git` DIR. A git worktree or a submodule checkout
    has a `.git` FILE (a `gitdir:` pointer). Both are source checkouts, so both
    install in editable mode, the mode CI runs in. A package with no `.git`
    beside it is not editable."""
    from jacked import cli as _cli

    repo = tmp_path / "repo"
    data = repo / "jacked" / "data"
    data.mkdir(parents=True)
    monkeypatch.setattr(_cli, "_get_data_root", lambda: data)
    assert _cli._is_editable_install() is False

    if git_entry == "file":
        (repo / ".git").write_text(
            "gitdir: /elsewhere/claude-jacked/.git/worktrees/repo\n", encoding="utf-8",
        )
    else:
        (repo / ".git").mkdir()
    assert _cli._is_editable_install() is True


def test_uninstall_rechecks_ownership_after_staging(tmp_path, monkeypatch):
    """The shared removal helper stages the dir, then re-decides on the staged
    copy, so an edit racing the first check is never deleted."""
    from jacked import cli as _cli

    src = _with_sidecar(tmp_path / "src", "qa", "body", "// side")
    dest = _skills_base(tmp_path) / "qa"
    _cli._copy_skill_tree(src, dest)
    real = m.skill_removal_decision
    calls = []

    def racy(skill_dir, name, manifest, src_dir):
        out = real(skill_dir, name, manifest, src_dir)
        if not calls:
            (dest / "late.md").write_text("mine", encoding="utf-8")
        calls.append(skill_dir)
        return out

    monkeypatch.setattr(m, "skill_removal_decision", racy)
    why = _cli._remove_skill_if_owned(dest, "qa", None, src)
    assert why
    assert (dest / "late.md").read_text() == "mine"
    assert len(calls) == 2


@requires_symlinks
def test_remove_skill_if_owned_never_deletes_the_packaged_source(tmp_path):
    """A skills root symlinked into the data skills dir makes skill_dir the
    source itself. The uninstall helper must keep it, untouched."""
    from jacked import cli as _cli

    src_skills = tmp_path / "data" / "skills"
    src = _with_sidecar(src_skills, "qa", "body", "// side")
    root = tmp_path / ".claude" / "skills"
    root.parent.mkdir(parents=True)
    root.symlink_to(src_skills, target_is_directory=True)
    before = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}

    # An owning manifest is the dangerous case: both ownership checks pass, so
    # without the guard the staged "skill" (the source itself) is rmtree'd.
    for manifest in (_owning_manifest(src, "qa"), None):
        why = _cli._remove_skill_if_owned(root / "qa", "qa", manifest, src)
        assert "packaged source" in why
    assert {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()} == before
    assert not (tmp_path / ".claude" / "jacked-backups").exists()


@requires_symlinks
def test_remove_skill_if_owned_unlinks_a_relative_link_in_place(tmp_path):
    import os

    from jacked import cli as _cli

    src = _with_sidecar(tmp_path / "src", "qa", "body", "// side")
    dest = _skills_base(tmp_path) / "qa"
    os.symlink(os.path.relpath(src, dest.parent), dest, target_is_directory=True)
    assert _cli._remove_skill_if_owned(dest, "qa", None, src) == ""
    assert not dest.is_symlink()
    assert (src / "SKILL.md").read_text() == "body"


def test_stage_for_removal_falls_back_when_staging_mkdir_fails(tmp_path):
    from jacked import cli as _cli

    skills = _skills_base(tmp_path)
    d = _skill(skills, "qa", "body")
    (tmp_path / ".claude" / "jacked-backups").write_text("file", encoding="utf-8")
    staged = _cli._stage_for_removal(d, "qa")
    assert staged.parent == skills
    assert staged.name.startswith(".")
    assert (staged / "SKILL.md").read_text() == "body"
    assert not d.exists()


# --- end-to-end through the CLI --------------------------------------------

@pytest.fixture(autouse=True)
def _keep_guardrails_out_of_the_real_home(monkeypatch):
    """guardrails.deploy_templates resolves its destination from Path.home(),
    NOT from $JACKED_HOME, so a `jacked install --force` in a test rewrites the
    REAL ~/.claude/jacked-guardrails + ~/.claude/jacked-hooks (clobbering any
    local edits the user made to those templates). Nothing here asserts on
    template deployment, so stub it and keep the run inside tmp_path."""
    from jacked import guardrails

    monkeypatch.setattr(
        guardrails, "deploy_templates",
        lambda force=False: {"guardrails": [], "hooks": []},
    )


def _main():
    from jacked.cli import main

    return main


def _install(runner):
    # --no-packs keeps the run offline: the packs step shells out to `npx skills`.
    return runner.invoke(
        _main(),
        ["install", "--force", "--no-tray", "--no-codex", "--no-rules", "--no-packs"],
    )


def test_install_preserves_user_skill_then_uninstall_keeps_it(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    skills = _skills_base(tmp_path)
    _skill(skills, "dcr", "# my own dcr\n")

    runner = CliRunner()
    res = _install(runner)
    assert res.exit_code == 0, res.output
    assert "Preserved your existing skill dcr" in res.output

    backups = _backups_of(skills, "dcr")
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text() == "# my own dcr\n"
    # The notice names the real path (rich soft-wraps it, so collapse newlines).
    assert str(backups[0]) in res.output.replace("\n", "")
    assert "my own dcr" not in (skills / "dcr" / "SKILL.md").read_text()

    manifest = json.loads(
        (tmp_path / ".claude" / "jacked-manifest.json").read_text(encoding="utf-8")
    )
    assert m.skill_dir_hash(skills / "dcr") == manifest["artifacts"]["skills"]["dcr"]

    # A skill the user edits after install must survive uninstall. Replace the
    # file rather than writing into it: an editable install symlinks it back to
    # the repo source, and writing through the link would edit the repo.
    edited = skills / "qa" / "SKILL.md"
    edited.unlink()
    edited.write_text("# I edited this\n", encoding="utf-8")
    res = runner.invoke(_main(), ["uninstall", "--yes"])
    assert res.exit_code == 0, res.output
    assert (skills / "qa" / "SKILL.md").read_text() == "# I edited this\n"
    assert "Kept skill qa" in res.output
    assert not (skills / "dcr").exists()          # untouched jacked copy: removed
    assert backups[0].exists()                    # the user's dir survives both


def test_uninstall_without_manifest_deletes_pristine_keeps_modified(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0
    skills = tmp_path / ".claude" / "skills"
    (tmp_path / ".claude" / "jacked-manifest.json").unlink()

    edited = skills / "qa" / "SKILL.md"
    edited.unlink()
    edited.write_text("# mine now\n", encoding="utf-8")

    res = runner.invoke(_main(), ["uninstall", "--yes"])
    assert res.exit_code == 0, res.output
    assert not (skills / "dcr").exists()          # pristine jacked dir: deleted
    assert (skills / "qa" / "SKILL.md").read_text() == "# mine now\n"
    assert "no install manifest found for qa" in res.output


def test_uninstall_preserves_modified_flat_lens(tmp_path, monkeypatch):
    """The flat categories get the same uninstall gate as skill dirs: a lens the
    user edited is moved to jacked-backups (never unlinked), the console names
    it, and the 'No jacked lenses found' line must not fire alongside it."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0
    lenses = tmp_path / ".claude" / "lenses"
    target = next(lenses.glob("*.md"))
    # Replace, don't write through: an editable install symlinks to the repo.
    target.unlink()
    target.write_text("# my tuned lens\n", encoding="utf-8")

    res = runner.invoke(_main(), ["uninstall", "--yes"])
    assert res.exit_code == 0, res.output
    assert f"Preserved your modified lenses/{target.name}" in res.output
    assert "No jacked lenses found" not in res.output
    assert not target.exists()
    backups = list((tmp_path / ".claude" / "jacked-backups" / "lenses").glob("*.md"))
    assert any(b.read_text(encoding="utf-8") == "# my tuned lens\n" for b in backups)


def test_uninstall_warns_once_on_a_corrupt_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0
    (tmp_path / ".claude" / "jacked-manifest.json").write_text("{broken", encoding="utf-8")

    res = runner.invoke(_main(), ["uninstall", "--yes"])
    assert res.exit_code == 0, res.output
    assert "unreadable" in res.output
    assert not (tmp_path / ".claude" / "skills" / "dcr").exists()   # still cleaned up


def test_install_skips_a_failing_skill_and_continues(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    from jacked import cli as _cli

    real = _cli._copy_skill_tree

    def boom(src_root, skill_dir):
        if src_root.name == "qa":
            raise OSError(13, "Permission denied")
        return real(src_root, skill_dir)

    monkeypatch.setattr(_cli, "_copy_skill_tree", boom)
    res = _install(CliRunner())
    assert res.exit_code == 0, res.output
    assert "Skipped skill qa: " in res.output
    assert (tmp_path / ".claude" / "skills" / "dcr" / "SKILL.md").exists()


def test_skipped_skill_is_not_recorded_and_survives_uninstall(tmp_path, monkeypatch):
    """A skill skipped on OSError leaves the USER's dir at the destination.

    Recording its dir hash anyway would mark that dir jacked-owned, and a later
    uninstall would rmtree the user's data (the Wave-3 CRITICAL). Worst case on
    purpose: the user's SKILL.md is byte-identical to jacked's (they copied it)
    plus a sidecar of their own.
    """
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    import jacked

    pkg_qa = Path(jacked.__file__).parent / "data" / "skills" / "qa"
    user_qa = _skills_base(tmp_path) / "qa"
    import shutil as _sh

    _sh.copytree(pkg_qa, user_qa)
    (user_qa / "my-notes.md").write_text("# my tweaks\n", encoding="utf-8")

    # Fail BEFORE the preserve-move so the user's dir is still sitting at the
    # destination when the manifest is written — the exact deletion setup.
    real_preserve = m.preserve_user_skill_dir

    def boom(skill_dir, name, *a, **kw):
        if name == "qa":
            raise OSError(13, "Permission denied")
        return real_preserve(skill_dir, name, *a, **kw)

    monkeypatch.setattr(m, "preserve_user_skill_dir", boom)
    runner = CliRunner()
    res = _install(runner)
    assert res.exit_code == 0, res.output
    assert "Skipped skill qa: " in res.output
    assert (user_qa / "my-notes.md").exists()     # user's dir still in place

    manifest = json.loads(
        (tmp_path / ".claude" / "jacked-manifest.json").read_text(encoding="utf-8")
    )
    dirs = manifest["artifacts"][m.SKILLS_DIRS_KEY]
    assert "qa" not in dirs                       # skipped -> unrecorded
    assert "dcr" in dirs                          # installed ones still recorded

    monkeypatch.setattr(m, "preserve_user_skill_dir", real_preserve)
    res = runner.invoke(_main(), ["uninstall", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Kept skill qa" in res.output
    assert (user_qa / "my-notes.md").read_text() == "# my tweaks\n"
    assert not (tmp_path / ".claude" / "skills" / "dcr").exists()


@requires_symlinks
def test_uninstall_unlinks_a_symlinked_skill_and_survives_a_locked_one(
    tmp_path, monkeypatch
):
    """The rmtree guard: a symlinked skill dir is unlinked (target untouched),
    and one unremovable skill is reported and skipped, not fatal."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0
    skills = tmp_path / ".claude" / "skills"

    # Editable-install shape: the skill dir is a symlink to identical content
    # living elsewhere (the repo checkout). The hash gate says "jacked's", so
    # removal proceeds — but it must unlink the LINK, never empty the target.
    target = tmp_path / "elsewhere" / "qa"
    import shutil as _sh

    _sh.copytree(skills / "qa", target)
    _sh.rmtree(skills / "qa")
    (skills / "qa").symlink_to(target)

    # Make another skill unremovable.
    real_rmtree = _sh.rmtree

    def locked(path, *a, **kw):
        # Removal stages the dir under a unique name first (dcr-<token>).
        if Path(path).name == "dcr" or Path(path).name.startswith("dcr-"):
            raise OSError(13, "Permission denied")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr("shutil.rmtree", locked)
    res = runner.invoke(_main(), ["uninstall", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Could not remove skill dcr" in res.output
    assert (skills / "dcr").exists()              # locked skill left in place
    assert not (skills / "qa").is_symlink()       # link removed...
    assert (target / "SKILL.md").exists()         # ...target contents intact


def test_install_records_dir_hashes_and_preserves_a_sidecar_edit(tmp_path, monkeypatch):
    """Round trip: install -> user drops a file in a skill dir -> upgrade."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0

    manifest_path = tmp_path / ".claude" / "jacked-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["format"] == m.MANIFEST_FORMAT
    dirs = manifest["artifacts"][m.SKILLS_DIRS_KEY]
    assert dirs and set(dirs) <= set(manifest["artifacts"]["skills"])

    skills = tmp_path / ".claude" / "skills"
    # A sidecar the user adds — SKILL.md is untouched, so only the dir hash sees it.
    (skills / "qa" / "my-notes.md").write_text("# my tweaks\n", encoding="utf-8")
    assert m.skill_dir_hash(skills / "qa") == manifest["artifacts"]["skills"]["qa"]
    assert m.is_jacked_skill_dir(skills / "qa", "qa", manifest) is False

    assert _install(runner).exit_code == 0
    backups = _backups_of(skills, "qa")
    assert len(backups) == 1
    assert (backups[0] / "my-notes.md").read_text() == "# my tweaks\n"
    assert (skills / "qa" / "SKILL.md").exists()
    assert not (skills / "qa" / "my-notes.md").exists()   # fresh jacked copy


def test_second_install_overwrites_jacked_dir_without_new_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0
    assert _install(runner).exit_code == 0
    backups_root = tmp_path / ".claude" / "jacked-backups"
    assert not backups_root.exists()
    assert list((tmp_path / ".claude" / "skills").glob("*.pre-jacked*")) == []


# --- #145i: a failed copy puts the user's own skill back ---------------------

_ENOSPC = OSError(errno.ENOSPC, "No space left on device")


def _tree(d: Path) -> list:
    """Every entry under `d` (files AND dirs), as sorted posix paths."""
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))


def _fail_copy_after_one_file(monkeypatch, name: str = "dcr") -> list:
    """Make the CLI copy of skill `name` write ONE source sidecar and then fail
    with ENOSPC, the way a full disk stops a copy part-way. Other skills copy
    as usual. Returns the list the fake appends each written path to."""
    from jacked import cli as _cli

    real = _cli._copy_skill_tree
    written: list = []

    def half_copy(src_root, skill_dir):
        if Path(src_root).name != name:
            return real(src_root, skill_dir)
        sidecar = next(
            p for p in sorted(Path(src_root).rglob("*"))
            if p.is_file() and p.name != "SKILL.md"
        )
        dst = Path(skill_dir) / sidecar.relative_to(src_root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # An editable install links each file to the repo source: unlink the
        # link first, as `_link_or_copy` does, so this never writes through it.
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.write_bytes(sidecar.read_bytes())
        written.append(dst)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_cli, "_copy_skill_tree", half_copy)
    return written


def _install_wide(runner, monkeypatch):
    """`_install` with a console wide enough that rich never wraps a line, so
    the tests can match each printed line exactly."""
    monkeypatch.setenv("COLUMNS", "1000")
    return _install(runner)


def test_failed_copy_restores_the_users_own_skill(tmp_path, monkeypatch):
    """Acceptance (1): the copy writes one file and then hits a full disk. The
    partial copy is removed and the user's own dir is moved back from
    jacked-backups, byte for byte. Claude Code must never load a broken
    half-copy while the user's working skill sits in a backup dir."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    skills = _skills_base(tmp_path)
    user_dir = skills / "dcr"
    user_dir.mkdir()
    own = "# my own dcr\nnotes: café\n".encode("utf-8")
    (user_dir / "SKILL.md").write_bytes(own)
    written = _fail_copy_after_one_file(monkeypatch)

    res = _install_wide(CliRunner(), monkeypatch)

    assert res.exit_code == 0, res.output
    assert written, "the failing copy never ran for dcr"
    assert (user_dir / "SKILL.md").read_bytes() == own
    assert _tree(user_dir) == ["SKILL.md"]          # nothing of jacked's is left
    assert _backups_of(skills, "dcr") == []          # the backup entry is gone
    assert "Preserved your existing skill dcr" in res.output
    assert "Skipped skill dcr: " in res.output
    assert f"Skipped skill dcr: {_ENOSPC}; your own skill was restored" in res.output
    # The other skills still installed.
    assert (skills / "qa" / "SKILL.md").exists()


def test_failed_copy_over_jackeds_own_skill_attempts_no_restore(tmp_path, monkeypatch):
    """Acceptance (4): the dir is jacked's own copy (the manifest owns it), so
    nothing was moved aside and there is nothing of the user's to put back.
    The CLI prints the plain skip line and leaves the dir as the copy left it."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    runner = CliRunner()
    assert _install(runner).exit_code == 0           # dcr is jacked's own now
    skills = tmp_path / ".claude" / "skills"
    moves: list = []
    real_move = shutil.move

    def spy_move(src, dst, *a, **kw):
        moves.append((Path(src), Path(dst)))
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "move", spy_move)
    written = _fail_copy_after_one_file(monkeypatch)

    res = _install_wide(runner, monkeypatch)

    assert res.exit_code == 0, res.output
    assert written
    assert f"Skipped skill dcr: {_ENOSPC}\n" in res.output
    assert "your own skill" not in res.output
    assert "Preserved your existing skill dcr" not in res.output
    assert [m_ for m_ in moves if "dcr" in (m_[0].name, m_[1].name)] == []
    assert (skills / "dcr" / "SKILL.md").exists()    # left as the copy left it
    assert _backups_of(skills, "dcr") == []


def test_failed_restore_names_where_the_users_skill_is(tmp_path, monkeypatch, caplog):
    """Acceptance (5): the backup cannot be moved back. The CLI names the
    backup path, the backup keeps the user's bytes, the partial copy is gone,
    and the failure is logged at WARNING with both paths."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    skills = _skills_base(tmp_path)
    user_dir = skills / "dcr"
    user_dir.mkdir()
    (user_dir / "SKILL.md").write_bytes(b"# my own dcr\n")
    _fail_copy_after_one_file(monkeypatch)
    real_move = shutil.move

    def no_move_back(src, dst, *a, **kw):
        if "jacked-backups" in Path(src).parts and Path(dst).name == "dcr":
            raise OSError(errno.EACCES, "Permission denied")
        return real_move(src, dst, *a, **kw)

    monkeypatch.setattr(shutil, "move", no_move_back)

    with caplog.at_level(logging.WARNING, logger="jacked.cli"):
        res = _install_wide(CliRunner(), monkeypatch)

    assert res.exit_code == 0, res.output
    backups = _backups_of(skills, "dcr")
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_bytes() == b"# my own dcr\n"
    assert (
        f"Skipped skill dcr: {_ENOSPC}; your own skill is at {backups[0]}" in res.output
    )
    assert not user_dir.exists()                     # no broken half-copy is left
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(backups[0]) in w and str(user_dir) in w for w in warned), warned


def test_skip_line_is_plain_when_a_new_skill_fails(tmp_path, monkeypatch):
    """A skill dir that did not exist before: the partial copy is removed, and
    with no user dir to restore the CLI prints the plain skip line."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    skills = _skills_base(tmp_path)
    _fail_copy_after_one_file(monkeypatch)

    res = _install_wide(CliRunner(), monkeypatch)

    assert res.exit_code == 0, res.output
    assert f"Skipped skill dcr: {_ENOSPC}\n" in res.output
    assert "your own skill" not in res.output
    assert not (skills / "dcr").exists()             # partial copy removed


# --- the shared transaction helper, called directly --------------------------

def _transaction(src, dst, manifest, *, copy, preserve=None):
    from jacked import cli as _cli

    return _cli._install_skill_dir_transactional(
        src, dst, dst.name, manifest,
        preserve=preserve or m.preserve_user_skill_dir, copy=copy,
    )


def _half_copy(name: str = "partial.md"):
    def copy(src, dst):
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / name).write_text("half\n", encoding="utf-8")
        raise OSError(errno.ENOSPC, "No space left on device")

    return copy


def test_transaction_success_reports_backup_and_file_count(tmp_path):
    from jacked import cli as _cli

    src = _with_sidecar(tmp_path / "src", "dcr", "# jacked dcr\n", "// js\n")
    dst = _skill(_skills_base(tmp_path), "dcr", "# my own dcr\n")

    out = _transaction(src, dst, None, copy=_cli._copy_skill_tree)

    assert out.error is None and out.rollback_error is None
    assert out.restored is False
    assert out.copied == 2
    assert out.backup is not None and (out.backup / "SKILL.md").read_text() == "# my own dcr\n"
    assert (dst / "measure.js").read_text() == "// js\n"


def test_transaction_restores_the_moved_aside_dir(tmp_path):
    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    skills = _skills_base(tmp_path)
    dst = _with_sidecar(skills, "dcr", "# my own dcr\n", "// mine\n")

    out = _transaction(src, dst, None, copy=_half_copy())

    assert isinstance(out.error, OSError) and out.error.errno == errno.ENOSPC
    assert out.restored is True
    assert out.backup is None                        # it is back at its own path
    assert out.rollback_error is None
    assert _tree(dst) == ["SKILL.md", "measure.js"]
    assert (dst / "SKILL.md").read_text() == "# my own dcr\n"
    assert _backups_of(skills, "dcr") == []


def test_transaction_over_jackeds_own_copy_attempts_no_restore(tmp_path, monkeypatch):
    """Acceptance (4), at the helper: backup None, restored False, and the dir
    is left as the copy left it (no remove, no move)."""
    from jacked import cli as _cli

    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    dst = _skill(_skills_base(tmp_path), "dcr", "# jacked dcr\n")
    owning = _owning_manifest(dst, "dcr")
    touched: list = []
    monkeypatch.setattr(_cli, "_remove_skill_dir", lambda d: touched.append(("rm", d)))
    monkeypatch.setattr(shutil, "move", lambda s, d, *a, **kw: touched.append(("mv", s, d)))

    out = _transaction(src, dst, owning, copy=_half_copy())

    assert out.error is not None and out.error.errno == errno.ENOSPC
    assert out.backup is None and out.restored is False
    assert out.rollback_error is None
    assert touched == []
    assert _tree(dst) == ["SKILL.md", "partial.md"]


def test_transaction_removes_the_partial_copy_of_a_new_skill(tmp_path):
    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    dst = _skills_base(tmp_path) / "dcr"

    out = _transaction(src, dst, None, copy=_half_copy())

    assert out.error is not None
    assert out.backup is None and out.restored is False and out.rollback_error is None
    assert not dst.exists() and not dst.is_symlink()


def test_transaction_reports_a_move_back_that_fails(tmp_path, monkeypatch, caplog):
    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    skills = _skills_base(tmp_path)
    dst = _skill(skills, "dcr", "# my own dcr\n")
    real_move = shutil.move
    refused = OSError(errno.EACCES, "Permission denied")

    def no_move_back(s, d, *a, **kw):
        if Path(d) == dst:
            raise refused
        return real_move(s, d, *a, **kw)

    monkeypatch.setattr(shutil, "move", no_move_back)
    with caplog.at_level(logging.WARNING, logger="jacked.cli"):
        out = _transaction(src, dst, None, copy=_half_copy())

    assert out.restored is False
    assert out.rollback_error is refused
    assert out.backup is not None and (out.backup / "SKILL.md").read_text() == "# my own dcr\n"
    assert not dst.exists()                          # the partial copy is gone
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(out.backup) in w and str(dst) in w for w in msgs), msgs


def test_transaction_reports_a_partial_copy_it_cannot_remove(tmp_path, monkeypatch, caplog):
    """The partial copy cannot be removed, so the path is not free: the backup
    stays where it is (never moved INTO the partial dir) and both failures are
    visible."""
    from jacked import cli as _cli

    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    skills = _skills_base(tmp_path)
    dst = _skill(skills, "dcr", "# my own dcr\n")
    locked = OSError(errno.EACCES, "Permission denied")

    def cannot_remove(d):
        raise locked

    monkeypatch.setattr(_cli, "_remove_skill_dir", cannot_remove)
    with caplog.at_level(logging.WARNING, logger="jacked.cli"):
        out = _transaction(src, dst, None, copy=_half_copy())

    assert out.error is not None and out.error.errno == errno.ENOSPC
    assert out.rollback_error is locked
    assert out.restored is False
    assert out.backup is not None and (out.backup / "SKILL.md").read_text() == "# my own dcr\n"
    assert _tree(dst) == ["partial.md"]              # the backup was not nested in it
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(dst) in w for w in msgs), msgs


def test_transaction_never_moves_a_backup_into_a_path_that_is_in_use(tmp_path, caplog):
    """Something else takes the path between the move-aside and the copy. The
    helper did not make that dir, so it does not remove it, and the backup is
    not moved into it (shutil.move would nest it there)."""
    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    skills = _skills_base(tmp_path)
    dst = _skill(skills, "dcr", "# my own dcr\n")

    def preserve_then_reoccupy(skill_dir, name, src_dir, manifest):
        backup = m.preserve_user_skill_dir(skill_dir, name, src_dir, manifest)
        _skill(skills, "dcr", "# someone else\n")
        return backup

    def failing_copy(s, d):
        raise OSError(errno.ENOSPC, "No space left on device")

    with caplog.at_level(logging.WARNING, logger="jacked.cli"):
        out = _transaction(src, dst, None, copy=failing_copy, preserve=preserve_then_reoccupy)

    assert out.restored is False
    assert isinstance(out.rollback_error, FileExistsError)
    assert (dst / "SKILL.md").read_text() == "# someone else\n"
    assert _tree(dst) == ["SKILL.md"]
    assert out.backup is not None and (out.backup / "SKILL.md").read_text() == "# my own dcr\n"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(out.backup) in w and str(dst) in w for w in msgs), msgs


def test_transaction_never_raises_when_a_path_check_fails(tmp_path, monkeypatch, caplog):
    """One unreadable path must never stop a whole install: a failing path
    check goes into the outcome, the backup is left alone, and the rollback
    failure is logged with the backup path."""
    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    dst = _skills_base(tmp_path) / "dcr"
    kept = _skill(tmp_path / "kept", "dcr", "# my own dcr\n")
    real_exists = Path.exists

    def broken_exists(self, *a, **kw):
        if self == dst:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real_exists(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", broken_exists)
    with caplog.at_level(logging.WARNING, logger="jacked.cli"):
        out = _transaction(src, dst, None, copy=_half_copy(), preserve=lambda *a: kept)

    assert isinstance(out.error, PermissionError)
    assert isinstance(out.rollback_error, PermissionError)
    assert out.restored is False and out.backup == kept
    assert (kept / "SKILL.md").read_text() == "# my own dcr\n"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(kept) in w for w in msgs), msgs


def test_transaction_preserve_failure_never_starts_the_copy(tmp_path):
    src = _skill(tmp_path / "src", "dcr", "# jacked dcr\n")
    dst = _skill(_skills_base(tmp_path), "dcr", "# my own dcr\n")
    denied = OSError(errno.EACCES, "Permission denied")
    copied: list = []

    def preserve(*a):
        raise denied

    out = _transaction(src, dst, None, copy=lambda s, d: copied.append(d), preserve=preserve)

    assert out.error is denied
    assert out.backup is None and out.restored is False and out.rollback_error is None
    assert copied == []
    assert (dst / "SKILL.md").read_text() == "# my own dcr\n"

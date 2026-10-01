"""Unit tests for dynamic skill discovery and toggle helpers."""

import asyncio
import json
from pathlib import Path
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.testclient import TestClient

from jacked import install_manifest as mani
from jacked.api.routes.features import (
    _get_valid_knowledge_names,
    _get_valid_skill_names,
    _toggle_knowledge,
)
from tests._platform import requires_symlinks


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# _get_valid_skill_names
# ---------------------------------------------------------------------------

class TestGetValidSkillNames:
    def test_missing_skills_dir_returns_empty(self, tmp_path):
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            assert _get_valid_skill_names() == []

    def test_empty_skills_dir_returns_empty(self, tmp_path):
        (tmp_path / "skills").mkdir()
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            assert _get_valid_skill_names() == []

    def test_dirs_without_skill_md_excluded(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "no-skill-file").mkdir()  # dir without SKILL.md
        (skills / "stray.txt").write_text("x")  # file, not dir
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            assert _get_valid_skill_names() == []

    def test_valid_skill_included(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        skill_dir = skills / "dcr"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: dcr\n---\n")
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            assert _get_valid_skill_names() == ["dcr"]

    def test_mixed_valid_and_invalid_dirs(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        for name in ("qa", "ux"):
            d = skills / name
            d.mkdir()
            (d / "SKILL.md").write_text("---\nname: test\n---\n")
        (skills / "incomplete").mkdir()  # no SKILL.md
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            assert _get_valid_skill_names() == ["qa", "ux"]

    def test_result_is_sorted(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        for name in ("ux", "dcr", "qa"):
            d = skills / name
            d.mkdir()
            (d / "SKILL.md").write_text("")
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            assert _get_valid_skill_names() == ["dcr", "qa", "ux"]


# ---------------------------------------------------------------------------
# _get_valid_knowledge_names
# ---------------------------------------------------------------------------

class TestGetValidKnowledgeNames:
    def test_base_names_always_present(self, tmp_path):
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            result = _get_valid_knowledge_names()
        assert "rules" in result
        assert "reference" in result

    def test_no_skills_dir_returns_base_only(self, tmp_path):
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            result = _get_valid_knowledge_names()
        assert result == {"rules", "reference"}

    def test_skill_names_prefixed_correctly(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        for name in ("dcr", "qa"):
            d = skills / name
            d.mkdir()
            (d / "SKILL.md").write_text("")
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            result = _get_valid_knowledge_names()
        assert "skill_dcr" in result
        assert "skill_qa" in result
        assert "rules" in result
        assert "reference" in result

    def test_returns_set(self, tmp_path):
        with mock.patch("jacked.api.routes.features.DATA_ROOT", tmp_path):
            result = _get_valid_knowledge_names()
        assert isinstance(result, set)


# ---------------------------------------------------------------------------
# _toggle_knowledge — skill_ branch
# ---------------------------------------------------------------------------

@pytest.fixture()
def skill_env(tmp_path):
    """Set up a fake DATA_ROOT with a 'dcr' skill and a fake CLAUDE_DIR."""
    data_root = tmp_path / "data"
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir()

    skills_src = data_root / "skills" / "dcr"
    skills_src.mkdir(parents=True)
    (skills_src / "SKILL.md").write_text("---\nname: dcr\n---\n")

    # Provide a dummy rules dir so other toggle paths don't crash
    (data_root / "rules").mkdir(parents=True)

    with mock.patch("jacked.api.routes.features.DATA_ROOT", data_root), \
         mock.patch("jacked.api.routes.features.CLAUDE_DIR", claude_dir), \
         mock.patch("jacked.api.routes.features.SETTINGS_JSON", tmp_path / "settings.json"):
        (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
        yield {"data_root": data_root, "claude_dir": claude_dir}


class TestToggleKnowledgeSkillBranch:
    def test_enable_copies_skill_md(self, skill_env):
        result = _run(_toggle_knowledge("skill_dcr", True))
        dst = skill_env["claude_dir"] / "skills" / "dcr" / "SKILL.md"
        assert dst.exists()
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": True}

    def test_disable_removes_skill_md(self, skill_env):
        # First enable
        _run(_toggle_knowledge("skill_dcr", True))
        dst = skill_env["claude_dir"] / "skills" / "dcr" / "SKILL.md"
        assert dst.exists()

        # Then disable
        result = _run(_toggle_knowledge("skill_dcr", False))
        assert not dst.exists()
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": False}

    def test_invalid_skill_name_returns_422(self, skill_env):
        from fastapi.responses import JSONResponse
        result = _run(_toggle_knowledge("skill_nonexistent", True))
        assert isinstance(result, JSONResponse)
        assert result.status_code == 422

    def test_path_traversal_attempt_returns_422(self, skill_env):
        from fastapi.responses import JSONResponse
        result = _run(_toggle_knowledge("skill_../../etc/passwd", True))
        assert isinstance(result, JSONResponse)
        assert result.status_code == 422

    def test_disable_nonexistent_file_is_noop(self, skill_env):
        # Disabling when already disabled should not raise
        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": False}


# ---------------------------------------------------------------------------
# _toggle_knowledge: the WHOLE skill tree, with the CLI's ownership rules
# ---------------------------------------------------------------------------
#
# A skill is a directory, not a file: logo-forge/launch-post ship scripts/,
# night-shift ships agents/, dcr ships references/. The dashboard toggle used to
# copy only SKILL.md on enable (a broken skill) and unlink only SKILL.md on
# disable (orphaned sidecars). It now reuses the CLI installer's tree copy and
# uninstall gate, and records what it wrote in the install manifest.



@pytest.fixture()
def tree_env(skill_env, monkeypatch):
    """skill_env plus sidecars on the 'dcr' source, copy-mode by default."""
    import jacked.cli as cli

    src = skill_env["data_root"] / "skills" / "dcr"
    (src / "references").mkdir()
    (src / "references" / "lenses.md").write_text("lens notes\n", encoding="utf-8")
    (src / "scripts").mkdir()
    (src / "scripts" / "run.js").write_text("console.log(1)\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    skill_env["src"] = src
    skill_env["dst"] = skill_env["claude_dir"] / "skills" / "dcr"
    skill_env["manifest"] = skill_env["claude_dir"] / "jacked-manifest.json"
    return skill_env


def _body(resp: JSONResponse) -> dict:
    return json.loads(resp.body)


class TestToggleSkillTree:
    def test_enable_copies_every_sidecar(self, tree_env):
        result = _run(_toggle_knowledge("skill_dcr", True))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": True}
        dst = tree_env["dst"]
        assert (dst / "SKILL.md").read_text() == "---\nname: dcr\n---\n"
        assert (dst / "references" / "lenses.md").read_text() == "lens notes\n"
        assert (dst / "scripts" / "run.js").read_text() == "console.log(1)\n"
        assert not (dst / "SKILL.md").is_symlink()

    def test_disable_removes_whole_unmodified_dir(self, tree_env):
        _run(_toggle_knowledge("skill_dcr", True))
        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": False}
        assert not tree_env["dst"].exists()
        # The packaged source is never touched.
        assert (tree_env["src"] / "scripts" / "run.js").exists()

    def test_disable_keeps_user_modified_dir_and_says_why(self, tree_env):
        _run(_toggle_knowledge("skill_dcr", True))
        edited = tree_env["dst"] / "scripts" / "run.js"
        edited.unlink()
        edited.write_text("// my tweak\n", encoding="utf-8")

        result = _run(_toggle_knowledge("skill_dcr", False))
        assert isinstance(result, JSONResponse)
        assert result.status_code == 409
        err = _body(result)["error"]
        assert err["code"] == "SKILL_MODIFIED"
        assert "dcr" in err["message"]
        assert "—" not in err["message"]
        assert edited.read_text() == "// my tweak\n"
        assert (tree_env["dst"] / "SKILL.md").exists()

    def test_disable_keeps_user_added_file(self, tree_env):
        _run(_toggle_knowledge("skill_dcr", True))
        (tree_env["dst"] / "my-notes.md").write_text("mine\n", encoding="utf-8")
        result = _run(_toggle_knowledge("skill_dcr", False))
        assert isinstance(result, JSONResponse) and result.status_code == 409
        assert (tree_env["dst"] / "my-notes.md").read_text() == "mine\n"

    def test_enable_records_ownership_in_existing_manifest(self, tree_env):
        manifest = tree_env["manifest"]
        manifest.write_text(json.dumps({
            "version": "0.105.0", "format": 2, "written_at": "2026-09-11T00:00:00+00:00",
            "artifacts": {"skills": {"qa": "sha256:abc"}, "skills_dirs": {}, "agents": {"x": "sha256:1"}},
        }), encoding="utf-8")

        _run(_toggle_knowledge("skill_dcr", True))

        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["version"] == "0.105.0"
        assert data["artifacts"]["agents"] == {"x": "sha256:1"}
        assert data["artifacts"]["skills"]["qa"] == "sha256:abc"
        assert data["artifacts"]["skills"]["dcr"] == mani.skill_dir_hash(tree_env["dst"])
        assert data["artifacts"]["skills_dirs"]["dcr"] == mani.skill_content_hash(tree_env["dst"])
        # A later upgrade/uninstall recognizes the dashboard copy as jacked's own,
        # even after the packaged source has moved on.
        (tree_env["src"] / "scripts" / "run.js").write_text("console.log(2)\n", encoding="utf-8")
        assert mani.is_jacked_skill_dir(tree_env["dst"], "dcr", data)
        assert mani.preserve_user_skill_dir(tree_env["dst"], "dcr", tree_env["src"], data) is None

    def test_enable_without_manifest_does_not_create_one(self, tree_env):
        _run(_toggle_knowledge("skill_dcr", True))
        assert not tree_env["manifest"].exists()

    def test_enable_leaves_corrupt_manifest_alone(self, tree_env):
        tree_env["manifest"].write_text("{broken", encoding="utf-8")
        result = _run(_toggle_knowledge("skill_dcr", True))
        assert result["enabled"] is True
        assert tree_env["manifest"].read_text(encoding="utf-8") == "{broken"

    def test_enable_moves_a_users_own_dir_aside(self, tree_env):
        dst = tree_env["dst"]
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text("# my own dcr\n", encoding="utf-8")

        result = _run(_toggle_knowledge("skill_dcr", True))
        backup = Path(result["preserved_backup"])
        assert (backup / "SKILL.md").read_text() == "# my own dcr\n"
        assert backup.parent == tree_env["claude_dir"] / "jacked-backups" / "skills"
        assert (dst / "references" / "lenses.md").exists()

    def test_enable_failure_rolls_back_a_fresh_dir(self, tree_env, monkeypatch):
        import jacked.cli as cli

        def half_copy(src_root, skill_dir):
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("partial", encoding="utf-8")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(cli, "_copy_skill_tree", half_copy)
        result = _run(_toggle_knowledge("skill_dcr", True))
        assert isinstance(result, JSONResponse) and result.status_code == 500
        assert _body(result)["error"]["code"] == "SKILL_INSTALL_FAILED"
        assert not tree_env["dst"].exists()

    @requires_symlinks
    def test_editable_enable_links_files_and_disable_spares_source(self, tree_env, monkeypatch):
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_is_editable_install", lambda: True)
        _run(_toggle_knowledge("skill_dcr", True))
        link = tree_env["dst"] / "references" / "lenses.md"
        assert link.is_symlink()
        assert link.resolve() == (tree_env["src"] / "references" / "lenses.md").resolve()

        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result["enabled"] is False
        assert not tree_env["dst"].exists()
        assert (tree_env["src"] / "references" / "lenses.md").read_text() == "lens notes\n"
        assert (tree_env["src"] / "SKILL.md").exists()

    @requires_symlinks
    def test_whole_dir_symlink_disable_unlinks_only_the_link(self, tree_env):
        dst = tree_env["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(tree_env["src"], target_is_directory=True)

        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result["enabled"] is False
        assert not dst.exists() and not dst.is_symlink()
        assert (tree_env["src"] / "scripts" / "run.js").exists()

    @requires_symlinks
    def test_whole_dir_symlink_enable_never_writes_through_to_source(self, tree_env, monkeypatch):
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_is_editable_install", lambda: True)
        dst = tree_env["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(tree_env["src"], target_is_directory=True)

        _run(_toggle_knowledge("skill_dcr", True))
        src = tree_env["src"]
        for rel in ("SKILL.md", "references/lenses.md", "scripts/run.js"):
            assert (src / rel).is_file() and not (src / rel).is_symlink(), rel
        assert (src / "SKILL.md").read_text() == "---\nname: dcr\n---\n"
        assert not dst.is_symlink() and dst.is_dir()
        assert (dst / "scripts" / "run.js").resolve() == (src / "scripts" / "run.js").resolve()

    def test_route_reports_kept_skill_as_409(self, tree_env):
        from jacked.api.routes.features import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        client = TestClient(app)
        assert client.put("/api/features/knowledge/skill_dcr", json={"enabled": True}).status_code == 200
        (tree_env["dst"] / "SKILL.md").unlink()
        (tree_env["dst"] / "SKILL.md").write_text("# edited\n", encoding="utf-8")

        resp = client.put("/api/features/knowledge/skill_dcr", json={"enabled": False})
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "SKILL_MODIFIED"
        assert (tree_env["dst"] / "SKILL.md").read_text() == "# edited\n"

    def test_route_rejects_dotdot_skill_name(self, tree_env, monkeypatch):
        """`skill_..` reaches the handler intact (no slash for the router to
        split on). The route's own name check must be what rejects it: a spy
        proves `_validate_name` saw the name and said no, and the message is the
        name-check one, not the allowlist's "Unknown knowledge item"."""
        from jacked.api.routes import features as feat
        from jacked.api.routes.features import router

        (tree_env["data_root"] / "SKILL.md").write_text("planted", encoding="utf-8")
        seen = []
        real = feat._validate_name

        def spy(n):
            out = real(n)
            seen.append((n, out))
            return out

        monkeypatch.setattr(feat, "_validate_name", spy)
        app = FastAPI()
        app.include_router(router, prefix="/api")
        resp = TestClient(app).put("/api/features/knowledge/skill_..", json={"enabled": True})
        assert resp.status_code == 422
        assert resp.json()["error"] == {"message": "Invalid feature name", "code": "INVALID_FEATURE"}
        assert ("skill_..", False) in seen
        assert not (tree_env["claude_dir"] / "skills").exists()

    def test_direct_traversal_name_is_rejected_by_the_name_guard(self, tree_env, monkeypatch):
        """Plant `data_root/x/SKILL.md` so the source-exists allowlist would
        ACCEPT `../x`. Only the name guard stands in the way."""
        from jacked.api.routes import features as feat

        (tree_env["data_root"] / "x").mkdir()
        (tree_env["data_root"] / "x" / "SKILL.md").write_text("planted", encoding="utf-8")
        seen = []
        real = feat._validate_name

        def spy(n):
            out = real(n)
            seen.append((n, out))
            return out

        monkeypatch.setattr(feat, "_validate_name", spy)
        result = _run(_toggle_knowledge("skill_../x", True))
        assert isinstance(result, JSONResponse) and result.status_code == 422
        assert _body(result)["error"]["code"] == "INVALID_FEATURE"
        assert ("../x", False) in seen
        assert not (tree_env["claude_dir"] / "x").exists()

    @pytest.mark.parametrize("bad", ["..", "../x"])
    def test_final_path_check_holds_even_if_the_name_guard_fails(self, tree_env, monkeypatch, bad):
        """Defense in depth: with `_validate_name` broken open and a planted
        source, `skill_..` would resolve skill_dir to CLAUDE_DIR itself. The
        final path check in `_toggle_skill` must still refuse."""
        from jacked.api.routes import features as feat

        (tree_env["data_root"] / "SKILL.md").write_text("planted", encoding="utf-8")
        (tree_env["data_root"] / "x").mkdir()
        (tree_env["data_root"] / "x" / "SKILL.md").write_text("planted", encoding="utf-8")
        monkeypatch.setattr(feat, "_validate_name", lambda n: True)
        before = sorted(p.name for p in tree_env["claude_dir"].iterdir())
        for enabled in (True, False):
            result = _run(_toggle_knowledge(f"skill_{bad}", enabled))
            assert isinstance(result, JSONResponse) and result.status_code == 422
        assert sorted(p.name for p in tree_env["claude_dir"].iterdir()) == before
        assert not (tree_env["claude_dir"] / "SKILL.md").exists()
        assert (tree_env["data_root"] / "SKILL.md").exists()

    def test_unknown_skill_is_rejected(self, tree_env):
        result = _run(_toggle_knowledge("skill_nope", True))
        assert isinstance(result, JSONResponse) and result.status_code == 422
        assert _body(result)["error"]["code"] == "INVALID_FEATURE"
        assert not (tree_env["claude_dir"] / "skills" / "nope").exists()

    # --- review wave fixes ------------------------------------------------

    @requires_symlinks
    @pytest.mark.parametrize("editable", [True, False])
    def test_enable_over_a_symlinked_subdir_never_touches_source(self, tree_env, monkeypatch, editable):
        """`references -> <src>/references` inside a REAL skill dir: writing
        into it went through the link, and `_link_or_copy` unlinked the source
        file before replacing it."""
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_is_editable_install", lambda: editable)
        src, dst = tree_env["src"], tree_env["dst"]
        before = {p.relative_to(src): p.read_bytes() for p in src.rglob("*") if p.is_file()}
        dst.mkdir(parents=True)
        (dst / "references").symlink_to(src / "references", target_is_directory=True)

        result = _run(_toggle_knowledge("skill_dcr", True))
        assert result["enabled"] is True
        for rel, data in before.items():
            p = src / rel
            assert p.is_file() and not p.is_symlink(), rel
            assert p.read_bytes() == data, rel
        assert not (dst / "references").is_symlink()
        assert (dst / "references" / "lenses.md").read_text() == "lens notes\n"

    def test_enable_failure_restores_the_users_own_dir(self, tree_env, monkeypatch):
        import jacked.cli as cli

        dst = tree_env["dst"]
        dst.mkdir(parents=True)
        (dst / "SKILL.md").write_text("# my own dcr\n", encoding="utf-8")
        (dst / "notes.md").write_text("mine\n", encoding="utf-8")

        def half_copy(src_root, skill_dir):
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text("partial", encoding="utf-8")
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(cli, "_copy_skill_tree", half_copy)
        result = _run(_toggle_knowledge("skill_dcr", True))
        assert isinstance(result, JSONResponse) and result.status_code == 500
        err = _body(result)["error"]
        assert err["code"] == "SKILL_INSTALL_FAILED"
        assert "restored" in err["message"]
        assert "preserved_backup" not in err
        assert (dst / "SKILL.md").read_text() == "# my own dcr\n"
        assert (dst / "notes.md").read_text() == "mine\n"
        backups = tree_env["claude_dir"] / "jacked-backups" / "skills"
        assert not backups.exists() or list(backups.iterdir()) == []

    def test_disable_rechecks_ownership_after_staging(self, tree_env, monkeypatch):
        """A user edit that lands between the ownership check and the delete
        must survive: the dir is staged first, then checked again."""
        _run(_toggle_knowledge("skill_dcr", True))
        real = mani.skill_removal_decision
        calls = []

        def racy(skill_dir, name, manifest, src_dir):
            out = real(skill_dir, name, manifest, src_dir)
            if not calls:
                (tree_env["dst"] / "late-edit.md").write_text("mine\n", encoding="utf-8")
            calls.append(Path(skill_dir))
            return out

        monkeypatch.setattr(mani, "skill_removal_decision", racy)
        result = _run(_toggle_knowledge("skill_dcr", False))
        assert isinstance(result, JSONResponse) and result.status_code == 409
        assert len(calls) == 2 and calls[1] != tree_env["dst"]
        assert (tree_env["dst"] / "late-edit.md").read_text() == "mine\n"
        assert (tree_env["dst"] / "SKILL.md").exists()
        staging = tree_env["claude_dir"] / "jacked-backups"
        leftovers = [p for p in staging.rglob("*") if "dcr" in p.name] if staging.exists() else []
        assert leftovers == []

    @requires_symlinks
    def test_disable_unlinks_a_dangling_skill_symlink(self, tree_env):
        dst = tree_env["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(tree_env["claude_dir"] / "gone", target_is_directory=True)
        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": False}
        assert not dst.is_symlink()

    def test_enable_logs_when_the_manifest_cannot_record(self, tree_env, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="jacked.api.routes.features"):
            _run(_toggle_knowledge("skill_dcr", True))
        assert any("manifest" in r.getMessage() and "dcr" in r.getMessage() for r in caplog.records)

    @requires_symlinks
    def test_enable_works_with_a_symlinked_skills_root(self, tree_env, tmp_path):
        """Dotfiles setups link ~/.claude/skills elsewhere. The name is
        allowlisted, so the root's real location is not a traversal."""
        elsewhere = tmp_path / "dotfiles" / "skills"
        elsewhere.mkdir(parents=True)
        (tree_env["claude_dir"] / "skills").symlink_to(elsewhere, target_is_directory=True)
        result = _run(_toggle_knowledge("skill_dcr", True))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": True}
        assert (elsewhere / "dcr" / "references" / "lenses.md").exists()
        assert _run(_toggle_knowledge("skill_dcr", False))["enabled"] is False
        assert not (elsewhere / "dcr").exists()

    # --- review wave 2 ----------------------------------------------------

    @requires_symlinks
    def test_symlinked_skills_root_into_the_source_is_never_deleted(self, tree_env):
        """~/.claude/skills -> <data>/skills: skill_dir is a REAL dir whose
        path IS the packaged source. Disable must keep it."""
        src_skills = tree_env["data_root"] / "skills"
        root = tree_env["claude_dir"] / "skills"
        root.symlink_to(src_skills, target_is_directory=True)
        before = {p.relative_to(src_skills): p.read_bytes() for p in src_skills.rglob("*") if p.is_file()}

        result = _run(_toggle_knowledge("skill_dcr", False))
        assert isinstance(result, JSONResponse) and result.status_code == 409
        assert "packaged source" in _body(result)["error"]["message"]
        after = {p.relative_to(src_skills): p.read_bytes() for p in src_skills.rglob("*") if p.is_file()}
        assert after == before
        assert not (tree_env["claude_dir"] / "jacked-backups").exists()

    @requires_symlinks
    @pytest.mark.parametrize("editable", [True, False])
    def test_enable_through_a_root_symlinked_into_the_source_is_a_noop(self, tree_env, monkeypatch, editable):
        import jacked.cli as cli

        monkeypatch.setattr(cli, "_is_editable_install", lambda: editable)
        src_skills = tree_env["data_root"] / "skills"
        (tree_env["claude_dir"] / "skills").symlink_to(src_skills, target_is_directory=True)
        before = {p.relative_to(src_skills): p.read_bytes() for p in src_skills.rglob("*") if p.is_file()}

        result = _run(_toggle_knowledge("skill_dcr", True))
        assert result["enabled"] is True
        for rel, data in before.items():
            p = src_skills / rel
            assert p.is_file() and not p.is_symlink(), rel
            assert p.read_bytes() == data, rel
        assert sorted(before) == sorted(
            p.relative_to(src_skills) for p in src_skills.rglob("*") if p.is_file()
        )

    @requires_symlinks
    def test_disable_unlinks_a_relative_whole_dir_symlink(self, tree_env, tmp_path):
        """A RELATIVE link breaks when renamed into the staging dir, which made
        the re-check refuse it. A link holds no content: unlink it in place."""
        import os
        import shutil

        target = tmp_path / "x" / "dcr"
        shutil.copytree(tree_env["src"], target)
        dst = tree_env["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(target, dst.parent), dst, target_is_directory=True)
        assert dst.exists()

        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": False}
        assert not dst.is_symlink()
        assert (target / "SKILL.md").exists()

    def test_disable_falls_back_when_the_staging_dir_cannot_be_made(self, tree_env):
        _run(_toggle_knowledge("skill_dcr", True))
        blocker = tree_env["claude_dir"] / "jacked-backups"
        blocker.write_text("not a dir", encoding="utf-8")

        result = _run(_toggle_knowledge("skill_dcr", False))
        assert result == {"name": "skill_dcr", "category": "knowledge", "enabled": False}
        assert not tree_env["dst"].exists()
        assert blocker.read_text() == "not a dir"
        assert [p.name for p in (tree_env["claude_dir"] / "skills").iterdir()] == []

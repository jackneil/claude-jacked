"""Unit tests for dynamic skill discovery and toggle helpers."""

import asyncio
from unittest import mock

import pytest

from jacked.api.routes.features import (
    _get_valid_knowledge_names,
    _get_valid_skill_names,
    _toggle_knowledge,
)


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

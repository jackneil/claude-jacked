"""A skill that only the user may start stays that way in Codex.

Claude Code reads ``disable-model-invocation: true`` from the frontmatter of a
skill or a command. Codex reads its own policy file, ``agents/openai.yaml`` in
the skill dir. With ``allow_implicit_invocation: false``, Codex does not start
the skill from a prompt by itself. An invocation by name (``$review-animations``)
still works.

A shipped skill carries the policy file as a real source file, so the Claude
install and its manifest check see one more source file and nothing else. A
command gets the file from the generator, in the skill that the Codex install
makes from the command.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

import jacked
from jacked.codex import _fsutil as fs
from jacked.codex import installer as ins

DATA = Path(jacked.__file__).parent / "data"
POLICY = "agents/openai.yaml"
EXACT = b"policy:\n  allow_implicit_invocation: false\n"


def _homes(tmp_path: Path) -> dict:
    return {"home": tmp_path / "codex", "agents_home": tmp_path / "agents"}


def _install(data_root: Path, homes: dict):
    return ins.install_codex(
        data_root, home=homes["home"], agents_home=homes["agents_home"],
        version="1.0", now_iso="now",
    )


def _data_root(tmp_path: Path, *, skill: bool = False, command: bool = False) -> Path:
    """A small data tree with copies of the REAL shipped items, so the test does
    not run the hooks and MCP steps of a full real install."""
    root = tmp_path / "data"
    (root / "skills").mkdir(parents=True)
    (root / "commands").mkdir(parents=True)
    if skill:
        shutil.copytree(DATA / "skills" / "review-animations",
                        root / "skills" / "review-animations")
    if command:
        shutil.copy2(DATA / "commands" / "cleanup.md", root / "commands" / "cleanup.md")
    return root


def _skill_dir(homes: dict, name: str) -> Path:
    return ins.agents_skills_dir(homes["agents_home"]) / name


def _has_the_key(skill_md: Path) -> bool:
    from jacked.skill_listing import truthy, parse_frontmatter

    meta, _ = parse_frontmatter(skill_md.read_bytes().decode("utf-8"))
    return truthy(meta.get("disable-model-invocation"))


def test_each_skill_with_the_key_ships_the_policy_file_with_the_exact_content():
    from jacked.codex._policy_files import CODEX_NO_IMPLICIT

    assert CODEX_NO_IMPLICIT.encode("utf-8") == EXACT
    with_key = [p.parent for p in sorted((DATA / "skills").glob("*/SKILL.md"))
                if _has_the_key(p)]
    assert DATA / "skills" / "review-animations" in with_key
    for skill in with_key:
        policy = skill / POLICY
        assert policy.is_file(), f"{skill.name} has disable-model-invocation and no {POLICY}"
        assert policy.read_bytes() == EXACT, skill.name


def test_install_gives_review_animations_the_policy_file(tmp_path):
    homes = _homes(tmp_path)
    _install(_data_root(tmp_path, skill=True), homes)
    target = _skill_dir(homes, "review-animations")
    assert (target / POLICY).read_bytes() == EXACT
    assert (target / "SKILL.md").is_file()


def test_install_gives_the_cleanup_command_skill_the_policy_file(tmp_path):
    homes = _homes(tmp_path)
    _install(_data_root(tmp_path, command=True), homes)
    target = _skill_dir(homes, "cleanup")
    assert (target / POLICY).read_bytes() == EXACT
    files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert files == ["SKILL.md", POLICY]


def test_a_command_without_the_key_gets_no_policy_file(tmp_path):
    from jacked.codex._policy_files import _command_codex_policy

    root = _data_root(tmp_path)
    cmd = root / "commands" / "plain.md"
    cmd.write_bytes(b"---\ndescription: a plain command\n---\nrun\n")
    off = root / "commands" / "off.md"
    off.write_bytes(b"---\ndescription: off\ndisable-model-invocation: false\n---\nrun\n")
    assert _command_codex_policy(cmd) is None
    assert _command_codex_policy(off) is None
    homes = _homes(tmp_path)
    _install(root, homes)
    for name in ("plain", "off"):
        target = _skill_dir(homes, name)
        assert [p.name for p in target.iterdir()] == ["SKILL.md"], name
    # The hash of a command skill with no extras is the same value as before.
    content = ins._command_skill_md(cmd)
    legacy = "sha256:" + hashlib.sha256(b"SKILL.md" + content.encode("utf-8")).hexdigest()
    assert fs._sha_solo_skill(content) == legacy
    assert fs._sha_solo_skill(content, None) == legacy
    assert fs._sha_solo_skill(content, {}) == legacy
    assert fs._sha_dir(_skill_dir(homes, "plain")) == legacy


def test_sha_solo_skill_with_extras_equals_sha_dir_of_the_written_folder(tmp_path):
    # SKILL.md against agents/ is a case order (it differs on Windows), and
    # a/b.txt against a-b/c.txt is a separator order (a plain string sort of
    # the paths gives a different order than a sort of Path objects).
    content = "---\nname: x\ndescription: y\n---\nline one\nline two\n"
    extras = {
        POLICY: EXACT.decode("utf-8"),
        "a/b.txt": "first\n",
        "a-b/c.txt": "second\n",
    }
    dest = tmp_path / "skill"
    fs._write_solo_skill(dest, content, extras)
    assert fs._sha_solo_skill(content, extras) == fs._sha_dir(dest)
    assert (dest / "SKILL.md").read_bytes() == content.encode("utf-8")
    assert (dest / POLICY).read_bytes() == EXACT
    # The order of the mapping does not change the hash.
    assert fs._sha_solo_skill(content, dict(reversed(list(extras.items())))) == fs._sha_dir(dest)


@pytest.mark.parametrize("bad", [
    "../outside.yaml", "agents/../../outside.yaml", "/abs/outside.yaml",
    "agents\\openai.yaml", "", "agents//openai.yaml", "./agents/openai.yaml",
    "SKILL.md",
])
def test_write_solo_skill_refuses_an_extra_path_outside_the_skill_folder(tmp_path, bad):
    dest = tmp_path / "skill"
    with pytest.raises(ValueError):
        fs._write_solo_skill(dest, "body\n", {bad: "x\n"})
    assert not dest.exists()
    assert not (tmp_path / "outside.yaml").exists()
    with pytest.raises(ValueError):
        fs._sha_solo_skill("body\n", {bad: "x\n"})


def test_a_second_install_moves_nothing_aside(tmp_path):
    homes = _homes(tmp_path)
    root = _data_root(tmp_path, skill=True, command=True)
    first = _install(root, homes)
    assert first.preserved == []
    second = _install(root, homes)
    assert second.preserved == []
    assert second.changed is False
    assert not [p for p in tmp_path.rglob("jacked-backups") if any(p.rglob("*"))]
    for name in ("review-animations", "cleanup"):
        assert (_skill_dir(homes, name) / POLICY).read_bytes() == EXACT, name


def test_the_claude_install_of_review_animations_still_passes_its_manifest_check(tmp_path):
    from jacked import install_manifest as m
    from jacked.cli import _copy_skill_tree, _install_skill_dir_transactional

    # A copy of the real source: an editable install links each file to its
    # source, and the source of the package must never be the link target here.
    src = tmp_path / "src" / "review-animations"
    shutil.copytree(DATA / "skills" / "review-animations", src)
    skills_base = tmp_path / ".claude" / "skills"
    skills_base.mkdir(parents=True)
    dest = skills_base / "review-animations"

    first = _install_skill_dir_transactional(
        src, dest, "review-animations", None,
        preserve=m.preserve_user_skill_dir, copy=_copy_skill_tree,
    )
    assert first.error is None and first.backup is None
    assert (dest / POLICY).read_bytes() == EXACT
    assert m.is_source_subset(dest, src) is True

    manifest = {
        "version": "1.0", "format": 2,
        "artifacts": {
            "skills": {"review-animations": m.skill_dir_hash(dest)},
            m.SKILLS_DIRS_KEY: {"review-animations": m.skill_content_hash(dest)},
        },
    }
    assert m.is_jacked_skill_dir(dest, "review-animations", manifest, src) is True
    second = _install_skill_dir_transactional(
        src, dest, "review-animations", manifest,
        preserve=m.preserve_user_skill_dir, copy=_copy_skill_tree,
    )
    assert second.error is None and second.backup is None
    assert not (tmp_path / ".claude" / "jacked-backups").exists()
    assert (dest / POLICY).read_bytes() == EXACT


def test_each_shipped_command_gets_the_policy_exactly_when_it_has_the_key():
    from jacked.codex._generate import _split_command_frontmatter
    from jacked.codex._policy_files import CODEX_NO_IMPLICIT, _command_codex_policy
    from jacked.skill_listing import truthy

    with_key, without_key = [], []
    for cmd in sorted((DATA / "commands").glob("*.md")):
        meta, _ = _split_command_frontmatter(cmd.read_text(encoding="utf-8"))
        if truthy(meta.get("disable-model-invocation")):
            with_key.append(cmd.stem)
            assert _command_codex_policy(cmd) == CODEX_NO_IMPLICIT, cmd.name
        else:
            without_key.append(cmd.stem)
            assert _command_codex_policy(cmd) is None, cmd.name
    assert "cleanup" in with_key
    assert without_key, "the walk found no command without the key"


def test_each_shipped_policy_file_belongs_to_a_skill_with_the_key():
    policies = [p for p in sorted((DATA / "skills").glob("*/" + POLICY))
                if b"allow_implicit_invocation" in p.read_bytes()]
    assert DATA / "skills" / "review-animations" / POLICY in policies
    for policy in policies:
        skill_md = policy.parent.parent / "SKILL.md"
        assert _has_the_key(skill_md), (
            f"{policy.parent.parent.name} ships a Codex policy file, "
            "but its SKILL.md does not set disable-model-invocation"
        )

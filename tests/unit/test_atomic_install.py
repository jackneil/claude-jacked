"""Every jacked install of its own artifacts is atomic (#260i).

A copy that fails part way (a full disk, ENOSPC, or a file that becomes
unwritable, EACCES) must leave the target as it was: the complete old tree
or file, never a mix of old and new and never a partial new copy.

The shared mechanism is ``jacked.atomic_install``:
  * ``install_dir`` builds a directory in a private staging dir beside the
    target and swaps it into place with renames (set the old dir aside,
    rename the new dir in, remove the old dir), with a rollback when a step
    fails;
  * ``atomic_copy_file``, ``atomic_symlink`` and ``atomic_hardlink`` make
    the new file or link under a temp name beside the target and replace
    the target in one step, through ``jacked.atomic_io.replace_with_retry``.

Every install site goes through them: the skill transaction of the CLI, the
dashboard and the Codex installer, the CLI agent and command install
(`_link_or_copy`), the dashboard agent and command toggle, the Codex prompt
copy and the reference doc.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path

import pytest

from jacked import atomic_install, atomic_io
from tests._platform import requires_symlinks

_ERRORS = {
    "ENOSPC": (errno.ENOSPC, "No space left on device"),
    "EACCES": (errno.EACCES, "Permission denied"),
}


def _tree(d: Path) -> dict:
    """Every file under `d` and its bytes, keyed by posix path."""
    return {
        p.relative_to(d).as_posix(): p.read_bytes()
        for p in sorted(d.rglob("*")) if p.is_file()
    }


def _make_tree(d: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def _hidden(parent: Path) -> list:
    """The dot entries in `parent`: staging, previous and temp leftovers."""
    return sorted(p.name for p in parent.iterdir() if p.name.startswith("."))


def _builder(files: dict, *, fail_after: int | None = None, err: str = "ENOSPC"):
    """A build step that writes `files` into the new dir it is given. With
    `fail_after`, it writes that many files and then raises, as a full disk
    or an unwritable file stops a copy part way."""
    def build(staging: Path):
        staging.mkdir(parents=True)
        for i, (rel, text) in enumerate(files.items()):
            if fail_after is not None and i == fail_after:
                raise OSError(*_ERRORS[err])
            p = staging / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return len(files)

    return build


_OLD = {"SKILL.md": "# old\n", "retired.js": "// old only\n", "ref/a.md": "old a\n"}
_NEW = {"SKILL.md": "# new\n", "measure.js": "// new\n", "ref/a.md": "new a\n"}


# --------------------------------------------------------------------------- #
# jacked.atomic_install.install_dir
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("err", sorted(_ERRORS))
def test_a_failed_build_over_an_existing_dir_keeps_the_old_tree(tmp_path, err):
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)
    before = _tree(target)

    with pytest.raises(OSError) as info:
        atomic_install.install_dir(
            target, _builder(_NEW, fail_after=2, err=err), replace_existing=True,
        )

    assert info.value.errno == _ERRORS[err][0]
    assert _tree(target) == before
    assert _hidden(target.parent) == []


@pytest.mark.parametrize("err", sorted(_ERRORS))
def test_a_failed_build_of_a_new_dir_leaves_nothing(tmp_path, err):
    skills = tmp_path / "skills"
    skills.mkdir()
    target = skills / "dcr"

    with pytest.raises(OSError):
        atomic_install.install_dir(target, _builder(_NEW, fail_after=1, err=err))

    assert not target.exists() and not target.is_symlink()
    assert sorted(p.name for p in skills.iterdir()) == []


def test_a_good_install_replaces_the_whole_tree(tmp_path):
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)

    count = atomic_install.install_dir(target, _builder(_NEW), replace_existing=True)

    assert count == 3
    assert _tree(target) == {k: v.encode() for k, v in _NEW.items()}
    assert _hidden(target.parent) == []


def test_a_failed_swap_in_puts_the_old_tree_back(tmp_path, monkeypatch):
    """The second rename (the new dir into place) fails. The old dir, already
    renamed aside, goes back to the target path, the staging dir is removed,
    and the error of the rename propagates."""
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)
    before = _tree(target)
    real = atomic_io.replace_with_retry
    refused = OSError(errno.EACCES, "Permission denied")

    def no_swap_in(src, dst, **kw):
        if Path(src).name.startswith(".dcr.jacked-staging-"):
            raise refused
        return real(src, dst, **kw)

    monkeypatch.setattr(atomic_io, "replace_with_retry", no_swap_in)
    with pytest.raises(OSError) as info:
        atomic_install.install_dir(target, _builder(_NEW), replace_existing=True)

    assert info.value is refused
    assert _tree(target) == before
    assert _hidden(target.parent) == []


def test_a_failed_put_back_keeps_the_old_tree_and_reports_it(tmp_path, monkeypatch):
    """The swap-in fails and the put-back fails too. The old tree is never
    removed: it stays in the previous dir, and the rollback reports the
    failure with that path."""
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)
    before = _tree(target)
    real = atomic_io.replace_with_retry

    def stuck(src, dst, **kw):
        if Path(dst) == target:
            raise OSError(errno.EACCES, "Permission denied")
        return real(src, dst, **kw)

    reports: list = []
    monkeypatch.setattr(atomic_io, "replace_with_retry", stuck)
    with pytest.raises(OSError):
        atomic_install.install_dir(
            target, _builder(_NEW), replace_existing=True,
            on_rollback_error=lambda e, step, path: reports.append((step, path)),
        )

    previous = atomic_install.side_dir(target, atomic_install.PREVIOUS_TAG)
    assert reports == [("previous", previous)]
    assert _tree(previous) == before
    assert not target.exists()


def test_a_check_that_refuses_stops_the_swap_before_any_rename(tmp_path):
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)
    before = _tree(target)

    def refuse(path):
        raise FileExistsError(errno.EEXIST, "changed", str(path))

    with pytest.raises(FileExistsError):
        atomic_install.install_dir(
            target, _builder(_NEW), replace_existing=True, check=refuse,
        )

    assert _tree(target) == before
    assert _hidden(target.parent) == []


def test_an_interrupt_during_the_build_removes_the_staging_dir(tmp_path):
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)
    before = _tree(target)

    def interrupted(staging):
        staging.mkdir()
        (staging / "SKILL.md").write_text("half\n", encoding="utf-8")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        atomic_install.install_dir(target, interrupted, replace_existing=True)

    assert _tree(target) == before
    assert _hidden(target.parent) == []


def test_the_side_dirs_are_hidden_and_carry_the_pid(tmp_path):
    """A leftover of a crash must never load as a skill: its name starts
    with a dot. The pid lets a later run tell a dead run's leftover from an
    install that is still running."""
    target = tmp_path / "skills" / "dcr"
    for tag in (atomic_install.STAGING_TAG, atomic_install.PREVIOUS_TAG):
        side = atomic_install.side_dir(target, tag)
        assert side.parent == target.parent
        assert side.name == f".dcr.{tag}-{os.getpid()}"


def test_install_removes_the_leftover_of_this_process(tmp_path):
    target = _make_tree(tmp_path / "skills" / "dcr", _OLD)
    for tag in (atomic_install.STAGING_TAG, atomic_install.PREVIOUS_TAG):
        _make_tree(atomic_install.side_dir(target, tag), {"SKILL.md": "# crash\n"})

    atomic_install.install_dir(target, _builder(_NEW), replace_existing=True)

    assert _tree(target) == {k: v.encode() for k, v in _NEW.items()}
    assert _hidden(target.parent) == []


@requires_symlinks
def test_a_whole_dir_link_is_set_aside_without_following_it(tmp_path):
    linked = _make_tree(tmp_path / "elsewhere", _OLD)
    before = _tree(linked)
    target = tmp_path / "skills" / "dcr"
    target.parent.mkdir()
    target.symlink_to(linked, target_is_directory=True)

    atomic_install.install_dir(target, _builder(_NEW), replace_existing=True)

    assert not target.is_symlink() and target.is_dir()
    assert _tree(linked) == before
    assert _hidden(target.parent) == []


# --------------------------------------------------------------------------- #
# jacked.atomic_install: one-step file copy and links
# --------------------------------------------------------------------------- #


def _half_copyfileobj(monkeypatch, err: str = "ENOSPC"):
    """Make the byte copy write a few bytes and then fail."""
    def half(fsrc, fdst, *a, **kw):
        fdst.write(fsrc.read(3))
        raise OSError(*_ERRORS[err])

    monkeypatch.setattr(atomic_install.shutil, "copyfileobj", half)


@pytest.mark.parametrize("err", sorted(_ERRORS))
def test_a_failed_file_copy_keeps_the_old_file(tmp_path, monkeypatch, err):
    src = tmp_path / "new.md"
    src.write_text("# the new command body\n", encoding="utf-8")
    dst = tmp_path / "commands" / "dcr.md"
    dst.parent.mkdir()
    dst.write_text("# the old command body\n", encoding="utf-8")
    _half_copyfileobj(monkeypatch, err)

    with pytest.raises(OSError):
        atomic_install.atomic_copy_file(src, dst)

    assert dst.read_text(encoding="utf-8") == "# the old command body\n"
    assert _hidden(dst.parent) == []


def test_a_file_copy_keeps_the_mode_of_the_source(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permission bits")
    src = tmp_path / "hook.sh"
    src.write_text("#!/bin/sh\n", encoding="utf-8")
    src.chmod(0o755)
    dst = tmp_path / "out" / "hook.sh"
    dst.parent.mkdir()

    atomic_install.atomic_copy_file(src, dst)

    assert dst.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert (dst.stat().st_mode & 0o777) == 0o755
    assert _hidden(dst.parent) == []


@requires_symlinks
def test_a_file_copy_replaces_a_link_and_never_writes_through_it(tmp_path):
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("not jacked's\n", encoding="utf-8")
    src = tmp_path / "new.md"
    src.write_text("new\n", encoding="utf-8")
    dst = tmp_path / "commands" / "dcr.md"
    dst.parent.mkdir()
    dst.symlink_to(elsewhere)

    atomic_install.atomic_copy_file(src, dst)

    assert not dst.is_symlink() and dst.read_text(encoding="utf-8") == "new\n"
    assert elsewhere.read_text(encoding="utf-8") == "not jacked's\n"


@requires_symlinks
def test_a_link_replaces_the_old_file_in_one_step(tmp_path):
    src = tmp_path / "src.md"
    src.write_text("source\n", encoding="utf-8")
    dst = tmp_path / "commands" / "dcr.md"
    dst.parent.mkdir()
    dst.write_text("old\n", encoding="utf-8")

    atomic_install.atomic_symlink(src, dst)

    assert dst.is_symlink() and dst.resolve() == src.resolve()
    assert _hidden(dst.parent) == []


@requires_symlinks
@pytest.mark.parametrize("make", ["atomic_symlink", "atomic_hardlink"])
def test_a_failed_link_replace_keeps_the_old_file(tmp_path, monkeypatch, make):
    src = tmp_path / "src.md"
    src.write_text("source\n", encoding="utf-8")
    dst = tmp_path / "commands" / "dcr.md"
    dst.parent.mkdir()
    dst.write_text("old\n", encoding="utf-8")

    def refuse(s, d):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(atomic_io.os, "replace", refuse)
    with pytest.raises(OSError):
        getattr(atomic_install, make)(src, dst)

    assert not dst.is_symlink() and dst.read_text(encoding="utf-8") == "old\n"
    assert _hidden(dst.parent) == []


# --------------------------------------------------------------------------- #
# The install sites
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("editable", [False, True])
def test_link_or_copy_keeps_the_old_file_when_the_new_one_fails(
    tmp_path, monkeypatch, editable,
):
    """`jacked install` agents and commands: the old code unlinked the
    installed file first and then copied or linked, so a failure left no
    file. Now the old file stays until the new one replaces it."""
    from jacked import cli as _cli

    monkeypatch.setattr(_cli, "_is_editable_install", lambda: editable)
    src = tmp_path / "pkg" / "dcr.md"
    src.parent.mkdir()
    src.write_text("# new\n", encoding="utf-8")
    dst = tmp_path / ".claude" / "commands" / "dcr.md"
    dst.parent.mkdir(parents=True)
    dst.write_text("# old\n", encoding="utf-8")

    def refuse(s, d):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(atomic_io.os, "replace", refuse)
    with pytest.raises(OSError):
        _cli._link_or_copy(src, dst)

    assert not dst.is_symlink() and dst.read_text(encoding="utf-8") == "# old\n"
    assert _hidden(dst.parent) == []


def test_link_or_copy_replaces_the_old_file(tmp_path, monkeypatch):
    from jacked import cli as _cli

    monkeypatch.setattr(_cli, "_is_editable_install", lambda: False)
    src = tmp_path / "pkg" / "dcr.md"
    src.parent.mkdir()
    src.write_text("# new\n", encoding="utf-8")
    dst = tmp_path / ".claude" / "commands" / "dcr.md"
    dst.parent.mkdir(parents=True)
    dst.write_text("# old\n", encoding="utf-8")

    assert _cli._link_or_copy(src, dst) == "copied"
    assert dst.read_text(encoding="utf-8") == "# new\n"
    assert _hidden(dst.parent) == []


def _skill_src(root: Path, name: str = "dcr") -> Path:
    return _make_tree(root / name, {
        "SKILL.md": "# jacked dcr v2\n", "measure.js": "// v2\n", "references/n.md": "v2\n",
    })


def _owning_manifest(skill_dir: Path, name: str) -> dict:
    from jacked import install_manifest as m

    return {
        "version": "0.111.2",
        "format": 2,
        "artifacts": {
            "skills": {name: m.skill_dir_hash(skill_dir)},
            m.SKILLS_DIRS_KEY: {name: m.skill_content_hash(skill_dir)},
        },
    }


@pytest.mark.parametrize("err", sorted(_ERRORS))
@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.parametrize("owner", ["new", "user", "jacked"])
def test_skill_transaction_survives_a_copy_that_fails_after_one_file(
    tmp_path, monkeypatch, err, editable, owner,
):
    """The real CLI copy step (`_copy_skill_tree`, which links each file in
    editable mode and copies it otherwise) fails on its second file. A new
    skill leaves nothing, a user's dir comes back byte for byte, and
    jacked's own old copy is not touched. No staging or previous dir is
    left beside the skill."""
    from jacked import cli as _cli
    from jacked import install_manifest as m

    if editable:
        from tests._platform import symlinks_supported

        if not symlinks_supported():
            pytest.skip("editable mode links each file")
    monkeypatch.setattr(_cli, "_is_editable_install", lambda: editable)
    src = _skill_src(tmp_path / "src")
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    dst = skills / "dcr"
    manifest = None
    if owner != "new":
        _make_tree(dst, {"SKILL.md": "# dcr as it was\n", "old.js": "// as it was\n"})
        if owner == "jacked":
            manifest = _owning_manifest(dst, "dcr")
    before = _tree(dst) if owner != "new" else None
    real = _cli._link_or_copy
    calls: list = []

    def fail_second(s, d):
        calls.append(d)
        if len(calls) == 2:
            raise OSError(*_ERRORS[err])
        return real(s, d)

    monkeypatch.setattr(_cli, "_link_or_copy", fail_second)
    out = _cli._install_skill_dir_transactional(
        src, dst, "dcr", manifest, preserve=m.preserve_user_skill_dir,
        copy=_cli._copy_skill_tree,
    )

    assert out.error is not None and out.error.errno == _ERRORS[err][0]
    assert out.rollback_error is None
    assert len(calls) == 2
    assert all(".dcr.jacked-staging-" in str(c) for c in calls), calls
    if owner == "new":
        assert not dst.exists() and not dst.is_symlink()
    else:
        assert _tree(dst) == before
        assert out.restored is (owner == "user")
    assert _hidden(skills) == []


def test_skill_transaction_rolls_back_when_the_swap_in_fails(tmp_path, monkeypatch):
    """Jacked's own copy is renamed aside, then the rename of the new copy
    into place fails. The old copy goes back, the outcome reports the error,
    and nothing is left beside the skill."""
    from jacked import cli as _cli
    from jacked import install_manifest as m

    src = _skill_src(tmp_path / "src")
    skills = tmp_path / ".claude" / "skills"
    dst = _make_tree(skills / "dcr", {"SKILL.md": "# v1\n", "old.js": "// v1\n"})
    manifest = _owning_manifest(dst, "dcr")
    before = _tree(dst)
    real = atomic_io.replace_with_retry

    def no_swap_in(s, d, **kw):
        if Path(s).name.startswith(".dcr.jacked-staging-"):
            raise OSError(errno.EACCES, "Permission denied")
        return real(s, d, **kw)

    monkeypatch.setattr(atomic_io, "replace_with_retry", no_swap_in)
    out = _cli._install_skill_dir_transactional(
        src, dst, "dcr", manifest, preserve=m.preserve_user_skill_dir,
        copy=_cli._copy_skill_tree,
    )

    assert out.error is not None and out.error.errno == errno.EACCES
    assert out.rollback_error is None and out.backup is None
    assert _tree(dst) == before
    assert _hidden(skills) == []


def test_dashboard_file_toggle_keeps_the_old_file_when_the_copy_fails(
    tmp_path, monkeypatch,
):
    """The dashboard agent and command toggle used `shutil.copy`, which
    truncates the installed file and then writes it. A copy that failed part
    way left a truncated command."""
    from jacked.api.routes import features

    claude = tmp_path / ".claude"
    dst = claude / "commands" / "dcr.md"
    dst.parent.mkdir(parents=True)
    dst.write_text("# the old command\n", encoding="utf-8")
    src = tmp_path / "pkg" / "dcr.md"
    src.parent.mkdir()
    src.write_text("# the new command, longer than three bytes\n", encoding="utf-8")
    monkeypatch.setattr(features, "CLAUDE_DIR", claude)
    _half_copyfileobj(monkeypatch)

    with pytest.raises(OSError):
        asyncio.run(features._toggle_file_feature(src, dst, True, "dcr", "command"))

    assert dst.read_text(encoding="utf-8") == "# the old command\n"
    assert _hidden(dst.parent) == []


def test_codex_prompt_copy_keeps_the_old_prompt_when_the_copy_fails(tmp_path, monkeypatch):
    """`jacked codex install` copied each command to ~/.codex/prompts with
    `shutil.copy`, so a copy that failed part way left a truncated prompt."""
    from jacked.codex import installer as ins

    data_root = tmp_path / "data"
    _make_tree(data_root / "commands", {"dcr.md": "---\ndescription: d\n---\nrun dcr v2\n"})
    homes = {"home": tmp_path / "codex", "agents_home": tmp_path / "agents"}
    prompt = ins.codex_prompts_dir(homes["home"]) / "dcr.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("the old prompt\n", encoding="utf-8")
    _half_copyfileobj(monkeypatch)

    ins.install_codex(data_root, home=homes["home"], agents_home=homes["agents_home"])

    assert prompt.read_text(encoding="utf-8") == "the old prompt\n"
    assert _hidden(prompt.parent) == []


def test_no_install_site_copies_over_a_live_target_in_place():
    """A static guard for the class. Each install site goes through the
    one-step helpers; a bare `shutil.copy`, `copy2` or `copytree` onto a live
    target would bring the partial-copy defect back."""
    import inspect

    from jacked import cli as _cli
    from jacked import guardrails
    from jacked.api.routes import features
    from jacked.codex import installer as ins

    checks = {
        "cli._link_or_copy": inspect.getsource(_cli._link_or_copy),
        "features._toggle_file_feature": inspect.getsource(features._toggle_file_feature),
        "codex.install_codex": inspect.getsource(ins.install_codex),
        "guardrails.install_hook": inspect.getsource(guardrails.install_hook),
        "guardrails.deploy_templates": inspect.getsource(guardrails.deploy_templates),
    }
    for where, source in checks.items():
        for banned in ("shutil.copy(", "shutil.copy2(", "shutil.copytree("):
            assert banned not in source, f"{where} uses {banned!r}"
    assert "shutil.copy(ref_src" not in inspect.getsource(_cli._run_install)

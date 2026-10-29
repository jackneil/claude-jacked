"""Route handlers write files only through the shared atomic writer.

Many handlers run in the threadpool (no synchronous jacked.db work on the
event loop, #162i), so two requests can write the same file at once. A plain
``write_text`` truncates first and writes second, so two writers interleave
into a file that is neither version. ``jacked.memory.vault.atomic_write_text``
writes a writer-unique temp file and replaces the target in one step.
"""

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[3] / "jacked" / "api" / "routes"


def _raw_writes(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"write_text", "write_bytes"}:
            found.append(f"{path.name}:{node.lineno} .{func.attr}()")
        is_open = (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr == "open"
        )
        if not is_open:
            continue
        # open(path, "w") puts the mode second; Path.open("w") puts it first.
        position = 1 if isinstance(func, ast.Name) else 0
        mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
        if mode is None and len(node.args) > position:
            mode = node.args[position]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
            found.append(f"{path.name}:{node.lineno} open(..., {mode.value!r})")
    return found


def test_route_modules_have_no_raw_file_writes():
    offenders = [hit for path in sorted(ROUTES.glob("*.py")) for hit in _raw_writes(path)]
    assert not offenders, "use jacked.memory.vault.atomic_write_text: " + ", ".join(offenders)


def test_the_guard_sees_a_raw_write(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from pathlib import Path\nPath('x').write_text('y')\nopen('z', 'w')\n"
        "open('z', mode='a')\nPath('z').open('w')\nPath('z').open(mode='x')\n"
        "open('r')\nPath('z').open()\n"
    )
    assert len(_raw_writes(bad)) == 5


def test_concurrent_lesson_saves_never_mix_their_contents(tmp_path):
    """Two saves of different lengths race; the file ends as one of them."""
    import threading

    from jacked.memory.vault import atomic_write_text

    target = tmp_path / "lessons.md"
    long_text, short_text = "L" * 200_000 + "\n", "S\n"
    barrier = threading.Barrier(2)

    def save(text):
        barrier.wait()
        atomic_write_text(target, text)

    for _ in range(25):
        threads = [threading.Thread(target=save, args=(t,)) for t in (long_text, short_text)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert target.read_text() in (long_text, short_text)


def test_the_atomic_writer_follows_a_symlink_only_when_asked(tmp_path):
    """Default: a link is replaced by a plain file, so a repo-shipped
    lessons.md that links to ~/.bashrc cannot redirect a save. The user's own
    global CLAUDE.md opts in and stays a link to its dotfiles copy."""
    import os

    import pytest

    from jacked.memory.vault import atomic_write_text

    outside = tmp_path / "home" / ".bashrc"
    outside.parent.mkdir()
    outside.write_text("export SAFE=1\n")
    repo_lessons = tmp_path / "repo" / "lessons.md"
    repo_lessons.parent.mkdir()
    try:
        repo_lessons.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    atomic_write_text(repo_lessons, "# Lessons\n")
    assert not os.path.islink(repo_lessons) and repo_lessons.read_text() == "# Lessons\n"
    assert outside.read_text() == "export SAFE=1\n"

    shared = tmp_path / "dotfiles" / "CLAUDE.md"
    shared.parent.mkdir()
    shared.write_text("old\n")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(shared)
    atomic_write_text(link, "new\n", follow_symlinks=True)
    assert os.path.islink(link) and shared.read_text() == "new\n"


def test_only_the_global_claude_md_follows_links():
    """Every route write is no-follow except the ~/.claude/CLAUDE.md toggles."""
    following = []
    for path in sorted(ROUTES.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "atomic_write_text":
                if any(kw.arg == "follow_symlinks" for kw in node.keywords):
                    following.append(f"{path.name}:{ast.unparse(node.args[0])}")
    assert following and all(hit.endswith(":CLAUDE_MD") for hit in following), following

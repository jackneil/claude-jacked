"""Tests for jacked.testdiet.globs: one glob rule for --junit and --input."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jacked.testdiet import globs

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tree(root: Path) -> Path:
    for rel in ("a.json", "sub/b.json", "sub/deep/c.json", ".hidden/d.json", "sub/e.txt"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")
    return root


@pytest.mark.parametrize("pattern", ["**/*.json", "sub/**", "sub/**/*.json", "*.json", "sub/*",
                                     "**", "**/deep/*.json", "sub/**/deep", "**/", "sub/**/",
                                     "**/.hidden/*", "nothing/**", "[ab].json", "sub/?.json"])
def test_matches_gives_the_names_of_the_standard_glob(tmp_path, pattern):
    import glob

    root = _tree(tmp_path)
    expected = sorted(glob.glob(pattern, root_dir=root, recursive=True, include_hidden=True))
    assert globs.matches(root, pattern) == expected


def test_an_absolute_pattern_gives_absolute_names(tmp_path):
    root = _tree(tmp_path)
    found = globs.matches(Path("/nowhere"), str(root / "**" / "*.json"))
    assert found and all(os.path.isabs(name) for name in found)
    assert str(root / "sub" / "deep" / "c.json") in found


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege on Windows")
def test_a_double_star_glob_does_not_follow_a_link_to_a_folder(tmp_path):
    root = _tree(tmp_path / "repo")
    before = globs.inputs_fingerprint(root, ["**/*.json"])
    (root / "a").symlink_to(".")
    (root / "b").symlink_to(".")
    script = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from jacked.testdiet import globs
        print(globs.inputs_fingerprint(Path(sys.argv[2]), ["**/*.json"]))
    """)
    proc = subprocess.Popen([sys.executable, "-c", script, str(REPO_ROOT), str(root)],
                            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("the ** walk followed the links and did not end")
    assert proc.returncode == 0 and out.strip() == before

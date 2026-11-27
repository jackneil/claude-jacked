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


# --------------------------------------------------------------------------- the fixed base of a ** pattern

import ntpath  # noqa: E402
import posixpath  # noqa: E402


@pytest.mark.parametrize("pattern, base, parts", [
    ("\\\\server\\share\\data\\**\\*.db", "\\\\server\\share\\data", ["**", "*.db"]),
    ("//server/share/data/**/*.db", "//server/share/data", ["**", "*.db"]),
    ("\\\\server\\share\\**", "\\\\server\\share\\", ["**"]),
    ("C:\\data\\**\\*.db", "C:\\data", ["**", "*.db"]),
    ("C:\\**\\report.xml", "C:\\", ["**", "report.xml"]),
    ("data\\**\\*.db", "data", ["**", "*.db"]),
    ("**\\*.db", "", ["**", "*.db"]),
])
def test_a_windows_pattern_gives_the_right_base(pattern, base, parts):
    assert globs._split_base(pattern, ntpath) == (base, parts)


@pytest.mark.parametrize("pattern, base, parts", [
    ("/data/**/*.db", "/data", ["**", "*.db"]),
    ("/**/*.db", "/", ["**", "*.db"]),
    ("data/**", "data", ["**"]),
    ("**", "", ["**"]),
])
def test_a_posix_pattern_gives_the_right_base(pattern, base, parts):
    assert globs._split_base(pattern, posixpath) == (base, parts)


def test_an_absolute_double_star_input_fingerprint_changes_with_a_file(tmp_path):
    # On Windows this is a drive-letter pattern; on POSIX a rooted one.
    data = tmp_path / "data" / "deep"
    data.mkdir(parents=True)
    (data / "a.db").write_bytes(b"one")
    pattern = os.path.join(str(tmp_path / "data"), "**", "*.db")
    assert globs.matches(Path("/nowhere"), pattern) == [str(data / "a.db")]
    first = globs.inputs_fingerprint(Path("/nowhere"), [pattern])
    (data / "a.db").write_bytes(b"a longer content")
    assert globs.inputs_fingerprint(Path("/nowhere"), [pattern]) != first


def _on_ci() -> bool:
    """True when the CI variable is true, 1 or yes (any case). "false", "0" and empty are not CI."""
    return os.environ.get("CI", "").strip().lower() in ("true", "1", "yes")


def _unc_share_or_stop(share_dir: str, exists=os.path.isdir) -> None:
    """Go on when the share folder is there. Else skip with the reason, but on CI fail with one line.

    The UNC test is the only test that walks a real UNC path, so a CI leg must not skip it in silence.
    """
    if exists(share_dir):
        return
    if _on_ci():
        pytest.fail(f"the administrative share {share_dir} is not available on CI, "
                    "so the only real UNC test cannot run", pytrace=False)
    pytest.skip("the administrative share of this drive is not available")


@pytest.mark.skipif(os.name != "nt", reason="a UNC path exists only on Windows")
def test_a_unc_double_star_pattern_finds_the_files_of_the_share(tmp_path):
    drive, rest = os.path.splitdrive(str(tmp_path))
    share_root = f"\\\\localhost\\{drive.rstrip(':')}$"
    _unc_share_or_stop(share_root + rest)
    (tmp_path / "data" / "deep").mkdir(parents=True)
    (tmp_path / "data" / "deep" / "a.db").write_bytes(b"one")
    pattern = share_root + rest + "\\data\\**\\*.db"
    found = globs.matches(Path("C:\\nowhere"), pattern)
    assert [os.path.basename(name) for name in found] == ["a.db"]
    first = globs.inputs_fingerprint(Path("C:\\nowhere"), [pattern])
    (tmp_path / "data" / "deep" / "a.db").write_bytes(b"a longer content")
    assert globs.inputs_fingerprint(Path("C:\\nowhere"), [pattern]) != first


@pytest.mark.parametrize("ci, stops_with", [
    ("true", "fail"), ("TRUE", "fail"), ("1", "fail"), ("Yes", "fail"),
    ("false", "skip"), ("0", "skip"), ("", "skip"), (None, "skip"),
])
def test_a_missing_share_fails_on_ci_and_skips_elsewhere(monkeypatch, ci, stops_with):
    # The UNC test is the only real UNC proof: on CI a missing share must fail, not skip silently.
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    expected = pytest.fail.Exception if stops_with == "fail" else pytest.skip.Exception
    with pytest.raises(expected) as caught:
        _unc_share_or_stop("\\\\localhost\\C$\\x", exists=lambda path: False)
    message = str(caught.value)
    assert len(message.splitlines()) == 1 and "administrative share" in message
    assert _unc_share_or_stop("\\\\localhost\\C$\\x", exists=lambda path: True) is None

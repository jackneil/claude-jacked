# tests/unit/test_install_asset_dir.py
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_asset_dirs(tmp_path):
    """Create source and dest directories with test markdown files."""
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    # Don't create dst — helper should create it
    (src / "one.md").write_text("# One\ncontent one")
    (src / "two.md").write_text("# Two\ncontent two")
    return src, dst


def test_install_asset_dir_creates_dst_and_copies(tmp_asset_dirs):
    from jacked.cli import _install_asset_dir

    src, dst = tmp_asset_dirs
    with patch("jacked.cli._is_editable_install", return_value=False):
        installed, skipped, method = _install_asset_dir(
            src, dst, "test-assets", glob_pattern="*.md", force=False
        )
    assert installed == 2
    assert skipped == 0
    assert (dst / "one.md").read_text() == "# One\ncontent one"
    assert (dst / "two.md").read_text() == "# Two\ncontent two"


def test_install_asset_dir_skips_unchanged(tmp_asset_dirs):
    from jacked.cli import _install_asset_dir

    src, dst = tmp_asset_dirs
    dst.mkdir()
    # Pre-populate with identical content
    (dst / "one.md").write_text("# One\ncontent one")
    (dst / "two.md").write_text("# Two\ncontent two")
    with patch("jacked.cli._is_editable_install", return_value=False):
        installed, skipped, method = _install_asset_dir(
            src, dst, "test-assets", glob_pattern="*.md", force=False
        )
    assert installed == 0
    assert skipped == 2


def test_install_asset_dir_force_overwrites(tmp_asset_dirs):
    from jacked.cli import _install_asset_dir

    src, dst = tmp_asset_dirs
    dst.mkdir()
    (dst / "one.md").write_text("old content that differs")
    with patch("jacked.cli._is_editable_install", return_value=False):
        installed, skipped, method = _install_asset_dir(
            src, dst, "test-assets", glob_pattern="*.md", force=True
        )
    assert installed == 2
    assert (dst / "one.md").read_text() == "# One\ncontent one"


def test_install_asset_dir_missing_src(tmp_path):
    from jacked.cli import _install_asset_dir

    src = tmp_path / "nonexistent"
    dst = tmp_path / "dst"
    installed, skipped, method = _install_asset_dir(
        src, dst, "test-assets", glob_pattern="*.md", force=False
    )
    assert installed == 0
    assert skipped == 0

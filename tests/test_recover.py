# tests/test_recover.py
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jacked import recover as rec


def _write_session(project_dir: Path, session_id: str, records: list[dict]) -> Path:
    """Write a JSONL transcript fixture; return its path."""
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for rec_obj in records:
            f.write(json.dumps(rec_obj) + "\n")
    return path


def _user_line(cwd: str, ts: str = "2026-06-15T10:00:00.000Z", branch: str = "master") -> dict:
    return {"type": "user", "cwd": cwd, "gitBranch": branch, "timestamp": ts,
            "message": {"role": "user", "content": "hello"}}


SID_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


def test_resolve_matches_by_cwd_field_despite_lossy_slug(tmp_path):
    projects = tmp_path / "projects"
    cwd = "/Users/jack.neil/Github/claude-jacked"  # dot in username defeats naive slug
    # Folder name uses the CURRENT encoding (dots -> dash, leading dash kept):
    pdir = projects / "-Users-jack-neil-Github-claude-jacked"
    _write_session(pdir, SID_A, [_user_line(cwd)])
    assert rec.resolve_project_dir(cwd, projects_root=projects) == pdir


def test_resolve_returns_none_when_no_match(tmp_path):
    projects = tmp_path / "projects"
    pdir = projects / "-some-other-repo"
    _write_session(pdir, SID_A, [_user_line("/some/other/repo")])
    assert rec.resolve_project_dir("/Users/jack.neil/Github/claude-jacked",
                                   projects_root=projects) is None


def test_resolve_returns_none_when_root_missing(tmp_path):
    assert rec.resolve_project_dir("/whatever", projects_root=tmp_path / "nope") is None

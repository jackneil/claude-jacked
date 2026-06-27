"""Tests that find_session_files excludes agent-* sidecar transcripts.

Sub-agent sidecar transcripts (files named ``agent-*.jsonl``) are sub-agent
sessions whose content is already folded into the parent session's summaries.
Indexing them as standalone sessions only pollutes /jacked search results, so
``find_session_files`` (the enumeration used by ``jacked backfill``) must skip
them and yield only real UUID-named session files.
"""

import uuid

from jacked.transcript import find_session_files


def _encode_repo_dir(projects_dir, name="C--Github-demo"):
    """Create an encoded repo directory under a fake projects dir."""
    repo_dir = projects_dir / name
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


def test_skips_agent_sidecar_files(tmp_path):
    """agent-*.jsonl files must not be yielded as standalone sessions."""
    projects_dir = tmp_path / "projects"
    repo_dir = _encode_repo_dir(projects_dir)

    real_session = f"{uuid.uuid4()}"
    (repo_dir / f"{real_session}.jsonl").write_text("{}\n")
    (repo_dir / "agent-a08e819.jsonl").write_text("{}\n")
    (repo_dir / f"agent-{uuid.uuid4()}.jsonl").write_text("{}\n")

    found = list(find_session_files(projects_dir))
    stems = {path.stem for path, _repo in found}

    assert real_session in stems
    assert not any(stem.startswith("agent-") for stem in stems)
    assert len(found) == 1


def test_yields_multiple_real_sessions(tmp_path):
    """Real UUID sessions are still enumerated; only agent-* are dropped."""
    projects_dir = tmp_path / "projects"
    repo_dir = _encode_repo_dir(projects_dir)

    sessions = {str(uuid.uuid4()) for _ in range(3)}
    for sid in sessions:
        (repo_dir / f"{sid}.jsonl").write_text("{}\n")
    (repo_dir / "agent-deadbeef.jsonl").write_text("{}\n")

    found_stems = {path.stem for path, _repo in find_session_files(projects_dir)}

    assert found_stems == sessions


def test_ignores_non_session_jsonl(tmp_path):
    """Non-UUID, non-agent .jsonl files (e.g. stray logs) are not yielded."""
    projects_dir = tmp_path / "projects"
    repo_dir = _encode_repo_dir(projects_dir)

    real_session = str(uuid.uuid4())
    (repo_dir / f"{real_session}.jsonl").write_text("{}\n")
    (repo_dir / "notes.jsonl").write_text("{}\n")
    (repo_dir / "agent-1234567.jsonl").write_text("{}\n")

    found_stems = {path.stem for path, _repo in find_session_files(projects_dir)}

    assert found_stems == {real_session}

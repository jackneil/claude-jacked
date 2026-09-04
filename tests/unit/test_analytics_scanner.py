"""Unit tests for jacked.web.analytics_scanner — incremental JSONL scanner.

Covers: assistant message parsing, dedup within file, incremental byte-offset
scanning, file-rewrite detection (offset > size), subagent directory scanning,
mtime-based skip, malformed JSON handling.
"""

import json
import os
import time


from jacked.web.analytics_db import AnalyticsDB
from jacked.web.analytics_scanner import (
    _parse_assistant_message,
    parse_jsonl_from_offset,
    scan_project_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path, messages):
    """Write a list of dicts as newline-delimited JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")


def _assistant_msg(msg_id, model="claude-opus-4-6", input_t=100, output_t=500,
                   cache_read=50000, cache_create=1000, session_id="sess_1"):
    return {
        "type": "assistant",
        "timestamp": "2026-04-07T12:00:00Z",
        "sessionId": session_id,
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
        },
    }


def _user_msg(msg_id="u1"):
    return {
        "type": "human",
        "timestamp": "2026-04-07T12:00:00Z",
        "sessionId": "sess_1",
        "message": {
            "id": msg_id,
            "role": "user",
            "content": "hello",
        },
    }


def _system_msg():
    return {
        "type": "system",
        "timestamp": "2026-04-07T12:00:00Z",
        "sessionId": "sess_1",
        "subtype": "init",
    }


def _make_db():
    return AnalyticsDB(db_path=":memory:")


# ---------------------------------------------------------------------------
# _parse_assistant_message
# ---------------------------------------------------------------------------

class TestParseAssistantMessage:
    def test_valid_message(self):
        record = _assistant_msg("msg_1")
        result = _parse_assistant_message(record, "sess_1", "proj_abc", is_subagent=False)
        assert result is not None
        assert result["id"] == "msg_1"
        assert result["session_id"] == "sess_1"
        assert result["project_hash"] == "proj_abc"
        assert result["model"] == "claude-opus-4-6"
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 500
        assert result["cache_read_tokens"] == 50000
        assert result["cache_create_tokens"] == 1000
        assert result["is_subagent"] == 0
        assert result["estimated_cost_usd"] > 0

    def test_subagent_flag(self):
        record = _assistant_msg("msg_2")
        result = _parse_assistant_message(record, "sess_1", "proj_abc", is_subagent=True)
        assert result is not None
        assert result["is_subagent"] == 1

    def test_missing_message_id(self):
        record = _assistant_msg("msg_1")
        del record["message"]["id"]
        result = _parse_assistant_message(record, "sess_1", "proj_abc", is_subagent=False)
        assert result is None

    def test_missing_usage(self):
        record = _assistant_msg("msg_1")
        del record["message"]["usage"]
        result = _parse_assistant_message(record, "sess_1", "proj_abc", is_subagent=False)
        assert result is None

    def test_missing_model_defaults_to_unknown(self):
        record = _assistant_msg("msg_1")
        del record["message"]["model"]
        result = _parse_assistant_message(record, "sess_1", "proj_abc", is_subagent=False)
        assert result is not None
        assert result["model"] == "unknown"

    def test_timestamp_extracted(self):
        record = _assistant_msg("msg_1")
        result = _parse_assistant_message(record, "sess_1", "proj_abc", is_subagent=False)
        assert result["timestamp"] == "2026-04-07T12:00:00Z"


# ---------------------------------------------------------------------------
# parse_jsonl_from_offset
# ---------------------------------------------------------------------------

class TestParseJsonlFromOffset:
    def test_parse_full_file(self, tmp_path):
        path = tmp_path / "03885b4e-c92a-4c9d-8ae6-37db89f16302.jsonl"
        msgs = [_assistant_msg("m1"), _assistant_msg("m2")]
        _write_jsonl(path, msgs)

        parsed, new_offset = parse_jsonl_from_offset(path, 0)
        assert len(parsed) == 2
        assert parsed[0]["id"] == "m1"
        assert parsed[1]["id"] == "m2"
        assert new_offset == path.stat().st_size

    def test_skips_non_assistant_messages(self, tmp_path):
        path = tmp_path / "test.jsonl"
        msgs = [_user_msg(), _assistant_msg("m1"), _system_msg()]
        _write_jsonl(path, msgs)

        parsed, _ = parse_jsonl_from_offset(path, 0)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "m1"

    def test_deduplicates_within_batch(self, tmp_path):
        path = tmp_path / "test.jsonl"
        msgs = [_assistant_msg("dup"), _assistant_msg("dup"), _assistant_msg("unique")]
        _write_jsonl(path, msgs)

        parsed, _ = parse_jsonl_from_offset(path, 0)
        assert len(parsed) == 2
        ids = [m["id"] for m in parsed]
        assert "dup" in ids
        assert "unique" in ids

    def test_incremental_from_offset(self, tmp_path):
        path = tmp_path / "test.jsonl"
        # Write initial content
        msgs1 = [_assistant_msg("m1")]
        _write_jsonl(path, msgs1)
        _, offset1 = parse_jsonl_from_offset(path, 0)

        # Append more
        with open(path, "a") as f:
            f.write(json.dumps(_assistant_msg("m2")) + "\n")

        parsed, offset2 = parse_jsonl_from_offset(path, offset1)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "m2"
        assert offset2 > offset1

    def test_handles_malformed_json(self, tmp_path):
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps(_assistant_msg("m1")) + "\n")
            f.write("this is not json\n")
            f.write("{incomplete json\n")
            f.write(json.dumps(_assistant_msg("m2")) + "\n")

        parsed, _ = parse_jsonl_from_offset(path, 0)
        assert len(parsed) == 2
        ids = [m["id"] for m in parsed]
        assert "m1" in ids
        assert "m2" in ids

    def test_skips_oversized_lines(self, tmp_path):
        path = tmp_path / "test.jsonl"
        with open(path, "w") as f:
            f.write(json.dumps(_assistant_msg("m1")) + "\n")
            # Write a line > 1MB
            big = {"type": "assistant", "data": "x" * (1024 * 1024 + 1)}
            f.write(json.dumps(big) + "\n")
            f.write(json.dumps(_assistant_msg("m2")) + "\n")

        parsed, _ = parse_jsonl_from_offset(path, 0)
        assert len(parsed) == 2

    def test_empty_file(self, tmp_path):
        path = tmp_path / "test.jsonl"
        path.touch()

        parsed, offset = parse_jsonl_from_offset(path, 0)
        assert parsed == []
        assert offset == 0

    def test_session_id_from_filename(self, tmp_path):
        """Session ID should come from the calling code (scan_project_dir),
        but parse_jsonl_from_offset should extract from sessionId field."""
        path = tmp_path / "03885b4e-c92a-4c9d-8ae6-37db89f16302.jsonl"
        msgs = [_assistant_msg("m1", session_id="sess_from_record")]
        _write_jsonl(path, msgs)

        parsed, _ = parse_jsonl_from_offset(path, 0)
        # parse_jsonl_from_offset returns raw parsed messages without session_id override
        assert parsed[0]["session_id"] == "sess_from_record"


# ---------------------------------------------------------------------------
# scan_project_dir
# ---------------------------------------------------------------------------

class TestScanProjectDir:
    def test_scans_top_level_jsonl(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "proj_hash"
        session_file = project_dir / "03885b4e-c92a-4c9d-8ae6-37db89f16302.jsonl"
        _write_jsonl(session_file, [_assistant_msg("m1"), _assistant_msg("m2")])

        count = scan_project_dir(project_dir, "proj_hash", db)
        assert count == 2

        # Messages in DB
        msgs = db.get_messages_for_session("03885b4e-c92a-4c9d-8ae6-37db89f16302")
        assert len(msgs) == 2
        db.close()

    def test_incremental_scan_only_new_bytes(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "proj_hash"
        session_file = project_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"

        # First scan
        _write_jsonl(session_file, [_assistant_msg("m1")])
        count1 = scan_project_dir(project_dir, "proj_hash", db)
        assert count1 == 1

        # Append and scan again — should only get the new message
        with open(session_file, "a") as f:
            f.write(json.dumps(_assistant_msg("m2")) + "\n")
        # Touch to change mtime
        time.sleep(0.05)
        os.utime(session_file, None)

        count2 = scan_project_dir(project_dir, "proj_hash", db)
        assert count2 == 1  # Only the new message

        db.close()

    def test_rewrite_detection_offset_gt_size(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "proj_hash"
        session_file = project_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"

        # First scan with a large file
        big_msgs = [_assistant_msg(f"m{i}") for i in range(10)]
        _write_jsonl(session_file, big_msgs)
        count1 = scan_project_dir(project_dir, "proj_hash", db)
        assert count1 == 10

        # Rewrite with a smaller file (Claude Code resumes)
        small_msgs = [_assistant_msg("new_m1"), _assistant_msg("new_m2")]
        _write_jsonl(session_file, small_msgs)
        # The scanner skips a file whose mtime is unchanged, so the rewrite has
        # to land on a later tick or the offset-reset branch is never reached.
        # Windows updates the clock about every 15 ms, which is coarse enough
        # for both writes to share an mtime. Same guard as the append test.
        time.sleep(0.05)
        os.utime(session_file, None)

        count2 = scan_project_dir(project_dir, "proj_hash", db)
        assert count2 == 2  # Should have reset and re-read

        db.close()

    def test_scans_subagent_directories(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "proj_hash"

        # Main session file
        main_session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        main_file = project_dir / f"{main_session}.jsonl"
        _write_jsonl(main_file, [_assistant_msg("main_msg")])

        # Subagent file inside session dir
        subagent_file = (
            project_dir / main_session / "subagents" / "agent-abc123.jsonl"
        )
        _write_jsonl(subagent_file, [_assistant_msg("sub_msg")])

        count = scan_project_dir(project_dir, "proj_hash", db)
        assert count == 2

        # Subagent message should use the parent session's UUID as session_id
        sub_msgs = db.get_messages_for_session(main_session)
        sub_ids = [m["id"] for m in sub_msgs]
        assert "main_msg" in sub_ids
        assert "sub_msg" in sub_ids

        # The subagent message should be flagged
        sub_msg = next(m for m in sub_msgs if m["id"] == "sub_msg")
        assert sub_msg["is_subagent"] == 1

        db.close()

    def test_skips_unchanged_mtime(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "proj_hash"
        session_file = project_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        _write_jsonl(session_file, [_assistant_msg("m1")])

        count1 = scan_project_dir(project_dir, "proj_hash", db)
        assert count1 == 1

        # Second scan without touching the file — mtime unchanged
        count2 = scan_project_dir(project_dir, "proj_hash", db)
        assert count2 == 0

        db.close()

    def test_handles_empty_project_dir(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "empty_proj"
        project_dir.mkdir()

        count = scan_project_dir(project_dir, "empty_proj", db)
        assert count == 0
        db.close()

    def test_handles_non_jsonl_files(self, tmp_path):
        db = _make_db()
        project_dir = tmp_path / "proj_hash"
        project_dir.mkdir(parents=True)

        # Non-JSONL file should be ignored
        (project_dir / "notes.txt").write_text("hello")
        (project_dir / "config.json").write_text("{}")

        count = scan_project_dir(project_dir, "proj_hash", db)
        assert count == 0
        db.close()

    def test_dedup_across_scans(self, tmp_path):
        """Messages already in DB should be deduped by INSERT OR IGNORE."""
        db = _make_db()
        project_dir = tmp_path / "proj_hash"
        session_file = project_dir / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"

        # Write file with duplicate messages (common in Claude Code)
        _write_jsonl(session_file, [
            _assistant_msg("m1"),
            _assistant_msg("m1"),  # dup within file
            _assistant_msg("m2"),
        ])

        count = scan_project_dir(project_dir, "proj_hash", db)
        # Within-batch dedup removes one dup
        assert count == 2

        # Verify DB has exactly 2
        msgs = db.get_messages_for_session("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        assert len(msgs) == 2
        db.close()


# ---------------------------------------------------------------------------
# scan_all_projects (async)
# ---------------------------------------------------------------------------

class TestScanAllProjects:
    def test_scan_all_projects(self, tmp_path):
        import asyncio
        from jacked.web.analytics_scanner import scan_all_projects

        # Use file-based DB: asyncio.to_thread runs scan_project_dir in a
        # separate thread, and :memory: DBs use threading.local() connections
        # so the worker thread would see an empty database.
        db = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
        data_dir = tmp_path / "projects"
        data_dir.mkdir()

        # Create two project dirs
        proj1 = data_dir / "proj1"
        proj2 = data_dir / "proj2"
        _write_jsonl(
            proj1 / "aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
            [_assistant_msg("p1m1")],
        )
        _write_jsonl(
            proj2 / "ffff-gggg-hhhh-iiii-jjjjjjjjjjjj.jsonl",
            [_assistant_msg("p2m1"), _assistant_msg("p2m2")],
        )

        progress_reports = []

        async def on_progress(info):
            progress_reports.append(info)

        async def _run():
            return await scan_all_projects([data_dir], db, progress_callback=on_progress)

        result = asyncio.run(_run())
        assert result["projects"] == 2
        assert result["messages"] == 3
        assert len(progress_reports) == 2
        assert progress_reports[-1]["projects_scanned"] == 2
        assert progress_reports[-1]["projects_total"] == 2
        db.close()

    def test_scan_all_empty(self, tmp_path):
        import asyncio
        from jacked.web.analytics_scanner import scan_all_projects

        db = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
        data_dir = tmp_path / "empty"
        data_dir.mkdir()

        async def _run():
            return await scan_all_projects([data_dir], db)

        result = asyncio.run(_run())
        assert result["projects"] == 0
        assert result["messages"] == 0
        db.close()


# ---------------------------------------------------------------------------
# prune_stale_scan_state
# ---------------------------------------------------------------------------

class TestPruneStaleScanState:
    def test_prune_removes_deleted_files(self, tmp_path):
        from jacked.web.analytics_scanner import prune_stale_scan_state

        db = _make_db()
        data_dir = tmp_path / "projects"
        proj = data_dir / "proj1"

        # Create a file and scan it
        session_file = proj / "aaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
        _write_jsonl(session_file, [_assistant_msg("m1")])
        scan_project_dir(proj, "proj1", db)

        # File exists, so it should not be pruned
        prune_stale_scan_state(db, [data_dir])
        state = db.get_scan_state(str(session_file))
        assert state is not None

        # Delete the file
        session_file.unlink()

        # Now prune should remove the scan state
        prune_stale_scan_state(db, [data_dir])
        state = db.get_scan_state(str(session_file))
        assert state is None
        db.close()

"""A rotation that no sync could prove before exit is not lost (#158i).

The sync thread syncs the file once when it starts. The exit-time sync sends
one fresh profile request. The next launch proves the rotation before a
refresh spends the row's refresh token, and it keeps a file whose tokens it
could not check.

The ownership cases are in test_launch_token_sync.py. The shared helpers are
in launch_sync_helpers.py.
"""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from unittest import mock

import click
import pytest

from jacked.api.authority_guard import ProfileLookup
from jacked.api.credential_helpers import clear_ownership_cache
from jacked.web.database import Database
from tests._platform import requires_symlinks
from tests.unit.launch_sync_helpers import (
    FUTURE_MS,
    INTRUDER,
    OWNER,
    _account_dir,
    _cc,
    _exit_claude,
    _file_pair,
    _Lookup,
    _make_row,
    _next_launch,
    _patched,
    _profile,
    _sync,
    _write_credentials,
)


@pytest.fixture(autouse=True)
def _fresh_ownership_cache():
    """Verdicts are cached per (account id, token hash): start each test clean."""
    clear_ownership_cache()
    yield
    clear_ownership_cache()


# The launch's log lines for row 1. They give the row id, never a token.
KEEP_WARNING = (
    "Account 1: the per-account file holds tokens whose ownership could not be "
    "checked (lookup failed); keeping them for this launch"
)
SKIP_REFRESH_WARNING = (
    "Account 1: skipping the pre-launch Claude Code refresh; the per-account "
    "file holds an unchecked rotation"
)
REPLACE_WARNING = (
    "Account 1: replacing the per-account file's token pair with the row's "
    "(sync result: refused)"
)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# A rotation that no sync could prove before exit is not lost
# ---------------------------------------------------------------------------


class TestAStrandedRotation:
    def test_the_exit_sync_sends_one_fresh_request_after_a_cached_failure(self, tmp_path):
        """The loop's failed request is cached for 300 s. The exit sync must
        not reuse it: one fresh request decides, and here it proves the
        owner."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        with _patched(_Lookup(ProfileLookup(None))):
            assert _sync(config_dir, tmp_path) == "deferred"  # a loop tick

        owner = _Lookup(_profile(OWNER))
        with _patched(owner):
            _exit_claude(config_dir, tmp_path)

        assert owner.calls == 1
        assert _cc(db)[:2] == ("rot-at-2", "rot-rt-2")

    def test_a_rotation_deferred_through_exit_is_kept_by_the_next_launch(self, tmp_path):
        """The scenario of #158i: the loop's request fails inside the 300 s
        window, the exit request fails too, and the next launch proves the
        owner before it writes the row's tokens over the file."""
        db = _make_row(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        with _patched(_Lookup(ProfileLookup(None))):
            assert _sync(config_dir, tmp_path) == "deferred"  # a loop tick

        down = _Lookup(ProfileLookup(None))
        with _patched(down):
            _exit_claude(config_dir, tmp_path)
        assert down.calls == 1  # exactly one fresh request at exit
        assert _cc(db)[:2] == ("row-at-1", "row-rt-1")  # still stranded

        # The next launch is a new `jacked claude` process: its cache is empty.
        clear_ownership_cache()
        with _patched(_Lookup(_profile(OWNER))):
            _next_launch(db, tmp_path)

        assert _cc(db)[:2] == ("rot-at-2", "rot-rt-2")
        assert _file_pair(config_dir) == ("rot-at-2", "rot-rt-2")

    def test_the_next_launch_overwrites_a_refused_file_as_before(self, tmp_path, caplog):
        """Refused (the profile names another owner): the row keeps its tokens
        and the launch writes them over the file, as before #158i. The log
        says so, with the row id and the sync result only."""
        db = _make_row(tmp_path, cc_expires=int(time.time()) - 7200, status="invalid")
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        caplog.set_level(logging.INFO)

        # The row's own token is expired, so the launch would refresh it; a
        # running session's hold skips that refresh (and reconcile).
        with _patched(_Lookup(_profile(INTRUDER, uuid="uuid-intruder"))), mock.patch(
            "jacked.api.credential_helpers.cc_rotation_block",
            return_value="live_session_holds",
        ):
            _next_launch(db, tmp_path)

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert _file_pair(config_dir) == ("row-at-1", "row-rt-1")
        assert _warnings(caplog).count(REPLACE_WARNING) == 1

    @pytest.mark.parametrize("case", ["lookup_fails", "expired"])
    def test_the_next_launch_keeps_an_unchecked_rotation(self, tmp_path, caplog, case):
        """Deferred (the lookup failed, or the rotated access token expired
        before anyone could check it): the row keeps its tokens AND the file
        keeps the rotation. The row's refresh token may be the one that the
        rotation spent, so the launch does not refresh the row's expired
        token either."""
        from jacked.web.auth import should_refresh_cc

        db = _make_row(tmp_path, cc_expires=int(time.time()) - 7200, status="invalid")
        assert should_refresh_cc(db.get_account(1))  # a refresh would be due
        config_dir = _account_dir(tmp_path)
        expires_ms = FUTURE_MS if case == "lookup_fails" else (int(time.time()) - 60) * 1000
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2", expires_ms=expires_ms)
        answer = {"lookup_fails": ProfileLookup(503), "expired": _profile(OWNER)}[case]
        rotation_block = mock.MagicMock(return_value=None)  # nothing holds the row
        refresh = mock.MagicMock()
        caplog.set_level(logging.INFO)

        with (
            _patched(_Lookup(answer)),
            mock.patch("jacked.api.credential_helpers.cc_rotation_block", rotation_block),
            mock.patch("jacked.web.auth.refresh_cc_token", new=refresh),
        ):
            _next_launch(db, tmp_path)

        assert _cc(db) == ("row-at-1", "row-rt-1", "invalid")
        assert _file_pair(config_dir) == ("rot-at-2", "rot-rt-2")
        rotation_block.assert_not_called()
        refresh.assert_not_called()
        for message in (KEEP_WARNING, SKIP_REFRESH_WARNING):
            assert _warnings(caplog).count(message) == 1
        assert REPLACE_WARNING not in caplog.messages

    def test_the_rescue_runs_before_the_pre_launch_refresh(self, tmp_path):
        """A refresh first would spend the row's refresh token, which Claude
        Code already spent when it rotated. With the rotation in the row, the
        token is fresh and no refresh runs."""
        db = _make_row(tmp_path, cc_expires=int(time.time()) - 60)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        refresh = mock.MagicMock()

        with (
            _patched(_Lookup(_profile(OWNER))),
            mock.patch("jacked.api.credential_helpers.cc_rotation_block", return_value=None),
            mock.patch("jacked.web.auth.refresh_cc_token", new=refresh),
        ):
            _next_launch(db, tmp_path)

        refresh.assert_not_called()
        assert _cc(db)[:2] == ("rot-at-2", "rot-rt-2")
        assert _file_pair(config_dir) == ("rot-at-2", "rot-rt-2")

    @pytest.mark.parametrize("database", ["file", "mock", "spec_mock", "memory"])
    def test_the_rescue_runs_only_with_a_database_file(
        self, tmp_path, monkeypatch, database
    ):
        """The rescue reads the database file with raw sqlite3. A stand-in
        database has no such file: the rescue does not run, and nothing is
        created in the working directory."""
        from jacked.launch import prepare_account_dir

        monkeypatch.chdir(tmp_path)
        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        def _file_db():
            database_ = Database(str(tmp_path / "test.db"))
            database_.create_account(OWNER, "primary-at", 9_999_999_999)
            return database_

        db = {
            "file": _file_db,
            "mock": mock.MagicMock,
            "spec_mock": lambda: mock.MagicMock(spec=Database),
            "memory": lambda: Database(":memory:"),
        }[database]()
        before = set(tmp_path.iterdir())
        sync = mock.MagicMock(return_value="unchanged")

        with (
            mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"),
            mock.patch("jacked.launch._sync_tokens_from_file", sync),
            mock.patch("jacked.launch.should_refresh",
                       side_effect=RuntimeError("stop after the rescue")),
            pytest.raises(RuntimeError, match="stop after the rescue"),
        ):
            prepare_account_dir({"id": 1, "email": OWNER}, db)

        if database == "file":
            sync.assert_called_once_with(config_dir, str(tmp_path / "test.db"))
        else:
            sync.assert_not_called()
        assert set(tmp_path.iterdir()) == before

    @requires_symlinks
    def test_a_symlinked_account_dir_is_refused_before_the_rescue_reads_it(self, tmp_path):
        db = _make_row(tmp_path)
        target = tmp_path / "elsewhere-dir"
        target.mkdir()
        _write_credentials(target, "rot-at-2", "rot-rt-2")
        (tmp_path / "accounts").mkdir()
        (tmp_path / "accounts" / "1").symlink_to(target, target_is_directory=True)
        lookup = _Lookup(_profile(OWNER))

        with _patched(lookup), pytest.raises(click.ClickException, match="symlink"):
            _next_launch(db, tmp_path)

        assert lookup.calls == 0
        assert _cc(db)[:2] == ("row-at-1", "row-rt-1")


def test_a_rescue_that_lost_its_cas_still_publishes_the_winning_tokens(tmp_path, monkeypatch):
    """Another writer stored newer tokens while the pre-launch sync ran: the
    sync answers "unchanged" (its compare-and-swap lost), and the scoped file
    must carry the WINNER's tokens, not the snapshot prepare_account_dir
    read before the sync."""
    import json

    from jacked import launch
    from jacked.web.database import Database

    db = Database(str(tmp_path / "jacked.db"))
    aid = db.create_account("owner@test.com", "primary-at", 9_999_999_999)["id"]
    db.update_account(
        aid, cc_access_token="old-at", cc_refresh_token="old-rt", cc_expires_at=9_999_999_999,
        validation_status="valid",
    )
    config_dir = tmp_path / ".claude" / "accounts" / str(aid)
    config_dir.mkdir(parents=True)
    monkeypatch.setattr(launch, "ACCOUNTS_DIR", tmp_path / ".claude" / "accounts")

    def _sync_that_lost(config_dir_arg, db_path):
        # The concurrent writer lands before this sync's CAS.
        db.update_account(aid, cc_access_token="winner-at", cc_refresh_token="winner-rt")
        return "unchanged"

    monkeypatch.setattr(launch, "_sync_tokens_from_file", _sync_that_lost)
    monkeypatch.setattr(launch, "should_refresh", lambda account: False)
    monkeypatch.setattr(launch, "should_refresh_cc", lambda account: False, raising=False)

    launch.prepare_account_dir(db.get_account(aid), db)

    written = json.loads((config_dir / ".credentials.json").read_text(encoding="utf-8"))
    assert written["claudeAiOauth"]["accessToken"] == "winner-at"
    assert written["claudeAiOauth"]["refreshToken"] == "winner-rt"


# ---------------------------------------------------------------------------
# The sync thread syncs the file that it starts with
# ---------------------------------------------------------------------------


class TestTheThreadSyncsTheFileItStartsWith:
    """A rotation can land between the launch's own write and the start of
    the sync thread. The thread syncs the file once before it accepts the
    file's mtime, so it does not take that rotation as already synced."""

    @staticmethod
    def _run_loop(monkeypatch, tmp_path, first_answer):
        """Run the loop for one tick. The file exists before the thread
        starts and does not change. Return (ticks, mtime) for each sync."""
        from jacked import launch

        config_dir = _account_dir(tmp_path)
        _write_credentials(config_dir, "rot-at-2", "rot-rt-2")
        cred_path = config_dir / ".credentials.json"
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0]
        answers = iter([first_answer, "imported"])
        calls = []

        def counting_sync(_config_dir, _db_path):
            calls.append((proc.poll.call_count, cred_path.stat().st_mtime_ns))
            return next(answers)

        monkeypatch.setattr(launch, "_sync_tokens_from_file", counting_sync)
        monkeypatch.setattr(
            launch, "_time", SimpleNamespace(sleep=lambda _seconds: None, time=time.time)
        )
        launch._token_sync_loop(proc, config_dir, str(tmp_path / "test.db"))
        assert proc.poll.call_count == 2  # the loop ran exactly one tick
        return calls

    def test_a_deferred_first_sync_is_tried_again_on_the_first_tick(
        self, monkeypatch, tmp_path
    ):
        calls = self._run_loop(monkeypatch, tmp_path, "deferred")

        # Once before the first poll, then again on the first tick.
        assert [ticks for ticks, _ in calls] == [0, 1]
        assert calls[0][1] == calls[1][1]  # the mtime did not change

    @pytest.mark.parametrize("answer", ["imported", "refused", "unchanged"])
    def test_a_settled_first_sync_accepts_the_mtime(self, monkeypatch, tmp_path, answer):
        calls = self._run_loop(monkeypatch, tmp_path, answer)

        # The first tick sees the same mtime, so it does not sync again.
        assert [ticks for ticks, _ in calls] == [0]


# ---------------------------------------------------------------------------
# The next launch keeps a file whose tokens it could not check
# ---------------------------------------------------------------------------


def test_a_deferred_rescue_keeps_the_whole_file_and_warns(tmp_path, caplog):
    """The pre-launch rescue could not check the file's tokens (the profile
    request failed). The launch keeps the file as it is, with the keys that
    Claude Code added beside claudeAiOauth, and logs one WARNING that gives
    the row id only."""
    db = _make_row(tmp_path)
    config_dir = _account_dir(tmp_path)
    original = {
        "claudeAiOauth": {
            "accessToken": "rot-at-2",
            "refreshToken": "rot-rt-2",
            "expiresAt": FUTURE_MS,
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
        },
        "mcpOAuth": {"server-1": {"accessToken": "mcp-at-1"}},
    }
    (config_dir / ".credentials.json").write_text(json.dumps(original))
    caplog.set_level(logging.INFO)

    with _patched(_Lookup(ProfileLookup(503))):
        _next_launch(db, tmp_path)

    assert json.loads((config_dir / ".credentials.json").read_text()) == original
    assert _cc(db) == ("row-at-1", "row-rt-1", "unknown")  # nothing proven or imported
    kept = [r.levelno for r in caplog.records if r.getMessage() == KEEP_WARNING]
    assert kept == [logging.WARNING]
    for value in ("rot-at-2", "rot-rt-2", "mcp-at-1", "row-at-1", OWNER):
        assert value not in caplog.text

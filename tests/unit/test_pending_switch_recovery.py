"""Startup recovery of credential switches an earlier process left pending (#143i).

A switch is journaled before its stores are written, so a crash (or an
upgrade's exec) between the two leaves the journal row pending.
``recover_pending_switches`` runs once at service start, off the event loop
and under the switch lease. It observes the authority, classifies each row
without rewriting a credential, and never raises.

Every topology jacked ships today is GLOBAL_UNCOOPERATIVE. Its engine
journals an unverified row (empty verifiers, no recovery key), so recovery
can only call such a row indeterminate. Only a cooperative capability signs
its row with the private recovery key, and only a signed row lets recovery
prove "the target landed" (committed) or "nothing changed"
(failed_preserved). The classification tests below certify a cooperative
Linux topology on the real global credential file to drive those two
outcomes end to end: real key file, real file authority, real repository and
real identity publisher.
"""

from __future__ import annotations

import json
import asyncio
import dataclasses
import logging
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from jacked.credentials import runtime, startup_recovery
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.capabilities import CapabilityRecord, CapabilityRegistry
from jacked.credentials.key import FileInstallKeyProvider, machine_install_id
from jacked.credentials.models import (
    CapabilityMode,
    CredentialCapability,
    ExecutableIdentity,
    InteractionMode,
    PendingSwitchRecord,
    SwitchContext,
)
from jacked.credentials.transaction import (
    SwitchRequest,
    build_pending_record,
    build_unverified_pending_record,
)
from jacked.web.credential_repository import DatabaseCredentialSwitchRepository
from jacked.web.database import Database

RUNTIME_LOGGER = "jacked.credentials.startup_recovery"
MAIN_LOGGER = "jacked.api.main"
OPERATION_ID = "op-1"
BEFORE_ACCOUNT_ID = 1
TARGET_ACCOUNT_ID = 2
TARGET_EMAIL = "two@example.com"
# The install id the engine journals for an unfenced (uncooperative) switch.
UNFENCED_INSTALL_ID = "unfenced-local"

LINUX = ExecutableIdentity(
    resolved_path="/different/install/location/claude",
    sha256="0" * 64,
    build_version=runtime.INSPECTED_CLAUDE_BUILD,
    config_mode="global",
    platform_system="linux",
    platform_machine="x86_64",
)


def _payload(account_id: int, token: str) -> CredentialPayload:
    return CredentialPayload.from_mapping(
        {"_jackedAccountId": account_id, "claudeAiOauth": {"accessToken": token}}
    )


BEFORE = _payload(BEFORE_ACCOUNT_ID, "before-token")
TARGET = _payload(TARGET_ACCOUNT_ID, "target-token")


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "jacked.db"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A private home that the recovery sweep resolves through Path.home()."""
    path = tmp_path / "home"
    (path / ".claude").mkdir(parents=True)
    # A custom OAuth endpoint makes Claude's config path unknown, which would
    # turn every identity publish into committed_degraded.
    monkeypatch.delenv("CLAUDE_CODE_CUSTOM_OAUTH_URL", raising=False)
    with mock.patch("jacked.credentials.runtime.Path.home", return_value=path):
        yield path


@pytest.fixture
def linux_claude():
    with mock.patch(
        "jacked.credentials.runtime.detect_claude_identity", return_value=LINUX
    ) as detect:
        yield detect


@pytest.fixture
def shipped_capability() -> CredentialCapability:
    """The Linux capability jacked ships: global file, uncooperative."""
    capability = runtime.SHIPPED_REGISTRY.resolve(LINUX).capability
    assert capability.mode is CapabilityMode.GLOBAL_UNCOOPERATIVE
    return capability


@pytest.fixture
def cooperative_linux(shipped_capability) -> CredentialCapability:
    """Certify a cooperative Linux topology on the same global credential file."""
    capability = replace(shipped_capability, mode=CapabilityMode.GLOBAL_COOPERATIVE)
    registry = CapabilityRegistry(
        (
            CapabilityRecord(
                platform_system="linux",
                config_mode="global",
                min_build=runtime.MIN_CLAUDE_BUILD,
                inspected_through=runtime.INSPECTED_CLAUDE_BUILD,
                capability=capability,
            ),
        )
    )
    with mock.patch.object(runtime, "SHIPPED_REGISTRY", registry):
        yield capability


def _install_key(home: Path) -> bytes:
    """Write the recovery key the way FileInstallKeyProvider keeps it.

    The provider reads exactly 32 raw bytes from a private (0o600) regular
    file with one link, at ``<home>/.claude/credential-recovery.key``.
    """
    key = bytes(range(32))
    path = home / ".claude" / "credential-recovery.key"
    path.write_bytes(key)
    path.chmod(0o600)
    assert FileInstallKeyProvider(path).get_key() == key
    return key


def _signed_pending(capability: CredentialCapability, key: bytes) -> PendingSwitchRecord:
    """The row a cooperative switch journals: verifiers signed with the key."""
    return build_pending_record(
        operation_id=OPERATION_ID,
        account_id=TARGET_ACCOUNT_ID,
        organization_id="org-2",
        context=SwitchContext.MANUAL,
        before=BEFORE,
        target=TARGET,
        capability=capability,
        machine_install_id=machine_install_id(key),
        install_key=key,
        email=TARGET_EMAIL,
        display_name="Two Example",
        organization_name="Example Org",
    )


def _unverified_pending(capability: CredentialCapability) -> PendingSwitchRecord:
    """The row a shipped (uncooperative) switch journals: no verifiers."""
    request = SwitchRequest(
        operation_id=OPERATION_ID,
        account_id=TARGET_ACCOUNT_ID,
        email=TARGET_EMAIL,
        organization_id="org-2",
        payload=TARGET,
        context=SwitchContext.MANUAL,
        interaction=InteractionMode.FOREGROUND,
    )
    return build_unverified_pending_record(
        request=request,
        capability=capability,
        machine_install_id=UNFENCED_INSTALL_ID,
    )


def _write_authority(home: Path, payload: CredentialPayload) -> Path:
    path = home / ".claude" / ".credentials.json"
    path.write_bytes(payload.to_bytes())
    path.chmod(0o600)
    return path


def _pending_ids(db) -> list[str]:
    return [row["operation_id"] for row in db.list_pending_credential_switches()]


def _runtime_records(caplog, level: int | None = None) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == RUNTIME_LOGGER and (level is None or record.levelno == level)
    ]


def _recovered_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in _runtime_records(caplog, logging.INFO)
        if record.getMessage().startswith("Settled credential switch ")
    ]


# ---------------------------------------------------------------------------
# recover_pending_switches
# ---------------------------------------------------------------------------


def test_no_pending_rows_returns_at_once_without_probing_claude(db, caplog):
    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            side_effect=AssertionError("probed Claude with nothing to recover"),
        ) as detect,
        caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER),
    ):
        assert startup_recovery.recover_pending_switches(db) == []

    # The sweep swallows every exception, so the AssertionError alone would
    # pass silently: the call count is the proof.
    assert detect.call_count == 0
    assert _runtime_records(caplog) == []


def test_a_row_whose_target_landed_is_committed_and_settled(
    db, home, linux_claude, cooperative_linux, caplog
):
    """The authority holds the exact target payload the row signed.

    ``CredentialRecovery.recover`` then republishes the identity Claude Code
    watches. The row carries that identity (email and names) and the
    temporary home's config is writable, so the publish lands and the outcome
    is ``committed``. A row without an identity, or a config that cannot be
    written, would give ``committed_degraded`` instead.
    """
    key = _install_key(home)
    DatabaseCredentialSwitchRepository(db).create_pending(
        _signed_pending(cooperative_linux, key)
    )
    authority = _write_authority(home, TARGET)
    stored = authority.read_bytes()

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        recovered = startup_recovery.recover_pending_switches(db)

    assert recovered == [(OPERATION_ID, "committed")]
    assert db.list_pending_credential_switches() == []
    row = db.get_credential_switch(OPERATION_ID)
    assert row["phase"] == "committed"
    assert row["outcome"] == "committed"
    assert db.get_setting("active_account_id") == str(TARGET_ACCOUNT_ID)
    # Observation only: the credentials are never rewritten.
    assert authority.read_bytes() == stored
    # Running sessions follow the switch through the published identity.
    config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert config["oauthAccount"]["emailAddress"] == TARGET_EMAIL
    assert _recovered_lines(caplog) == [f"Settled credential switch {OPERATION_ID!r}: committed"]


def test_a_row_whose_target_never_landed_is_failed_preserved(
    db, home, linux_claude, cooperative_linux, caplog
):
    key = _install_key(home)
    DatabaseCredentialSwitchRepository(db).create_pending(
        _signed_pending(cooperative_linux, key)
    )
    authority = _write_authority(home, BEFORE)
    stored = authority.read_bytes()

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        recovered = startup_recovery.recover_pending_switches(db)

    assert recovered == [(OPERATION_ID, "failed_preserved")]
    assert OPERATION_ID not in _pending_ids(db)
    row = db.get_credential_switch(OPERATION_ID)
    assert row["phase"] == "failed"
    assert row["outcome"] == "failed_preserved"
    assert db.get_setting("active_account_id") is None
    assert authority.read_bytes() == stored
    # A preserved switch never publishes an identity.
    assert not (home / ".claude.json").exists()
    assert _recovered_lines(caplog) == [
        f"Settled credential switch {OPERATION_ID!r}: failed_preserved"
    ]


def test_a_shipped_row_without_verifiers_is_indeterminate(
    db, home, linux_claude, shipped_capability, caplog
):
    """The row every shipped topology journals has nothing to verify against.

    Even with the target payload in the authority, recovery cannot prove
    which state it observes, so it records the row as indeterminate and
    leaves the credentials and the active pointer alone.
    """
    DatabaseCredentialSwitchRepository(db).create_pending(
        _unverified_pending(shipped_capability)
    )
    authority = _write_authority(home, TARGET)
    stored = authority.read_bytes()

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        recovered = startup_recovery.recover_pending_switches(db)

    assert recovered == [(OPERATION_ID, "indeterminate")]
    row = db.get_credential_switch(OPERATION_ID)
    assert row["phase"] == "indeterminate"
    assert row["outcome"] == "indeterminate"
    assert db.get_setting("active_account_id") is None
    assert authority.read_bytes() == stored
    assert not (home / ".claude.json").exists()
    assert _recovered_lines(caplog) == [
        f"Settled credential switch {OPERATION_ID!r}: indeterminate"
    ]

    # Observed once, recorded once: a later service start does not sweep the
    # indeterminate row again (it can never change), so the decision log
    # does not grow by one row per restart.
    def outcome_rows() -> int:
        with db._reader() as conn:
            return conn.execute(
                "SELECT count(*) FROM decision_log "
                "WHERE action = 'credential_switch_outcome'"
            ).fetchone()[0]

    recorded = outcome_rows()
    assert recorded >= 1
    assert startup_recovery.recover_pending_switches(db) == []
    assert startup_recovery.recover_pending_switches(db) == []
    assert outcome_rows() == recorded
    assert db.get_credential_switch(OPERATION_ID)["phase"] == "indeterminate"


def test_recovery_skips_while_a_switch_holds_the_lease(
    db, home, linux_claude, cooperative_linux, caplog
):
    key = _install_key(home)
    DatabaseCredentialSwitchRepository(db).create_pending(
        _signed_pending(cooperative_linux, key)
    )
    # The target landed, so only the lease stands between this row and a commit.
    _write_authority(home, TARGET)

    with runtime.switch_lease_for(home).acquire() as held:
        assert held is True
        with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
            recovered = startup_recovery.recover_pending_switches(db)

    assert recovered == []
    assert _pending_ids(db) == [OPERATION_ID]
    assert db.get_credential_switch(OPERATION_ID)["phase"] == "pending"
    assert [r.getMessage() for r in _runtime_records(caplog, logging.INFO)] == [
        "Pending credential switch recovery skipped: a switch is in progress"
    ]
    assert _runtime_records(caplog, logging.WARNING) == []


def test_a_failed_claude_probe_is_logged_and_never_raises(
    db, home, cooperative_linux, caplog
):
    key = _install_key(home)
    DatabaseCredentialSwitchRepository(db).create_pending(
        _signed_pending(cooperative_linux, key)
    )
    _write_authority(home, TARGET)
    private_detail = "/opt/private-install/claude: detail that stays out of the log"

    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            side_effect=OSError(private_detail),
        ),
        caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER),
    ):
        recovered = startup_recovery.recover_pending_switches(db)

    assert recovered == []
    warnings = _runtime_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "OSError" in warnings[0].getMessage()
    # Type name only: no traceback and no exception text in the log.
    assert warnings[0].exc_info is None
    assert warnings[0].exc_text is None
    assert private_detail not in caplog.text
    assert _pending_ids(db) == [OPERATION_ID]
    assert db.get_credential_switch(OPERATION_ID)["phase"] == "pending"


# ---------------------------------------------------------------------------
# Service start
# ---------------------------------------------------------------------------


def test_service_start_recovers_pending_switches_with_the_app_database(
    quiet_lifespan, monkeypatch, caplog
):
    from fastapi.testclient import TestClient

    from jacked.api import main as api_main

    seen: list[tuple[object, bool]] = []

    def _recover(db):
        seen.append((db, db is api_main.app.state.db))
        return [("op-x", "committed")]

    recover = mock.Mock(side_effect=_recover)
    monkeypatch.setattr(api_main, "recover_pending_switches", recover)

    with caplog.at_level(logging.INFO, logger=MAIN_LOGGER):
        with TestClient(api_main.app):
            pass

    assert recover.call_count == 1
    [(db, is_app_db)] = seen
    assert db is not None
    assert is_app_db is True
    main_info = [
        r.getMessage()
        for r in caplog.records
        if r.name == MAIN_LOGGER and r.levelno == logging.INFO
    ]
    assert "Settled 1 pending credential switch(es)" in main_info


def test_service_start_survives_a_recovery_that_raises(
    quiet_lifespan, monkeypatch, caplog
):
    from fastapi.testclient import TestClient

    from jacked.api import main as api_main

    recover = mock.Mock(side_effect=RuntimeError("database is locked"))
    monkeypatch.setattr(api_main, "recover_pending_switches", recover)
    before = api_main._lifespan_startup_count

    with caplog.at_level(logging.INFO, logger=MAIN_LOGGER):
        with TestClient(api_main.app):
            pass

    assert recover.call_count == 1
    assert api_main._lifespan_startup_count == before + 1
    main_records = [r for r in caplog.records if r.name == MAIN_LOGGER]
    assert "Pending credential switch recovery failed: RuntimeError" in [
        r.getMessage() for r in main_records if r.levelno == logging.WARNING
    ]
    # Type name only, and no claim that anything was recovered.
    assert not any("database is locked" in r.getMessage() for r in main_records)
    assert not any(r.getMessage().startswith("Settled ") for r in main_records)


def test_a_row_that_fails_to_settle_does_not_hide_the_rows_that_did(
    db, home, linux_claude, shipped_capability, caplog
):
    """One row raising mid-sweep is logged by id and skipped; the rows already
    settled are still returned and the remaining rows are still visited."""
    repository = DatabaseCredentialSwitchRepository(db)
    first = _unverified_pending(shipped_capability)
    second = dataclasses.replace(first, operation_id="op-2")
    third = dataclasses.replace(first, operation_id="op-3")
    for record in (first, second, third):
        repository.create_pending(record)
    _write_authority(home, TARGET)

    real_recovery = startup_recovery.CredentialRecovery

    class _FlakyRecovery(real_recovery):
        def recover(self, operation_id):
            if operation_id == "op-2":
                raise RuntimeError("database is locked")
            return super().recover(operation_id)

    with (
        mock.patch.object(startup_recovery, "CredentialRecovery", _FlakyRecovery),
        caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER),
    ):
        settled = startup_recovery.recover_pending_switches(db)

    assert settled == [(OPERATION_ID, "indeterminate"), ("op-3", "indeterminate")]
    warnings = [
        r.getMessage() for r in _runtime_records(caplog, logging.WARNING)
    ]
    assert warnings == ["Settling credential switch 'op-2' failed: RuntimeError"]
    assert "database is locked" not in caplog.text
    assert db.get_credential_switch("op-2")["phase"] == "pending"


def test_the_heal_cadence_retries_the_sweep(monkeypatch, caplog):
    """A sweep the start skipped (lease held) is retried on the heal cadence."""
    from types import SimpleNamespace

    from jacked.api import main as api_main

    recover = mock.Mock(return_value=[("op-late", "committed")])
    monkeypatch.setattr(api_main, "recover_pending_switches", recover)
    app = SimpleNamespace(state=SimpleNamespace(db=object()))

    with caplog.at_level(logging.INFO, logger="jacked.api.main"):
        asyncio.run(api_main._retry_pending_switch_recovery(app))

    recover.assert_called_once_with(app.state.db)
    assert "Settled 1 pending credential switch(es)" in caplog.text
    # No database: nothing to sweep, nothing called.
    recover.reset_mock()
    asyncio.run(api_main._retry_pending_switch_recovery(SimpleNamespace(state=SimpleNamespace(db=None))))
    recover.assert_not_called()


def test_a_malformed_journal_row_does_not_hide_a_valid_one(
    db, home, linux_claude, shipped_capability, caplog
):
    """An overflowing or corrupt capability_epoch in one row is skipped with a
    WARNING naming the row; the valid row is still settled."""
    repository = DatabaseCredentialSwitchRepository(db)
    repository.create_pending(_unverified_pending(shipped_capability))
    good = dataclasses.replace(_unverified_pending(shipped_capability), operation_id="op-good")
    repository.create_pending(good)
    with db._writer() as conn:
        conn.execute(
            "UPDATE credential_switches SET capability_epoch = ? WHERE operation_id = ?",
            (float("inf"), OPERATION_ID),
        )
    _write_authority(home, TARGET)

    with caplog.at_level(logging.INFO, logger=RUNTIME_LOGGER):
        settled = startup_recovery.recover_pending_switches(db)

    assert settled == [("op-good", "indeterminate")]
    repo_warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "jacked.web.credential_repository" and r.levelno == logging.WARNING
    ]
    # SQLite may hand the stored infinity back as a REAL (OverflowError on
    # int()) or as text (ValueError); either way the row is skipped by id
    # with the exception type only, never its text.
    assert len(repo_warnings) == 1
    assert repo_warnings[0].startswith(
        f"Skipping malformed credential switch row {OPERATION_ID!r}: "
    )
    assert repo_warnings[0].endswith("Error")
    assert "inf" not in repo_warnings[0]

"""Database contracts for credential transactions and session observations."""

from jacked.web.database import Database
from jacked.web.credential_repository import DatabaseCredentialSwitchRepository
from jacked.credentials.models import (
    CapabilityMode,
    CredentialIdentity,
    FinalizeSwitchRecord,
    OutcomeSwitchRecord,
    PendingSwitchRecord,
    SwitchContext,
    SwitchOutcome,
)


def test_session_observation_inbox_coalesces_and_drains(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    db.enqueue_session_observation(
        "session-1",
        "UserPromptSubmit",
        credential_revision="rev-1",
        repo_path="/first",
        idempotency_key="session-1:prompt:rev-1",
    )
    db.enqueue_session_observation(
        "session-1",
        "UserPromptSubmit",
        credential_revision="rev-1",
        repo_path="/latest",
        idempotency_key="session-1:prompt:rev-1",
    )

    rows = db.drain_session_observations()

    assert len(rows) == 1
    assert rows[0]["repo_path"] == "/latest"
    assert db.drain_session_observations() == []


def test_only_known_global_sessions_become_pending(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    db.record_session_account(
        "global",
        account_id=1,
        credential_scope="global",
        observation_state="observed",
    )
    db.record_session_account(
        "scoped",
        account_id=2,
        credential_scope="scoped",
        observation_state="observed",
    )

    assert db.mark_global_sessions_pending() == 1
    assert db.get_session_accounts("global")[0]["observation_state"] == "pending"
    assert db.get_session_accounts("scoped")[0]["observation_state"] == "observed"


def test_credential_switch_journal_contains_verifiers_not_payload(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    db.create_credential_switch(
        {
            "operation_id": "op-1",
            "account_id": 5,
            "previous_account_id": 6,
            "context": "manual",
            "capability_mode": "GLOBAL_COOPERATIVE",
            "capability_epoch": "claude-2.1.81",
            "backend_locator": "file:test",
            "before_hmac": "before-verifier",
            "target_hmac": "target-verifier",
            "detail": {"safe": True},
        }
    )

    row = db.get_credential_switch("op-1")

    assert row["phase"] == "pending"
    assert row["before_hmac"] == "before-verifier"
    assert row["target_hmac"] == "target-verifier"
    assert "token" not in row["detail_json"].lower()


def test_auth_action_idempotency_replays_result_and_rejects_mismatch(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    claim = {
        "session_key": "local-session",
        "action": "use-account",
        "request_digest": "digest-a",
        "operation_id": "op-1",
        "expires_at": "2999-01-01T00:00:00+00:00",
    }
    assert db.claim_auth_action("action-1", **claim) == ("new", None)
    db.finish_auth_action("action-1", {"status": "committed"})

    assert db.claim_auth_action("action-1", **claim) == (
        "complete",
        {"status": "committed"},
    )
    assert db.claim_auth_action(
        "action-1", **{**claim, "request_digest": "digest-b"}
    ) == ("mismatch", None)


def test_sqlite_repository_finalize_is_atomic_and_secret_free(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    repository = DatabaseCredentialSwitchRepository(db)
    repository.create_pending(
        PendingSwitchRecord(
            operation_id="op-final",
            account_id=7,
            organization_id="org-1",
            context=SwitchContext.MANUAL,
            capability_mode=CapabilityMode.GLOBAL_COOPERATIVE,
            machine_install_id="machine-1",
            backend_locator="file:credentials",
            capability_epoch=2,
            canonicalizer_version=1,
            before_hmac="before",
            target_hmac="target",
        )
    )

    repository.finalize(
        FinalizeSwitchRecord(
            operation_id="op-final",
            account_id=7,
            outcome=SwitchOutcome.COMMITTED,
            observed_identity=CredentialIdentity(7, "person@example.com", "org-1"),
            credential_revision="revision-1",
        )
    )

    row = db.get_credential_switch("op-final")
    assert row["phase"] == "committed"
    assert db.get_setting("active_account_id") == "7"
    assert db.get_setting("desired_account_id") == "7"
    assert repository.get_pending("op-final") is None


def test_unfenced_observation_updates_desired_not_committed_pointer(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    db.set_setting("active_account_id", "6")
    repository = DatabaseCredentialSwitchRepository(db)

    repository.record_outcome(
        OutcomeSwitchRecord(
            operation_id="op-observed",
            account_id=5,
            outcome=SwitchOutcome.OBSERVED_TARGET_UNFENCED,
            observed_identity=CredentialIdentity(5, "target@example.com", None),
            message="target observed",
        )
    )

    assert db.get_setting("desired_account_id") == "5"
    assert db.get_setting("active_account_id") == "6"
    assert db.get_credential_switch("op-observed")["phase"] == "observed_only"


def test_pending_audit_prefers_validated_desired_previous_identity(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    prior = db.create_account("prior@example.com", "prior-token", 2_000_000_000)
    desired = db.create_account(
        "desired@example.com", "desired-token", 2_000_000_000
    )
    db.set_setting("active_account_id", str(prior["id"]))
    db.set_setting("desired_account_id", str(desired["id"]))
    repository = DatabaseCredentialSwitchRepository(db)

    repository.create_pending(
        PendingSwitchRecord(
            operation_id="op-desired-audit",
            account_id=prior["id"],
            organization_id=None,
            context=SwitchContext.MANUAL,
            capability_mode=CapabilityMode.GLOBAL_UNCOOPERATIVE,
            machine_install_id="unfenced-local",
            backend_locator="keychain:test",
            capability_epoch=1,
            canonicalizer_version=1,
            before_hmac="",
            target_hmac="",
        )
    )

    row = db.get_credential_switch("op-desired-audit")
    assert row["previous_account_id"] == desired["id"]

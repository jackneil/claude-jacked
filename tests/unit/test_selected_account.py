"""One rule picks the account jacked selected for new Claude sessions (#226i).

The rule: ``desired_account_id``, else ``active_account_id``. A value that is
missing, empty, not an integer or not positive is skipped, so a bad desired
value never hides a valid active id. Every reader goes through
``jacked.web.database.selected_account_id``.

The pin table drives the real switch repository through every outcome that
moves a pointer. The macOS outcome (``observed_target_unfenced``) writes only
the desired pointer and leaves the active pointer on the old account, so a
reader that takes the wrong pointer reads a stale account.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import jacked
from jacked.api import identity_watcher, session_observer, stamped_rotation
from jacked.api.authority_guard import (
    _repair_committed,
    heal_foreign_authority,
    reset_authority_guard_state,
)
from jacked.api.routes.auth import router
from jacked.credentials.models import (
    CapabilityMode,
    CredentialIdentity,
    FinalizeSwitchRecord,
    IdentityAxis,
    OutcomeSwitchRecord,
    PendingSwitchRecord,
    ProviderVerificationState,
    SessionActivationState,
    SwitchContext,
    SwitchOutcome,
    SwitchResult,
)
from jacked.web.credential_repository import DatabaseCredentialSwitchRepository
from jacked.web.database import SELECTED_ACCOUNT_KEYS, Database, selected_account_id
from jacked.web.oauth import OAuthFlow


@pytest.fixture(autouse=True)
def _clean_guard_state():
    reset_authority_guard_state()
    yield
    reset_authority_guard_state()


def _insert_account(db: Database, account_id: int, email: str, **columns) -> None:
    """Insert one Claude account row with an explicit id."""
    values = {
        "id": account_id,
        "email": email,
        "provider": "claude",
        "organization_uuid": f"org-{account_id}",
        "access_token": f"at_{account_id}",
        "expires_at": 1_900_000_000,
        "is_active": 1,
        "is_deleted": 0,
        "validation_status": "valid",
        "cc_access_token": f"cc_at_{account_id}",
        "cc_refresh_token": f"cc_rt_{account_id}",
        "cc_expires_at": 1_900_000_000,
        **columns,
    }
    names = ", ".join(values)
    marks = ", ".join("?" * len(values))
    with db._writer() as conn:
        conn.execute(
            f"INSERT INTO accounts ({names}) VALUES ({marks})", tuple(values.values())
        )


class _Settings:
    """A settings double with no ``get_account`` at all."""

    def __init__(self, **values) -> None:
        self.values = values

    def get_setting(self, key: str):
        return self.values.get(key)


class _Store(_Settings):
    """A settings double that answers ``get_account`` from ``rows``."""

    def __init__(self, rows: dict | None = None, **values) -> None:
        super().__init__(**values)
        self.rows = rows or {}
        self.lookups: list[int] = []

    def get_account(self, account_id: int):
        self.lookups.append(account_id)
        return self.rows.get(account_id)


# --- the rule --------------------------------------------------------------


def test_the_keys_name_the_desired_pointer_first():
    assert SELECTED_ACCOUNT_KEYS == ("desired_account_id", "active_account_id")


def test_the_desired_account_wins_over_the_active_account():
    settings = _Settings(desired_account_id="5", active_account_id="3")

    assert selected_account_id(settings) == 5


@pytest.mark.parametrize("desired", ["abc", "2.5", "0", "-2", "", None])
def test_an_unusable_desired_value_falls_back_to_the_active_account(desired):
    settings = _Settings(desired_account_id=desired, active_account_id="3")

    assert selected_account_id(settings) == 3


def test_an_integer_setting_is_read_as_an_id():
    assert selected_account_id(_Settings(desired_account_id=7)) == 7


def test_nothing_selected_is_none():
    assert selected_account_id(_Settings()) is None
    assert selected_account_id(_Settings(desired_account_id="x", active_account_id="0")) is None


def test_existing_only_skips_a_deleted_desired_row():
    store = _Store(
        rows={5: {"id": 5, "is_deleted": 1}, 3: {"id": 3, "is_deleted": 0}},
        desired_account_id="5",
        active_account_id="3",
    )

    assert selected_account_id(store, existing_only=True) == 3
    assert selected_account_id(store) == 5


def test_existing_only_skips_an_unknown_desired_row():
    store = _Store(rows={3: {"id": 3}}, desired_account_id="999", active_account_id="3")

    assert selected_account_id(store, existing_only=True) == 3
    assert store.lookups == [999, 3]


def test_existing_only_with_no_existing_row_is_none():
    store = _Store(desired_account_id="5", active_account_id="3")

    assert selected_account_id(store, existing_only=True) is None


def test_no_row_is_read_unless_existence_is_asked_for():
    store = _Store(desired_account_id="5", active_account_id="3")

    assert selected_account_id(store) == 5
    assert store.lookups == []
    # A double with no get_account at all works the same way.
    assert selected_account_id(_Settings(desired_account_id="5")) == 5


def test_existing_only_skips_a_soft_deleted_row_in_a_real_database(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _insert_account(db, 3, "three@example.com")
    _insert_account(db, 5, "five@example.com")
    db.set_settings({"desired_account_id": "5", "active_account_id": "3"})
    assert db.delete_account(5)

    assert selected_account_id(db, existing_only=True) == 3
    assert selected_account_id(db) == 5
    assert db.get_selected_account_id(existing_only=True) == 3
    assert db.get_selected_account_id() == 5
    db.close()


def test_the_codex_selection_honors_existing_only(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _insert_account(db, 4, "codex@example.com", provider="codex")
    db.set_active_account_id(4, provider="codex")

    assert db.get_selected_account_id("codex", existing_only=True) == 4
    db.set_active_account_id(99, provider="codex")
    assert db.get_selected_account_id("codex") == 99
    assert db.get_selected_account_id("codex", existing_only=True) is None
    db.close()


# --- the session observer ----------------------------------------------------


def test_an_invalid_desired_value_does_not_hide_a_valid_active_id():
    """``desired or active`` took the non-empty 'abc' and gave up: the
    observer then reported no desired identity at all."""
    db = Database(":memory:")
    _insert_account(db, 3, "three@example.com")
    db.set_settings({"desired_account_id": "abc", "active_account_id": "3"})

    identity = session_observer._desired_identity(db)

    assert identity is not None
    assert identity.account_id == 3
    # The identity watcher reads through the observer, so it agrees.
    assert identity_watcher._desired_account_id(db) == 3


def test_a_deleted_desired_row_still_gives_the_observer_no_identity(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _insert_account(db, 3, "three@example.com")
    _insert_account(db, 5, "five@example.com")
    db.set_settings({"desired_account_id": "5", "active_account_id": "3"})
    db.delete_account(5)

    assert session_observer._desired_identity(db) is None
    db.close()


# --- the pin table -----------------------------------------------------------

# Every outcome after which the switch repository moves a pointer, with the
# repository step that records it: finalize writes both pointers, and
# record_outcome writes only the desired pointer.
_POINTER_OUTCOMES = [
    pytest.param("finalize", SwitchOutcome.COMMITTED, id="committed"),
    pytest.param("finalize", SwitchOutcome.COMMITTED_DEGRADED, id="committed_degraded"),
    pytest.param(
        "record_outcome",
        SwitchOutcome.OBSERVED_TARGET_UNFENCED,
        id="observed_target_unfenced",
    ),
]


def test_the_table_walks_every_successful_outcome():
    """A new successful outcome needs a row in the pin table."""
    walked = {param.values[1] for param in _POINTER_OUTCOMES}

    assert walked == {outcome for outcome in SwitchOutcome if _repair_committed(outcome)}


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jacked.db"))
    _insert_account(database, 3, "three@example.com")
    _insert_account(database, 5, "five@example.com")
    database.set_setting("active_account_id", "3")
    yield database
    database.close()


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return TestClient(app, headers={"X-Jacked-Page-Session": "page-session-123456"})


def _pending(operation_id: str, account_id: int) -> PendingSwitchRecord:
    return PendingSwitchRecord(
        operation_id=operation_id,
        account_id=account_id,
        organization_id=f"org-{account_id}",
        context=SwitchContext.MANUAL,
        capability_mode=CapabilityMode.GLOBAL_UNCOOPERATIVE,
        machine_install_id="machine-test",
        backend_locator="keychain:test",
        capability_epoch=1,
        canonicalizer_version=1,
        before_hmac="",
        target_hmac="",
    )


def _switch(db: Database, account_id: int, step: str, outcome: SwitchOutcome) -> None:
    """Record a switch to ``account_id`` the way the transaction engine does."""
    repository = DatabaseCredentialSwitchRepository(db)
    repository.create_pending(_pending("op-switch", account_id))
    observed = CredentialIdentity(account_id, f"account{account_id}@example.com")
    if step == "finalize":
        repository.finalize(
            FinalizeSwitchRecord("op-switch", account_id, outcome, observed, "switch:op-switch")
        )
    else:
        repository.record_outcome(
            OutcomeSwitchRecord(
                "op-switch",
                account_id,
                outcome,
                observed,
                "target observed",
                SwitchContext.MANUAL,
                CapabilityMode.GLOBAL_UNCOOPERATIVE,
            )
        )


def _activate(db, account, context, operation_id, **_repair):
    """A repair that the engine reports as done, with no store write."""
    account_id = int(account["id"])
    return SwitchResult(
        operation_id=operation_id,
        outcome=SwitchOutcome.OBSERVED_TARGET_UNFENCED,
        desired_default=IdentityAxis(account_id, "desired"),
        storage=IdentityAxis(account_id, "observed_target_unfenced"),
        committed_authority=IdentityAxis(account_id, "observed_target_unfenced"),
        existing_session_activation=SessionActivationState.PENDING_NEXT_ACTIVITY,
        provider_verification=ProviderVerificationState.UNVERIFIED,
        observed_identity=CredentialIdentity(account_id),
    )


def _heal_after_a_foreign_write_by_3(db) -> tuple[int | None, str]:
    """The selection the guard reports after Claude Code wrote account 3's
    tokens, and the reason the heal gives (empty for a clean reassert)."""
    result = heal_foreign_authority(
        db,
        now=100.0,
        profile_lookup=lambda _token: {
            "account": {"email": "three@example.com"},
            "organization": {"uuid": "org-3"},
        },
        activate=_activate,
        read_authority=lambda: {
            "claudeAiOauth": {
                "accessToken": "live_at_3",
                "refreshToken": "live_rt_3",
                "expiresAt": 9_999_999_999_000,
            }
        },
    )
    return result.desired_account_id, result.reason


def _read_database_accessor(db, client):
    return db.get_selected_account_id()


def _read_authority_guard(db, client):
    """A foreign write by account 3 makes the guard reassert its selection."""
    desired_id, reason = _heal_after_a_foreign_write_by_3(db)
    assert reason == "", reason
    return desired_id


def _read_stamped_rotation(db, client):
    """A stale-session rotation stamped for 5 restores 5 only when 5 is selected."""
    row, reason = stamped_rotation._stale_target(db, 5)
    return row["id"] if row else reason


def _read_session_observer(db, client):
    identity = session_observer._desired_identity(db)
    return identity.account_id if identity else None


def _read_identity_watcher(db, client):
    return identity_watcher._desired_account_id(db)


def _read_session_states_route(db, client):
    response = client.get("/api/auth/session-states")
    assert response.status_code == 200
    desired = response.json()["desired_global"]
    return desired["account_id"] if desired else None


def _read_oauth_flow(db, client):
    """Of the two rows, only the selected one stays active after a re-auth."""
    flow = OAuthFlow(db, purpose="primary")
    keeps = [i for i in (3, 5) if flow._should_become_active({"id": i})]
    return keeps[0] if len(keeps) == 1 else keeps


def _read_later_switch_audit(db, client):
    """The next switch records the selected account as the one it leaves."""
    DatabaseCredentialSwitchRepository(db).create_pending(_pending("op-later", 3))
    return db.get_credential_switch("op-later")["previous_account_id"]


_READERS = [
    pytest.param(_read_database_accessor, id="database_accessor"),
    pytest.param(_read_authority_guard, id="authority_guard"),
    pytest.param(_read_stamped_rotation, id="stamped_rotation"),
    pytest.param(_read_session_observer, id="session_observer"),
    pytest.param(_read_identity_watcher, id="identity_watcher"),
    pytest.param(_read_session_states_route, id="session_states_route"),
    pytest.param(_read_oauth_flow, id="oauth_flow"),
    pytest.param(_read_later_switch_audit, id="later_switch_audit"),
]


@pytest.mark.parametrize("step, outcome", _POINTER_OUTCOMES)
@pytest.mark.parametrize("reader", _READERS)
def test_every_reader_resolves_the_switch_target(db, client, step, outcome, reader):
    _switch(db, 5, step, outcome)
    assert db.get_setting("desired_account_id") == "5"
    stale_active = "3" if outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED else "5"
    assert db.get_setting("active_account_id") == stale_active

    assert reader(db, client) == 5


# A selection whose row was deleted (desired 5 deleted, active 3). Readers
# that need a row (existing_only) use the active id. The guard and the
# observer keep the selection: the guard refuses it and re-stamps the holder,
# and the observer reports no desired identity.
_DELETED_SELECTION = [
    pytest.param(
        lambda db, client: _heal_after_a_foreign_write_by_3(db),
        (5, "desired account row is missing"),
        id="authority_guard",
    ),
    pytest.param(
        lambda db, client: stamped_rotation._stale_target(db, 5),
        (None, "desired account row is missing"),
        id="stamped_rotation",
    ),
    pytest.param(_read_session_observer, None, id="session_observer"),
    pytest.param(_read_identity_watcher, None, id="identity_watcher"),
    pytest.param(_read_database_accessor, 5, id="database_accessor"),
    pytest.param(_read_session_states_route, 3, id="session_states_route"),
    pytest.param(_read_oauth_flow, 3, id="oauth_flow"),
    pytest.param(_read_later_switch_audit, 3, id="later_switch_audit"),
]


@pytest.mark.parametrize("reader, expected", _DELETED_SELECTION)
def test_a_deleted_selection_keeps_each_reader_on_its_rule(db, client, reader, expected):
    db.set_setting("desired_account_id", "5")
    assert db.delete_account(5)

    assert reader(db, client) == expected


# --- one copy of the rule ----------------------------------------------------

_POINTER_PAIR = re.compile(
    r"""["'](?P<first>desired|active)_account_id["']\s*,\s*"""
    r"""["'](?P<second>desired|active)_account_id["']"""
)


def test_the_pointer_pair_is_written_out_only_in_the_database_module():
    """Any other module imports SELECTED_ACCOUNT_KEYS or selected_account_id."""
    package = Path(jacked.__file__).parent
    found = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in _POINTER_PAIR.finditer(text):
            if match["first"] != match["second"]:
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(package.parent).as_posix()}:{line}")

    assert [entry.rsplit(":", 1)[0] for entry in found] == ["jacked/web/database.py"], found

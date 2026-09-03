"""The session hook accepts only canonical resolver identity evidence."""

import jacked.data.hooks.session_account_tracker as sat
from jacked.web.database import Database


def test_resolver_identity_excludes_same_email_codex(tmp_path, monkeypatch):
    db_path = tmp_path / "jacked.db"
    db = Database(str(db_path))
    # Codex created FIRST (lower id) — without the provider filter it would win
    # the `ORDER BY priority, id` tiebreak.
    db.create_account(
        "dual@x.com",
        "codex-managed",
        4102444800,
        provider="codex",
        organization_uuid="acct-CX",
    )
    claude = db.create_account(
        "dual@x.com",
        "claude-tok",
        9999999999,
        provider="claude",
        organization_uuid="org-claude",
    )
    db.close()

    monkeypatch.setattr(sat, "DB_PATH", db_path)
    snapshot = {
        "state": "resolved",
        "observed": {
            "account_id": claude["id"],
            "email": "dual@x.com",
            "organization_id": "org-claude",
        },
    }
    acct_id, email = sat._match_token_to_account(None, cred_data=snapshot)
    assert email == "dual@x.com"
    assert acct_id == claude["id"]  # the Claude account, NOT the codex one


def test_resolver_identity_requires_email_and_organization(tmp_path, monkeypatch):
    """Sanity: a normal Claude-only account still resolves."""
    db_path = tmp_path / "jacked.db"
    db = Database(str(db_path))
    claude = db.create_account(
        "solo@x.com",
        "claude-tok",
        9999999999,
        provider="claude",
        organization_uuid="org-solo",
    )
    db.close()

    monkeypatch.setattr(sat, "DB_PATH", db_path)
    snapshot = {
        "state": "resolved",
        "observed": {
            "account_id": claude["id"],
            "email": "solo@x.com",
            "organization_id": "org-solo",
        },
    }
    acct_id, _ = sat._match_token_to_account(None, cred_data=snapshot)
    assert acct_id == claude["id"]

    snapshot["observed"]["organization_id"] = ""
    assert sat._match_token_to_account(None, cred_data=snapshot) == (None, None)

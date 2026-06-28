"""The Claude Code session hook must resolve a CLAUDE account by email, never a
same-email Codex account.

Regression: _match_token_to_account's Layer-1 email lookup had no provider
predicate, so a Codex account sharing the user's email (and a lower id) could be
stamped as the active Claude account.
"""

import json

import jacked.data.hooks.session_account_tracker as sat
from jacked.web.database import Database


def test_layer1_email_match_excludes_codex(tmp_path, monkeypatch):
    db_path = tmp_path / "jacked.db"
    db = Database(str(db_path))
    # Codex created FIRST (lower id) — without the provider filter it would win
    # the `ORDER BY priority, id` tiebreak.
    db.create_account("dual@x.com", "codex-managed", 4102444800,
                      provider="codex", organization_uuid="acct-CX")
    claude = db.create_account("dual@x.com", "claude-tok", 9999999999, provider="claude")
    db.close()

    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"oauthAccount": {"emailAddress": "dual@x.com"}}))
    monkeypatch.setattr(sat, "DB_PATH", db_path)
    monkeypatch.setattr(sat, "CLAUDE_CONFIG", claude_json)
    # Point CRED_PATH at a non-existent file so Layers 2/3 don't run.
    monkeypatch.setattr(sat, "CRED_PATH", tmp_path / "no-creds.json")

    acct_id, email = sat._match_token_to_account(None, cred_data=None)
    assert email == "dual@x.com"
    assert acct_id == claude["id"]  # the Claude account, NOT the codex one


def test_layer1_resolves_claude_when_no_codex(tmp_path, monkeypatch):
    """Sanity: a normal Claude-only account still resolves."""
    db_path = tmp_path / "jacked.db"
    db = Database(str(db_path))
    claude = db.create_account("solo@x.com", "claude-tok", 9999999999, provider="claude")
    db.close()

    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"oauthAccount": {"emailAddress": "solo@x.com"}}))
    monkeypatch.setattr(sat, "DB_PATH", db_path)
    monkeypatch.setattr(sat, "CLAUDE_CONFIG", claude_json)
    monkeypatch.setattr(sat, "CRED_PATH", tmp_path / "no-creds.json")

    acct_id, _ = sat._match_token_to_account(None, cred_data=None)
    assert acct_id == claude["id"]

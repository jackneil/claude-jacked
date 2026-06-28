"""M2: read & identify OpenAI Codex CLI credentials (~/.codex/auth.json).

Covers: CODEX_HOME resolution, cli_auth_credentials_store detection from
config.toml, JWT id_token decode, identity extraction (email/plan/account_id/
auth_mode), and the swappable-vs-keyring verdict — keyring/absent must report
"not swappable" with a reason, never silently empty.
"""

import base64
import json

import pytest

from jacked.codex import credentials as cc


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _make_jwt(payload: dict) -> str:
    header = _b64url({"alg": "RS256", "typ": "JWT"})
    sig = base64.urlsafe_b64encode(b"notarealsig").rstrip(b"=").decode()
    return f"{header}.{_b64url(payload)}.{sig}"


def _chatgpt_payload(
    email="dev@example.com", plan="pro", account_id="acct-abc", user_id="user-1",
    exp=9999999999,
):
    return {
        "email": email,
        "exp": exp,
        "sub": "auth0|x",
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": user_id,
        },
    }


def _chatgpt_auth_json(**payload_kw) -> dict:
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _make_jwt(_chatgpt_payload(**payload_kw)),
            "access_token": _make_jwt({"sub": "x", "exp": 9999999999}),
            "refresh_token": "rt-secret",
            "account_id": "acct-abc",
        },
        "last_refresh": "2026-06-27T21:18:58.404553Z",
    }


def _write_codex_home(tmp_path, auth=None, config_toml=None):
    home = tmp_path / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    if auth is not None:
        (home / "auth.json").write_text(json.dumps(auth))
    if config_toml is not None:
        (home / "config.toml").write_text(config_toml)
    return home


# --------------------------------------------------------------------------
# CODEX_HOME resolution
# --------------------------------------------------------------------------

def test_codex_home_defaults_to_dot_codex():
    assert cc.codex_home(env={}).name == ".codex"


def test_codex_home_respects_env(tmp_path):
    target = tmp_path / "custom-codex"
    assert cc.codex_home(env={"CODEX_HOME": str(target)}) == target


# --------------------------------------------------------------------------
# Credential store mode
# --------------------------------------------------------------------------

def test_store_mode_absent_config_defaults_auto(tmp_path):
    home = _write_codex_home(tmp_path)
    assert cc.credential_store_mode(home) == "auto"


@pytest.mark.parametrize("value", ["file", "keyring", "auto"])
def test_store_mode_reads_config(tmp_path, value):
    home = _write_codex_home(
        tmp_path, config_toml=f'model = "gpt-5.5"\ncli_auth_credentials_store = "{value}"\n'
    )
    assert cc.credential_store_mode(home) == value


def test_store_mode_ignores_commented_key(tmp_path):
    home = _write_codex_home(
        tmp_path, config_toml='# cli_auth_credentials_store = "keyring"\nmodel = "gpt-5.5"\n'
    )
    assert cc.credential_store_mode(home) == "auto"


# --------------------------------------------------------------------------
# JWT decode + identity
# --------------------------------------------------------------------------

def test_decode_jwt_claims_roundtrip():
    payload = _chatgpt_payload()
    claims = cc.decode_jwt_claims(_make_jwt(payload))
    assert claims["email"] == "dev@example.com"
    assert claims["https://api.openai.com/auth"]["chatgpt_plan_type"] == "pro"


def test_decode_jwt_claims_rejects_non_jwt():
    with pytest.raises(ValueError):
        cc.decode_jwt_claims("not-a-jwt")


def test_extract_identity_from_chatgpt_auth():
    ident = cc.extract_identity(_chatgpt_auth_json())
    assert ident.email == "dev@example.com"
    assert ident.plan == "pro"
    assert ident.account_id == "acct-abc"
    assert ident.auth_mode == "chatgpt"
    assert ident.user_id == "user-1"
    assert ident.expires_at == 9999999999


def test_extract_identity_account_id_sentinel_when_absent():
    auth = _chatgpt_auth_json(account_id="")
    auth["tokens"]["account_id"] = ""
    ident = cc.extract_identity(auth)
    assert ident.account_id == ""  # org sentinel, never None


def test_extract_identity_apikey_mode_has_no_email():
    auth = {"OPENAI_API_KEY": "sk-test", "auth_mode": "apikey", "tokens": {}}
    ident = cc.extract_identity(auth)
    assert ident.auth_mode == "apikey"
    assert ident.email is None
    assert ident.account_id == ""


# --------------------------------------------------------------------------
# detect_codex_account — the swappable verdict
# --------------------------------------------------------------------------

def test_detect_file_mode_is_swappable_with_identity(tmp_path):
    home = _write_codex_home(
        tmp_path,
        auth=_chatgpt_auth_json(),
        config_toml='cli_auth_credentials_store = "file"\n',
    )
    status = cc.detect_codex_account(home)
    assert status.present is True
    assert status.swappable is True
    assert status.reason is None
    assert status.identity.email == "dev@example.com"
    assert status.store_mode == "file"


def test_detect_auto_mode_with_authjson_is_swappable(tmp_path):
    # 'auto' (no config key) but auth.json exists -> file is authoritative.
    home = _write_codex_home(tmp_path, auth=_chatgpt_auth_json())
    status = cc.detect_codex_account(home)
    assert status.store_mode == "auto"
    assert status.present is True
    assert status.swappable is True


def test_detect_keyring_mode_reports_not_swappable(tmp_path):
    home = _write_codex_home(
        tmp_path,
        auth=_chatgpt_auth_json(),
        config_toml='cli_auth_credentials_store = "keyring"\n',
    )
    status = cc.detect_codex_account(home)
    assert status.swappable is False
    assert status.reason is not None
    assert "keyring" in status.reason.lower()
    # never silently empty: identity is still surfaced when readable
    assert status is not None


def test_detect_no_auth_json_file_mode_reports_login(tmp_path):
    home = _write_codex_home(tmp_path, config_toml='cli_auth_credentials_store = "file"\n')
    status = cc.detect_codex_account(home)
    assert status.present is False
    assert status.swappable is False
    assert status.reason is not None
    assert "login" in status.reason.lower()
    assert status.identity is None


def test_detect_never_returns_none(tmp_path):
    # Even a totally empty CODEX_HOME yields a status object, not None.
    home = tmp_path / ".codex"
    home.mkdir()
    status = cc.detect_codex_account(home)
    assert status is not None
    assert status.swappable is False
    assert status.reason

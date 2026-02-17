"""Unit tests for the session_account_tracker hook.

Tests keychain fallback in _get_cred_data(), removal of the short-circuit
in _handle_event(), and _match_token_to_account accepting None token.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

# The hook is a standalone script — import its functions directly
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent / "jacked" / "data" / "hooks"),
)
import session_account_tracker as sat  # noqa: E402


# ------------------------------------------------------------------
# _get_cred_data: file + keychain fallback
# ------------------------------------------------------------------


def test_get_cred_data_file_exists():
    """Reads from credential file when it exists.

    >>> test_get_cred_data_file_exists()
    """
    with tempfile.TemporaryDirectory() as tmp:
        cred_path = Path(tmp) / ".credentials.json"
        data = {
            "claudeAiOauth": {"accessToken": "file_token"},
            "_jackedAccountId": 1,
        }
        cred_path.write_text(json.dumps(data), encoding="utf-8")

        with mock.patch.object(sat, "CRED_PATH", cred_path):
            token, result = sat._get_cred_data()

        assert token == "file_token"
        assert result["_jackedAccountId"] == 1


def test_get_cred_data_file_missing_keychain_fallback():
    """Falls back to macOS Keychain when file doesn't exist.

    >>> test_get_cred_data_file_missing_keychain_fallback()
    """
    keychain_json = json.dumps({
        "claudeAiOauth": {"accessToken": "keychain_token"},
    })
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = keychain_json

    fake_path = Path("/nonexistent/.credentials.json")
    with (
        mock.patch.object(sat, "CRED_PATH", fake_path),
        mock.patch.object(sat, "sys") as mock_sys,
        mock.patch("subprocess.run", return_value=mock_result),
    ):
        mock_sys.platform = "darwin"
        mock_sys.stderr = sys.stderr
        token, data = sat._get_cred_data()

    assert token == "keychain_token"
    assert data is not None


def test_get_cred_data_file_missing_linux():
    """Returns (None, None) on Linux when file is missing (no keychain).

    >>> test_get_cred_data_file_missing_linux()
    """
    fake_path = Path("/nonexistent/.credentials.json")
    with (
        mock.patch.object(sat, "CRED_PATH", fake_path),
        mock.patch.object(sat, "sys") as mock_sys,
    ):
        mock_sys.platform = "linux"
        token, data = sat._get_cred_data()

    assert token is None
    assert data is None


def test_get_cred_data_keychain_locked():
    """Returns (None, None) when keychain is locked (non-zero exit).

    >>> test_get_cred_data_keychain_locked()
    """
    mock_result = mock.MagicMock()
    mock_result.returncode = 36  # user denied access
    mock_result.stdout = ""
    mock_result.stderr = "User denied access"

    fake_path = Path("/nonexistent/.credentials.json")
    with (
        mock.patch.object(sat, "CRED_PATH", fake_path),
        mock.patch.object(sat, "sys") as mock_sys,
        mock.patch("subprocess.run", return_value=mock_result),
    ):
        mock_sys.platform = "darwin"
        mock_sys.stderr = sys.stderr
        token, data = sat._get_cred_data()

    assert token is None
    assert data is None


def test_get_cred_data_keychain_malformed():
    """Returns (None, None) when keychain returns invalid JSON.

    >>> test_get_cred_data_keychain_malformed()
    """
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "not-json{{"

    fake_path = Path("/nonexistent/.credentials.json")
    with (
        mock.patch.object(sat, "CRED_PATH", fake_path),
        mock.patch.object(sat, "sys") as mock_sys,
        mock.patch("subprocess.run", return_value=mock_result),
    ):
        mock_sys.platform = "darwin"
        mock_sys.stderr = sys.stderr
        token, data = sat._get_cred_data()

    assert token is None
    assert data is None


# ------------------------------------------------------------------
# _match_token_to_account: accepts None token
# ------------------------------------------------------------------


def test_match_token_accepts_none():
    """_match_token_to_account(None, None) doesn't crash.

    >>> test_match_token_accepts_none()
    """
    # Uses the real DB path — but if DB doesn't exist, returns (None, None)
    with mock.patch.object(sat, "DB_PATH", Path("/nonexistent/jacked.db")):
        account_id, email = sat._match_token_to_account(None, None)

    assert account_id is None
    assert email is None


# ------------------------------------------------------------------
# _handle_event: no short-circuit, always calls _match_token_to_account
# ------------------------------------------------------------------


def test_handle_event_always_calls_match():
    """_handle_event always calls _match_token_to_account, even with no token.

    >>> test_handle_event_always_calls_match()
    """
    with (
        mock.patch.object(sat, "_get_cred_data", return_value=(None, None)),
        mock.patch.object(
            sat, "_match_token_to_account", return_value=(None, None)
        ) as mock_match,
        mock.patch.object(sat, "_record_session", return_value="2025-01-01T00:00:00Z"),
        mock.patch.object(sat, "_tag_subagent"),
    ):
        sat._handle_event("SessionStart", "test-sess", "/repo")

    mock_match.assert_called_once_with(None, None)


def test_handle_event_no_token_uses_layer3():
    """Even without a token, _match_token_to_account is called for Layer 3.

    >>> test_handle_event_no_token_uses_layer3()
    """
    with (
        mock.patch.object(sat, "_get_cred_data", return_value=(None, None)),
        mock.patch.object(
            sat, "_match_token_to_account", return_value=(42, "user@test.com")
        ) as mock_match,
        mock.patch.object(sat, "_record_session", return_value="ts") as mock_record,
        mock.patch.object(sat, "_tag_subagent"),
        mock.patch.object(sat, "_clear_account_error") as mock_clear,
    ):
        sat._handle_event("SessionStart", "test-sess", "/repo")

    mock_match.assert_called_once_with(None, None)
    mock_record.assert_called_once_with("test-sess", 42, "user@test.com", "session_start", "/repo")
    mock_clear.assert_called_once_with(42)


def test_handle_event_notification_closes_old_session():
    """Notification event closes old session then records new one.

    >>> test_handle_event_notification_closes_old_session()
    """
    with (
        mock.patch.object(sat, "_get_cred_data", return_value=("tok", {"claudeAiOauth": {}})),
        mock.patch.object(
            sat, "_match_token_to_account", return_value=(1, "a@test.com")
        ),
        mock.patch.object(sat, "_end_session") as mock_end,
        mock.patch.object(sat, "_record_session") as mock_record,
        mock.patch.object(sat, "_clear_account_error"),
    ):
        sat._handle_event("Notification", "test-sess", "/repo")

    mock_end.assert_called_once_with("test-sess")
    mock_record.assert_called_once_with("test-sess", 1, "a@test.com", "auth_success", "/repo")


# ------------------------------------------------------------------
# UserPromptSubmit: heartbeat only, no credential reads
# ------------------------------------------------------------------


def test_user_prompt_submit_triggers_heartbeat():
    """UserPromptSubmit calls _heartbeat_session, not _record_session or _get_cred_data.

    >>> test_user_prompt_submit_triggers_heartbeat()
    """
    with (
        mock.patch.object(sat, "_heartbeat_session") as mock_hb,
        mock.patch.object(sat, "_get_cred_data") as mock_cred,
        mock.patch.object(sat, "_record_session") as mock_record,
    ):
        sat._handle_event("UserPromptSubmit", "test-sess", "/repo")

    mock_hb.assert_called_once_with("test-sess")
    mock_cred.assert_not_called()
    mock_record.assert_not_called()


def test_user_prompt_submit_passes_main_event_filter():
    """main() allows UserPromptSubmit through its event allowlist.

    >>> test_user_prompt_submit_passes_main_event_filter()
    """
    input_data = json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sess-ups-001",
        "cwd": "/test/project",
    })
    with (
        mock.patch.object(sat, "sys") as mock_sys,
        mock.patch.object(sat, "_handle_event") as mock_handle,
        mock.patch("threading.Thread") as mock_thread,
    ):
        mock_sys.stdin.read.return_value = input_data
        mock_thread_instance = mock.MagicMock()
        mock_thread.return_value = mock_thread_instance

        sat.main()

    mock_thread.assert_called_once()
    call_args = mock_thread.call_args
    assert call_args[1]["args"] == ("UserPromptSubmit", "sess-ups-001", "/test/project")
    mock_thread_instance.start.assert_called_once()

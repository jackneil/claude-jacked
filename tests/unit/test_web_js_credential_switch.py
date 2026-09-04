from pathlib import Path


WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"


def test_use_account_sends_distinct_idempotency_headers() -> None:
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    assert "const actionId = crypto.randomUUID()" in source
    assert "const operationId = crypto.randomUUID()" in source
    assert "'X-Jacked-Action-Id': actionId" in source
    assert "'X-Jacked-Operation-Id': operationId" in source
    assert "sessionStorage.getItem('jacked-page-session-id')" in source
    assert "'X-Jacked-Page-Session': pageSessionId" in source
    action_index = source.index("const actionId = crypto.randomUUID()")
    assert action_index < source.index("try {", action_index)
    assert "/api/auth/credential-operations/${actionId}" in source


def test_use_account_ui_handles_truthful_outcomes_without_blanket_switched_claim() -> None:
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    for outcome in (
        "committed",
        "committed_degraded",
        "observed_target_unfenced",
        "interactive_required",
        "unsupported",
    ):
        assert outcome in source
    assert "showToast(`Switched to ${email}`" not in source
    assert "switches all Claude Code sessions" not in source


def test_api_client_preserves_structured_error_outcome_and_custom_headers() -> None:
    source = (WEB_JS / "app.js").read_text()

    assert "...headers" in source
    assert "apiError.payload = err" in source
    assert "/api/auth/credential-operations/${actionId}" in (
        WEB_JS / "components" / "account-actions.js"
    ).read_text()


def test_lost_response_polls_until_terminal_result_with_same_action_binding() -> None:
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    poller = source.split("async function pollCredentialOperation", 1)[1].split(
        "async function activateAccountFromDashboard", 1
    )[0]

    assert "while (Date.now() < deadline)" in poller
    assert "/api/auth/credential-operations/${actionId}" in poller
    assert "'X-Jacked-Page-Session': pageSessionId" in poller
    assert "operation.state === 'complete'" in poller
    assert "operation.state === 'expired'" in poller
    assert "statusError.status === 404" in poller
    assert "setTimeout(resolve, 1500)" in poller
    assert "showCredentialActivationResult(operation.result, email)" in poller

    timeout_branch = source.split("if (e.code === 'TIMEOUT')", 1)[1].split(
        "} else if", 1
    )[0]
    assert "await pollCredentialOperation(" in timeout_branch
    assert "actionId, operationId, pageSessionId, email" in timeout_branch


def test_auto_swap_recommendation_is_visible_and_uses_safe_activation_path() -> None:
    actions = (WEB_JS / "components" / "account-actions.js").read_text()
    websocket = (WEB_JS / "websocket.js").read_text()

    assert "jackedWS.on('auto_swap_recommended'" in websocket
    assert "showAutoSwapRecommendation(data)" in websocket
    assert "function showAutoSwapRecommendation(data)" in actions
    assert "Account switch recommended:" in actions
    assert "Use Account" in actions
    assert "await activateAccountFromDashboard(" in actions
    assert "banner.setAttribute('role', 'region')" in actions
    assert "banner.setAttribute('aria-label', 'Account switch recommendation')" in actions
    assert "text.setAttribute('aria-live', 'polite')" in actions
    assert "focus-visible:outline" in actions
    assert "close.setAttribute('aria-label', 'Dismiss account switch recommendation')" in actions
    assert "setTimeout(function() { if (banner.parentNode)" not in actions.split(
        "function showAutoSwapRecommendation(data)", 1
    )[1].split("function bindAccountEvents", 1)[0]


def test_use_account_ui_tells_the_user_whether_open_sessions_follow() -> None:
    """The headline is the switch; the second sentence is what open sessions do.

    Regression 2026-09-04: the engine wrote the Keychain but not the identity
    Claude Code watches, so every open session kept the old account while the
    toast said the switch was observed. The UI must key the session sentence
    on the API's ``existing_sessions`` field, never on the raw engine message.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    assert "function sessionsFollowCopy(" in source
    assert "result.existing_sessions === 'pending_next_activity'" in source
    assert "pick it up on their next message" in source
    assert "Restart them to use this account" in source
    # The engine's diagnostic text is not the user's headline.
    assert "concurrent writers cannot be excluded" not in source
    body = source.split("function showCredentialActivationResult")[1].split(
        "async function pollCredentialOperation"
    )[0]
    for outcome in ("committed", "committed_degraded", "observed_target_unfenced"):
        branch = body.split(f"result.status === '{outcome}'")[1].split("} else")[0]
        assert "sessionsFollowCopy(" in branch
        assert "result.message ||" not in branch


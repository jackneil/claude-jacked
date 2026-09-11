from pathlib import Path


WEB_JS = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web" / "js"


def test_use_account_sends_distinct_idempotency_headers() -> None:
    """Ids come from ``generateUuid`` and are minted inside the ``try``.

    Regression 2026-09-11: ``crypto.randomUUID`` is secure-context only, so
    on ``http://<hostname>:8321`` (remote access on) the three id calls threw
    a ``TypeError`` *before* the ``try``, leaving the button stuck on
    "Switching..." and ``_accountActionInFlight`` latched true forever.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()

    assert "const actionId = generateUuid()" in source
    assert "const operationId = generateUuid()" in source
    assert "pageSessionId = generateUuid()" in source
    assert "crypto.randomUUID()" not in source
    assert "'X-Jacked-Action-Id': actionId" in source
    assert "'X-Jacked-Operation-Id': operationId" in source
    assert "sessionStorage.getItem('jacked-page-session-id')" in source
    assert "'X-Jacked-Page-Session': pageSessionId" in source
    assert "/api/auth/credential-operations/${actionId}" in source

    activate = source.index("async function activateAccountFromDashboard")
    flag_index = source.index("_accountActionInFlight = true", activate)
    try_index = source.index("try {", flag_index)
    action_index = source.index("const actionId = generateUuid()", activate)
    post_index = source.index("`/api/auth/accounts/${id}/use`", activate)
    assert flag_index < try_index < action_index < post_index


def test_use_account_restores_the_button_and_clears_the_in_flight_flag() -> None:
    """A failure must not strand the button or the in-flight latch."""
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    body = source.split("async function activateAccountFromDashboard", 1)[1].split(
        "function showAutoSwapRecommendation", 1
    )[0]

    assert "const originalButtonText = sourceButton" in body
    finally_block = body.split("} finally {", 1)[1]
    assert "sourceButton.disabled = false" in finally_block
    assert "sourceButton.textContent = originalButtonText" in finally_block
    restore_index = finally_block.index("sourceButton.disabled = false")
    clear_index = finally_block.index("_accountActionInFlight = false")
    assert restore_index < clear_index
    assert clear_index < finally_block.index("refreshAndRender()")

    # A synchronous throw (no ApiError shape) still reaches the user.
    catch_block = body.split("} catch (e) {", 1)[1].split("} finally {", 1)[0]
    assert "showToast(e.message || String(e), 'error')" in catch_block


def test_copy_command_fallback_is_reachable_without_navigator_clipboard() -> None:
    """``navigator.clipboard`` is undefined in a non-secure context.

    The property access must therefore happen *inside* the ``try`` so the
    resulting TypeError lands in the ``execCommand`` fallback.
    """
    source = (WEB_JS / "components" / "account-actions.js").read_text()
    handler = source.split(".btn-copy-cmd", 1)[1].split("btn-dismiss-tip", 1)[0]

    try_index = handler.index("try {")
    assert try_index < handler.index("navigator.clipboard.writeText(cmd)")
    catch_block = handler.split("} catch {", 1)[1]
    assert "document.execCommand('copy')" in catch_block


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


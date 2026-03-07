/**
 * Token pill rendering — status indicators for primary (usage) and CC tokens.
 * Extracted from accounts.js for file-size compliance.
 */

const PILL_SEP = '\u00B7';
const PRIMARY_LABEL = 'Usage Token';
const CC_LABEL = 'CC Token';

/**
 * Render a single token pill (primary or CC).
 * Status pills are <span>, actionable pills are <button> with dashed border + chevron.
 */
function _renderSinglePill(cfg) {
    const { label, isPrimary, expiresInSecs, hasToken, needsAuth,
            validationStatus, isExpired, accountId, email, hasRefreshToken } = cfg;

    let dotClass, statusText, isActionable = false, cssModifier = '', action = '', ariaLabel = '';

    if (!isPrimary && hasToken === false) {
        dotClass = 'ring';
        statusText = 'add';
        isActionable = true;
        cssModifier = 'pill-missing';
        action = 'auth-cc';
        ariaLabel = `Add CC token for ${escapeHtml(email)}`;
    } else if (!isPrimary && needsAuth) {
        dotClass = 'filled orange';
        statusText = 're-auth';
        isActionable = true;
        cssModifier = 'pill-warn';
        action = 'auth-cc';
        ariaLabel = `Re-auth CC token for ${escapeHtml(email)}`;
    } else if (isPrimary && validationStatus === 'checking') {
        dotClass = 'pulse';
        statusText = 'refreshing\u2026';
    } else if (isPrimary && validationStatus === 'invalid') {
        dotClass = 'triangle';
        statusText = 're-auth';
        isActionable = true;
        cssModifier = 'pill-error';
        action = 'reauth-primary';
        ariaLabel = `Re-auth ${label} for ${escapeHtml(email)}`;
    } else if (hasRefreshToken) {
        // Auto-refreshable token — show green even if locally "expired";
        // the background refresh loop handles renewal, re-auth is not needed.
        dotClass = 'filled green';
        statusText = 'valid';
    } else if (isExpired || (expiresInSecs != null && expiresInSecs <= 0)) {
        // Only non-refreshable tokens (API keys) need manual re-auth when expired
        dotClass = 'triangle';
        statusText = 're-auth';
        isActionable = true;
        cssModifier = 'pill-error';
        action = isPrimary ? 'reauth-primary' : 'auth-cc';
        ariaLabel = `Re-auth ${label} for ${escapeHtml(email)}`;
    } else {
        // Non-refreshable (e.g. API key) — show time remaining
        const timeStr = formatTimeRemaining(expiresInSecs);
        if (!timeStr) {
            return `<span class="token-pill"><span class="pill-dot filled green"></span><span class="pill-label">${label}</span></span>`;
        }
        dotClass = expiresInSecs < TOKEN_EXPIRY_WARN_SECS ? 'filled yellow' : 'filled green';
        statusText = timeStr;
    }

    const statusHtml = statusText
        ? `<span class="pill-status">${PILL_SEP} ${statusText}</span>`
        : '';
    const chevronHtml = isActionable ? '<span class="pill-chevron">\u203A</span>' : '';

    if (isActionable) {
        const cls = `token-pill actionable ${cssModifier}`;
        return `<button type="button" class="${cls}" data-action="${action}" data-account-id="${accountId}" data-email="${escapeHtml(email)}" aria-label="${ariaLabel}"><span class="pill-dot ${dotClass}"></span><span class="pill-label">${label}</span>${statusHtml}${chevronHtml}</button>`;
    }
    return `<span class="token-pill"><span class="pill-dot ${dotClass}"></span><span class="pill-label">${label}</span>${statusHtml}</span>`;
}

/**
 * Render both token pills (primary + CC) for an account card.
 */
function renderTokenPills(acct) {
    const primaryPill = _renderSinglePill({
        label: PRIMARY_LABEL,
        isPrimary: true,
        expiresInSecs: acct.expires_in_seconds,
        hasToken: true,
        needsAuth: false,
        validationStatus: acct.validation_status,
        isExpired: acct.is_expired,
        accountId: acct.id,
        email: acct.email || '',
        hasRefreshToken: !!acct.has_refresh_token,
    });

    if (acct.has_cc_token === undefined) {
        return `<span class="token-pills-container">${primaryPill}</span>`;
    }

    let ccExpiresInSecs = null;
    let ccIsExpired = false;
    if (acct.has_cc_token && acct.cc_expires_at) {
        ccExpiresInSecs = acct.cc_expires_at - Math.floor(Date.now() / 1000);
        ccIsExpired = ccExpiresInSecs <= 0;
    }

    const ccPill = _renderSinglePill({
        label: CC_LABEL,
        isPrimary: false,
        expiresInSecs: ccExpiresInSecs,
        hasToken: acct.has_cc_token,
        needsAuth: acct.cc_needs_auth,
        validationStatus: null,
        isExpired: ccIsExpired,
        accountId: acct.id,
        email: acct.email || '',
        hasRefreshToken: !!acct.has_cc_refresh_token,
    });

    return `<span class="token-pills-container">${primaryPill}${ccPill}</span>`;
}

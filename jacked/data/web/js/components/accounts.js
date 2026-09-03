/**
 * jacked web dashboard — accounts component (rendering)
 * Account list, cards, usage bars, expandable details.
 * Action handlers live in account-actions.js.
 */

// Model display name mapping
const MODEL_DISPLAY_NAMES = {
    sonnet: 'Sonnet',
    opus: 'Opus',
    oauth_apps: 'OAuth Apps',
    cowork: 'Cowork',
};

/**
 * Determine visual status for an account (worst-of-two: primary + CC).
 * Uses server-computed fields: acct.is_expired, acct.expires_in_seconds.
 * Priority: disabled > expired/invalid > warning > cc-missing > checking > valid > unknown
 */
function getAccountStatus(acct) {
    if (!acct.is_active) return 'disabled';
    // Only flag expired for non-refreshable accounts (API keys); OAuth tokens
    // renew automatically so the card should not show an expired state.
    if (acct.is_expired && !acct.has_refresh_token) return 'expired';
    if (acct.validation_status === 'invalid') return 'invalid';

    // Near-expiry warning (primary < 1h) — only for non-refreshable (API key) accounts.
    // OAuth tokens renew automatically; showing a warning would contradict the pill's "valid".
    if (!acct.has_refresh_token && acct.expires_in_seconds != null && acct.expires_in_seconds < TOKEN_EXPIRY_WARN_SECS) {
        return 'warning';
    }

    // CC worst-of-two (only if CC fields present — backward compat)
    if (acct.has_cc_token !== undefined) {
        if (!acct.has_cc_token) return 'cc-missing';
        if (acct.cc_needs_auth) return 'warning';
        // CC expiry check — only flag for non-refreshable CC tokens
        if (acct.cc_expires_at && !acct.has_cc_refresh_token) {
            const ccRemaining = acct.cc_expires_at - Math.floor(Date.now() / 1000);
            if (ccRemaining <= 0) return 'expired';
            if (ccRemaining < TOKEN_EXPIRY_WARN_SECS) return 'warning';
        }
    }

    if (acct.validation_status === 'checking') return 'checking';
    if (acct.validation_status === 'valid') return 'valid';
    return 'unknown';
}

/**
 * Get subscription display text.
 */
function getSubDisplay(acct) {
    const sub = acct.subscription_type || 'unknown';
    const tier = acct.rate_limit_tier || '';

    // Extract multiplier from tier string like "default_claude_max_20x"
    const tierMatch = tier.match(/(\d+)x/);
    const tierSuffix = tierMatch ? ` (${tierMatch[1]}x tier)` : '';

    const subLabel = sub.charAt(0).toUpperCase() + sub.slice(1);
    return `${subLabel} subscription${tierSuffix}`;
}

/**
 * Get priority badge HTML.
 */
function getPriorityBadge(priority) {
    if (priority === 0) {
        return '<span class="badge badge-primary">Primary</span>';
    }
    return `<span class="badge badge-muted">#${priority + 1}</span>`;
}

/**
 * Render cache age display.
 * NOTE: data-cache-age is a DOM contract — queried by _usageUpdateCardDOM (websocket.js)
 * for surgical updates during usage refresh. Do not rename without updating consumers.
 */
function renderCacheAge(usageCachedAt, acctId) {
    if (usageCachedAt === null || usageCachedAt === undefined) {
        return '<span class="text-xs text-slate-500" data-cache-age>Usage: never fetched</span>';
    }
    const ago = timeAgoFromUnix(usageCachedAt);
    let checkHtml = '';
    const ss = window.jackedState.swapSettings || {};
    if (ss.auto_swap_enabled && acctId === window.jackedState.activeCredentialAccountId) {
        var acctArr = window.jackedState.accounts || [];
        var acctObj = acctArr.find(function(a) { return a.id === acctId; });
        var ageS = Math.floor(Date.now() / 1000) - usageCachedAt;
        var u5 = acctObj ? (acctObj.cached_usage_5h || 0) : 0;
        // Prefer the REAL interval/tier the backend published over the
        // active-poll WS broadcast; fall back to the tier table (mirrors
        // auth._TIER_INTERVALS) only before the first broadcast arrives.
        var pollInterval;
        if (acctObj && acctObj._poll_interval) pollInterval = acctObj._poll_interval;
        else if (u5 > 85) pollInterval = 180;
        else if (u5 > 70) pollInterval = 240;
        else if (u5 > 50) pollInterval = 300;
        else pollInterval = 600;
        var rem = Math.max(0, pollInterval - ageS);
        var label = rem > 0 ? escapeHtml(rem + 's') : 'checking\u2026';
        var tierLabel = '';
        if (acctObj && acctObj._poll_tier) tierLabel = ' (' + escapeHtml(acctObj._poll_tier) + ')';
        else if (u5 > 85) tierLabel = ' (critical)';
        else if (u5 > 70) tierLabel = ' (warning)';
        else if (u5 > 50) tierLabel = ' (normal)';
        else tierLabel = ' (idle)';
        checkHtml = ' \u00b7 <span class="text-teal-500" data-next-check data-cached-at="' + usageCachedAt + '">' + label + escapeHtml(tierLabel) + '</span>';
    }
    return '<span class="text-xs text-slate-500" data-cache-age>Usage updated ' + escapeHtml(ago) + checkHtml + '</span>';
}

/**
 * Render per-model usage bars inside expandable details.
 */
function renderPerModelBars(usage) {
    if (!usage || !usage.per_model || Object.keys(usage.per_model).length === 0) {
        return '<div class="text-xs text-slate-500 mb-2">No per-model breakdown available</div>';
    }

    let html = '<div class="text-xs text-slate-400 font-medium mb-1.5">7-Day Per-Model Breakdown</div>';
    for (const [key, model] of Object.entries(usage.per_model)) {
        // Prefer the provider's display name (e.g. "Fable", "GPT-5.3-Codex-Spark");
        // fall back to the legacy static map, then the raw key.
        const label = model.label || MODEL_DISPLAY_NAMES[key] || key;
        const pct = model.utilization || 0;
        html += renderUsageBar(pct, model.resets_at, null, label);
    }
    return html;
}

/**
 * Render the inline "binding model" bar shown right under the 5h/7d bars — the
 * per-model weekly cap the provider flags as the currently-active constraint
 * (e.g. Fable at 92%). Returns a stable [data-binding-bar] wrapper ALWAYS (empty
 * inner when there's no binding model) so websocket.js can patch it in place and
 * the 5h/7d child indexing in _usageUpdateCardDOM stays fixed.
 */
function renderBindingBar(acct) {
    const bm = acct.usage && acct.usage.binding_model;
    const inner = bm
        ? renderUsageBar(bm.utilization, bm.resets_at, null, bm.label || 'model')
        : '';
    // Title stays on the wrapper (not the inner bar) so it survives websocket.js's
    // innerHTML swaps and clarifies that this row is a per-model weekly cap, not
    // another time window like the 5h/7d bars above it.
    return `<div data-binding-bar title="Per-model weekly usage cap for this account">${inner}</div>`;
}

/**
 * Render extra usage credits section.
 */
function renderExtraUsageCredits(usage) {
    if (!usage || !usage.extra_usage || !usage.extra_usage.is_enabled) {
        return '';
    }

    const extra = usage.extra_usage;
    const used = extra.used_credits;
    const limit = extra.monthly_limit;
    const pct = extra.utilization || 0;

    let creditText;
    if (used !== null && used !== undefined && limit !== null && limit !== undefined) {
        creditText = `$${used.toFixed(2)} / $${limit.toFixed(2)} used`;
    } else {
        creditText = 'Extra usage enabled (no data yet)';
    }

    const colorClass = pct >= 90 ? 'red' : (pct >= 71 ? 'yellow' : 'green');
    const barColor = colorClass === 'red' ? 'bg-red-500' : colorClass === 'yellow' ? 'bg-yellow-500' : 'bg-green-500';
    // Themed via usageTextClass (usage.js, loaded before this file) so the America
    // 250 scheme recolors the percent label alongside the CSS-themed bar fill.
    const textColor = usageTextClass(colorClass);

    return `
        <div class="mt-2 pt-2 border-t border-slate-700/30">
            <div class="text-xs text-slate-400 font-medium mb-1.5">Extra Usage Credits</div>
            <div class="flex items-center gap-3 mb-1">
                <span class="text-xs text-slate-400 w-14 shrink-0">Credits</span>
                <div class="usage-bar flex-1">
                    <div class="fill ${colorClass}" style="width: ${Math.min(100, pct).toFixed(1)}%"></div>
                </div>
                <span class="text-xs font-mono w-10 text-right tabular-nums ${textColor}">${Math.round(pct)}%</span>
                <span class="text-xs text-slate-500 w-36 text-right tabular-nums">${escapeHtml(creditText)}</span>
            </div>
        </div>
    `;
}


/**
 * Render expandable details section for an account.
 */
function renderExpandableDetails(acct) {
    const usage = acct.usage;
    const hasPerModel = usage && usage.per_model && Object.keys(usage.per_model).length > 0;
    const hasExtraUsage = usage && usage.extra_usage && usage.extra_usage.is_enabled;

    if (!hasPerModel && !hasExtraUsage) {
        return '';
    }

    return `
        <div class="mt-2">
            <button class="btn-toggle-details text-xs text-slate-400 hover:text-slate-200 transition-colors" data-id="${acct.id}">
                Show details <span class="details-arrow">&#9660;</span>
            </button>
            <div class="account-details hidden mt-2 pt-2 border-t border-slate-700/30" data-details-id="${acct.id}">
                ${renderPerModelBars(usage)}
                ${renderExtraUsageCredits(usage)}
            </div>
        </div>
    `;
}

/**
 * Organization label for an account: the org name when the API returned one,
 * otherwise a truncated org UUID, otherwise ''. Shared by the card body and
 * the overflow menu's metadata header.
 */
function accountOrgLabel(acct) {
    if (acct.organization_name) return acct.organization_name;
    if (acct.organization_uuid) return acct.organization_uuid.slice(0, 8) + '…';
    return '';
}

// Icon paths for the overflow-menu rows (same stroke style as the rest of the
// card: fill="none", stroke="currentColor", viewBox 0 0 24 24).
const ACCOUNT_MENU_ICONS = {
    copy: 'M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z',
    rename: 'M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z',
    reauth: 'M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15',
    disable: 'M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636',
    enable: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
    delete: 'M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16',
};

/**
 * Render one overflow-menu row.
 *
 * `cls` MUST stay the legacy button class (btn-copy-cmd, btn-edit-label,
 * btn-reauth, btn-toggle, btn-delete) and `attrs` MUST carry the same data-*
 * attributes the old inline buttons carried — the handlers in
 * account-actions.js bind by those classes and read those attributes.
 */
function renderAccountMenuItem(cls, attrs, iconKey, text, colorCls) {
    const color = colorCls || 'text-slate-300 hover:bg-slate-700/60';
    return `<button class="${cls} account-menu-item w-full flex items-center gap-2.5 px-3 py-2 text-xs text-left ${color} transition-colors" role="menuitem" ${attrs}>
                <svg class="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="${ACCOUNT_MENU_ICONS[iconKey]}"/></svg>
                <span>${text}</span>
            </button>`;
}

/**
 * Render the kebab (three-dot) overflow menu for an account card.
 *
 * Everything the user does rarely lives here instead of on the card face:
 * copy launch command, rename, re-auth, enable/disable, delete — plus the
 * account id and organization, which are worth seeing occasionally but not
 * worth permanent card space.
 *
 * Re-auth is ALWAYS present for Claude accounts, never gated on
 * validation_status: accounts revalidate at most hourly, so the DB can still
 * say 'valid' after a token died, and gating the only re-auth affordance on
 * jacked's own state hid it exactly when that state was wrong. The kebab
 * carries a small attention dot when jacked does know re-auth is needed, so
 * the now-hidden action stays discoverable at a glance.
 */
function renderAccountMenu(acct) {
    const status = getAccountStatus(acct);
    const needsAttention = status === 'invalid' || status === 'expired';
    const provider = acct.provider || 'claude';
    const hasCustomLabel = acct.display_name && acct.display_name !== acct.email;
    const orgLabel = accountOrgLabel(acct);

    // Metadata header — non-interactive. The account id is here because the
    // REST fallback (`jacked claude <id>`, PATCH /api/auth/accounts/<id>)
    // needs it and nothing else on the card shows it.
    const orgLine = orgLabel
        ? `<div class="text-[11px] text-slate-500 truncate" title="${escapeHtml(orgLabel)}">${escapeHtml(orgLabel)}</div>`
        : '';
    const header = `<div class="px-3 py-2 border-b border-slate-700" role="presentation">
                <div class="text-xs text-slate-500">Account #${escapeHtml(String(acct.id))}</div>
                ${orgLine}
            </div>`;

    let items = renderAccountMenuItem(
        'btn-copy-cmd',
        `data-id="${acct.id}" data-cmd="jacked claude ${acct.id}"`,
        'copy',
        'Copy launch command',
    );

    items += renderAccountMenuItem(
        'btn-edit-label',
        `data-id="${acct.id}" data-label="${escapeHtml(hasCustomLabel ? acct.display_name : '')}"`,
        'rename',
        hasCustomLabel ? 'Rename' : 'Add label',
    );

    // Codex re-auth is `codex login` in a terminal (the Codex pill's
    // "re-login" tooltip guides that); the backend rejects browser OAuth for
    // it, so the row is omitted entirely rather than rendered and failing.
    if (provider !== 'codex') {
        items += renderAccountMenuItem(
            'btn-reauth',
            `data-id="${acct.id}" data-email="${escapeHtml(acct.email || '')}"`,
            'reauth',
            'Re-auth',
        );
    }

    items += renderAccountMenuItem(
        'btn-toggle',
        `data-id="${acct.id}" data-active="${acct.is_active}"`,
        acct.is_active ? 'disable' : 'enable',
        acct.is_active ? 'Disable' : 'Enable',
    );

    items += '<div class="border-t border-slate-700 my-1" role="separator"></div>';

    items += renderAccountMenuItem(
        'btn-delete',
        `data-id="${acct.id}"`,
        'delete',
        'Delete account',
        'text-red-400 hover:text-red-300 hover:bg-red-900/30',
    );

    const dot = needsAttention
        ? '<span class="account-menu-dot" aria-hidden="true"></span>'
        : '';

    // z-30 clears the sibling cards in .accounts-grid (they carry no z-index).
    // A disabled card also gets opacity-60, which opens its own stacking
    // context — .provider-card.menu-open in style.css lifts the whole card
    // while its menu is open so the panel still paints over the next card.
    return `
        <div class="account-menu-wrap relative">
            <button class="btn-account-menu relative p-1.5 rounded text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition-colors" data-id="${acct.id}" aria-haspopup="menu" aria-expanded="false" aria-label="Account actions" title="More actions">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6a1 1 0 110-2 1 1 0 010 2zm0 7a1 1 0 110-2 1 1 0 010 2zm0 7a1 1 0 110-2 1 1 0 010 2z"/></svg>
                ${dot}
            </button>
            <div class="account-menu hidden absolute right-0 top-full mt-1 min-w-[13rem] py-1 bg-slate-800 border border-slate-700 rounded-md shadow-lg z-30" role="menu" aria-label="Account actions" data-menu-for="${acct.id}">
                ${header}
                ${items}
            </div>
        </div>
    `;
}

/**
 * Render the action row for an account card: the one action people take often
 * (Use Account) on the left, everything else behind the kebab on the right.
 */
function renderActionButtons(acct) {
    const isActiveInCC = window.jackedState.activeCredentialAccountId === acct.id;

    // "Use Account" button or "Active" badge.
    // Show on all enabled accounts — backend validates and returns clear
    // error messages for cc-missing, invalid, etc.
    let setActiveHtml = '';
    if (isActiveInCC) {
        setActiveHtml = '<span class="text-xs px-3 py-1.5 bg-green-600/20 text-green-400 border border-green-600/30 rounded font-medium">Active in Claude Code</span>';
    } else if (acct.is_active) {
        setActiveHtml = `<button class="btn-use-account text-xs px-3 py-1.5 bg-teal-600/20 text-teal-400 hover:bg-teal-600/40 border border-teal-600/30 rounded font-medium transition active:scale-[0.96]" data-id="${acct.id}" data-email="${escapeHtml(acct.email || '')}">Use Account</button>`;
    }

    return `
        <div class="flex items-center gap-2 mt-2 pt-2 border-t border-slate-700/50">
            ${setActiveHtml}
            <div class="flex-1"></div>
            ${renderAccountMenu(acct)}
        </div>
    `;
}

/**
 * Render a single account card.
 */
function renderAccountCard(acct, idx, total) {
    const status = getAccountStatus(acct);
    const email = acct.email || 'Unknown';
    // Show display_name as custom label only when it differs from email
    const hasCustomLabel = acct.display_name && acct.display_name !== acct.email;
    const label = hasCustomLabel ? acct.display_name : '';
    const orgLabel = accountOrgLabel(acct);
    const subDisplay = getSubDisplay(acct);
    const priorityBadge = getPriorityBadge(acct.priority || 0);

    // Token pills replace status/CC badges
    const pillsHtml = renderTokenPills(acct);

    // Disabled badge (pills don't cover disabled state)
    let disabledBadge = '';
    if (status === 'disabled') {
        disabledBadge = '<span class="badge badge-muted">Disabled</span>';
    }

    // Usage bars
    const elapsed5h = computeElapsedFraction5h(acct.cached_5h_resets_at);
    const elapsed7d = computeElapsedFraction7d(acct.cached_7d_resets_at);
    const usage5h = renderUsageBar(acct.cached_usage_5h, acct.cached_5h_resets_at, elapsed5h, '5h limit');
    const usage7d = renderUsageBar(acct.cached_usage_7d, acct.cached_7d_resets_at, elapsed7d, '7d limit');
    const cacheAgeHtml = renderCacheAge(acct.usage_cached_at, acct.id);

    // Priority reorder buttons
    const upDisabled = idx === 0 ? 'disabled' : '';
    const downDisabled = idx === total - 1 ? 'disabled' : '';
    const priorityButtons = total > 1 ? `
        <div class="flex flex-col gap-0.5 mr-2">
            <button class="priority-btn btn-priority-up" data-id="${acct.id}" ${upDisabled} title="Move up">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 15l7-7 7 7"/></svg>
            </button>
            <button class="priority-btn btn-priority-down" data-id="${acct.id}" ${downDisabled} title="Move down">
                <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
            </button>
        </div>
    ` : '<div class="w-6 mr-2"></div>';

    // Error display
    let errorHtml = '';
    if (acct.last_error && (acct.validation_status === 'invalid' || acct.consecutive_failures > 0)) {
        errorHtml = `<div class="text-xs text-red-400 mt-2">${escapeHtml(acct.last_error)}</div>`;
    }

    // Extra usage flag
    let extraUsageHtml = '';
    if (acct.has_extra_usage) {
        extraUsageHtml = '<span class="badge badge-success ml-2">Extra Usage</span>';
    }

    const detailsHtml = renderExpandableDetails(acct);
    const actionsHtml = renderActionButtons(acct);

    const disabledClass = status === 'disabled' ? ' opacity-60' : '';
    const primaryName = label || email;
    const provider = (acct.provider || 'claude');
    return `
        <div class="bg-slate-800 border border-slate-700 rounded-lg px-4 py-3 card-hover provider-card provider-${provider}${disabledClass}" data-account-id="${acct.id}">
            <div class="flex items-start">
                ${priorityButtons}
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 flex-wrap">
                        ${providerBadge(provider)}
                        <span class="status-dot ${status}"></span>
                        <span class="font-medium text-white truncate max-w-[300px]" title="${escapeHtml(primaryName)}">${escapeHtml(primaryName)}</span>
                        ${priorityBadge}
                        ${disabledBadge}
                        ${pillsHtml}
                        ${extraUsageHtml}
                    </div>
                    <div class="flex items-center gap-3 mb-2 ml-5">
                        <span class="text-xs text-slate-400">${escapeHtml(email)}${orgLabel ? ` (${escapeHtml(orgLabel)})` : ''}</span>
                        <span class="text-slate-600">|</span>
                        <span class="text-xs text-slate-400">${escapeHtml(subDisplay)}</span>
                        <span class="text-slate-600">|</span>
                        ${cacheAgeHtml}
                        <button class="btn-refresh-single text-slate-500 hover:text-slate-300 transition-colors p-1.5 -m-1 rounded relative" data-id="${acct.id}" title="Refresh usage" aria-label="Refresh usage">
                            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                        </button>
                    </div>
                    <!-- data-usage-container: queried by _usageUpdateCardDOM (websocket.js) for surgical bar updates -->
                    <div class="ml-5" data-usage-container>
                        ${usage5h}
                        ${usage7d}
                        ${renderBindingBar(acct)}
                        ${renderActiveSessions(acct)}
                        ${detailsHtml}
                    </div>
                    ${errorHtml ? '<div class="ml-5">' + errorHtml + '</div>' : ''}
                    ${actionsHtml}
                </div>
            </div>
            <div class="delete-confirm-container hidden" data-id="${acct.id}"></div>
        </div>
    `;
}

/**
 * Render the full accounts page.
 */
function renderAccounts(accounts) {
    const visible = (accounts || []).filter(a => !a.is_deleted);

    if (visible.length === 0) {
        return renderEmptyState();
    }

    const invalidCount = visible.filter(a => a.validation_status === 'invalid').length;
    let bannerHtml = '';
    if (invalidCount > 0) {
        bannerHtml = `
            <div class="bg-orange-900/30 border border-orange-700 rounded-lg px-4 py-3 mb-4 text-sm text-orange-200">
                <strong>${invalidCount} account${invalidCount > 1 ? 's' : ''}</strong> require${invalidCount === 1 ? 's' : ''} re-authentication.
                Tokens may have expired or been revoked.
            </div>
        `;
    }

    // Session isolation tip banner (dismissible via localStorage)
    let tipHtml = '';
    if (!localStorage.getItem('jacked_tip_dismissed')) {
        tipHtml = `
            <div id="session-tip-banner" class="bg-slate-700/50 border border-slate-600 rounded-lg px-4 py-3 mb-4 text-sm text-slate-300">
                <div class="flex items-start justify-between">
                    <div>
                        <strong class="text-slate-200">Per-account sessions:</strong>
                        <strong>Use Account</strong> changes the desired global account. Existing sessions update only when fresh evidence confirms it. Use <code class="bg-slate-800 px-1.5 py-0.5 rounded text-teal-400 text-xs">jacked claude &lt;id&gt;</code> to launch with per-account credentials.
                        Supports pass-through args: <code class="bg-slate-800 px-1.5 py-0.5 rounded text-teal-400 text-xs">jacked claude 2 --resume</code>
                    </div>
                    <button id="btn-dismiss-tip" class="text-slate-500 hover:text-slate-300 ml-3 shrink-0 text-lg leading-none" title="Dismiss">&times;</button>
                </div>
            </div>
        `;
    }

    const sorted = [...visible].sort((a, b) => (a.priority || 0) - (b.priority || 0));
    const cardsHtml = sorted.map((acct, idx) => renderAccountCard(acct, idx, sorted.length)).join('');

    return `
        <div class="max-w-[1700px]">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white text-balance">Accounts</h2>
                <div class="flex items-center gap-2">
                    <button id="btn-refresh-all-usage" class="flex items-center gap-2 px-4 py-2 text-slate-300 hover:text-white hover:bg-slate-700 text-sm font-medium rounded-lg border border-slate-600 transition active:scale-[0.96]">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                        Refresh All Usage
                    </button>
                    <select id="sel-auto-refresh" class="bg-slate-700 border border-slate-600 text-slate-300 text-sm rounded-lg px-3 py-2 cursor-pointer hover:border-slate-500 transition-colors" title="Auto-refresh interval" aria-label="Auto-refresh interval">
                        <option value="0">Auto: Off</option>
                        <option value="120">Auto: 2 min</option>
                        <option value="300">Auto: 5 min</option>
                        <option value="600">Auto: 10 min</option>
                    </select>
                    <button id="btn-add-account" class="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-lg transition active:scale-[0.96]">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
                        Add Account
                    </button>
                </div>
            </div>
            ${typeof renderAutoSwapPanel === 'function' ? renderAutoSwapPanel() : ''}
            ${tipHtml}
            ${bannerHtml}
            <div id="oauth-flow-status"></div>
            ${typeof renderSessionControls === 'function' ? renderSessionControls() : ''}
            ${typeof renderSessionLookupResult === 'function' ? renderSessionLookupResult() : ''}
            ${typeof renderUnknownSessions === 'function' ? renderUnknownSessions() : ''}
            <!-- Responsive card grid — see .accounts-grid in style.css.
                 Reflows to 2 columns only when each card can be a comfortable
                 width; the wrapper's max-width caps the flow at 2. -->
            <div id="accounts-list" class="accounts-grid">
                ${cardsHtml}
            </div>

            <div class="mt-6 bg-slate-800 border border-slate-700 rounded-lg p-4">
                <h3 class="text-sm font-medium text-slate-300 mb-3">Swap History</h3>
                <div id="swap-history-container" class="overflow-x-auto">
                    <div class="text-xs text-slate-500">Loading...</div>
                </div>
            </div>

            <div class="mt-6 bg-slate-800 border border-slate-700 rounded-lg p-4">
                <h3 class="text-sm font-medium text-slate-300 mb-3">Decision Log</h3>
                <div id="decision-log-container" class="overflow-x-auto" data-show-all="false">
                    <div class="text-xs text-slate-500">Loading...</div>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render empty state when no accounts exist.
 */
function renderEmptyState() {
    return `
        <div class="flex flex-col items-center justify-center py-24 px-8">
            <div class="w-16 h-16 rounded-full bg-slate-800 flex items-center justify-center mb-4">
                <svg class="w-8 h-8 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z"/></svg>
            </div>
            <h3 class="text-lg font-semibold text-white mb-1">No accounts connected</h3>
            <p class="text-sm text-slate-400 mb-6">Connect a Claude or Codex account to get started</p>
            <button id="btn-add-account" class="flex items-center gap-2 px-6 py-3 bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg transition active:scale-[0.96]">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
                Add Account
            </button>
            <div id="oauth-flow-status" class="mt-4"></div>
        </div>
    `;
}

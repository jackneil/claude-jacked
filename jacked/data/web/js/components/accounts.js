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
 * Determine visual status for an account.
 */
function getAccountStatus(acct) {
    if (!acct.is_active) return 'disabled';
    if (acct.validation_status === 'checking') return 'checking';
    if (acct.validation_status === 'invalid') return 'invalid';

    // Check expiry — computed, not stored
    const now = Math.floor(Date.now() / 1000);
    if (acct.expires_at && now >= acct.expires_at) return 'expired';

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
 */
function renderCacheAge(usageCachedAt) {
    if (usageCachedAt === null || usageCachedAt === undefined) {
        return '<span class="text-xs text-slate-500">Usage: never fetched</span>';
    }
    const ago = timeAgoFromUnix(usageCachedAt);
    return `<span class="text-xs text-slate-500">Usage updated ${escapeHtml(ago)}</span>`;
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
        const label = MODEL_DISPLAY_NAMES[key] || key;
        const pct = model.utilization || 0;
        html += renderUsageBar(pct, model.resets_at, null, label);
    }
    return html;
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
    const textColor = colorClass === 'red' ? 'text-red-400' : colorClass === 'yellow' ? 'text-yellow-400' : 'text-slate-300';

    return `
        <div class="mt-2 pt-2 border-t border-slate-700/30">
            <div class="text-xs text-slate-400 font-medium mb-1.5">Extra Usage Credits</div>
            <div class="flex items-center gap-3 mb-1">
                <span class="text-xs text-slate-400 w-14 shrink-0">Credits</span>
                <div class="usage-bar flex-1">
                    <div class="fill ${colorClass}" style="width: ${Math.min(100, pct).toFixed(1)}%"></div>
                </div>
                <span class="text-xs font-mono w-10 text-right ${textColor}">${Math.round(pct)}%</span>
                <span class="text-xs text-slate-500 w-36 text-right">${escapeHtml(creditText)}</span>
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
 * Render action buttons for an account card.
 */
function renderActionButtons(acct) {
    const status = getAccountStatus(acct);
    const isActiveInCC = window.jackedState.activeCredentialAccountId === acct.id;

    // Set Active / Active badge (left side, primary action)
    let setActiveHtml = '';
    if (isActiveInCC) {
        setActiveHtml = '<span class="text-xs px-3 py-1.5 bg-green-600/20 text-green-400 border border-green-600/30 rounded font-medium">Active in Claude Code</span>';
    } else if (status === 'valid' || status === 'unknown') {
        setActiveHtml = `<button class="btn-set-active text-xs px-3 py-1.5 bg-teal-600/20 text-teal-400 hover:bg-teal-600/40 rounded transition-colors" data-id="${acct.id}" data-email="${escapeHtml(acct.email || '')}">Set Active</button>`;
    }

    // Copy launch command button
    const copyCmd = `jacked claude ${acct.id}`;
    const copyHtml = `<button class="btn-copy-cmd text-xs px-3 py-1.5 text-slate-400 hover:text-slate-200 hover:bg-slate-700 rounded transition-colors" data-cmd="${escapeHtml(copyCmd)}" title="Copy launch command">
        <svg class="w-4 h-4 inline-block mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3"/></svg>
        ${escapeHtml(copyCmd)}
    </button>`;

    // Re-auth button (if invalid/expired)
    const showReauth = status === 'invalid' || status === 'expired';
    let reauthHtml = '';
    if (showReauth) {
        reauthHtml = `<button class="btn-reauth text-xs px-3 py-1.5 bg-blue-600/20 text-blue-400 hover:bg-blue-600/40 rounded transition-colors" data-id="${acct.id}">Re-auth</button>`;
    }

    // Toggle active/disabled
    const toggleLabel = acct.is_active ? 'Disable' : 'Enable';
    const toggleClass = acct.is_active ? 'text-yellow-400 hover:text-yellow-300' : 'text-green-400 hover:text-green-300';

    return `
        <div class="flex items-center flex-wrap gap-2 mt-3 pt-3 border-t border-slate-700/50">
            ${setActiveHtml}
            ${copyHtml}
            <div class="flex-1"></div>
            ${reauthHtml}
            <button class="btn-toggle text-xs px-3 py-1.5 ${toggleClass} hover:bg-slate-700 rounded transition-colors" data-id="${acct.id}" data-active="${acct.is_active}">${toggleLabel}</button>
            <button class="btn-delete text-xs px-3 py-1.5 text-red-400 hover:text-red-300 hover:bg-red-900/30 rounded transition-colors" data-id="${acct.id}" title="Delete account">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
            </button>
        </div>
    `;
}

/**
 * Render a single account card.
 */
function renderAccountCard(acct, idx, total) {
    const status = getAccountStatus(acct);
    const email = escapeHtml(acct.email || 'Unknown');
    const subDisplay = getSubDisplay(acct);
    const priorityBadge = getPriorityBadge(acct.priority || 0);

    // Status badge for non-valid states
    let statusBadge = '';
    if (status === 'checking') {
        statusBadge = '<span class="badge badge-info">Checking...</span>';
    } else if (status === 'invalid') {
        statusBadge = '<span class="badge badge-warning">Token Invalid</span>';
    } else if (status === 'expired') {
        statusBadge = '<span class="badge badge-warning">Expired</span>';
    } else if (status === 'disabled') {
        statusBadge = '<span class="badge badge-muted">Disabled</span>';
    }

    // Usage bars
    const elapsed5h = computeElapsedFraction5h(acct.cached_5h_resets_at);
    const elapsed7d = computeElapsedFraction7d(acct.cached_7d_resets_at);
    const usage5h = renderUsageBar(acct.cached_usage_5h, acct.cached_5h_resets_at, elapsed5h, '5h limit');
    const usage7d = renderUsageBar(acct.cached_usage_7d, acct.cached_7d_resets_at, elapsed7d, '7d limit');
    const cacheAgeHtml = renderCacheAge(acct.usage_cached_at);

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
    if (acct.last_error) {
        errorHtml = `<div class="text-xs text-red-400 mt-2">${escapeHtml(acct.last_error)}</div>`;
    }

    // Extra usage flag
    let extraUsageHtml = '';
    if (acct.has_extra_usage) {
        extraUsageHtml = '<span class="badge badge-success ml-2">Extra Usage</span>';
    }

    const detailsHtml = renderExpandableDetails(acct);
    const actionsHtml = renderActionButtons(acct);

    return `
        <div class="bg-slate-800 border border-slate-700 rounded-lg p-4 card-hover" data-account-id="${acct.id}">
            <div class="flex items-start">
                ${priorityButtons}
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2 mb-1">
                        <span class="status-dot ${status}"></span>
                        <span class="font-medium text-white truncate">${email}</span>
                        ${priorityBadge}
                        ${statusBadge}
                        ${extraUsageHtml}
                    </div>
                    <div class="flex items-center gap-3 mb-3 ml-5">
                        <span class="text-xs text-slate-400">${escapeHtml(subDisplay)}</span>
                        <span class="text-slate-600">|</span>
                        ${cacheAgeHtml}
                    </div>
                    <div class="ml-5">
                        ${usage5h}
                        ${usage7d}
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
                        <strong class="text-slate-200">Per-account sessions</strong> &mdash;
                        Use <code class="bg-slate-800 px-1.5 py-0.5 rounded text-teal-400 text-xs">jacked claude &lt;id&gt;</code> to launch Claude Code with isolated credentials per account.
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
        <div class="max-w-3xl">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Accounts</h2>
                <div class="flex items-center gap-2">
                    <button id="btn-refresh-all-usage" class="flex items-center gap-2 px-4 py-2 text-slate-300 hover:text-white hover:bg-slate-700 text-sm font-medium rounded-lg border border-slate-600 transition-colors">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
                        Refresh All Usage
                    </button>
                    <div class="flex items-center gap-1.5" title="Auto-refresh usage every 60s">
                        <span class="text-xs text-slate-400">Auto</span>
                        <label class="toggle-switch toggle-sm">
                            <input type="checkbox" id="chk-auto-refresh">
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                    <button id="btn-add-account" class="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-lg transition-colors">
                        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
                        Add Account
                    </button>
                </div>
            </div>
            ${tipHtml}
            ${bannerHtml}
            <div id="oauth-flow-status"></div>
            ${typeof renderSessionControls === 'function' ? renderSessionControls() : ''}
            ${typeof renderSessionLookupResult === 'function' ? renderSessionLookupResult() : ''}
            <div id="accounts-list" class="flex flex-col gap-3">
                ${cardsHtml}
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
            <p class="text-sm text-slate-400 mb-6">Connect your Claude account to get started</p>
            <button id="btn-add-account" class="flex items-center gap-2 px-6 py-3 bg-blue-600 hover:bg-blue-500 text-white font-medium rounded-lg transition-colors">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
                Connect Account
            </button>
            <div id="oauth-flow-status" class="mt-4"></div>
        </div>
    `;
}

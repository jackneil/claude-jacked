/**
 * jacked web dashboard — accounts component
 * Full account management: list, add, delete, reorder, validate, re-auth.
 * Enhanced with per-model usage breakdowns, extra usage credits, cache age.
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
        // Reuse renderUsageBar with no elapsed marker for per-model
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

    // Don't show expand button if there's nothing to expand
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
 * Render the full accounts page.
 */
function renderAccounts(accounts) {
    // Filter out deleted
    const visible = (accounts || []).filter(a => !a.is_deleted);

    if (visible.length === 0) {
        return renderEmptyState();
    }

    // Check for any global errors
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

    // Sort by priority
    const sorted = [...visible].sort((a, b) => (a.priority || 0) - (b.priority || 0));

    const cardsHtml = sorted.map((acct, idx) => renderAccountCard(acct, idx, sorted.length)).join('');

    return `
        <div class="max-w-3xl">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Accounts</h2>
                <button id="btn-add-account" class="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-lg transition-colors">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>
                    Add Account
                </button>
            </div>
            ${bannerHtml}
            <div id="oauth-flow-status"></div>
            <div id="accounts-list" class="flex flex-col gap-3">
                ${cardsHtml}
            </div>
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

    // Usage bars (from top-level cached fields)
    const elapsed5h = computeElapsedFraction5h(acct.cached_5h_resets_at);
    const elapsed7d = computeElapsedFraction7d(acct.cached_7d_resets_at);
    const usage5h = renderUsageBar(acct.cached_usage_5h, acct.cached_5h_resets_at, elapsed5h, '5h limit');
    const usage7d = renderUsageBar(acct.cached_usage_7d, acct.cached_7d_resets_at, elapsed7d, '7d limit');

    // Cache age
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

    // Action buttons
    const showReauth = status === 'invalid' || status === 'expired';
    const toggleLabel = acct.is_active ? 'Disable' : 'Enable';
    const toggleClass = acct.is_active ? 'text-yellow-400 hover:text-yellow-300' : 'text-green-400 hover:text-green-300';

    let actionsHtml = `
        <div class="flex items-center gap-2 mt-3 pt-3 border-t border-slate-700/50">
            <div class="flex-1"></div>
    `;
    if (showReauth) {
        actionsHtml += `<button class="btn-reauth text-xs px-3 py-1.5 bg-blue-600/20 text-blue-400 hover:bg-blue-600/40 rounded transition-colors" data-id="${acct.id}">Re-auth</button>`;
    }
    actionsHtml += `
            <button class="btn-refresh-usage text-xs px-3 py-1.5 text-slate-400 hover:text-white hover:bg-slate-700 rounded transition-colors" data-id="${acct.id}" title="Refresh usage">
                <svg class="w-3.5 h-3.5 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
            </button>
            <button class="btn-toggle text-xs px-3 py-1.5 ${toggleClass} hover:bg-slate-700 rounded transition-colors" data-id="${acct.id}" data-active="${acct.is_active}">${toggleLabel}</button>
            <button class="btn-delete text-xs px-3 py-1.5 text-red-400 hover:text-red-300 hover:bg-red-900/30 rounded transition-colors" data-id="${acct.id}" title="Delete account">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
            </button>
    `;
    actionsHtml += '</div>';

    // Error display
    let errorHtml = '';
    if (acct.last_error) {
        errorHtml = `<div class="text-xs text-red-400 mt-2">${escapeHtml(acct.last_error)}</div>`;
    }

    // Extra usage flag (badge in header)
    let extraUsageHtml = '';
    if (acct.has_extra_usage) {
        extraUsageHtml = '<span class="badge badge-success ml-2">Extra Usage</span>';
    }

    // Expandable details (per-model bars, extra usage credits)
    const detailsHtml = renderExpandableDetails(acct);

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

// ---------------------------------------------------------------------------
// Event binding
// ---------------------------------------------------------------------------
function bindAccountEvents() {
    // Add Account button
    document.querySelectorAll('#btn-add-account').forEach(btn => {
        btn.addEventListener('click', startAddAccountFlow);
    });

    // Re-auth buttons
    document.querySelectorAll('.btn-reauth').forEach(btn => {
        btn.addEventListener('click', () => startAddAccountFlow());
    });

    // Toggle active/disabled
    document.querySelectorAll('.btn-toggle').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.id;
            const isActive = btn.dataset.active === 'true';
            try {
                await api.patch(`/api/auth/accounts/${id}`, { is_active: !isActive });
                showToast(isActive ? 'Account disabled' : 'Account enabled', 'success');
                await refreshAndRender();
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    });

    // Delete buttons
    document.querySelectorAll('.btn-delete').forEach(btn => {
        btn.addEventListener('click', () => {
            const id = btn.dataset.id;
            showDeleteConfirm(id);
        });
    });

    // Refresh usage buttons
    document.querySelectorAll('.btn-refresh-usage').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.id;
            try {
                await api.post(`/api/auth/accounts/${id}/refresh-usage`);
                showToast('Usage refreshed', 'success');
                await refreshAndRender();
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    });

    // Priority up/down
    document.querySelectorAll('.btn-priority-up').forEach(btn => {
        btn.addEventListener('click', () => handlePriorityMove(btn.dataset.id, -1));
    });
    document.querySelectorAll('.btn-priority-down').forEach(btn => {
        btn.addEventListener('click', () => handlePriorityMove(btn.dataset.id, 1));
    });

    // Expandable details toggle
    document.querySelectorAll('.btn-toggle-details').forEach(btn => {
        btn.addEventListener('click', () => {
            const id = btn.dataset.id;
            const details = document.querySelector(`.account-details[data-details-id="${id}"]`);
            const arrow = btn.querySelector('.details-arrow');
            if (!details) return;

            if (details.classList.contains('hidden')) {
                details.classList.remove('hidden');
                if (arrow) arrow.innerHTML = '&#9650;';
                btn.childNodes[0].textContent = 'Hide details ';
            } else {
                details.classList.add('hidden');
                if (arrow) arrow.innerHTML = '&#9660;';
                btn.childNodes[0].textContent = 'Show details ';
            }
        });
    });
}

// ---------------------------------------------------------------------------
// Priority reorder
// ---------------------------------------------------------------------------
async function handlePriorityMove(accountId, direction) {
    const sorted = [...window.jackedState.accounts]
        .filter(a => !a.is_deleted)
        .sort((a, b) => (a.priority || 0) - (b.priority || 0));

    const idx = sorted.findIndex(a => String(a.id) === String(accountId));
    if (idx < 0) return;

    const newIdx = idx + direction;
    if (newIdx < 0 || newIdx >= sorted.length) return;

    // Swap
    [sorted[idx], sorted[newIdx]] = [sorted[newIdx], sorted[idx]];
    const order = sorted.map(a => a.id);

    try {
        await api.post('/api/auth/accounts/reorder', { order });
        await refreshAndRender();
    } catch (e) {
        showToast(e.message, 'error');
    }
}

// ---------------------------------------------------------------------------
// Delete confirmation
// ---------------------------------------------------------------------------
function showDeleteConfirm(accountId) {
    const container = document.querySelector(`.delete-confirm-container[data-id="${accountId}"]`);
    if (!container) return;

    container.classList.remove('hidden');
    container.innerHTML = `
        <div class="delete-confirm flex items-center gap-3 mt-3 pt-3 border-t border-red-800/50 text-sm">
            <span class="text-red-300">Remove this account?</span>
            <button class="btn-confirm-yes px-3 py-1 bg-red-600 hover:bg-red-500 text-white text-xs rounded transition-colors" data-id="${accountId}">Yes, Remove</button>
            <button class="btn-confirm-cancel px-3 py-1 text-slate-400 hover:text-white text-xs rounded transition-colors" data-id="${accountId}">Cancel</button>
        </div>
    `;

    // Auto-cancel after 5 seconds
    const timer = setTimeout(() => hideDeleteConfirm(accountId), 5000);
    container.dataset.timer = timer;

    container.querySelector('.btn-confirm-yes').addEventListener('click', async () => {
        clearTimeout(timer);
        try {
            await api.delete(`/api/auth/accounts/${accountId}`);
            showToast('Account removed', 'success');
            await refreshAndRender();
        } catch (e) {
            showToast(e.message, 'error');
        }
    });

    container.querySelector('.btn-confirm-cancel').addEventListener('click', () => {
        clearTimeout(timer);
        hideDeleteConfirm(accountId);
    });
}

function hideDeleteConfirm(accountId) {
    const container = document.querySelector(`.delete-confirm-container[data-id="${accountId}"]`);
    if (!container) return;
    if (container.dataset.timer) clearTimeout(Number(container.dataset.timer));
    container.classList.add('hidden');
    container.innerHTML = '';
}

// ---------------------------------------------------------------------------
// OAuth add-account flow
// ---------------------------------------------------------------------------
async function startAddAccountFlow() {
    const statusEl = document.getElementById('oauth-flow-status');
    if (!statusEl) return;

    statusEl.innerHTML = `
        <div class="bg-blue-900/30 border border-blue-700 rounded-lg px-4 py-3 text-sm text-blue-200 flex items-center gap-3">
            <div class="spinner"></div>
            <div>
                <div class="font-medium">Waiting for authorization...</div>
                <div class="text-xs text-blue-300 mt-1">A browser window should open. Complete the authorization there.</div>
            </div>
        </div>
    `;

    let flowId;
    try {
        const result = await api.post('/api/auth/accounts/add');
        flowId = result.flow_id;
    } catch (e) {
        statusEl.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to start auth flow: ${escapeHtml(e.message)}
            </div>
        `;
        return;
    }

    if (!flowId) {
        statusEl.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                No flow ID returned from server
            </div>
        `;
        return;
    }

    // Poll every 1s, timeout at 2 minutes
    let elapsed = 0;
    const maxWait = 120;
    const pollInterval = setInterval(async () => {
        elapsed++;
        if (elapsed > maxWait) {
            clearInterval(pollInterval);
            statusEl.innerHTML = `
                <div class="bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200">
                    Authorization timed out after 2 minutes. Please try again.
                </div>
            `;
            return;
        }

        try {
            const poll = await api.get(`/api/auth/flow/${flowId}`);

            if (poll.status === 'completed') {
                clearInterval(pollInterval);
                statusEl.innerHTML = `
                    <div class="bg-green-900/30 border border-green-700 rounded-lg px-4 py-3 text-sm text-green-200">
                        Account connected successfully!
                    </div>
                `;
                setTimeout(() => { statusEl.innerHTML = ''; }, 3000);
                await refreshAndRender();
            } else if (poll.status === 'error') {
                clearInterval(pollInterval);
                statusEl.innerHTML = `
                    <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                        Authorization failed: ${escapeHtml(poll.error || 'Unknown error')}
                    </div>
                `;
            }
            // status === 'pending' — keep polling
        } catch (e) {
            // not_found means flow expired
            if (e.status === 404) {
                clearInterval(pollInterval);
                statusEl.innerHTML = `
                    <div class="bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-3 text-sm text-yellow-200">
                        Authorization flow expired. Please try again.
                    </div>
                `;
            }
        }
    }, 1000);

    window.jackedState.flowPolling = pollInterval;
}

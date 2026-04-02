// jacked web dashboard — account actions
// Event handlers, OAuth flows, delete/reorder, and credential switching.

// ---------------------------------------------------------------------------
// Auto-refresh usage state
// ---------------------------------------------------------------------------
let _autoRefreshInterval = null;
let _checkCountdownInterval = null;

function _startCheckCountdown() {
    if (_checkCountdownInterval) return;
    var _checkingTicks = 0;
    _checkCountdownInterval = setInterval(function() {
        var els = document.querySelectorAll('[data-next-check]');
        if (els.length === 0) { _checkingTicks = 0; return; }
        var now = Math.floor(Date.now() / 1000);
        // Read latest usage_cached_at from state (updated by UI refresh
        // and WebSocket), not from the stale data-cached-at HTML attribute.
        var activeId = window.jackedState.activeCredentialAccountId;
        var accounts = window.jackedState.accounts || [];
        var activeAcct = accounts.find(function(a) { return a.id === activeId; });
        var cachedAt = activeAcct ? activeAcct.usage_cached_at : null;
        if (!cachedAt) return;
        var u5data = activeAcct ? (activeAcct.cached_usage_5h || 0) : 0;
        var pollInterval = 300;
        if (u5data > 85) pollInterval = 65;
        else if (u5data > 70) pollInterval = 90;
        else if (u5data > 50) pollInterval = 150;
        else pollInterval = 300;
        var rem = Math.max(0, pollInterval - (now - cachedAt));
        els.forEach(function(el) {
            el.textContent = rem > 0 ? rem + 's' : 'checking\u2026';
            el.setAttribute('data-cached-at', String(cachedAt));
        });
        // If stuck at "checking" for >15s, re-fetch accounts to get fresh
        // usage_cached_at from the server (the backend poll updated it but
        // didn't push via WebSocket).
        if (rem === 0) {
            _checkingTicks++;
            if (_checkingTicks >= 15 && typeof refreshAndRender === 'function') {
                _checkingTicks = 0;
                refreshAndRender();
            }
        } else {
            _checkingTicks = 0;
        }
    }, 1000);
}

function _stopCheckCountdown() {
    if (_checkCountdownInterval) {
        clearInterval(_checkCountdownInterval);
        _checkCountdownInterval = null;
    }
}

let _autoRefreshCountdown = 0;
let _lastAutoRefreshAt = null; // Date.now() of last COMPLETED auto-refresh; null = no prior refresh or stopped
const _singleRefreshInFlight = new Set(); // tracks accountIds with pending single-refresh
// Shared via window.jackedState so websocket.js can check it without cross-file globals
if (window.jackedState) window.jackedState._usageRefreshInProgress = false;
const _refreshSvg = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>';

// Migrate old auto-refresh localStorage key (checkbox '0'/'1') to new interval key
if (localStorage.getItem('jacked_auto_refresh') === '1' && !localStorage.getItem('jacked_auto_refresh_interval')) {
    localStorage.setItem('jacked_auto_refresh_interval', '120');
}
localStorage.removeItem('jacked_auto_refresh');

// Get user's configured auto-refresh interval (seconds), 0 = off
function _getAutoRefreshSeconds() {
    return parseInt(localStorage.getItem('jacked_auto_refresh_interval') || '0', 10) || 0;
}

// Format countdown: "2:05" for >= 60s, "45s" for < 60s
function _formatCountdown(seconds) {
    if (seconds >= 60) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return m + ':' + String(s).padStart(2, '0');
    }
    return seconds + 's';
}

// ---------------------------------------------------------------------------
// SweetAlert confirmation helpers for auth flows
// ---------------------------------------------------------------------------
async function _confirmAddAccount() {
    return Swal.fire({
        title: 'Add Account',
        html: `Add a new Google account?<br><br>
               This opens browser tabs for authorization:<br>
               1. Usage token (for jacked dashboard)<br>
               2. Claude Code token (refresh-capable, for CC sessions)<br><br>
               Complete both to fully authorize the new account.`,
        icon: 'info',
        showCancelButton: true,
        confirmButtonText: 'Add Account',
        cancelButtonText: 'Cancel',
        focusCancel: true,
    });
}

async function _confirmReauth(email) {
    return Swal.fire({
        title: 'Re-authenticate Account?',
        html: `Re-authenticate <strong>${escapeHtml(email)}</strong>?<br><br>
               This opens browser tabs for authorization:<br>
               1. Usage token (for jacked dashboard)<br>
               2. Claude Code token (refresh-capable, for CC sessions)<br><br>
               Complete both to fully authorize this account.<br>
               Sign in with the same Google account.`,
        icon: 'info',
        showCancelButton: true,
        confirmButtonText: 'Authorize',
        cancelButtonText: 'Cancel',
        focusCancel: true,
    });
}

async function _confirmCcAuth(email) {
    return Swal.fire({
        title: 'Authorize Claude Code Token?',
        html: `Authorize a separate token for <strong>${escapeHtml(email)}</strong>?<br><br>
               This opens a browser tab. Sign in with the same Google account.<br><br>
               Claude Code uses its own refresh-capable token, independent from the usage token.
               This lets CC sessions refresh without re-authenticating.`,
        icon: 'info',
        showCancelButton: true,
        confirmButtonText: 'Authorize',
        cancelButtonText: 'Cancel',
        focusCancel: true,
    });
}

// ---------------------------------------------------------------------------
// Pill click handler — re-attaches on every render (safe: old element is GC'd)
// ---------------------------------------------------------------------------
function initPillHandlers() {
    const list = document.getElementById('accounts-list');
    if (!list) return;
    list.addEventListener('click', async (e) => {
        const pill = e.target.closest('button.token-pill.actionable');
        if (!pill) return;
        if (window.jackedState._accountActionInFlight) {
            showToast('Another action is in progress', 'warning', 2000);
            return;
        }
        const action = pill.dataset.action;
        const id = pill.dataset.accountId;
        const email = pill.dataset.email;
        try {
            if (action === 'reauth-primary') {
                const result = await _confirmReauth(email);
                if (!result.isConfirmed) return;
                if (window.jackedState._accountActionInFlight) {
                    showToast('Another action started — please try again', 'warning', 2000);
                    return;
                }
                startReauthFlow(id, email);
            } else if (action === 'auth-cc') {
                const result = await _confirmCcAuth(email);
                if (!result.isConfirmed) return;
                if (window.jackedState._accountActionInFlight) {
                    showToast('Another action started — please try again', 'warning', 2000);
                    return;
                }
                startCcAuthFlow(id, email);
            }
        } catch (err) {
            console.error('Auth confirmation error:', err);
            showToast('Something went wrong — please try again', 'error');
        }
    });
}

function bindAccountEvents() {
    initPillHandlers();
    if (typeof bindSessionControlEvents === 'function') bindSessionControlEvents();

    // Add Account button
    document.querySelectorAll('#btn-add-account').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (window.jackedState._accountActionInFlight) {
                showToast('Another action is in progress', 'warning', 2000);
                return;
            }
            try {
                const result = await _confirmAddAccount();
                if (!result.isConfirmed) return;
                if (window.jackedState._accountActionInFlight) {
                    showToast('Another action started — please try again', 'warning', 2000);
                    return;
                }
                startAddAccountFlow();
            } catch (err) {
                console.error('Add account confirmation error:', err);
                showToast('Something went wrong — please try again', 'error');
            }
        });
    });

    // Re-auth buttons
    document.querySelectorAll('.btn-reauth').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (window.jackedState._accountActionInFlight) {
                showToast('Another action is in progress', 'warning', 2000);
                return;
            }
            const email = btn.dataset.email || '';
            try {
                const result = await _confirmReauth(email);
                if (!result.isConfirmed) return;
                if (window.jackedState._accountActionInFlight) {
                    showToast('Another action started — please try again', 'warning', 2000);
                    return;
                }
                startAddAccountFlow();
            } catch (err) {
                console.error('Reauth confirmation error:', err);
                showToast('Something went wrong — please try again', 'error');
            }
        });
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

    // Edit label buttons
    document.querySelectorAll('.btn-edit-label').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (window.jackedState._accountActionInFlight) return;
            const id = btn.dataset.id;
            const current = btn.dataset.label || '';
            const result = await Swal.fire({
                title: 'Account Label',
                input: 'text',
                inputValue: current,
                inputPlaceholder: 'e.g., Work Max, Personal Pro',
                showCancelButton: true,
                confirmButtonText: 'Save',
                cancelButtonText: 'Cancel',
                inputAttributes: { maxlength: 50, autocomplete: 'off' },
            });
            if (!result.isConfirmed) return;
            const value = (result.value || '').trim();
            try {
                await api.patch(`/api/auth/accounts/${id}`, { display_name: value });
                showToast(value ? `Label set to "${value}"` : 'Label cleared', 'success');
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

    // "Use Account" button — switches all Claude Code sessions to this account
    document.querySelectorAll('.btn-use-account').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (window.jackedState._accountActionInFlight) {
                showToast('Another action is in progress', 'warning', 2000);
                return;
            }
            const id = btn.dataset.id;
            const email = btn.dataset.email || '';
            window.jackedState._accountActionInFlight = true;
            btn.disabled = true;
            btn.textContent = 'Switching\u2026';
            try {
                await api.post(`/api/auth/accounts/${id}/use`);
                showToast(`Switched to ${email}`, 'success');
                await loadActiveCredential();
            } catch (e) {
                showToast(e.message, 'error');
            } finally {
                window.jackedState._accountActionInFlight = false;
                await refreshAndRender();
            }
        });
    });

    // Refresh All Usage button
    const refreshAllBtn = document.getElementById('btn-refresh-all-usage');
    if (refreshAllBtn) {
        refreshAllBtn.addEventListener('click', () => _triggerUsageRefresh().catch(() => {}));
    }

    // Per-card refresh usage buttons
    document.querySelectorAll('.btn-refresh-single').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            _triggerSingleUsageRefresh(btn.dataset.id);
        });
    });

    // Priority up/down
    document.querySelectorAll('.btn-priority-up').forEach(btn => {
        btn.addEventListener('click', () => handlePriorityMove(btn.dataset.id, -1));
    });
    document.querySelectorAll('.btn-priority-down').forEach(btn => {
        btn.addEventListener('click', () => handlePriorityMove(btn.dataset.id, 1));
    });

    // Copy launch command buttons
    document.querySelectorAll('.btn-copy-cmd').forEach(btn => {
        btn.addEventListener('click', async () => {
            const cmd = btn.dataset.cmd;
            try {
                await navigator.clipboard.writeText(cmd);
                showToast(`Copied: ${cmd}`, 'success', 2000);
            } catch {
                // Fallback for insecure contexts — verify execCommand success
                const ta = document.createElement('textarea');
                ta.value = cmd;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                const ok = document.execCommand('copy');
                document.body.removeChild(ta);
                if (ok) {
                    showToast(`Copied: ${cmd}`, 'success', 2000);
                } else {
                    showToast(`Copy failed \u2014 run manually: ${cmd}`, 'warning', 4000);
                }
            }
        });
    });

    // Dismiss session tip banner
    const dismissBtn = document.getElementById('btn-dismiss-tip');
    if (dismissBtn) {
        dismissBtn.addEventListener('click', () => {
            localStorage.setItem('jacked_tip_dismissed', '1');
            const banner = document.getElementById('session-tip-banner');
            if (banner) banner.remove();
        });
    }

    // Auto-refresh toggle
    bindAutoRefreshToggle();

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

    _startCheckCountdown();
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
    const safeId = escapeHtml(String(accountId));
    container.innerHTML = `
        <div class="delete-confirm flex items-center gap-3 mt-3 pt-3 border-t border-red-800/50 text-sm">
            <span class="text-red-300">Remove this account?</span>
            <button class="btn-confirm-yes px-3 py-1 bg-red-600 hover:bg-red-500 text-white text-xs rounded transition-colors" data-id="${safeId}">Yes, Remove</button>
            <button class="btn-confirm-cancel px-3 py-1 text-slate-400 hover:text-white text-xs rounded transition-colors" data-id="${safeId}">Cancel</button>
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

// startAddAccountFlow() and startCcAuthFlow() moved to oauth-flows.js

// ---------------------------------------------------------------------------
// Usage refresh — shared by button click and auto-refresh
// ---------------------------------------------------------------------------
async function _triggerUsageRefresh() {
    if (window.jackedState._usageRefreshInProgress) {
        showToast('Usage refresh already in progress', 'warning', 2000);
        return;
    }
    const btn = document.getElementById('btn-refresh-all-usage');
    window.jackedState._usageRefreshInProgress = true;
    if (btn) {
        btn.disabled = true;
        _setRefreshBtnText(btn, 'Refreshing...');
    }

    // Immediately mark all active accounts as queued (frontend-side, before server responds)
    // Provides instant visual feedback even if WebSocket usage_refresh_started is slightly delayed
    if (typeof _usageInjectOverlay === 'function') {
        const activeAccounts = (window.jackedState.accounts || []).filter(a => !a.is_deleted && a.is_active);
        for (const acct of activeAccounts) {
            const card = document.querySelector('[data-account-id="' + acct.id + '"]');
            if (card) {
                card.classList.remove('usage-checking', 'usage-done', 'usage-failed');
                card.classList.add('usage-queued');
                _usageInjectOverlay(card, 'queued', 'Waiting\u2026');
            }
        }
    }

    try {
        const ss = window.jackedState.swapSettings || {};
        const activeId = window.jackedState.activeCredentialAccountId;
        const skipParam = (ss.auto_swap_enabled && activeId) ? '?skip_account=' + activeId : '';
        const result = await api.post('/api/auth/accounts/refresh-all-usage' + skipParam);
        if (result.refreshed === 0 && result.failed === 0) {
            showToast('No active accounts to refresh', 'warning');
        } else if (result.failed > 0) {
            const failedAccounts = (result.results || [])
                .filter(r => !r.success)
                .map(r => r.email)
                .join(', ');
            showToast('Usage refreshed (' + result.refreshed + ' ok, ' + result.failed + ' failed: ' + failedAccounts + ')', 'warning');
        } else {
            showToast('Usage refreshed for ' + result.refreshed + ' account' + (result.refreshed !== 1 ? 's' : ''), 'success');
        }
        if (_autoRefreshInterval) {
            // Reset countdown and anchor only when auto-refresh is running.
            // This applies to both auto-tick fires and manual "Refresh All" clicks while auto-refresh is on —
            // both reset the countdown, which is correct (a manual refresh acts like an early tick).
            // When auto-refresh is OFF (_autoRefreshInterval is null), we skip this so a manual click
            // doesn't silently start tracking _lastAutoRefreshAt without a timer running.
            _autoRefreshCountdown = _getAutoRefreshSeconds();
            _lastAutoRefreshAt = Date.now();
        }
        // Clean up any remaining overlays (e.g., if WS events were missed)
        document.querySelectorAll('.usage-status-overlay').forEach(el => el.remove());
        document.querySelectorAll('[data-account-id].usage-queued, [data-account-id].usage-checking').forEach(
            el => el.classList.remove('usage-queued', 'usage-checking')
        );
        // Only re-render if still on accounts tab (user may have navigated away)
        if (window.jackedState.activeRoute === 'accounts') {
            await refreshAndRender();
        }
    } catch (e) {
        // Clean up overlays on error too
        document.querySelectorAll('.usage-status-overlay').forEach(el => el.remove());
        document.querySelectorAll('[data-account-id].usage-queued, [data-account-id].usage-checking').forEach(
            el => el.classList.remove('usage-queued', 'usage-checking')
        );
        showToast(e.message, 'error');
        throw e; // re-throw so callers (e.g., _autoRefreshTick) can react
    } finally {
        window.jackedState._usageRefreshInProgress = false;
        if (btn) {
            btn.disabled = false;
            _updateRefreshBtnLabel();
        }
    }
}

// ---------------------------------------------------------------------------
// Single-account usage refresh
// ---------------------------------------------------------------------------
async function _triggerSingleUsageRefresh(accountId) {
    if (window.jackedState._usageRefreshInProgress) {
        showToast('Bulk refresh in progress — please wait', 'warning', 2000);
        return;
    }
    if (_singleRefreshInFlight.has(accountId)) return;

    const card = document.querySelector('[data-account-id="' + accountId + '"]');
    if (!card) return;

    _singleRefreshInFlight.add(accountId);
    card.classList.remove('usage-done', 'usage-failed');
    card.classList.add('usage-checking');
    if (typeof _usageInjectOverlay === 'function') {
        _usageInjectOverlay(card, 'checking', 'Checking usage\u2026');
    }

    try {
        await api.post('/api/auth/accounts/' + accountId + '/refresh-usage');
        if (window.jackedState.activeRoute === 'accounts') {
            await refreshAndRender();
            // Apply success overlay to the newly-rendered card (old DOM node was replaced)
            const fresh = document.querySelector('[data-account-id="' + accountId + '"]');
            if (fresh) {
                fresh.classList.add('usage-done');
                if (typeof _usageInjectOverlay === 'function') {
                    _usageInjectOverlay(fresh, 'done', 'Updated!');
                    setTimeout(() => {
                        if (typeof _usageRemoveOverlay === 'function') _usageRemoveOverlay(fresh);
                        fresh.classList.remove('usage-done');
                    }, _OVERLAY_DONE_MS);
                } else {
                    setTimeout(() => fresh.classList.remove('usage-done'), _OVERLAY_DONE_MS);
                }
            }
        }
    } catch (e) {
        const current = document.querySelector('[data-account-id="' + accountId + '"]');
        if (current) {
            current.classList.remove('usage-checking');
            current.classList.add('usage-failed');
            if (typeof _usageInjectOverlay === 'function') {
                _usageInjectOverlay(current, 'failed', 'Failed');
                setTimeout(() => {
                    if (typeof _usageRemoveOverlay === 'function') _usageRemoveOverlay(current);
                    current.classList.remove('usage-failed');
                }, _OVERLAY_FAILED_MS);
            } else {
                setTimeout(() => current.classList.remove('usage-failed'), _OVERLAY_FAILED_MS);
            }
        }
        showToast('Refresh failed: ' + e.message, 'error');
    } finally {
        _singleRefreshInFlight.delete(accountId);
    }
}

// Create a small inline spinner element (shared by refresh button + WS handler)
function _createInlineSpinner() {
    const spinner = document.createElement('div');
    spinner.className = 'spinner';
    spinner.style.cssText = 'width:16px;height:16px;border-width:2px;display:inline-block;vertical-align:middle';
    return spinner;
}

// Set button text safely (no innerHTML with untrusted data)
function _setRefreshBtnText(btn, text) {
    btn.textContent = '';
    const spinner = _createInlineSpinner();
    btn.appendChild(spinner);
    btn.appendChild(document.createTextNode(' ' + text));
}

function _updateRefreshBtnLabel() {
    const btn = document.getElementById('btn-refresh-all-usage');
    if (!btn) return;
    if (_autoRefreshInterval) {
        btn.textContent = '';
        btn.insertAdjacentHTML('afterbegin', _refreshSvg);
        btn.appendChild(document.createTextNode(' Refresh now \u00b7 ' + _formatCountdown(_autoRefreshCountdown)));
    } else {
        btn.textContent = '';
        btn.insertAdjacentHTML('afterbegin', _refreshSvg);
        btn.appendChild(document.createTextNode(' Refresh All Usage'));
    }
}

// ---------------------------------------------------------------------------
// Auto-refresh usage
// ---------------------------------------------------------------------------
function _startAutoRefresh() {
    // Cold start — clear timestamp so _changeAutoRefreshInterval takes the "fresh start" path
    _lastAutoRefreshAt = null;
    _changeAutoRefreshInterval(_getAutoRefreshSeconds());
}

function _stopAutoRefresh() {
    if (_autoRefreshInterval) {
        clearInterval(_autoRefreshInterval);
        _autoRefreshInterval = null;
    }
    _stopCheckCountdown();
    // Clearing _lastAutoRefreshAt ensures re-enable takes the "fresh start" path in
    // _changeAutoRefreshInterval (null timestamp = no elapsed time to carry over).
    // This applies to both: user selecting "Off" (manual stop) and the error circuit-breaker
    // in _autoRefreshTick calling _stopAutoRefresh() after a failure.
    _lastAutoRefreshAt = null;
    _updateRefreshBtnLabel();
}

function _changeAutoRefreshInterval(newSecs) {
    if (_autoRefreshInterval) clearInterval(_autoRefreshInterval);
    _autoRefreshInterval = null;

    if (!_lastAutoRefreshAt) {
        // Coming from OFF state or cold start — always start fresh, no immediate fire
        _autoRefreshCountdown = newSecs;
        _lastAutoRefreshAt = Date.now();
        _autoRefreshInterval = setInterval(_autoRefreshTick, 1000);
        _updateRefreshBtnLabel();
        return;
    }

    const elapsedSecs = Math.floor((Date.now() - _lastAutoRefreshAt) / 1000);
    const remaining = newSecs - elapsedSecs;

    if (remaining <= 0) {
        // Overdue at new interval — fire immediately if not already refreshing.
        // Note: .catch(() => {}) is intentional — unlike _autoRefreshTick's circuit-breaker catch,
        // this is a one-shot user-triggered fire; transient errors should not stop the timer.
        _autoRefreshCountdown = newSecs;
        // IMPORTANT: _autoRefreshInterval MUST be assigned before _triggerUsageRefresh() is called.
        // The success block in _triggerUsageRefresh checks _autoRefreshInterval to decide whether
        // to update _lastAutoRefreshAt. Swapping this order would silently skip that update.
        _autoRefreshInterval = setInterval(_autoRefreshTick, 1000);
        _updateRefreshBtnLabel();
        // Do NOT set _lastAutoRefreshAt here — let _triggerUsageRefresh success block do it on
        // completion. If the fire is skipped (in-flight guard), _lastAutoRefreshAt stays at the
        // previous completed-refresh time, which is the correct anchor for elapsed-time calculations.
        if (!window.jackedState._usageRefreshInProgress) {
            _triggerUsageRefresh().catch(() => {});
        }
    } else {
        // Carry over elapsed time — next tick is sooner than a full new interval
        _autoRefreshCountdown = remaining;
        _autoRefreshInterval = setInterval(_autoRefreshTick, 1000);
        _updateRefreshBtnLabel();
    }
}

async function _autoRefreshTick() {
    const btn = document.getElementById('btn-refresh-all-usage');
    if (!btn) return;
    if (window.jackedState._usageRefreshInProgress || window.jackedState._accountActionInFlight) return;

    _autoRefreshCountdown--;
    if (_autoRefreshCountdown > 0) {
        _updateRefreshBtnLabel();
        return;
    }

    try {
        await _triggerUsageRefresh();
    } catch (e) {
        showToast('Auto-refresh failed: ' + e.message, 'error');
        localStorage.setItem('jacked_auto_refresh_interval', '0');
        const sel = document.getElementById('sel-auto-refresh');
        if (sel) sel.value = '0';
        _stopAutoRefresh();
    }
}

function bindAutoRefreshToggle() {
    const sel = document.getElementById('sel-auto-refresh');
    if (!sel) return;

    const stored = String(_getAutoRefreshSeconds());
    sel.value = stored;
    // If stored value isn't a valid option, reset to off
    if (sel.value !== stored) {
        sel.value = '0';
        localStorage.setItem('jacked_auto_refresh_interval', '0');
    }
    const storedSecs = _getAutoRefreshSeconds();
    if (storedSecs > 0) {
        if (!_autoRefreshInterval) {
            _startAutoRefresh();
        } else if (_lastAutoRefreshAt) {
            // Timer was running while on another tab — _autoRefreshTick pauses when btn is absent.
            // Check elapsed time and correct the frozen countdown regardless of whether we're overdue.
            const elapsed = Math.floor((Date.now() - _lastAutoRefreshAt) / 1000);
            if (elapsed >= storedSecs) {
                // Overdue — fire immediately and restart the interval.
                _changeAutoRefreshInterval(storedSecs);
            } else {
                // Not yet overdue — correct the frozen countdown to reflect actual elapsed time.
                // (Countdown froze while off-tab because _autoRefreshTick bails when btn is absent.)
                _autoRefreshCountdown = storedSecs - elapsed;
                _updateRefreshBtnLabel();
            }
        }
    }

    sel.addEventListener('change', () => {
        const val = sel.value;
        localStorage.setItem('jacked_auto_refresh_interval', val);
        if (val === '0') {
            _stopAutoRefresh();
        } else {
            _changeAutoRefreshInterval(parseInt(val, 10));
        }
    });
}

// ---------------------------------------------------------------------------
// Active credential loader
// ---------------------------------------------------------------------------
async function loadActiveCredential() {
    try {
        const data = await api.get('/api/auth/active-credential');
        window.jackedState.activeCredentialAccountId = data.account_id || null;
    } catch {
        window.jackedState.activeCredentialAccountId = null;
    }
}

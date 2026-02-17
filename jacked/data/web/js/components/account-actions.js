/**
 * jacked web dashboard — account actions
 * Event handlers, OAuth flows, delete/reorder, and credential switching.
 * Split from accounts.js for guardrails compliance.
 */

// Flag to suppress polling re-renders during in-flight actions
let _accountActionInFlight = false;

// ---------------------------------------------------------------------------
// Auto-refresh usage state
// ---------------------------------------------------------------------------
let _autoRefreshInterval = null;
let _autoRefreshCountdown = 60;
const AUTO_REFRESH_PERIOD = 60;
const _refreshSvg = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>';

// ---------------------------------------------------------------------------
// Event binding (called after renderAccounts)
// ---------------------------------------------------------------------------
function bindAccountEvents() {
    // Session display controls (defined in sessions.js)
    if (typeof bindSessionControlEvents === 'function') bindSessionControlEvents();

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

    // Set Active buttons
    document.querySelectorAll('.btn-set-active').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.id;
            const email = btn.dataset.email || 'this account';
            const result = await Swal.fire({
                title: 'Switch Active Account?',
                html: `Set <strong>${email}</strong> as Claude Code's active account?<br><br><span style="color:#94a3b8">You'll need to restart Claude Code for this to take effect.</span>`,
                icon: 'question',
                showCancelButton: true,
                confirmButtonText: 'Switch Account',
                cancelButtonText: 'Cancel',
                focusCancel: true,
            });
            if (!result.isConfirmed) return;

            btn.disabled = true;
            const originalHtml = btn.innerHTML;
            btn.innerHTML = '<div class="spinner" style="width:12px;height:12px;border-width:2px"></div>';
            _accountActionInFlight = true;

            try {
                await api.post(`/api/auth/accounts/${id}/use`);
                showToast(`Switched to ${email} — restart Claude Code`, 'success');
                await refreshAndRender();
            } catch (e) {
                showToast(e.message, 'error');
                btn.disabled = false;
                btn.innerHTML = originalHtml;
            } finally {
                _accountActionInFlight = false;
            }
        });
    });

    // Refresh All Usage button
    const refreshAllBtn = document.getElementById('btn-refresh-all-usage');
    if (refreshAllBtn) {
        refreshAllBtn.addEventListener('click', async () => {
            refreshAllBtn.disabled = true;
            refreshAllBtn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px"></div> Refreshing...';
            try {
                const result = await api.post('/api/auth/accounts/refresh-all-usage');
                if (result.refreshed === 0 && result.failed === 0) {
                    showToast('No active accounts to refresh', 'warning');
                } else if (result.failed > 0) {
                    const failedAccounts = (result.results || [])
                        .filter(r => !r.success)
                        .map(r => r.email)
                        .join(', ');
                    showToast(`Usage refreshed (${result.refreshed} ok, ${result.failed} failed: ${failedAccounts})`, 'warning');
                } else {
                    showToast(`Usage refreshed for ${result.refreshed} account${result.refreshed !== 1 ? 's' : ''}`, 'success');
                }
                if (_autoRefreshInterval) _autoRefreshCountdown = AUTO_REFRESH_PERIOD;
                await refreshAndRender();
            } catch (e) {
                showToast(e.message, 'error');
            }
            refreshAllBtn.disabled = false;
            if (_autoRefreshInterval) {
                refreshAllBtn.innerHTML = `${_refreshSvg} Refresh now \u00b7 ${_autoRefreshCountdown}s`;
            } else {
                refreshAllBtn.innerHTML = `${_refreshSvg} Refresh All Usage`;
            }
        });
    }

    // Priority up/down
    document.querySelectorAll('.btn-priority-up').forEach(btn => {
        btn.addEventListener('click', () => handlePriorityMove(btn.dataset.id, -1));
    });
    document.querySelectorAll('.btn-priority-down').forEach(btn => {
        btn.addEventListener('click', () => handlePriorityMove(btn.dataset.id, 1));
    });

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

// ---------------------------------------------------------------------------
// Auto-refresh usage
// ---------------------------------------------------------------------------
function _startAutoRefresh() {
    if (_autoRefreshInterval) clearInterval(_autoRefreshInterval);
    _autoRefreshCountdown = AUTO_REFRESH_PERIOD;
    _autoRefreshInterval = setInterval(_autoRefreshTick, 1000);
    const btn = document.getElementById('btn-refresh-all-usage');
    if (btn) btn.innerHTML = `${_refreshSvg} Refresh now \u00b7 ${_autoRefreshCountdown}s`;
}

function _stopAutoRefresh() {
    if (_autoRefreshInterval) {
        clearInterval(_autoRefreshInterval);
        _autoRefreshInterval = null;
    }
    const btn = document.getElementById('btn-refresh-all-usage');
    if (btn) btn.innerHTML = `${_refreshSvg} Refresh All Usage`;
}

async function _autoRefreshTick() {
    const btn = document.getElementById('btn-refresh-all-usage');
    if (!btn) return;
    if (_accountActionInFlight) return;

    _autoRefreshCountdown--;
    if (_autoRefreshCountdown > 0) {
        btn.innerHTML = `${_refreshSvg} Refresh now \u00b7 ${_autoRefreshCountdown}s`;
        return;
    }

    _accountActionInFlight = true;
    btn.disabled = true;
    btn.innerHTML = '<div class="spinner" style="width:16px;height:16px;border-width:2px"></div> Refreshing...';
    try {
        await api.post('/api/auth/accounts/refresh-all-usage');
        _autoRefreshCountdown = AUTO_REFRESH_PERIOD;
        await refreshAndRender();
    } catch (e) {
        showToast(`Auto-refresh failed: ${e.message}`, 'error');
        localStorage.setItem('jacked_auto_refresh', '0');
        const chk = document.getElementById('chk-auto-refresh');
        if (chk) chk.checked = false;
        _stopAutoRefresh();
    } finally {
        _accountActionInFlight = false;
        if (btn) btn.disabled = false;
    }
}

function bindAutoRefreshToggle() {
    const chk = document.getElementById('chk-auto-refresh');
    if (!chk) return;

    const isOn = localStorage.getItem('jacked_auto_refresh') === '1';
    chk.checked = isOn;
    if (isOn && !_autoRefreshInterval) _startAutoRefresh();

    chk.addEventListener('change', () => {
        if (chk.checked) {
            localStorage.setItem('jacked_auto_refresh', '1');
            _startAutoRefresh();
        } else {
            localStorage.setItem('jacked_auto_refresh', '0');
            _stopAutoRefresh();
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

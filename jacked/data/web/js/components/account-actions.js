// jacked web dashboard — account actions
// Event handlers, OAuth flows, delete/reorder, and credential switching.

// ---------------------------------------------------------------------------
// Auto-refresh usage state
// ---------------------------------------------------------------------------
let _autoRefreshInterval = null;
let _checkCountdownInterval = null;

function _startCheckCountdown() {
    if (_checkCountdownInterval) return;
    _checkCountdownInterval = setInterval(function() {
        var els = document.querySelectorAll('[data-next-check]');
        if (els.length === 0) return;
        var now = Math.floor(Date.now() / 1000);
        // Read latest usage_cached_at from state — updated in real time
        // by the usage_poll_updated WebSocket event.
        var activeId = window.jackedState.activeCredentialAccountId;
        var accounts = window.jackedState.accounts || [];
        var activeAcct = accounts.find(function(a) { return a.id === activeId; });
        var cachedAt = activeAcct ? activeAcct.usage_cached_at : null;
        // Use backend-provided poll interval and last-poll timestamp
        // instead of guessing from raw usage thresholds.
        var pollInterval = activeAcct._poll_interval || 300;
        var lastPollAt = activeAcct._last_poll_at || cachedAt;
        if (!lastPollAt) {
            els.forEach(function(el) {
                el.textContent = 'starting\u2026';
            });
            return;
        }
        var rem = Math.max(0, pollInterval - (now - lastPollAt));
        var tierLabel = activeAcct._poll_tier ? ' (' + activeAcct._poll_tier + ')' : '';
        els.forEach(function(el) {
            if (rem > 0) {
                el.textContent = rem + 's' + tierLabel;
            } else if (lastPollAt && (now - lastPollAt) > 2 * pollInterval) {
                el.textContent = 'delayed';
            } else {
                el.textContent = 'checking\u2026';
            }
            el.setAttribute('data-cached-at', String(lastPollAt));
        });
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
// Absolute floor for automated bulk refresh passes — matches the smallest dropdown option.
// No automated path may fire more often than this, no matter what localStorage says
// (the key is shared across tabs, so another tab can write values under our feet).
const _AUTO_REFRESH_FLOOR_SECS = 120;
let _lastBulkStartedAt = null; // Date.now() when the last bulk pass STARTED (any initiator)
let _rateGuardWarned = false;  // warn once per suppression streak, reset when a pass starts
const _singleRefreshInFlight = new Map(); // accountId -> Date.now() when the refresh started
// Backstop: entries older than this are treated as stale (orphaned by a refresh whose
// finally never ran) so the user isn't locked out of refreshing that account forever.
const _SINGLE_REFRESH_STALE_MS = 240000;
// Shared via window.jackedState so websocket.js can check it without cross-file globals
if (window.jackedState) window.jackedState._usageRefreshInProgress = false;
const _refreshSvg = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>';

// Migrate old auto-refresh localStorage key (checkbox '0'/'1') to new interval key
if (localStorage.getItem('jacked_auto_refresh') === '1' && !localStorage.getItem('jacked_auto_refresh_interval')) {
    localStorage.setItem('jacked_auto_refresh_interval', '120');
}
localStorage.removeItem('jacked_auto_refresh');

// Get user's configured auto-refresh interval (seconds), 0 = off.
// Non-zero values are clamped to the floor at this single choke point so no
// consumer (tick, success-path reset, carry-over math) can run faster than it.
function _getAutoRefreshSeconds() {
    const secs = parseInt(localStorage.getItem('jacked_auto_refresh_interval') || '0', 10) || 0;
    return secs <= 0 ? 0 : Math.max(secs, _AUTO_REFRESH_FLOOR_SECS);
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
        title: 'Add account',
        html: `Which provider?<br><br>
               <b>Claude</b> — opens browser tabs to authorize (usage + Claude Code tokens).<br><br>
               <b>Codex</b> — imports your signed-in OpenAI Codex account
               (run <code>codex login</code> in a terminal first if you're not signed in).`,
        showDenyButton: true,
        showCancelButton: true,
        confirmButtonText: 'Claude',
        denyButtonText: 'Codex',
        cancelButtonText: 'Cancel',
        // SweetAlert's deny button is RED by default — that reads as danger for a
        // benign choice. Color both buttons with their provider hue instead.
        confirmButtonColor: '#a78bfa',  // Claude violet
        denyButtonColor: '#60a5fa',     // Codex blue
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
               Sign in with the same Claude account.`,
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
               This opens a browser tab. Sign in with the same Claude account.<br><br>
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

// ---------------------------------------------------------------------------
// Account overflow (kebab) menu
//
// The menu holds the rare actions (copy launch command, rename, re-auth,
// enable/disable, delete). Each row keeps the legacy button class and data-*
// attributes, so the handlers below bind to them exactly as before — this
// block only handles open/close/focus.
// ---------------------------------------------------------------------------

// The single open menu, or null. Kept as a reference (not a DOM query) so the
// stale-node case after a re-render is impossible to act on: every render
// clears it in initAccountMenuHandlers().
let _openAccountMenu = null;   // { btn, menu, card }
// Document-level listeners survive re-renders, so they are bound exactly once.
let _accountMenuGlobalsBound = false;

function closeAccountMenu(opts) {
    if (!_openAccountMenu) return;
    const { btn, menu, card } = _openAccountMenu;
    _openAccountMenu = null;
    if (menu) menu.classList.add('hidden');
    if (btn) {
        btn.setAttribute('aria-expanded', 'false');
        if (opts && opts.focusButton && typeof btn.focus === 'function') btn.focus();
    }
    if (card) card.classList.remove('menu-open');
}

function openAccountMenu(btn) {
    const wrap = btn.closest('.account-menu-wrap');
    const menu = wrap ? wrap.querySelector('.account-menu') : null;
    if (!menu) return;
    closeAccountMenu();          // only one menu open at a time
    menu.classList.remove('hidden');
    btn.setAttribute('aria-expanded', 'true');
    const card = btn.closest('[data-account-id]');
    if (card) card.classList.add('menu-open');
    _openAccountMenu = { btn, menu, card };
}

function toggleAccountMenu(btn) {
    if (_openAccountMenu && _openAccountMenu.btn === btn) {
        closeAccountMenu();
        return;
    }
    openAccountMenu(btn);
}

// The account id whose menu is open, or null. Callers that rebuild
// #accounts-list (rerenderAccountsView) save this before the swap.
function openAccountMenuId() {
    if (!_openAccountMenu || !_openAccountMenu.btn) return null;
    const id = _openAccountMenu.btn.dataset ? _openAccountMenu.btn.dataset.id : null;
    return id === undefined ? null : id;
}

// Re-open the menu for `id` against the CURRENT DOM. This is the restore half of
// the save/restore pair rerenderAccountsView uses for every other piece of
// transient UI state (expanded details, repo groups, the lookup input, the live
// OAuth banner) — without it a routine websocket re-render closes a menu the
// user is still reading. Returns true when a menu was opened.
//
// The account can legitimately be gone (deleted, filtered out, disabled between
// renders): that is a no-op, never a throw. Buttons are matched by dataset
// rather than an interpolated attribute selector so an exotic id can't build a
// malformed selector.
function openAccountMenuForId(id) {
    if (id === null || id === undefined || id === '') return false;
    const wanted = String(id);
    const scope = document.getElementById('accounts-list') || document;
    if (!scope || typeof scope.querySelectorAll !== 'function') return false;
    const buttons = Array.prototype.slice.call(scope.querySelectorAll('.btn-account-menu'));
    const btn = buttons.find(b => b && b.dataset && String(b.dataset.id) === wanted);
    if (!btn) return false;
    openAccountMenu(btn);
    return !!_openAccountMenu;
}

function _accountMenuItems() {
    if (!_openAccountMenu || !_openAccountMenu.menu) return [];
    return Array.prototype.slice.call(
        _openAccountMenu.menu.querySelectorAll('[role="menuitem"]')
    );
}

// delta = +1 (ArrowDown) / -1 (ArrowUp), wrapping at both ends. Focus starting
// on the kebab button (index -1) enters the list at the matching edge.
function _moveAccountMenuFocus(delta) {
    const items = _accountMenuItems();
    if (items.length === 0) return;
    const current = items.indexOf(document.activeElement);
    const next = current < 0
        ? (delta > 0 ? 0 : items.length - 1)
        : (current + delta + items.length) % items.length;
    if (items[next] && typeof items[next].focus === 'function') items[next].focus();
}

function initAccountMenuHandlers() {
    // Every render replaces #accounts-list wholesale, so a menu that was open
    // belongs to a detached node — drop the reference so no menu can be stuck
    // open (and no stale card keeps the raised z-index) across a poll.
    // Re-opening the menu on the FRESH nodes is the caller's job: see the
    // openAccountMenuId()/openAccountMenuForId() pair in rerenderAccountsView.
    _openAccountMenu = null;

    const list = document.getElementById('accounts-list');
    if (list) {
        // CAPTURE phase on purpose: the item handlers keep their own listeners
        // (.btn-edit-label even calls stopPropagation()), so closing on the
        // bubble would strand the menu open. Capture runs first and only
        // toggles a class, leaving every existing handler untouched — the
        // delete flow still renders its inline confirm into
        // .delete-confirm-container, which sits outside the menu.
        list.addEventListener('click', (e) => {
            const target = e.target;
            if (!target || typeof target.closest !== 'function') return;
            const kebab = target.closest('.btn-account-menu');
            if (kebab) {
                toggleAccountMenu(kebab);
                return;
            }
            if (target.closest('.account-menu')) closeAccountMenu();
        }, true);

        list.addEventListener('keydown', (e) => {
            if (!_openAccountMenu) return;
            const target = e.target;
            if (!target || typeof target.closest !== 'function') return;
            if (!target.closest('.account-menu') && !target.closest('.btn-account-menu')) return;
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                _moveAccountMenuFocus(1);
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                _moveAccountMenuFocus(-1);
            }
            // Enter/Space fall through to native <button> activation.
        });
    }

    // Delegation lives on #accounts-list (new element per render); these two
    // are on document and must never be stacked up render after render.
    if (_accountMenuGlobalsBound) return;
    _accountMenuGlobalsBound = true;

    // CAPTURE phase, for the same reason the list listener above uses it: an
    // outside element whose OWN handler calls stopPropagation() would otherwise
    // never let this run, stranding the menu open with its card still carrying
    // .menu-open (z-index 40) painting over the card beneath it. The per-card
    // .btn-refresh-single handler does exactly that, so opening card A's menu
    // and then clicking card B's refresh icon used to leave A stuck open.
    //
    // Capture is safe because of the two guards below. On the click that OPENS a
    // menu, _openAccountMenu is still null, so this returns before the list-level
    // capture handler opens it. On a click of ANOTHER card's kebab, the guard
    // compares against the currently tracked button — a different button falls
    // through to closeAccountMenu(), and the list handler then opens the new one,
    // which is the desired result.
    document.addEventListener('click', (e) => {
        if (!_openAccountMenu) return;
        const target = e.target;
        if (target && typeof target.closest === 'function') {
            // Clicks on the open menu's own button or panel are handled by the
            // list listener above; anything else counts as "outside".
            if (target.closest('.btn-account-menu') === _openAccountMenu.btn) return;
            if (target.closest('.account-menu') === _openAccountMenu.menu) return;
        }
        closeAccountMenu();
    }, true);

    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !_openAccountMenu) return;
        closeAccountMenu({ focusButton: true });
    });
}

// One sentence about the credentials, one about open sessions. The API's
// `existing_sessions` field is the only truthful source for the second part;
// the raw engine message is diagnostic text and never shown as the headline.
function sessionsFollowCopy(result, email, lead) {
    const sessions = result.existing_sessions;
    let tail;
    if (sessions === 'pending_next_activity') {
        tail = 'Open Claude Code sessions pick it up on their next message.';
    } else if (sessions === 'restart_required') {
        tail = 'Open Claude Code sessions could not be told. Restart them to use this account.';
    } else if (sessions === 'scoped_target') {
        tail = 'Only sessions launched for this account use it.';
    } else {
        tail = 'Open Claude Code sessions are unverified. Check /status inside a session.';
    }
    return `${lead} ${tail}`;
}

function showCredentialActivationResult(result, email) {
    if (result && result.restart_required) {
        showToast(
            result.message || 'Switched the active Codex account. Restart Codex to pick it up.',
            'info',
            7000,
        );
    } else if (result.status === 'committed') {
        showToast(sessionsFollowCopy(result, email, `Switched to ${email}.`), 'success', 6000);
    } else if (result.status === 'committed_degraded') {
        showToast(sessionsFollowCopy(result, email, `Switched to ${email}, but optional metadata is degraded.`), 'warning', 7000);
    } else if (result.status === 'observed_target_unfenced') {
        // The credentials are in every store. Whether open sessions follow
        // depends on the identity mirror, which the API reports explicitly.
        const follows = result.existing_sessions === 'pending_next_activity';
        showToast(
            sessionsFollowCopy(result, email, `Switched to ${email}.`),
            follows ? 'success' : 'warning',
            follows ? 6000 : 8000,
        );
    } else {
        showToast(result.message || `Credential activation returned ${result.status}.`, 'warning', 7000);
    }
}

async function pollCredentialOperation(actionId, operationId, pageSessionId, email) {
    const deadline = Date.now() + 120000;
    let announcedRunning = false;
    while (Date.now() < deadline) {
        try {
            const operation = await api.get(
                `/api/auth/credential-operations/${actionId}`,
                {
                    timeout: 10000,
                    headers: { 'X-Jacked-Page-Session': pageSessionId },
                },
            );
            if (operation.state === 'complete' && operation.result) {
                showCredentialActivationResult(operation.result, email);
                return;
            }
            if (operation.state === 'expired') {
                showToast(`Credential operation ${operation.operation_id} expired before a result was recorded.`, 'warning', 8000);
                return;
            }
            if (!announcedRunning) {
                showToast(`Credential operation ${operation.operation_id} is still running.`, 'info', 10000);
                announcedRunning = true;
            }
        } catch (statusError) {
            if (statusError.status === 404) {
                showToast(`Credential operation ${operationId} was not found.`, 'warning', 8000);
                return;
            }
            if (statusError.code !== 'TIMEOUT' && statusError.code !== 'NETWORK_ERROR') {
                showToast(statusError.message, 'error', 8000);
                return;
            }
        }
        await new Promise(resolve => setTimeout(resolve, 1500));
    }
    showToast(`Credential operation ${operationId} is still running. Check again before starting another switch.`, 'warning', 10000);
}

async function activateAccountFromDashboard(id, email, sourceButton) {
    if (window.jackedState._accountActionInFlight) {
        showToast('Another action is still running. Finish it, or cancel the sign-in banner above, then try again.', 'warning', 4000);
        return;
    }
    window.jackedState._accountActionInFlight = true;
    if (sourceButton) {
        sourceButton.disabled = true;
        sourceButton.textContent = 'Switching\u2026';
    }
    const actionId = crypto.randomUUID();
    const operationId = crypto.randomUUID();
    let pageSessionId = sessionStorage.getItem('jacked-page-session-id');
    if (!pageSessionId) {
        pageSessionId = crypto.randomUUID();
        sessionStorage.setItem('jacked-page-session-id', pageSessionId);
    }
    try {
        const result = await api.post(
            `/api/auth/accounts/${id}/use`,
            undefined,
            {
                headers: {
                    'X-Jacked-Action-Id': actionId,
                    'X-Jacked-Operation-Id': operationId,
                    'X-Jacked-Page-Session': pageSessionId,
                },
            },
        );
        showCredentialActivationResult(result, email);
        await loadActiveCredential();
    } catch (e) {
        const outcome = e.payload || {};
        if (e.code === 'TIMEOUT') {
            await pollCredentialOperation(
                actionId, operationId, pageSessionId, email
            );
        } else if (outcome.status === 'interactive_required') {
            showToast(outcome.message || 'Credential access needs foreground confirmation. Try again and approve the prompt.', 'warning', 8000);
        } else if (outcome.status === 'unsupported') {
            showToast(outcome.message || 'This Claude build is not certified for credential switching.', 'error', 8000);
        } else if (outcome.status === 'observed_target_unfenced') {
            showCredentialActivationResult(outcome, email);
        } else {
            showToast(outcome.message || e.message, 'error');
        }
    } finally {
        window.jackedState._accountActionInFlight = false;
        await refreshAndRender();
    }
}

function showAutoSwapRecommendation(data) {
    const targetId = data.to_account_id;
    if (!targetId) return;
    const accounts = window.jackedState.accounts || [];
    const target = accounts.find(account => account.id === targetId);
    const targetLabel = data.to_label || (target && (target.display_name || target.email)) || `account ${targetId}`;
    const reason = data.reason ? `: ${data.reason}` : '';
    const existing = document.getElementById('swap-recommendation-banner');
    if (existing) existing.remove();

    const banner = document.createElement('div');
    banner.id = 'swap-recommendation-banner';
    banner.setAttribute('role', 'region');
    banner.setAttribute('aria-label', 'Account switch recommendation');
    banner.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-50 bg-amber-950/95 border border-amber-600 rounded-lg px-5 py-3 shadow-lg max-w-xl flex items-center gap-3';
    const text = document.createElement('span');
    text.setAttribute('role', 'status');
    text.setAttribute('aria-live', 'polite');
    text.className = 'text-sm text-amber-100';
    text.textContent = `Account switch recommended: ${targetLabel}${reason}`;
    const useButton = document.createElement('button');
    useButton.className = 'text-xs px-3 py-1.5 rounded bg-amber-500 text-slate-950 font-semibold hover:bg-amber-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200';
    useButton.textContent = 'Use Account';
    useButton.onclick = async function() {
        await activateAccountFromDashboard(
            String(targetId),
            (target && target.email) || targetLabel,
            useButton,
        );
        if (banner.parentNode) banner.remove();
    };
    const close = document.createElement('button');
    close.className = 'text-amber-300 hover:text-white text-lg leading-none ml-auto rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200';
    close.textContent = '\u00d7';
    close.setAttribute('aria-label', 'Dismiss account switch recommendation');
    close.onclick = function() { banner.remove(); };
    banner.appendChild(text);
    banner.appendChild(useButton);
    banner.appendChild(close);
    document.body.appendChild(banner);
}

function bindAccountEvents() {
    initAccountMenuHandlers();
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
                if (!result.isConfirmed && !result.isDenied) return;  // cancelled
                if (window.jackedState._accountActionInFlight) {
                    showToast('Another action started — please try again', 'warning', 2000);
                    return;
                }
                if (result.isDenied) {
                    startAddCodexFlow();   // Codex
                } else {
                    startAddAccountFlow(); // Claude
                }
            } catch (err) {
                console.error('Add account confirmation error:', err);
                showToast('Something went wrong — please try again', 'error');
            }
        });
    });

    // Re-auth rows (kebab menu). Same targeted flow the token pill uses:
    // /accounts/{id}/reauth carries the account identity, so the launcher
    // opens that account's browser profile with a login hint and the callback
    // updates the existing row. The untargeted add-account flow has no
    // identity, falls back to an incognito window, and can create a duplicate
    // account instead of re-authing the one that was clicked.
    document.querySelectorAll('.btn-reauth').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (window.jackedState._accountActionInFlight) {
                showToast('Another action is in progress', 'warning', 2000);
                return;
            }
            const id = btn.dataset.id;
            const email = btn.dataset.email || '';
            try {
                const result = await _confirmReauth(email);
                if (!result.isConfirmed) return;
                if (window.jackedState._accountActionInFlight) {
                    showToast('Another action started — please try again', 'warning', 2000);
                    return;
                }
                startReauthFlow(id, email);
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

    // "Use Account" requests a default-credential activation. The server's
    // evidence-qualified outcome determines what the UI may claim.
    document.querySelectorAll('.btn-use-account').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.id;
            const email = btn.dataset.email || '';
            await activateAccountFromDashboard(id, email, btn);
        });
    });

    // Refresh All Usage button — userInitiated exempts it from the automated
    // fire-rate guard (the server throttles repeat manual passes gracefully).
    const refreshAllBtn = document.getElementById('btn-refresh-all-usage');
    if (refreshAllBtn) {
        refreshAllBtn.addEventListener('click', () => _triggerUsageRefresh({ userInitiated: true }).catch(() => {}));
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
async function _triggerUsageRefresh(options) {
    const userInitiated = !!(options && options.userInitiated);
    // Global fire-rate guard for ALL automated callers (auto-tick, overdue fires,
    // anything else): never start a bulk pass within the floor of the previous start,
    // regardless of which timer or tab-state bug got us here. Manual clicks are exempt —
    // the server throttles repeat passes gracefully with cached results.
    if (!userInitiated && _lastBulkStartedAt !== null
            && (Date.now() - _lastBulkStartedAt) < _AUTO_REFRESH_FLOOR_SECS * 1000) {
        if (!_rateGuardWarned) {
            _rateGuardWarned = true;
            console.warn('jacked: automated usage refresh suppressed — previous bulk pass started under '
                + _AUTO_REFRESH_FLOOR_SECS + 's ago');
        }
        return;
    }
    if (window.jackedState._usageRefreshInProgress) {
        showToast('Usage refresh already in progress', 'warning', 2000);
        return;
    }
    const btn = document.getElementById('btn-refresh-all-usage');
    window.jackedState._usageRefreshInProgress = true;
    _lastBulkStartedAt = Date.now();
    _rateGuardWarned = false;
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
        // user_initiated tells the server whether to apply the short manual floor
        // (human click) or the full automatic rate-limit ceiling (auto-refresh timer).
        const params = new URLSearchParams();
        if (ss.auto_swap_enabled && activeId) params.set('skip_account', activeId);
        if (userInitiated) params.set('user_initiated', '1');
        const qs = params.toString() ? '?' + params.toString() : '';
        // Bulk refresh runs ~2s per account server-side — needs more headroom than the
        // default 60s API timeout when many accounts are configured.
        const result = await api.post('/api/auth/accounts/refresh-all-usage' + qs, undefined, { timeout: 300000 });
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
            const secs = _getAutoRefreshSeconds();
            if (secs <= 0) {
                // Interval flipped to OFF mid-pass (the localStorage key is shared across
                // tabs — another tab's circuit breaker can write '0' under us). Never reset
                // the countdown to 0: that makes every subsequent 1s tick fire a bulk pass.
                _autoRefreshCircuitBreak();
            } else {
                _autoRefreshCountdown = Math.max(secs, _AUTO_REFRESH_FLOOR_SECS);
                _lastAutoRefreshAt = Date.now();
            }
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
        if (e && e.code === 'REFRESH_IN_PROGRESS') {
            // Server bulk lock held by another tab (or a manual click there) —
            // benign contention, not a backend failure. Info, not error.
            showToast('Another usage refresh is already in progress', 'info', 3000);
        } else {
            showToast(e.message, 'error');
        }
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
    const startedAt = _singleRefreshInFlight.get(accountId);
    if (startedAt !== undefined) {
        if (Date.now() - startedAt < _SINGLE_REFRESH_STALE_MS) {
            showToast('Refresh already running for this account', 'warning', 2000);
            return;
        }
        _singleRefreshInFlight.delete(accountId);
    }

    const card = document.querySelector('[data-account-id="' + accountId + '"]');
    if (!card) return;

    const startStamp = Date.now();
    _singleRefreshInFlight.set(accountId, startStamp);
    card.classList.remove('usage-done', 'usage-failed');
    card.classList.add('usage-checking');
    if (typeof _usageInjectOverlay === 'function') {
        _usageInjectOverlay(card, 'checking', 'Checking usage\u2026');
    }
    // Watchdog: if WS events never clear the class, this returns the card to
    // idle after _CHECKING_TIMEOUT_MS so the user can click refresh again.
    // (api.post itself times out after 60s, but WS-driven classes can still leak.)
    if (typeof _armCheckingWatchdog === 'function') _armCheckingWatchdog(accountId);

    try {
        await api.post('/api/auth/accounts/' + accountId + '/refresh-usage');
        if (typeof _clearCheckingWatchdog === 'function') _clearCheckingWatchdog(accountId);
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
        if (typeof _clearCheckingWatchdog === 'function') _clearCheckingWatchdog(accountId);
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
        // Only clear our own entry — a stale-superseded call settling late must not
        // delete the timestamp of the refresh that replaced it.
        if (_singleRefreshInFlight.get(accountId) === startStamp) {
            _singleRefreshInFlight.delete(accountId);
        }
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

// Circuit breaker shared by every auto-refresh failure / zero-interval path:
// persist OFF, sync the dropdown, and stop this tab's timer.
function _autoRefreshCircuitBreak() {
    localStorage.setItem('jacked_auto_refresh_interval', '0');
    const sel = document.getElementById('sel-auto-refresh');
    if (sel) sel.value = '0';
    _stopAutoRefresh();
}

function _changeAutoRefreshInterval(newSecs, opts) {
    // Optional one-shot phase offset (storage-listener arms only): tabs armed by
    // the same localStorage write share a wall-clock phase and fire in the same
    // ~1s window every cycle, contending for the server bulk lock.
    const phaseJitterSecs = (opts && opts.phaseJitterSecs) || 0;
    if (_autoRefreshInterval) clearInterval(_autoRefreshInterval);
    _autoRefreshInterval = null;

    // Defense in depth: callers route 0 through _stopAutoRefresh already, but a
    // 0/negative interval must never arm a timer (countdown 0 = bulk pass every
    // tick), and nothing may run faster than the floor.
    if (!newSecs || newSecs <= 0) {
        _stopAutoRefresh();
        return;
    }
    newSecs = Math.max(newSecs, _AUTO_REFRESH_FLOOR_SECS);

    if (!_lastAutoRefreshAt) {
        // Coming from OFF state or cold start — always start fresh, no immediate fire
        _autoRefreshCountdown = newSecs + phaseJitterSecs;
        _lastAutoRefreshAt = Date.now();
        _autoRefreshInterval = setInterval(_autoRefreshTick, 1000);
        _updateRefreshBtnLabel();
        return;
    }

    const elapsedSecs = Math.floor((Date.now() - _lastAutoRefreshAt) / 1000);
    const remaining = newSecs - elapsedSecs;

    if (remaining <= 0) {
        // Overdue at new interval — fire immediately if not already refreshing.
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
            // Same circuit breaker as _autoRefreshTick — a failed overdue fire must not
            // leave the timer armed against a broken backend (the old bare .catch let it spin).
            // REFRESH_IN_PROGRESS is the exception: another tab's pass holds the server bulk
            // lock — benign contention, skip this cycle (the countdown is already armed above).
            _triggerUsageRefresh().catch((e) => {
                if (e && e.code === 'REFRESH_IN_PROGRESS') return;
                _autoRefreshCircuitBreak();
            });
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

    const secs = _getAutoRefreshSeconds();
    if (secs <= 0) {
        // localStorage is shared across tabs: another tab's circuit breaker (or the user
        // there) wrote '0'. Stop OUR timer too — ticking on a 0 interval fires a bulk
        // pass every second.
        _autoRefreshCircuitBreak();
        return;
    }

    _autoRefreshCountdown--;
    if (_autoRefreshCountdown > 0) {
        _updateRefreshBtnLabel();
        return;
    }

    // Pre-arm the next cycle at >= the floor BEFORE firing, so a skipped or failed pass
    // can't leave the countdown at 0 and refire on the very next tick. The success path
    // in _triggerUsageRefresh re-resets it anyway.
    _autoRefreshCountdown = Math.max(secs, _AUTO_REFRESH_FLOOR_SECS);
    try {
        await _triggerUsageRefresh();
    } catch (e) {
        if (e && e.code === 'REFRESH_IN_PROGRESS') {
            // Another tab holds the server bulk lock — benign contention, not a
            // backend failure. The countdown was pre-armed above, so skipping
            // this cycle is safe. Circuit-breaking here would write '0' to the
            // SHARED localStorage key and the storage listener would then kill
            // auto-refresh in EVERY tab.
            return;
        }
        showToast('Auto-refresh failed: ' + e.message, 'error');
        _autoRefreshCircuitBreak();
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

// Cross-tab sync: 'jacked_auto_refresh_interval' is shared across tabs, and the circuit
// breaker in one tab writes '0' to it. Without this listener, other tabs keep their timer
// armed and read the new value on their own schedule — the runaway path in the 2026-06
// fetch storm (a running timer reading 0 fires a bulk pass on every 1s tick). Re-sync
// this tab's timer and dropdown whenever the key changes elsewhere.
window.addEventListener('storage', (e) => {
    if (!e || e.key !== 'jacked_auto_refresh_interval') return;
    const secs = _getAutoRefreshSeconds();
    const sel = document.getElementById('sel-auto-refresh');
    if (sel) sel.value = String(secs);
    if (secs <= 0) {
        _stopAutoRefresh();
    } else {
        // De-phase from the tab that wrote the key: arming an identical countdown
        // at the same wall-clock instant makes both tabs fire together, and the
        // loser of the server bulk-lock race eats a 429 on every cycle.
        _changeAutoRefreshInterval(secs, { phaseJitterSecs: Math.floor(Math.random() * 11) });
    }
});

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

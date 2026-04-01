/**
 * jacked web dashboard — WebSocket event bus client
 *
 * General-purpose WebSocket client with exponential backoff reconnect
 * and typed event dispatch.  Any component can register handlers for
 * specific event types (e.g. "credentials_changed") or "*" for all.
 */
const jackedWS = {
    ws: null,
    handlers: {},           // type -> [callback, ...]
    reconnectDelay: 1000,   // Exponential backoff: 1s -> 2s -> 4s -> ... -> 15s max
    maxReconnectDelay: 15000,
    _reconnectTimer: null,
    _messageQueue: Promise.resolve(),  // Serializes async handler execution

    connect() {
        // Clean up any pending reconnect timer
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }

        const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
        try {
            this.ws = new WebSocket(`${protocol}//${location.host}/api/ws`);
        } catch (e) {
            console.error('[WS] Failed to create WebSocket:', e);
            this._scheduleReconnect();
            return;
        }

        this.ws.onopen = () => {
            console.log('[WS] Connected');
            this.reconnectDelay = 1000;  // Reset backoff on successful connect
            // Stop logs fallback polling now that WS is live
            if (typeof stopLogsFallbackPolling === 'function') stopLogsFallbackPolling();
        };

        this.ws.onmessage = (event) => {
            let msg;
            try {
                msg = JSON.parse(event.data);
            } catch {
                return;
            }
            // Skip server keepalive pings silently
            if (msg.type === 'ping') return;

            const callbacks = this.handlers[msg.type] || [];
            const wildcards = this.handlers['*'] || [];
            const handlers = [...callbacks, ...wildcards];
            this._messageQueue = this._messageQueue.then(async () => {
                for (const cb of handlers) {
                    try { await cb(msg); } catch (e) { console.error('[WS] Handler error:', e); }
                }
            });
        };

        this.ws.onclose = () => {
            console.log('[WS] Disconnected — reconnecting in', this.reconnectDelay + 'ms');
            // Insert disconnect marker in server logs tab
            if (typeof _srvInsertDisconnectMarker === 'function') _srvInsertDisconnectMarker();
            // Reset polling to fast interval since WS is no longer providing updates
            if (typeof _wsPollingAdjusted !== 'undefined') _wsPollingAdjusted = false;
            if (typeof stopPolling === 'function' && typeof startPolling === 'function') {
                stopPolling();
                startPolling();
            }
            // Start logs fallback polling while WS is down
            if (typeof startLogsFallbackPolling === 'function') startLogsFallbackPolling();
            // Clean up stuck usage refresh states (no completion messages will arrive)
            document.querySelectorAll('[data-account-id].usage-checking, [data-account-id].usage-queued').forEach(
                el => el.classList.remove('usage-checking', 'usage-queued')
            );
            document.querySelectorAll('.usage-status-overlay').forEach(el => el.remove());
            // If upgrade modal is open and WS drops, server is likely restarting — begin health poll
            if (document.getElementById('upgrade-modal') && typeof _startHealthPolling === 'function') {
                _startHealthPolling();
            }
            this._scheduleReconnect();
        };

        this.ws.onerror = () => {
            // onclose will fire after onerror, so just close cleanly
            try { this.ws.close(); } catch {}
        };
    },

    _scheduleReconnect() {
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this.connect();
        }, this.reconnectDelay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, this.maxReconnectDelay);
    },

    /**
     * Force reconnect — used by visibility change handler when
     * the page comes back to foreground and the socket may be zombie.
     */
    forceReconnect() {
        if (this.ws) {
            try { this.ws.close(); } catch {}
        }
        this._messageQueue = Promise.resolve();  // Discard stale queued messages
        this.reconnectDelay = 1000;
        this.connect();
    },

    /**
     * Check if WebSocket is currently connected.
     */
    isConnected() {
        return this.ws && this.ws.readyState === WebSocket.OPEN;
    },

    /**
     * Register a handler for a specific event type.
     * Use "*" to receive all events.
     */
    on(type, callback) {
        if (!this.handlers[type]) this.handlers[type] = [];
        this.handlers[type].push(callback);
    },

    /**
     * Remove a previously registered handler.
     */
    off(type, callback) {
        if (!this.handlers[type]) return;
        this.handlers[type] = this.handlers[type].filter(cb => cb !== callback);
    },
};

// ---------------------------------------------------------------------------
// Persistent credential change toast (non-auto-dismissing)
// ---------------------------------------------------------------------------
function showPersistentCredentialToast(timestamp) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    // Remove any existing credential toast (no stacking)
    const existing = container.querySelector('[data-credential-toast]');
    if (existing) existing.remove();

    const time = timestamp
        ? new Date(timestamp * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })
        : 'just now';

    const toast = document.createElement('div');
    toast.className = 'toast border rounded-lg px-4 py-3 text-sm shadow-lg max-w-full md:max-w-sm bg-blue-900 border-blue-700 text-blue-200 flex items-center justify-between gap-3';
    toast.setAttribute('data-credential-toast', 'true');
    toast.innerHTML = `
        <span>Credentials changed at ${escapeHtml(time)}</span>
        <button class="text-blue-400 hover:text-white text-xs font-medium px-2 py-1 rounded hover:bg-blue-800 transition-colors" onclick="this.closest('.toast').remove()">Dismiss</button>
    `;
    container.appendChild(toast);
}

// ---------------------------------------------------------------------------
// Default event handlers — registered before connect() is called
// ---------------------------------------------------------------------------
jackedWS.on('credentials_changed', async (msg) => {
    if (typeof loadActiveCredential === 'function') await loadActiveCredential();
    if (window.jackedState && window.jackedState.activeRoute === 'accounts') {
        // Suppress re-renders during bulk usage refresh — the refresh handler
        // calls refreshAndRender() itself when it finishes.
        if (window.jackedState && window.jackedState._usageRefreshInProgress) {
            showPersistentCredentialToast(msg.timestamp);
            return;
        }
        if (typeof refreshAndRender === 'function') await refreshAndRender();
    }
    showPersistentCredentialToast(msg.timestamp);
});

// ---------------------------------------------------------------------------
// Usage refresh — overlay banner helpers (DOM API, no innerHTML)
// ---------------------------------------------------------------------------

/**
 * Create an SVG icon element from a path definition.
 * @param {string} pathD - SVG path d attribute.
 * @param {string} [cls] - Additional CSS classes.
 * @returns {SVGElement}
 */
function _usageCreateIcon(pathD, cls) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'status-icon w-3.5 h-3.5' + (cls ? ' ' + cls : ''));
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('d', pathD);
    svg.appendChild(path);
    return svg;
}

// SVG path data for status icons
const _USAGE_ICON_PATHS = {
    queued: 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z',       // clock
    done: 'M5 13l4 4L19 7',                                          // checkmark
    failed: 'M6 18L18 6M6 6l12 12',                                  // X
};

// Overlay display durations (ms) — also referenced by account-actions.js
const _OVERLAY_DONE_MS = 2000;
const _OVERLAY_FAILED_MS = 3000;
const _OVERLAY_FADE_FALLBACK_MS = 500;  // Must be >= CSS overlayFadeOut duration (400ms)

/**
 * Inject a status overlay banner at the top of a card.
 * Uses DOM API exclusively — no innerHTML.
 */
function _usageInjectOverlay(card, status, text) {
    const existing = card.querySelector('.usage-status-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.className = 'usage-status-overlay status-' + status;
    overlay.setAttribute('role', status === 'failed' ? 'alert' : 'status');
    overlay.setAttribute('aria-live', status === 'failed' ? 'assertive' : 'polite');

    // Icon: spinner div for checking, SVG for others
    if (status === 'checking') {
        const spinner = document.createElement('div');
        spinner.className = 'status-spinner';
        overlay.appendChild(spinner);
    } else if (_USAGE_ICON_PATHS[status]) {
        overlay.appendChild(_usageCreateIcon(_USAGE_ICON_PATHS[status]));
    }

    const span = document.createElement('span');
    span.textContent = text;
    overlay.appendChild(span);

    card.prepend(overlay);
}

/**
 * Remove overlay with fade-out animation.
 */
function _usageRemoveOverlay(card) {
    const overlay = card.querySelector('.usage-status-overlay');
    if (!overlay) return;
    overlay.classList.add('removing');
    overlay.addEventListener('animationend', () => overlay.remove(), { once: true });
    // Fallback if animation doesn't fire (reduced motion, etc.)
    setTimeout(() => { if (overlay.parentNode) overlay.remove(); }, _OVERLAY_FADE_FALLBACK_MS);
}

/**
 * Surgically update usage bars + cache age in an existing card DOM
 * without replacing the entire card (preserves event listeners).
 *
 * Depends on data attributes set in accounts.js card template:
 *   [data-usage-container] — wraps 5h + 7d bar elements (children 0, 1)
 *   [data-cache-age]       — the cache age span in the info row
 */
function _usageUpdateCardDOM(card, acctData) {
    if (!acctData) return;

    // Merge into window.jackedState.accounts (preserves any client-side fields)
    if (window.jackedState && window.jackedState.accounts) {
        const idx = window.jackedState.accounts.findIndex(a => a.id === acctData.id);
        if (idx >= 0) Object.assign(window.jackedState.accounts[idx], acctData);
    }

    // Re-render usage bars using existing renderUsageBar function
    const usageContainer = card.querySelector('[data-usage-container]');
    if (usageContainer &&
        typeof renderUsageBar === 'function' &&
        typeof computeElapsedFraction5h === 'function' &&
        typeof computeElapsedFraction7d === 'function') {

        const elapsed5h = computeElapsedFraction5h(acctData.cached_5h_resets_at);
        const elapsed7d = computeElapsedFraction7d(acctData.cached_7d_resets_at);

        // First two children of [data-usage-container] are the 5h and 7d bar wrappers
        const bar5h = usageContainer.children[0];
        const bar7d = usageContainer.children[1];
        if (bar5h && bar7d) {
            const temp5h = document.createElement('div');
            temp5h.insertAdjacentHTML('afterbegin', renderUsageBar(acctData.cached_usage_5h, acctData.cached_5h_resets_at, elapsed5h, '5h limit'));
            if (temp5h.firstElementChild) bar5h.replaceWith(temp5h.firstElementChild);

            const temp7d = document.createElement('div');
            temp7d.insertAdjacentHTML('afterbegin', renderUsageBar(acctData.cached_usage_7d, acctData.cached_7d_resets_at, elapsed7d, '7d limit'));
            if (temp7d.firstElementChild) bar7d.replaceWith(temp7d.firstElementChild);
        }
    }

    // Update cache age text via data attribute
    if (typeof renderCacheAge === 'function') {
        const cacheSpan = card.querySelector('[data-cache-age]');
        if (cacheSpan) {
            const tempCache = document.createElement('div');
            tempCache.insertAdjacentHTML('afterbegin', renderCacheAge(acctData.usage_cached_at, acctData.id));
            const newCache = tempCache.firstElementChild;
            if (newCache) cacheSpan.replaceWith(newCache);
        }
    }
}

// ---------------------------------------------------------------------------
// Usage refresh — WebSocket event handlers
// ---------------------------------------------------------------------------

/** Mark all accounts as queued when bulk refresh starts.
 *  Skips cards already marked queued (avoids double-animation from HTTP + WS race). */
jackedWS.on('usage_refresh_started', (msg) => {
    const d = msg.payload || msg;
    const ids = d.account_ids;
    if (!Array.isArray(ids)) return;

    for (const id of ids) {
        if (!Number.isInteger(id)) continue;
        const card = document.querySelector('[data-account-id="' + id + '"]');
        if (!card || card.classList.contains('usage-queued')) continue;
        card.classList.remove('usage-checking', 'usage-done', 'usage-failed');
        card.classList.add('usage-queued');
        _usageInjectOverlay(card, 'queued', 'Waiting\u2026');
    }
});

/** Per-account progress: checking → done/failed with immediate data update */
jackedWS.on('usage_refresh_progress', (msg) => {
    const d = msg.payload || msg;
    if (!Number.isInteger(d.account_id)) return;
    const validStatuses = ['checking', 'done', 'failed'];
    if (!validStatuses.includes(d.status)) return;

    const card = document.querySelector('[data-account-id="' + d.account_id + '"]');
    if (card) {
        card.classList.remove('usage-checking', 'usage-done', 'usage-failed', 'usage-queued');
        card.classList.add('usage-' + d.status);

        if (d.status === 'checking') {
            _usageInjectOverlay(card, 'checking', 'Checking usage\u2026');
        } else if (d.status === 'done') {
            _usageInjectOverlay(card, 'done', 'Updated!');
            // Surgically patch usage data in the card DOM
            if (d.account_data) _usageUpdateCardDOM(card, d.account_data);
            setTimeout(() => {
                _usageRemoveOverlay(card);
                card.classList.remove('usage-done');
            }, _OVERLAY_DONE_MS);
        } else if (d.status === 'failed') {
            _usageInjectOverlay(card, 'failed', 'Failed');
            setTimeout(() => {
                _usageRemoveOverlay(card);
                card.classList.remove('usage-failed');
            }, _OVERLAY_FAILED_MS);
        }
    }

    // Update button progress text
    const btn = document.getElementById('btn-refresh-all-usage');
    if (btn && Number.isInteger(d.progress) && Number.isInteger(d.total) && d.total > 0) {
        btn.textContent = '';
        const spinner = typeof _createInlineSpinner === 'function'
            ? _createInlineSpinner()
            : document.createTextNode('');
        btn.appendChild(spinner);
        btn.appendChild(document.createTextNode(' Refreshing ' + d.progress + '/' + d.total + '\u2026'));
    }
});

jackedWS.on('logs_changed', (msg) => {
    if (typeof refreshCurrentLogsSubTab === 'function') {
        refreshCurrentLogsSubTab(msg.payload?.tables);
    }
});

jackedWS.on('server_logs_changed', (msg) => {
    if (typeof _srvHandleWsEntries === 'function') {
        _srvHandleWsEntries(msg.payload?.entries);
    }
});

/** Upgrade progress events */
jackedWS.on('upgrade_started', () => {
    if (typeof _showUpgradeModal === 'function') _showUpgradeModal('Upgrading claude-jacked\u2026');
});

jackedWS.on('upgrade_progress', (msg) => {
    const d = msg.payload || msg;
    if (typeof _updateUpgradeModal === 'function') _updateUpgradeModal(d.message || 'Working\u2026');
});

jackedWS.on('upgrade_complete', () => {
    if (typeof _startHealthPolling === 'function') _startHealthPolling();
});

jackedWS.on('upgrade_failed', (msg) => {
    const d = msg.payload || msg;
    if (typeof _showUpgradeError === 'function') _showUpgradeError(d.error || 'Upgrade failed');
});

jackedWS.on('auto_swap_triggered', (msg) => {
    const d = msg.payload || msg;
    const toEmail = d.to_email || 'another account';
    const reason = d.reason || '';
    // Dismiss exhaustion banner — a swap means we found a target
    window.jackedState._exhaustionData = null;
    if (typeof renderExhaustionBanner === 'function') renderExhaustionBanner();
    // Persistent swap banner — dismissible, auto-hides after 5 minutes
    const existing = document.getElementById('swap-banner');
    if (existing) existing.remove();
    const banner = document.createElement('div');
    banner.id = 'swap-banner';
    banner.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-50 bg-teal-900/95 border border-teal-600 rounded-lg px-5 py-3 shadow-lg max-w-lg flex items-center gap-3';
    const text = document.createElement('span');
    text.className = 'text-sm text-teal-100';
    text.textContent = 'Auto-swapped to ' + toEmail + (reason ? ' \u2014 ' + reason : '');
    const close = document.createElement('button');
    close.className = 'text-teal-400 hover:text-white text-lg leading-none ml-auto';
    close.textContent = '\u00d7';
    close.onclick = function() { banner.remove(); };
    banner.appendChild(text);
    banner.appendChild(close);
    document.body.appendChild(banner);
    setTimeout(function() { if (banner.parentNode) banner.remove(); }, 300000);
    if (typeof loadActiveCredential === 'function') loadActiveCredential();
    if (typeof refreshAndRender === 'function') refreshAndRender();
});

jackedWS.on('all_accounts_exhausted', (msg) => {
    const d = msg.payload || msg;
    window.jackedState._exhaustionData = d;
    if (typeof renderExhaustionBanner === 'function') renderExhaustionBanner();
});

jackedWS.on('sessions_changed', async () => {
    if (typeof loadActiveSessions === 'function') await loadActiveSessions();
    if (typeof loadAccounts === 'function') await loadAccounts();
    // Suppress re-render during bulk usage refresh — refresh handler calls refreshAndRender() itself
    if (window.jackedState && window.jackedState._usageRefreshInProgress) return;
    if (typeof rerenderAccountsView === 'function') rerenderAccountsView();
});

// Adjust polling interval when WebSocket connects (less aggressive polling)
let _wsPollingAdjusted = false;
jackedWS.on('*', () => {
    if (!_wsPollingAdjusted && jackedWS.isConnected()) {
        _wsPollingAdjusted = true;
        if (typeof stopPolling === 'function' && typeof startPolling === 'function') {
            stopPolling();
            startPolling();
        }
    }
});

// Visibility change handler — reconnect when page comes back to foreground
// (mobile tab suspension, phone screen unlock, etc.)
// Debounced to prevent API hammering on rapid tab switches.
let _lastVisibilityRefresh = 0;
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        // Page is back in foreground — socket may be zombie
        if (!jackedWS.isConnected()) {
            _wsPollingAdjusted = false;
            jackedWS.forceReconnect();
        }
        // Debounce: skip data refresh if we did one within the last 5 seconds
        const now = Date.now();
        if (now - _lastVisibilityRefresh < 5000) return;
        _lastVisibilityRefresh = now;

        // Skip full re-render if bulk usage refresh is in progress — it will call refreshAndRender() itself
        if (window.jackedState && window.jackedState._usageRefreshInProgress) return;
        if (typeof loadAllData === 'function') {
            loadAllData().then(() => {
                if (typeof renderRoute === 'function' && typeof getRoute === 'function') {
                    renderRoute(getRoute());
                }
            });
        }
    }
});

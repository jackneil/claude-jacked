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
            // Reset polling to fast interval since WS is no longer providing updates
            if (typeof _wsPollingAdjusted !== 'undefined') _wsPollingAdjusted = false;
            if (typeof stopPolling === 'function' && typeof startPolling === 'function') {
                stopPolling();
                startPolling();
            }
            // Start logs fallback polling while WS is down
            if (typeof startLogsFallbackPolling === 'function') startLogsFallbackPolling();
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
        <span>Credentials changed at ${typeof escapeHtml === 'function' ? escapeHtml(time) : time}</span>
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
        if (typeof refreshAndRender === 'function') await refreshAndRender();
    }
    showPersistentCredentialToast(msg.timestamp);
});

jackedWS.on('logs_changed', (msg) => {
    if (typeof refreshCurrentLogsSubTab === 'function') {
        refreshCurrentLogsSubTab(msg.payload?.tables);
    }
});

jackedWS.on('sessions_changed', async () => {
    if (typeof loadActiveSessions === 'function') await loadActiveSessions();
    if (typeof loadAccounts === 'function') await loadAccounts();
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

        if (typeof loadAllData === 'function') {
            loadAllData().then(() => {
                if (typeof renderRoute === 'function' && typeof getRoute === 'function') {
                    renderRoute(getRoute());
                }
            });
        }
    }
});

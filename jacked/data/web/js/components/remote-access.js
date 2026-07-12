/**
 * jacked web dashboard - remote-access (network bind) card
 *
 * Lives at the TOP of Settings > Advanced. Flipping the master toggle or the
 * scope picker persists to the settings DB (PUT) and then restarts the jacked
 * service (POST) so the new bind takes effect. The dashboard has NO login, so
 * every change is gated behind a Swal confirm that spells out the real exposure
 * story before anything is saved.
 *
 * The live status line is driven exclusively by the server's `effective` bind
 * state (what it ACTUALLY bound at startup), never by the toggle's saved value,
 * so the UI cannot lie about where the server is listening.
 *
 * Restart overlay reuses header.js's single upgrade-modal pattern
 * (_showUpgradeModal / _startHealthPolling / _showUpgradeError /
 * _showRestartTerminal). The lockout case (a remote browser turning remote
 * access OFF) shows a terminal message and deliberately does NOT poll, because
 * that page will never reconnect.
 */

// Last-persisted state, mirrored from GET /api/settings/remote-access. `effective`
// is the live bind the server reported; the status line reads only from it.
const _remoteAccessState = {
    enabled: false,
    scope: 'tailscale',
    effective: null,
    loaded: false,
};

const _REMOTE_ACCESS_URL = '/api/settings/remote-access';
const _REMOTE_ACCESS_RESTART_URL = '/api/settings/remote-access/restart';
const _REMOTE_ACCESS_DANGER = '#dc2626'; // red-600, matches the danger palette

// --- Location helpers (guarded so the node test-harness can stub them) ---

function _remoteAccessLocation() {
    if (typeof window !== 'undefined' && window.location) return window.location;
    if (typeof location !== 'undefined') return location;
    return { hostname: '', port: '' };
}

function _remoteAccessPort() {
    return _remoteAccessLocation().port || '8321';
}

function _remoteAccessIsLoopbackHost(hostname) {
    const host = hostname || '';
    return host === '' || host === '127.0.0.1' || host === 'localhost' || host === '::1';
}

// Parse a hostname as an IPv4 dotted-quad literal. Returns [o0,o1,o2,o3] when the
// string is exactly four numeric octets each in 0..255, else null. MagicDNS names
// (*.ts.net), single-label shortnames, IPv6, and malformed strings all return null
// and never throw.
function _remoteAccessParseIPv4(hostname) {
    const host = (hostname == null) ? '' : String(hostname);
    const parts = host.split('.');
    if (parts.length !== 4) return null;
    const octets = [];
    for (const part of parts) {
        if (!/^[0-9]{1,3}$/.test(part)) return null;
        const n = Number(part);
        if (n > 255) return null;
        octets.push(n);
    }
    return octets;
}

// Tailscale's tailnet lives in the CGNAT range 100.64.0.0/10: first octet 100,
// second octet 64..127 inclusive. Anything else (including 100.63.x / 100.128.x)
// is outside the tailnet.
function _remoteAccessIsCGNAT(hostname) {
    const octets = _remoteAccessParseIPv4(hostname);
    return !!octets && octets[0] === 100 && octets[1] >= 64 && octets[1] <= 127;
}

// Deterministic, SHARED lockout rule - the single source of truth used by BOTH
// the confirm/overlay flow in this file AND the websocket.js restart_started
// handler, so the two can never disagree about whether applying `pending` will
// strand THIS browser:
//
//   - Loopback origin (127.0.0.1 / localhost / ::1 / empty): never a lockout;
//     loopback survives every re-bind.
//   - pending.enabled === false: the server re-binds loopback only, dropping
//     every non-loopback origin -> lockout.
//   - pending.scope === 'all': binds 0.0.0.0, covers every origin -> never a
//     lockout.
//   - pending.scope === 'tailscale': keeps loopback plus the tailnet
//     (100.64.0.0/10). MagicDNS names resolve to a tailnet IP and stay covered.
//     A bare LAN/other IPv4 literal OUTSIDE CGNAT is dropped by the tailnet-only
//     bind -> lockout.
function remoteAccessLockout(pending, hostname) {
    const p = pending || {};
    if (_remoteAccessIsLoopbackHost(hostname)) return false;
    if (p.enabled === false) return true;
    if (p.scope === 'all') return false;
    if (p.scope === 'tailscale') {
        return _remoteAccessParseIPv4(hostname) !== null && !_remoteAccessIsCGNAT(hostname);
    }
    return false;
}

// Shared across component files via a window global (same pattern the rest of the
// dashboard uses); websocket.js reads window.remoteAccessLockout.
if (typeof window !== 'undefined') {
    window.remoteAccessLockout = remoteAccessLockout;
}

// --- HTML builders (pure functions, unit-tested directly on their output) ---

function _remoteAccessLoadingHTML() {
    return `
        <div class="flex items-center gap-3 py-1">
            <div class="spinner"></div>
            <span class="text-slate-400 text-sm">Loading remote access settings...</span>
        </div>
    `;
}

function _remoteAccessErrorHTML(message) {
    return `
        <div class="flex items-center justify-between gap-3">
            <div class="min-w-0 flex-1">
                <h3 class="text-sm font-semibold text-white">Remote access</h3>
                <p class="text-xs text-red-400 mt-1">Could not load remote access settings: ${escapeHtml(message)}</p>
            </div>
            <button id="btn-remote-access-retry" class="text-xs px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-white rounded transition active:scale-[0.96] flex-shrink-0">Retry</button>
        </div>
    `;
}

function _remoteAccessScopePickerHTML(scope) {
    const tsActive = scope !== 'all';
    const allActive = scope === 'all';
    return `
        <div class="mt-4 space-y-2" role="radiogroup" aria-label="Remote access scope">
            <label class="flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors active:scale-[0.99] ${tsActive ? 'border-blue-500 bg-blue-900/20' : 'border-slate-700 hover:border-slate-600'}">
                <input type="radio" name="remote-access-scope" value="tailscale" class="mt-0.5 accent-blue-500" ${tsActive ? 'checked' : ''}>
                <span class="min-w-0 flex-1">
                    <span class="flex items-center gap-2">
                        <span class="text-sm font-medium text-white">Tailscale only</span>
                        <span class="badge badge-success">Recommended</span>
                    </span>
                    <span class="block text-xs text-slate-400 mt-0.5 text-pretty">Reachable only over your private tailnet (loopback plus your Tailscale IP). Not exposed to local Wi-Fi or the public internet.</span>
                </span>
            </label>
            <label class="flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors active:scale-[0.99] ${allActive ? 'border-red-500 bg-red-900/20' : 'border-red-900/40 hover:border-red-700'}">
                <input type="radio" name="remote-access-scope" value="all" class="mt-0.5 accent-red-500" ${allActive ? 'checked' : ''}>
                <span class="min-w-0 flex-1">
                    <span class="flex items-center gap-2">
                        <span class="text-sm font-medium text-red-300">All interfaces</span>
                        <span class="badge badge-danger">Unauthenticated</span>
                    </span>
                    <span class="block text-xs text-red-400/80 mt-0.5 text-pretty">Listens on every network (0.0.0.0), including public Wi-Fi and LAN. Anyone who can reach the port controls the dashboard. Use only on networks you trust.</span>
                </span>
            </label>
        </div>
    `;
}

function _remoteAccessStatusHTML(effective, port) {
    const eff = effective || {};
    const addrs = Array.isArray(eff.addresses) ? eff.addresses : [];
    const tsIp = eff.tailscale_ip || null;
    const parts = (addrs.length ? addrs : ['127.0.0.1']).map(a =>
        (tsIp && a === tsIp) ? `${escapeHtml(a)} (Tailscale)` : escapeHtml(a)
    );
    const listening = parts.join(' + ');

    const urlHint = tsIp
        ? `<div class="text-xs text-slate-400 mt-1.5">
               Open remotely at
               <a class="font-mono text-blue-400 hover:text-blue-300 transition-colors" href="http://${escapeHtml(tsIp)}:${escapeHtml(port)}">http://${escapeHtml(tsIp)}:${escapeHtml(port)}</a>
           </div>`
        : '';

    const fallback = eff.fallback_reason
        ? `<div class="flex items-start gap-2 mt-2 text-xs text-yellow-300 bg-yellow-900/20 border border-yellow-700/40 rounded px-2 py-1.5">
               <svg class="w-3.5 h-3.5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
               <span class="text-pretty">${escapeHtml(eff.fallback_reason)}</span>
           </div>`
        : '';

    return `
        <div class="mt-4 pt-3 border-t border-slate-700/60">
            <div class="flex items-start gap-2 text-xs">
                <svg class="w-3.5 h-3.5 flex-shrink-0 mt-0.5 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"/></svg>
                <span class="text-slate-300">Listening on <span class="font-mono text-slate-200">${listening}</span></span>
            </div>
            ${urlHint}
            ${fallback}
        </div>
    `;
}

function renderRemoteAccessCardHTML(state) {
    const enabled = !!state.enabled;
    const scope = state.scope === 'all' ? 'all' : 'tailscale';
    const port = _remoteAccessPort();

    const scopePicker = enabled ? _remoteAccessScopePickerHTML(scope) : '';

    return `
        <div class="flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1">
                <h3 class="text-sm font-semibold text-white">Remote access</h3>
                <p class="text-xs text-slate-400 mt-1 text-pretty">Choose which networks can reach this dashboard. It has no login, so where it listens is the security boundary. Saving a change restarts the jacked service.</p>
            </div>
            <label class="toggle-switch flex-shrink-0" title="Allow remote access">
                <input type="checkbox" id="chk-remote-access" ${enabled ? 'checked' : ''} aria-label="Allow remote access">
                <span class="toggle-slider"></span>
            </label>
        </div>
        ${scopePicker}
        ${_remoteAccessStatusHTML(state.effective, port)}
    `;
}

// --- Confirm dialog copy (NO em-dashes; commas/colons/parens only) ---

function _remoteAccessConfirmOptions(saved, pending, lockout) {
    const port = _remoteAccessPort();

    if (lockout) {
        // Two ways to strand this page: turning remote access OFF from a remote
        // origin, or narrowing scope to Tailscale only while browsing from a bare
        // LAN/other IP that the tailnet-only bind will drop. Copy differs so it
        // never claims "turning off" when the user is really switching scope.
        if (!pending.enabled) {
            return {
                title: 'Disconnect this remote session?',
                html: 'You are browsing from a remote machine. Turning off remote access makes the dashboard listen on this machine only, so this page will not reconnect. To turn it back on, open the dashboard on the machine itself (http://127.0.0.1:' + escapeHtml(port) + '). Applying this restarts the jacked service.',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonText: 'Turn off and disconnect',
                cancelButtonText: 'Cancel',
                confirmButtonColor: _REMOTE_ACCESS_DANGER,
                focusCancel: true,
            };
        }
        return {
            title: 'Disconnect this session?',
            html: 'You are browsing from an address that is not on your tailnet. Switching to Tailscale only makes the dashboard reachable over your tailnet and this machine only, so this page will not reconnect. To reach it again, open http://127.0.0.1:' + escapeHtml(port) + ' on the machine itself or use its Tailscale address. Applying this restarts the jacked service.',
            icon: 'warning',
            showCancelButton: true,
            confirmButtonText: 'Switch and disconnect',
            cancelButtonText: 'Cancel',
            confirmButtonColor: _REMOTE_ACCESS_DANGER,
            focusCancel: true,
        };
    }

    if (!pending.enabled) {
        return {
            title: 'Turn off remote access?',
            html: 'The dashboard will go back to listening on this machine only (127.0.0.1). Remote devices will no longer be able to reach it. Applying this restarts the jacked service.',
            icon: 'question',
            showCancelButton: true,
            confirmButtonText: 'Turn off',
            cancelButtonText: 'Cancel',
        };
    }

    if (pending.scope === 'all') {
        const lead = saved.enabled
            ? 'Switch remote access to all interfaces? '
            : 'Allow remote access on all interfaces? ';
        return {
            title: 'Expose the dashboard on all networks?',
            html: lead + 'The jacked dashboard will listen on every network this machine joins, including coffee-shop Wi-Fi and any LAN. It has no login, so anyone who can reach the port (' + escapeHtml(port) + ') can switch your accounts and trigger upgrades. Tailscale only is the safer choice. This setting is saved and survives restarts and upgrades. Applying it restarts the jacked service.',
            icon: 'warning',
            input: 'checkbox',
            inputPlaceholder: 'I understand this exposes the dashboard without authentication',
            inputValidator: (v) => (!v) && 'Please acknowledge this before continuing',
            showCancelButton: true,
            confirmButtonText: 'Expose on all interfaces',
            cancelButtonText: 'Cancel',
            confirmButtonColor: _REMOTE_ACCESS_DANGER,
            focusCancel: true,
        };
    }

    // enabled + tailscale
    const lead = saved.enabled
        ? 'Switch remote access to Tailscale only? '
        : 'Allow remote access over Tailscale? ';
    return {
        title: 'Allow remote access over Tailscale?',
        html: lead + 'The jacked dashboard has no login, so anyone on your tailnet who can reach this machine on port ' + escapeHtml(port) + ' will have full control, including switching accounts and triggering upgrades. Restrict who can reach it with a Tailscale ACL. This setting is saved and survives restarts and upgrades. Applying it restarts the jacked service.',
        icon: 'warning',
        showCancelButton: true,
        confirmButtonText: saved.enabled ? 'Switch to Tailscale only' : 'Allow over Tailscale',
        cancelButtonText: 'Cancel',
    };
}

// --- State machine: confirm -> PUT -> POST restart -> overlay ---

function _remoteAccessRerender(container) {
    if (!container) return;
    container.innerHTML = renderRemoteAccessCardHTML(_remoteAccessState);
    _bindRemoteAccessEvents(container);
}

async function _applyRemoteAccessChange(container, pending) {
    const saved = {
        enabled: !!_remoteAccessState.enabled,
        scope: _remoteAccessState.scope === 'all' ? 'all' : 'tailscale',
    };
    const norm = {
        enabled: !!pending.enabled,
        scope: pending.scope === 'all' ? 'all' : 'tailscale',
    };

    // No-op guard: re-selecting the current radio, or a toggle that ended up
    // back where it started, should not prompt or restart.
    if (norm.enabled === saved.enabled && norm.scope === saved.scope) {
        return { applied: false, reverted: false, noop: true };
    }

    const loc = _remoteAccessLocation();
    const lockout = remoteAccessLockout(norm, loc.hostname);
    const opts = _remoteAccessConfirmOptions(saved, norm, lockout);

    const result = await Swal.fire(opts);
    if (!result || !result.isConfirmed) {
        _remoteAccessRerender(container); // revert control to the saved state
        return { applied: false, reverted: true, lockout };
    }

    try {
        await api.put(_REMOTE_ACCESS_URL, { enabled: norm.enabled, scope: norm.scope });
        // Persist locally only after the PUT lands, so a PUT failure reverts to
        // the true last-known-good state.
        _remoteAccessState.enabled = norm.enabled;
        _remoteAccessState.scope = norm.scope;
        await api.post(_REMOTE_ACCESS_RESTART_URL);
    } catch (e) {
        _remoteAccessRerender(container);
        const emsg = (e && e.message) ? e.message : 'unknown error';
        if (typeof _showUpgradeError === 'function') {
            _showUpgradeError('Could not apply network settings: ' + emsg);
        }
        return { applied: false, reverted: true, error: true, lockout };
    }

    if (lockout) {
        const term = !norm.enabled
            ? 'Remote access is off. This page will not reconnect; open the dashboard on the machine itself.'
            : 'This page is not on your tailnet, so it will not reconnect. Open the dashboard on the machine itself or use its Tailscale address.';
        if (typeof _showRestartTerminal === 'function') _showRestartTerminal(term);
        else if (typeof _showUpgradeModal === 'function') _showUpgradeModal(term);
        // Intentionally no health polling: this page will never reconnect.
    } else {
        if (typeof _showUpgradeModal === 'function') _showUpgradeModal('Applying network settings...');
        if (typeof _startHealthPolling === 'function') _startHealthPolling();
    }
    return { applied: true, reverted: false, lockout };
}

// --- Event wiring ---

function _bindRemoteAccessEvents(container) {
    if (!container || typeof container.querySelector !== 'function') return;

    const toggle = container.querySelector('#chk-remote-access');
    if (toggle) {
        toggle.addEventListener('change', () => {
            _applyRemoteAccessChange(container, {
                enabled: toggle.checked,
                scope: _remoteAccessState.scope,
            });
        });
    }

    const radios = (typeof container.querySelectorAll === 'function')
        ? container.querySelectorAll('input[name="remote-access-scope"]')
        : [];
    radios.forEach(radio => {
        radio.addEventListener('change', () => {
            if (!radio.checked) return;
            _applyRemoteAccessChange(container, {
                enabled: true,
                scope: radio.value === 'all' ? 'all' : 'tailscale',
            });
        });
    });
}

function _bindRemoteAccessErrorEvents(container) {
    if (!container || typeof container.querySelector !== 'function') return;
    const btn = container.querySelector('#btn-remote-access-retry');
    if (btn) btn.addEventListener('click', () => renderRemoteAccessCard(container));
}

// --- Entry point: fetch live state, render card (loading -> populated/error) ---

async function renderRemoteAccessCard(container) {
    if (!container) return;
    container.innerHTML = _remoteAccessLoadingHTML();

    try {
        const state = await api.get(_REMOTE_ACCESS_URL);
        _remoteAccessState.enabled = !!(state && state.enabled);
        _remoteAccessState.scope = (state && state.scope === 'all') ? 'all' : 'tailscale';
        _remoteAccessState.effective = (state && state.effective) || null;
        _remoteAccessState.loaded = true;
        container.innerHTML = renderRemoteAccessCardHTML(_remoteAccessState);
        _bindRemoteAccessEvents(container);
    } catch (e) {
        const emsg = (e && e.message) ? e.message : 'unknown error';
        container.innerHTML = _remoteAccessErrorHTML(emsg);
        _bindRemoteAccessErrorEvents(container);
    }
}

/**
 * jacked web dashboard — Integrations settings tab
 *
 * Two managed external integrations live here:
 *   1. Agent Reach — vetted, pinned external capability layer (platform read/
 *      search access). Rendered from GET /api/integrations/agent-reach.
 *   2. Chrome DevTools MCP — the browser-testing bridge that powers /qa and /ux.
 *      Relocated from the Claude Code tab; the widget markup + wiring
 *      (_refreshChromeDevToolsStatus / _initChromeDevToolsMCP) moved here intact.
 *
 * The tab shell renders both sections immediately; the Agent Reach card fetches
 * its status independently so its own loading/error/empty states never block the
 * browser-tools widget.
 */

// Channels whose backends authenticate with cookies/login state. They carry the
// burner-account warning: those platforms can ban accounts used by tooling.
const REACH_LOGIN_CHANNELS = ['twitter', 'reddit', 'xiaohongshu', 'facebook', 'instagram', 'opencli', 'bilibili'];

// --- Tab entry point -------------------------------------------------------

async function renderIntegrationsTab(container) {
    container.innerHTML = `
        <div class="space-y-8">
            <div id="integration-agent-reach">
                <div class="flex items-center justify-center py-12">
                    <div class="spinner"></div>
                    <span class="ml-3 text-slate-400 text-sm">Loading Agent Reach...</span>
                </div>
            </div>
            ${_reachBrowserToolsHtml()}
        </div>
    `;
    // Browser-tools widget is self-contained and renders synchronously.
    _initChromeDevToolsMCP(container);
    // Agent Reach fetches its own status into #integration-agent-reach.
    await _loadReachCard(container);
}

// --- Agent Reach: data loading + card injection ----------------------------

async function _loadReachCard(container) {
    const slot = document.getElementById('integration-agent-reach');
    if (!slot) return;

    slot.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading Agent Reach...</span>
        </div>
    `;

    let status;
    try {
        status = await api.get('/api/integrations/agent-reach');
    } catch (e) {
        slot.innerHTML = `
            <div class="feature-card">
                <div class="text-sm font-semibold text-white mb-1">Agent Reach</div>
                <div class="text-red-400 text-sm mb-3">Failed to load status: ${escapeHtml(e.message || 'unknown error')}</div>
                <button id="reach-retry" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
        const retry = document.getElementById('reach-retry');
        if (retry) retry.addEventListener('click', () => _loadReachCard(container));
        return;
    }

    slot.innerHTML = _reachCardHtml(status);
    _bindReachEvents(container, status);
}

// --- Agent Reach: pure render (string in, HTML out — unit-tested directly) --

function _reachCardHtml(status) {
    status = status || {};
    const installed = !!status.installed;
    const pin = status.pin || {};
    const override = status.override || {};
    const drift = Array.isArray(status.drift) ? status.drift : [];

    const stateBadge = installed
        ? '<span class="badge badge-success">Installed</span>'
        : '<span class="badge badge-muted">Not installed</span>';

    const unvettedBadge = override.active
        ? '<span id="reach-unvetted-badge" class="badge badge-danger">Unvetted override</span>'
        : '';

    const pinLabel = escapeHtml(pin.version_label || 'unknown');
    const pinShort = escapeHtml(pin.short_sha || '');
    const vettedAt = escapeHtml(pin.vetted_at || 'unknown');
    const pinLine = `
        <div class="text-xs text-slate-500 mt-2">
            Vetted pin ${pinLabel}
            ${pinShort ? `<code class="text-slate-400 tabular-nums">${pinShort}</code>` : ''}
            <span class="text-slate-600">·</span> vetted <span class="tabular-nums">${vettedAt}</span>
        </div>
    `;

    return `
        <div id="reach-card" class="feature-card">
            <div class="flex items-start justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <div class="flex items-center flex-wrap gap-2">
                        <span class="text-sm font-semibold text-white">Agent Reach</span>
                        ${stateBadge}
                        ${unvettedBadge}
                    </div>
                    <p class="text-xs text-slate-400 mt-1 text-pretty">
                        Gives coding agents read and search access to platforms like Twitter/X, Reddit,
                        YouTube transcripts, and Bilibili. jacked installs it from a vetted, pinned commit
                        so a poisoned upstream release cannot reach your machine. Off by default, opt-in per machine.
                    </p>
                    ${pinLine}
                </div>
            </div>

            ${_reachOverrideNoticeHtml(override, pin)}
            ${_reachActionsHtml(installed)}
            <div id="reach-op-error" class="hidden mt-3 p-2 bg-red-900/30 border border-red-700/50 rounded text-xs text-red-300"></div>

            ${_reachDriftHtml(drift)}
            ${installed ? _reachDoctorHtml(status.doctor, status.doctor_error) : ''}
            ${_reachChannelsHtml(status.channels, installed)}
            ${_reachBreakGlassHtml(override, pin)}
        </div>
    `;
}

function _reachOverrideNoticeHtml(override, pin) {
    if (!override || !override.active) return '';
    const short = escapeHtml(_reachShort(override.sha));
    const pinLabel = escapeHtml((pin && pin.version_label) || 'the shipped pin');
    return `
        <div class="mt-4 p-3 bg-red-900/25 border border-red-700/60 rounded">
            <div class="flex items-center flex-wrap gap-2">
                <span class="badge badge-danger">Unvetted</span>
                <code class="text-xs text-red-200 tabular-nums">${short}</code>
            </div>
            <p class="text-xs text-red-200/90 mt-2 text-pretty">
                A break-glass override is active. jacked is installing an unvetted upstream commit that
                differs from the shipped vetted pin (${pinLabel}). Clearing the override returns you to the
                vetted pin. Clear it below once you no longer need the override.
            </p>
        </div>
    `;
}

function _reachActionsHtml(installed) {
    const base = 'reach-op-btn text-sm rounded transition active:scale-[0.96] disabled:opacity-40 disabled:cursor-not-allowed px-4 py-2';
    if (!installed) {
        return `
            <div class="flex items-center gap-2 mt-4">
                <button class="${base} bg-blue-600 hover:bg-blue-500 text-white" data-op="install">Install</button>
            </div>
        `;
    }
    return `
        <div class="flex items-center gap-2 mt-4">
            <button class="${base} bg-blue-600 hover:bg-blue-500 text-white" data-op="update">Update</button>
            <button class="${base} bg-red-600 hover:bg-red-500 text-white" data-op="remove">Remove</button>
        </div>
    `;
}

function _reachDriftHtml(drift) {
    const bad = (drift || []).filter(d => d && d.status && d.status !== 'ok');
    if (bad.length === 0) return '';
    const rows = bad.map(d => `
        <tr>
            <td class="font-mono text-xs text-red-200 py-1 pr-3">${escapeHtml(d.file || '')}</td>
            <td class="text-xs text-red-300 py-1 pr-3 uppercase">${escapeHtml(d.status || '')}</td>
            <td class="font-mono text-[11px] text-red-300/80 py-1 pr-3 tabular-nums">${escapeHtml(_reachShort(d.expected) || '-')}</td>
            <td class="font-mono text-[11px] text-red-300/80 py-1 tabular-nums">${escapeHtml(_reachShort(d.actual) || '-')}</td>
        </tr>
    `).join('');
    return `
        <div id="reach-drift-warning" class="mt-4 p-3 bg-red-900/30 border border-red-700 rounded">
            <div class="flex items-center gap-2">
                <svg class="w-4 h-4 text-red-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
                <span class="text-sm font-semibold text-red-300">Skill file drift detected</span>
            </div>
            <p class="text-xs text-red-200/90 mt-2 text-pretty">
                Installed skill files no longer match the vetted hashes. This may indicate tampering.
                Remove is the safe action.
            </p>
            <div class="overflow-x-auto mt-2">
                <table class="w-full">
                    <thead>
                        <tr class="text-left text-[11px] text-red-400/80 uppercase tracking-wider">
                            <th class="py-1 pr-3 font-medium">File</th>
                            <th class="py-1 pr-3 font-medium">State</th>
                            <th class="py-1 pr-3 font-medium">Expected</th>
                            <th class="py-1 font-medium">Actual</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>
    `;
}

function _reachDoctorHtml(doctor, doctorError) {
    let body;
    if (doctorError) {
        body = `
            <div id="reach-doctor-error" class="p-2 bg-amber-900/30 border border-amber-700/50 rounded text-xs text-amber-300">
                agent-reach doctor: ${escapeHtml(doctorError)}
            </div>
        `;
    } else if (doctor && Array.isArray(doctor.channels) && doctor.channels.length > 0) {
        const rows = doctor.channels.map(c => {
            const st = String((c && c.status) || '').toLowerCase();
            const stClass = st === 'ok' ? 'text-emerald-400' : 'text-amber-400';
            const hint = (c && (c.hint || c.fix)) || '';
            return `
                <tr class="border-t border-slate-700/50">
                    <td class="py-1.5 pr-3 text-slate-200">${escapeHtml((c && c.name) || '')}</td>
                    <td class="py-1.5 pr-3 font-mono text-xs text-slate-400">${escapeHtml((c && c.active_backend) || '-')}</td>
                    <td class="py-1.5 pr-3 ${stClass}">${escapeHtml((c && c.status) || '-')}</td>
                    <td class="py-1.5 text-xs text-slate-400">${escapeHtml(hint) || ''}</td>
                </tr>
            `;
        }).join('');
        body = `
            <div class="overflow-x-auto">
                <table class="w-full text-sm">
                    <thead>
                        <tr class="text-left text-[11px] text-slate-500 uppercase tracking-wider">
                            <th class="py-1 pr-3 font-medium">Channel</th>
                            <th class="py-1 pr-3 font-medium">Active backend</th>
                            <th class="py-1 pr-3 font-medium">Status</th>
                            <th class="py-1 font-medium">Fix</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
    } else {
        body = `<p class="text-xs text-slate-500">No channel diagnostics yet. Run doctor after enabling a channel.</p>`;
    }
    return `
        <div class="mt-5">
            <h4 class="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-2">Channel health</h4>
            <div id="reach-doctor-table">${body}</div>
        </div>
    `;
}

function _reachChannelsHtml(channels, installed) {
    let body;
    if (!Array.isArray(channels)) {
        // Field absent (older status payload) — treat as empty, never crash.
        body = `<p class="text-xs text-slate-500">Channel list unavailable.</p>`;
    } else if (channels.length === 0) {
        body = `<p class="text-xs text-slate-500">No channels defined in the vetted pin.</p>`;
    } else {
        body = channels.map(ch => _reachChannelRowHtml(ch, installed)).join('');
    }
    return `
        <div class="mt-5">
            <h4 class="text-xs font-semibold text-slate-300 uppercase tracking-wider mb-1">Channels</h4>
            <p class="text-xs text-slate-500 mb-3 text-pretty">
                Optional per-platform backends. jacked installs only the pinned versions shown, never a
                freestyle install from upstream docs.
            </p>
            <div class="space-y-2">${body}</div>
        </div>
    `;
}

function _reachChannelRowHtml(ch, installed) {
    ch = ch || {};
    const name = String(ch.name || '');
    const specs = (Array.isArray(ch.backends) ? ch.backends : [])
        .map(b => `<code class="text-[11px] text-slate-300 bg-slate-900/70 rounded px-1.5 py-0.5">${escapeHtml(_reachBackendSpec(b))}</code>`)
        .join(' ');
    const isLogin = REACH_LOGIN_CHANNELS.indexOf(name) !== -1;
    const warn = isLogin
        ? `<p class="text-[11px] text-amber-400/90 mt-1.5">Cookie/login platform: risk of account bans. Use a dedicated secondary (burner) account.</p>`
        : '';
    const action = ch.enabled
        ? `<span class="badge badge-success shrink-0">Enabled</span>`
        : `<button class="reach-channel-enable text-xs px-3 py-1 bg-slate-700 hover:bg-slate-600 text-white rounded transition active:scale-[0.96] disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                   data-channel="${escapeHtml(name)}" ${installed ? '' : 'disabled'}
                   ${installed ? '' : 'title="Install Agent Reach first"'}>Enable</button>`;
    return `
        <div class="p-3 bg-slate-900/50 rounded border border-slate-700/50" data-channel-row="${escapeHtml(name)}">
            <div class="flex items-start justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-2">
                        ${ch.enabled ? '<svg class="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg>' : ''}
                        <span class="text-sm text-white">${escapeHtml(name)}</span>
                    </div>
                    <div class="flex flex-wrap gap-1.5 mt-1.5">${specs}</div>
                    ${warn}
                    <div class="reach-channel-hint text-[11px] text-slate-400 mt-1.5" data-channel-hint="${escapeHtml(name)}"></div>
                </div>
                ${action}
            </div>
        </div>
    `;
}

function _reachBreakGlassHtml(override, pin) {
    const active = override && override.active;
    const clearBtn = active
        ? `<button id="reach-breakglass-clear" class="text-xs px-3 py-1.5 bg-red-600 hover:bg-red-500 text-white rounded transition active:scale-[0.96] disabled:opacity-40 disabled:cursor-not-allowed">Clear override</button>`
        : '';
    return `
        <details class="mt-5 border-t border-slate-700 pt-4">
            <summary class="text-xs font-semibold text-slate-400 uppercase tracking-wider cursor-pointer select-none">Break-glass override (advanced)</summary>
            <div class="mt-3 space-y-3">
                <p class="text-xs text-slate-500 text-pretty">
                    Escape hatch: install a specific upstream ref that jacked has not vetted. Use this only when a
                    platform breakage needs an upstream fix ahead of the next vetted pin. After setting the override,
                    run Install or Update to apply it.
                </p>
                <input type="text" id="reach-breakglass-ref"
                       class="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white font-mono placeholder-slate-500 focus:outline-none focus:border-blue-500"
                       placeholder="commit SHA, tag, or branch">
                <label class="flex items-start gap-2 text-xs text-slate-300 cursor-pointer">
                    <input type="checkbox" id="reach-breakglass-ack" class="mt-0.5">
                    <span>I understand this installs unvetted upstream code.</span>
                </label>
                <div class="flex items-center gap-2">
                    <button id="reach-breakglass-apply"
                            class="text-xs px-3 py-1.5 bg-amber-700 hover:bg-amber-600 text-white rounded transition active:scale-[0.96] disabled:opacity-40 disabled:cursor-not-allowed"
                            disabled>Apply override</button>
                    ${clearBtn}
                </div>
            </div>
        </details>
    `;
}

// --- Agent Reach: helpers --------------------------------------------------

function _reachShort(sha) {
    if (!sha) return '';
    const s = String(sha);
    return s.length > 12 ? s.slice(0, 12) : s;
}

function _reachBackendSpec(b) {
    if (!b) return '';
    if (b.kind && b.spec) return `${b.kind} ${b.spec}`;
    if (b.note) return b.note;
    if (b.spec) return b.spec;
    return '';
}

function _reachOpLabel(op) {
    if (op === 'install') return 'Installing...';
    if (op === 'update') return 'Updating...';
    if (op === 'remove') return 'Removing...';
    return 'Working...';
}

// --- Agent Reach: event wiring ---------------------------------------------

function _bindReachEvents(container, status) {
    container.querySelectorAll('.reach-op-btn').forEach(btn => {
        btn.addEventListener('click', () => _reachRunOp(container, btn.dataset.op));
    });

    container.querySelectorAll('.reach-channel-enable').forEach(btn => {
        btn.addEventListener('click', () => _reachEnableChannel(container, btn.dataset.channel, btn));
    });

    const ack = document.getElementById('reach-breakglass-ack');
    const applyBtn = document.getElementById('reach-breakglass-apply');
    if (ack && applyBtn) {
        ack.addEventListener('change', () => { applyBtn.disabled = !ack.checked; });
        applyBtn.addEventListener('click', () => _reachApplyOverride(container));
    }

    const clearBtn = document.getElementById('reach-breakglass-clear');
    if (clearBtn) clearBtn.addEventListener('click', () => _reachClearOverride(container));
}

async function _reachRunOp(container, op) {
    if (op === 'remove') {
        const result = await Swal.fire({
            title: 'Remove Agent Reach?',
            text: 'This uninstalls agent-reach, deletes its skill files, and clears any break-glass override.',
            icon: 'warning',
            showCancelButton: true,
            confirmButtonText: 'Remove',
            cancelButtonText: 'Cancel',
            focusCancel: true,
        });
        if (!result.isConfirmed) return;
    }

    const btns = [...container.querySelectorAll('.reach-op-btn')];
    const clicked = container.querySelector(`.reach-op-btn[data-op="${op}"]`);
    const origLabel = clicked ? clicked.textContent : '';
    btns.forEach(b => { b.disabled = true; });
    if (clicked) {
        clicked.innerHTML = `<span class="inline-flex items-center gap-2"><span class="spinner" style="width:14px;height:14px;border-width:2px;"></span>${_reachOpLabel(op)}</span>`;
    }
    const errEl = document.getElementById('reach-op-error');
    if (errEl) { errEl.classList.add('hidden'); errEl.textContent = ''; }

    try {
        // Ops are long-running; the default 60s timeout is too short for a real install.
        await api.post(`/api/integrations/agent-reach/${op}`, {}, { timeout: 300000 });
        showToast(`Agent Reach ${op} complete`, 'success');
        await _loadReachCard(container);
    } catch (e) {
        btns.forEach(b => { b.disabled = false; });
        if (clicked) clicked.textContent = origLabel;
        if (errEl) {
            errEl.textContent = e.message || `${op} failed`;
            errEl.classList.remove('hidden');
        }
    }
}

async function _reachEnableChannel(container, name, btn) {
    if (btn) { btn.disabled = true; btn.textContent = 'Enabling...'; }
    try {
        const res = await api.post(`/api/integrations/agent-reach/channels/${encodeURIComponent(name)}`, {}, { timeout: 300000 });
        const hintSlot = container.querySelector(`[data-channel-hint="${name}"]`);
        if (hintSlot && res && res.hint) hintSlot.textContent = res.hint;
        if (btn) { btn.textContent = 'Enabled'; btn.classList.add('opacity-60'); }
        showToast(`${name} channel enabled`, 'success');
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Enable'; }
        showToast(e.message || 'Enable failed', 'error');
    }
}

async function _reachApplyOverride(container) {
    const ref = (document.getElementById('reach-breakglass-ref')?.value || '').trim();
    const ack = document.getElementById('reach-breakglass-ack')?.checked;
    if (!ref) { showToast('Enter a ref (commit SHA, tag, or branch)', 'warning'); return; }
    if (!ack) { showToast('Acknowledge the unvetted-code risk first', 'warning'); return; }

    const btn = document.getElementById('reach-breakglass-apply');
    if (btn) { btn.disabled = true; btn.textContent = 'Applying...'; }
    try {
        await api.put('/api/integrations/agent-reach/override', { ref, confirm_unvetted: true }, { timeout: 300000 });
        showToast('Break-glass override set. Run Install or Update to apply it.', 'warning');
        await _loadReachCard(container);
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Apply override'; }
        showToast(e.message || 'Override failed', 'error');
    }
}

async function _reachClearOverride(container) {
    const btn = document.getElementById('reach-breakglass-clear');
    if (btn) { btn.disabled = true; btn.textContent = 'Clearing...'; }
    try {
        // Clearing reinstalls at the shipped pin when installed, so allow time.
        await api.delete('/api/integrations/agent-reach/override', { timeout: 300000 });
        showToast('Override cleared. Reverted to the shipped vetted pin.', 'success');
        await _loadReachCard(container);
    } catch (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Clear override'; }
        showToast(e.message || 'Clear failed', 'error');
    }
}

// --- Browser Tools (Chrome DevTools MCP) -----------------------------------
// Relocated verbatim from the Claude Code tab. Powers /qa and /ux. Kept in the
// Integrations tab so all managed external tooling lives in one place.

function _reachBrowserToolsHtml() {
    return `
        <div class="border-t border-slate-700 pt-5">
            <div class="flex items-center gap-2 mb-1">
                <svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"/></svg>
                <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Browser Tools</h3>
            </div>
            <p class="text-xs text-slate-500 mb-3">Chrome DevTools MCP powers <code class="text-slate-300">/qa</code> and <code class="text-slate-300">/ux</code> browser testing. Requires <span class="text-emerald-400">Chrome 144+</span> with remote debugging enabled.</p>
            <div id="cdp-mcp-status" class="p-3 bg-slate-900/50 rounded border border-slate-700/50">
                <div class="flex items-center justify-between">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">Chrome DevTools MCP</div>
                        <div id="cdp-mcp-status-text" class="text-xs text-slate-400">Checking...</div>
                    </div>
                    <div class="flex items-center gap-3">
                        <select id="cdp-mcp-mode" class="bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500" disabled>
                            <option value="autoConnect">Auto-connect (Chrome 144+)</option>
                            <option value="browserUrl">Browser URL (:9222)</option>
                            <option value="launch">Launch new Chrome</option>
                            <option value="headless">Headless (no UI)</option>
                        </select>
                        <label class="toggle-switch" id="cdp-mcp-toggle">
                            <input type="checkbox" disabled>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                </div>
                <div id="cdp-mcp-help" class="hidden mt-3 p-2 bg-slate-800/50 rounded border border-slate-700/30">
                    <p class="text-xs text-slate-400 mb-1 font-semibold">Setup instructions:</p>
                    <ol class="text-xs text-slate-500 list-decimal ml-4 space-y-1">
                        <li>Install Chrome 144 or newer, check at <code class="text-slate-300">chrome://version</code></li>
                        <li>Enable remote debugging at <code class="text-slate-300">chrome://inspect/#remote-debugging</code></li>
                        <li>Restart Claude Code after enabling</li>
                    </ol>
                </div>
            </div>
        </div>
    `;
}

async function _refreshChromeDevToolsStatus() {
    const statusText = document.getElementById('cdp-mcp-status-text');
    const modeSelect = document.getElementById('cdp-mcp-mode');
    const toggleInput = document.getElementById('cdp-mcp-toggle')?.querySelector('input');
    const helpDiv = document.getElementById('cdp-mcp-help');
    if (!statusText || !modeSelect || !toggleInput) return;

    try {
        const data = await api.get('/api/chrome-devtools-mcp');
        if (data.installed) {
            statusText.textContent = `Configured (${data.mode || 'default'})`;
            statusText.className = 'text-xs text-emerald-400';
            modeSelect.value = data.mode || 'autoConnect';
            modeSelect.disabled = false;
            toggleInput.checked = true;
            toggleInput.disabled = false;
            if (helpDiv) helpDiv.classList.add('hidden');
        } else {
            statusText.textContent = 'Not configured';
            statusText.className = 'text-xs text-amber-400';
            modeSelect.disabled = true;
            toggleInput.checked = false;
            toggleInput.disabled = false;
            if (helpDiv) helpDiv.classList.remove('hidden');
        }
    } catch (e) {
        statusText.textContent = 'Error loading status';
        statusText.className = 'text-xs text-red-400';
        if (helpDiv) helpDiv.classList.remove('hidden');
    }
}

function _initChromeDevToolsMCP(container) {
    const toggleInput = document.getElementById('cdp-mcp-toggle')?.querySelector('input');
    const modeSelect = document.getElementById('cdp-mcp-mode');
    if (!toggleInput || !modeSelect) return;

    // Load initial status
    _refreshChromeDevToolsStatus();

    // Bind events once only
    toggleInput.addEventListener('change', async () => {
        toggleInput.disabled = true;
        try {
            if (toggleInput.checked) {
                const mode = modeSelect.value || 'autoConnect';
                await api.put('/api/chrome-devtools-mcp', { mode });
                showToast(`Chrome DevTools MCP enabled (${mode}). Restart Claude Code.`, 'warning');
            } else {
                await api.delete('/api/chrome-devtools-mcp');
                showToast('Chrome DevTools MCP removed. Restart Claude Code.', 'warning');
            }
            await _refreshChromeDevToolsStatus();
        } catch (e) {
            toggleInput.checked = !toggleInput.checked;
            showToast(e.message || 'Failed to update Chrome DevTools MCP', 'error');
        } finally {
            toggleInput.disabled = false;
        }
    });

    modeSelect.addEventListener('change', async () => {
        if (!toggleInput.checked) return;
        modeSelect.disabled = true;
        try {
            await api.put('/api/chrome-devtools-mcp', { mode: modeSelect.value });
            showToast(`Mode changed to ${modeSelect.value}. Restart Claude Code.`, 'warning');
            await _refreshChromeDevToolsStatus();
        } catch (e) {
            showToast(e.message || 'Failed to change mode', 'error');
        } finally {
            modeSelect.disabled = false;
        }
    });
}

/**
 * jacked web dashboard — auto-swap & window keeper settings panel
 * Collapsible panel embedded on the accounts page.
 */

// ---------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------

async function loadAutoSwapSettings() {
    try {
        const data = await api.get('/api/settings/swap-settings');
        window.jackedState.swapSettings = data || {};
    } catch (e) {
        console.error('Failed to load swap settings:', e);
        window.jackedState.swapSettings = {};
    }
}

async function loadSwapLog() {
    try {
        const data = await api.get('/api/settings/swap-log?limit=20');
        // Legacy shape: bare array. Current shape: {swaps, swaps_last_24h}.
        if (Array.isArray(data)) return data;
        const swaps = Array.isArray(data && data.swaps) ? data.swaps : [];
        if (data && typeof data.swaps_last_24h === 'number') {
            window.jackedState.swapsLast24h = data.swaps_last_24h;
            updateSwapCounter();
        }
        return swaps;
    } catch (e) {
        console.error('Failed to load swap log:', e);
        return [];
    }
}

function updateSwapCounter() {
    const el = document.getElementById('auto-swap-24h-counter');
    if (!el) return;
    const n = window.jackedState.swapsLast24h;
    el.textContent = n == null ? '' : `${n} swaps / 24h`;
}

async function loadDecisionLog(showAll) {
    try {
        const params = showAll ? '?limit=200' : '?limit=100&action=swap&action=manual_switch';
        const data = await api.get('/api/settings/decision-log' + params);
        return Array.isArray(data) ? data : [];
    } catch (e) {
        console.error('Failed to load decision log:', e);
        return [];
    }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderAutoSwapPanel() {
    const s = window.jackedState.swapSettings || {};

    const autoSwapEnabled = s.auto_swap_enabled || false;
    const warn5h = s.auto_swap_5h_warning ?? 80;
    const crit5h = s.auto_swap_5h_critical ?? 90;
    const thresh7d = s.auto_swap_7d_threshold ?? 85;
    const checkInterval = s.usage_check_interval ?? 300;
    const pausedUntil = s.auto_swap_paused_until || null;

    // Compute pause status
    let pauseLabel = '';
    let pausedUntilTime = '';
    let isPaused = false;
    if (pausedUntil) {
        const pauseEnd = new Date(pausedUntil);
        const nowMs = Date.now();
        const remainMs = pauseEnd.getTime() - nowMs;
        if (remainMs > 0) {
            isPaused = true;
            pausedUntilTime = pauseEnd.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const remainMin = Math.ceil(remainMs / 60000);
            pauseLabel = remainMin >= 60
                ? `${Math.floor(remainMin / 60)}h ${remainMin % 60}m`
                : `${remainMin}m`;
        }
    }

    // UI-initiated pauses are tracked in _uiPauseUntil this session; any
    // other pause came from the backend's 15-min post-manual-switch hold.
    const isManualSwitchPause = isPaused && window.jackedState._uiPauseUntil !== pausedUntil;
    const pausedNotice = isPaused
        ? `<div class="text-xs text-amber-400">Auto-swap paused until ${escapeHtml(pausedUntilTime)}${isManualSwitchPause ? ' (manual switch)' : ''}</div>`
        : '';

    const swapsLast24h = window.jackedState.swapsLast24h;
    const counterText = swapsLast24h == null ? '' : `${swapsLast24h} swaps / 24h`;

    const wkEnabled = s.window_keeper_enabled || false;
    const activeStart = s.window_keeper_active_start || '06:00';
    const activeEnd = s.window_keeper_active_end || '23:00';
    const preWake = s.window_keeper_prewake || '04:00';

    return `
        <div id="swap-exhaustion-banner" class="hidden mb-3"></div>
        <div class="mt-6">
            <div class="bg-slate-800 border border-slate-700 rounded-lg">
                <!-- Collapsible Header -->
                <button id="btn-toggle-swap-panel" class="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-slate-750 transition-colors rounded-lg">
                    <div class="flex items-center gap-2">
                        <svg class="w-4 h-4 text-teal-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7h12m0 0l-4-4m4 4l-4 4m0 6H4m0 0l4 4m-4-4l4-4"/></svg>
                        <span class="text-white font-medium">Auto-Swap &amp; Window Keeper</span>
                        ${autoSwapEnabled ? '<span class="text-xs px-2 py-0.5 bg-teal-600/20 text-teal-400 border border-teal-600/30 rounded">Active</span>' : ''}
                        ${wkEnabled ? '<span class="text-xs px-2 py-0.5 bg-blue-600/20 text-blue-400 border border-blue-600/30 rounded">Window Keeper</span>' : ''}
                    </div>
                    <svg id="swap-panel-arrow" class="w-4 h-4 text-slate-400 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                </button>

                <!-- Collapsible Body -->
                <div id="swap-panel-body" class="hidden border-t border-slate-700">
                    <div class="px-4 py-4 space-y-6">

                        <!-- Section 1: Auto-Swap -->
                        <div>
                            <h3 class="text-white font-medium text-sm mb-3">Auto-Swap</h3>
                            <div class="space-y-4">
                                <!-- Enable toggle -->
                                <div class="flex items-center justify-between">
                                    <div class="flex items-center gap-2">
                                        <span class="text-slate-400 text-sm">Enable Auto-Swap</span>
                                        <span id="auto-swap-24h-counter" class="text-xs text-slate-500">${escapeHtml(counterText)}</span>
                                    </div>
                                    <label class="toggle-switch">
                                        <input type="checkbox" id="chk-auto-swap" ${autoSwapEnabled ? 'checked' : ''}>
                                        <span class="toggle-slider"></span>
                                    </label>
                                </div>
                                ${pausedNotice}

                                <!-- Pause / Snooze -->
                                <div class="flex items-center justify-between">
                                    <div class="flex items-center gap-2">
                                        <span class="text-slate-400 text-sm">Pause Auto-Swap</span>
                                        ${isPaused ? `<span class="text-xs px-2 py-0.5 bg-amber-600/20 text-amber-400 border border-amber-600/30 rounded">Paused — ${escapeHtml(pauseLabel)} left</span>` : ''}
                                    </div>
                                    <div class="flex items-center gap-1.5">
                                        ${isPaused
                                            ? '<button id="btn-swap-resume" class="text-xs px-2.5 py-1 bg-teal-600/20 text-teal-400 border border-teal-600/30 rounded hover:bg-teal-600/30 transition-colors">Resume</button>'
                                            : `<select id="sel-swap-pause" class="bg-slate-700 border border-slate-600 text-slate-300 text-xs rounded px-2 py-1 cursor-pointer hover:border-slate-500 transition-colors">
                                                    <option value="">Not paused</option>
                                                    <option value="30">30 min</option>
                                                    <option value="60">1 hour</option>
                                                    <option value="120">2 hours</option>
                                                </select>`}
                                    </div>
                                </div>

                                <!-- 5h Warning Threshold -->
                                <div>
                                    <div class="flex items-center justify-between mb-1">
                                        <span class="text-slate-400 text-sm">5h Warning Threshold</span>
                                        <span class="text-slate-300 text-sm font-mono" id="lbl-warn-5h">${warn5h}%</span>
                                    </div>
                                    <input type="range" id="rng-warn-5h" min="50" max="100" value="${warn5h}" class="w-full accent-teal-500 h-1.5 bg-slate-700 rounded-full appearance-none cursor-pointer">
                                </div>

                                <!-- 5h Critical Threshold -->
                                <div>
                                    <div class="flex items-center justify-between mb-1">
                                        <span class="text-slate-400 text-sm">5h Critical Threshold</span>
                                        <span class="text-slate-300 text-sm font-mono" id="lbl-crit-5h">${crit5h}%</span>
                                    </div>
                                    <input type="range" id="rng-crit-5h" min="50" max="100" value="${crit5h}" class="w-full accent-teal-500 h-1.5 bg-slate-700 rounded-full appearance-none cursor-pointer">
                                </div>

                                <!-- 7-Day Threshold -->
                                <div>
                                    <div class="flex items-center justify-between mb-1">
                                        <span class="text-slate-400 text-sm">7-Day Threshold</span>
                                        <span class="text-slate-300 text-sm font-mono" id="lbl-thresh-7d">${thresh7d}%</span>
                                    </div>
                                    <input type="range" id="rng-thresh-7d" min="50" max="100" value="${thresh7d}" class="w-full accent-teal-500 h-1.5 bg-slate-700 rounded-full appearance-none cursor-pointer">
                                </div>

                                <!-- Usage Check Interval -->
                                <div class="flex items-center justify-between">
                                    <span class="text-slate-400 text-sm">Usage Check Interval</span>
                                    <select id="sel-check-interval" class="bg-slate-700 border border-slate-600 text-slate-300 text-sm rounded px-3 py-1.5 cursor-pointer hover:border-slate-500 transition-colors">
                                        <option value="60" ${checkInterval === 60 ? 'selected' : ''}>1 min</option>
                                        <option value="120" ${checkInterval === 120 ? 'selected' : ''}>2 min</option>
                                        <option value="300" ${checkInterval === 300 ? 'selected' : ''}>5 min</option>
                                        <option value="600" ${checkInterval === 600 ? 'selected' : ''}>10 min</option>
                                        <option value="900" ${checkInterval === 900 ? 'selected' : ''}>15 min</option>
                                    </select>
                                </div>
                            </div>
                        </div>

                        <!-- Section 2: Window Keeper -->
                        <div class="border-t border-slate-700/50 pt-4">
                            <h3 class="text-white font-medium text-sm mb-3">Window Keeper</h3>
                            <div class="space-y-4">
                                <!-- Enable toggle -->
                                <div class="flex items-center justify-between">
                                    <span class="text-slate-400 text-sm">Enable Window Keeper</span>
                                    <label class="toggle-switch">
                                        <input type="checkbox" id="chk-window-keeper" ${wkEnabled ? 'checked' : ''}>
                                        <span class="toggle-slider"></span>
                                    </label>
                                </div>

                                <!-- Active Hours Start -->
                                <div class="flex items-center justify-between">
                                    <span class="text-slate-400 text-sm">Active Hours Start</span>
                                    <input type="time" id="inp-active-start" value="${escapeHtml(activeStart)}" class="bg-slate-700 border border-slate-600 text-slate-300 text-sm rounded px-3 py-1.5">
                                </div>

                                <!-- Active Hours End -->
                                <div class="flex items-center justify-between">
                                    <span class="text-slate-400 text-sm">Active Hours End</span>
                                    <input type="time" id="inp-active-end" value="${escapeHtml(activeEnd)}" class="bg-slate-700 border border-slate-600 text-slate-300 text-sm rounded px-3 py-1.5">
                                </div>

                                <!-- Pre-Wake Activation -->
                                <div class="flex items-center justify-between">
                                    <span class="text-slate-400 text-sm">Pre-Wake Activation</span>
                                    <input type="time" id="inp-pre-wake" value="${escapeHtml(preWake)}" class="bg-slate-700 border border-slate-600 text-slate-300 text-sm rounded px-3 py-1.5">
                                </div>
                            </div>
                        </div>


                    </div>
                </div>
            </div>
        </div>
    `;
}

/**
 * Format an account label from swap log entry fields.
 * @param {Object} entry - swap log entry
 * @param {string} prefix - 'from' or 'to'
 * @returns {string} formatted label like "jack@test.com (Acme)"
 */
function formatAccountLabel(entry, prefix) {
    const email = entry[prefix + '_email'] || '\u2014';
    const orgName = entry[prefix + '_org_name'] || '';
    const displayName = (entry[prefix + '_display_name'] || '').trim();

    let orgSuffix = '';
    if (orgName) {
        if (orgName.endsWith("'s Organization") || orgName.endsWith('\u2019s Organization')) {
            orgSuffix = ' (personal)';
        } else {
            orgSuffix = ' (' + orgName + ')';
        }
    }

    let labelPrefix = '';
    if (displayName) {
        const emailPrefix = email.split('@')[0].split('.')[0].toLowerCase();
        if (displayName.toLowerCase() !== emailPrefix && displayName.toLowerCase() !== 'user') {
            labelPrefix = displayName + ' \u2014 ';
        }
    }

    return labelPrefix + email + orgSuffix;
}

// ---------------------------------------------------------------------------
// Swap log table
// ---------------------------------------------------------------------------

// Residency below this is flagged red -- matches the backend's 15-min
// post-manual-switch hold (auto_swap_paused_until).
const RESIDENCY_WARN_SECONDS = 900;

function formatResidency(seconds) {
    if (seconds == null || !isFinite(seconds) || seconds < 0) return '\u2014';
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s % 60}s`;
    return `${s % 60}s`;
}

function renderSwapStatusBadge(status) {
    // Pre-migration rows have no status -- they were committed swaps.
    const st = status || 'committed';
    if (st === 'failed') {
        return '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-red-900/50 text-red-300">failed</span>';
    }
    if (st === 'pending') {
        return '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-slate-700/50 text-slate-400">pending</span>';
    }
    return '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-teal-900/50 text-teal-300">committed</span>';
}

function renderSwapLogTable(entries) {
    if (!entries || entries.length === 0) {
        return '<div class="text-xs text-slate-500">No recent swaps</div>';
    }

    const rows = entries.map(e => {
        const ts = e.timestamp
            ? escapeHtml(new Date(e.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }))
            : '\u2014';
        const from = escapeHtml(formatAccountLabel(e, 'from'));
        const to = escapeHtml(formatAccountLabel(e, 'to'));
        const reason = escapeHtml(e.reason || '\u2014');
        const statusBadge = renderSwapStatusBadge(e.status);
        const res = e.residency_seconds;
        const residencyCls = (res != null && res < RESIDENCY_WARN_SECONDS) ? 'text-red-400' : 'text-slate-400';
        const residency = escapeHtml(formatResidency(res));
        return `
            <tr class="border-t border-slate-700/30">
                <td class="py-1.5 pr-3 text-xs text-slate-400 whitespace-nowrap">${ts}</td>
                <td class="py-1.5 pr-3 text-xs text-slate-300 whitespace-nowrap">${from} \u2192 ${to}</td>
                <td class="py-1.5 pr-3 text-xs whitespace-nowrap">${statusBadge}</td>
                <td class="py-1.5 pr-3 text-xs ${residencyCls} whitespace-nowrap">${residency}</td>
                <td class="py-1.5 text-xs text-slate-500">${reason}</td>
            </tr>
        `;
    }).join('');

    return `
        <table class="w-full">
            <thead>
                <tr class="text-left">
                    <th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Time</th>
                    <th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Swap</th>
                    <th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Status</th>
                    <th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Residency</th>
                    <th class="pb-1 text-xs text-slate-500 font-medium">Reason</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
}

// ---------------------------------------------------------------------------
// Decision log table (full decision trace with expandable rows)
// ---------------------------------------------------------------------------

function renderDecisionLogTable(entries) {
    if (!entries || entries.length === 0) {
        return '<div class="text-xs text-slate-500">No decisions recorded yet</div>';
    }

    const rows = entries.map(function(e) {
        const ts = e.timestamp
            ? escapeHtml(new Date(e.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }))
            : '\u2014';

        const action = e.action || 'unknown';
        let actionBadge = '';
        if (action === 'swap') {
            actionBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-teal-900/50 text-teal-300">swap</span>';
        } else if (action === 'manual_switch') {
            actionBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-900/50 text-blue-300">manual</span>';
        } else {
            actionBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-slate-700/50 text-slate-400">check</span>';
        }

        const reason = escapeHtml(e.reason || '\u2014');
        const hasDetail = e.detail && typeof e.detail === 'object';
        const detailId = 'decision-detail-' + e.id;
        const accountLabel = hasDetail && e.detail.active
            ? escapeHtml(e.detail.active.label || e.detail.active.email || '\u2014')
            : '\u2014';

        let detailHtml = '';
        if (hasDetail) {
            detailHtml = buildDecisionDetailHtml(e.detail, detailId);
        }

        const clickHandler = hasDetail
            ? ' onclick="document.getElementById(\'' + detailId + '\').classList.toggle(\'hidden\')"'
            : '';
        const rowClass = hasDetail
            ? 'border-t border-slate-700/30 cursor-pointer hover:bg-slate-800/50'
            : 'border-t border-slate-700/30';

        return '<tr class="' + rowClass + '"' + clickHandler + '>' +
            '<td class="py-1.5 pr-3 text-xs text-slate-400 whitespace-nowrap">' + ts + '</td>' +
            '<td class="py-1.5 pr-3 text-xs">' + actionBadge + '</td>' +
            '<td class="py-1.5 pr-3 text-xs text-slate-300">' + accountLabel + '</td>' +
            '<td class="py-1.5 text-xs text-slate-500">' + reason + '</td>' +
            '</tr>' + detailHtml;
    }).join('');

    return '<table class="w-full">' +
        '<thead><tr class="text-left">' +
        '<th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Time</th>' +
        '<th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Action</th>' +
        '<th class="pb-1 pr-3 text-xs text-slate-500 font-medium">Account</th>' +
        '<th class="pb-1 text-xs text-slate-500 font-medium">Reason</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table>';
}

const TIER_LABELS = {
    0: 'T0 (<24h)',
    1: 'T1 (24-48h)',
    2: 'T2 (48h-4d)',
    3: 'T3 (4-7d)',
    4: 'T?',
};

const TIER_TOOLTIPS = {
    0: 'Tier 0: 7d window expires in under 24 hours. Drain to 100% — capacity not used will be lost.',
    1: 'Tier 1: 7d window expires in 24-48 hours. Drive to 90% (10% buffer for last-day work).',
    2: 'Tier 2: 7d window expires in 2-4 days. Stay slightly ahead of the white bar (week-elapsed line).',
    3: 'Tier 3: 7d window expires in 4-7 days. Don\'t exceed the white bar — save capacity for later.',
    4: 'No 7d data, or window already expired — excluded from selection.',
};

function buildDecisionDetailHtml(d, detailId) {
    const parts = [];

    // Active account info
    if (d.active) {
        parts.push(
            '<div class="mb-2"><span class="text-slate-400">Active:</span> ' +
            escapeHtml(d.active.label || d.active.email || '?') +
            ' <span class="text-slate-500">(5h=' +
            escapeHtml(String(d.active['5h'] != null ? d.active['5h'] : '?')) +
            '% 7d=' +
            escapeHtml(String(d.active['7d'] != null ? d.active['7d'] : '?')) +
            '%)</span></div>'
        );
    }

    // Candidates table
    if (d.candidates && d.candidates.length > 0) {
        let table = '<div class="mb-2"><span class="text-slate-400">Candidates evaluated:</span></div>' +
            '<table class="w-full text-[10px] mb-2"><thead><tr class="text-slate-500">' +
            '<th class="text-left pr-2">Account</th>' +
            '<th class="pr-2 text-right">5h</th>' +
            '<th class="pr-2 text-right">7d</th>' +
            '<th class="pr-2">Tier</th>' +
            '<th class="pr-2 text-right">Target</th>' +
            '<th class="pr-2 text-right">Deficit</th>' +
            '<th class="pr-2 text-center">Best</th>' +
            '</tr></thead><tbody>';

        d.candidates.forEach(function(c) {
            const isBest = c.is_best === true;
            const rowCls = isBest ? 'text-teal-300' : 'text-slate-400';
            const tierLabel = TIER_LABELS[c.tier] != null ? TIER_LABELS[c.tier] : '\u2014';
            const fivehStr = c['5h'] != null ? Number(c['5h']).toFixed(1) + '%' : '\u2014';
            const sevendStr = c['7d'] != null ? Number(c['7d']).toFixed(1) + '%' : '\u2014';
            const targetStr = c.target_7d != null ? Number(c.target_7d).toFixed(1) + '%' : '\u2014';
            let deficitStr = '\u2014';
            if (c.deficit != null) {
                const deficit = Number(c.deficit);
                deficitStr = (deficit >= 0 ? '+' : '') + deficit.toFixed(1) + '%';
            }
            const bestStr = isBest ? '\u2713' : '';
            table += '<tr class="' + rowCls + '">' +
                '<td class="pr-2">' + escapeHtml(c.label || c.email || '?') + '</td>' +
                '<td class="pr-2 text-right">' + escapeHtml(fivehStr) + '</td>' +
                '<td class="pr-2 text-right">' + escapeHtml(sevendStr) + '</td>' +
                '<td class="pr-2"><span title="' +
                escapeHtml(TIER_TOOLTIPS[c.tier] || '') +
                '" class="cursor-help">' + escapeHtml(tierLabel) + '</span></td>' +
                '<td class="pr-2 text-right">' + escapeHtml(targetStr) + '</td>' +
                '<td class="pr-2 text-right">' + escapeHtml(deficitStr) + '</td>' +
                '<td class="pr-2 text-center text-teal-300">' + bestStr + '</td>' +
                '</tr>';
        });
        table += '</tbody></table>';
        parts.push(table);
    }

    // Decision flags
    parts.push(
        '<div class="text-slate-500">should_swap=' +
        escapeHtml(String(d.should_swap)) +
        ' cooldown=' + escapeHtml(String(d.cooldown_active)) +
        '</div>'
    );

    return '<tr id="' + escapeHtml(detailId) + '" class="hidden">' +
        '<td colspan="4" class="py-2 px-3 bg-slate-900/50 border-t border-slate-700/30">' +
        '<div class="text-[11px] font-mono">' + parts.join('') + '</div></td></tr>';
}

async function renderDecisionLog(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const showAll = container.dataset.showAll === 'true';
    const entries = await loadDecisionLog(showAll);

    const toggleLabel = showAll ? 'Show swaps only' : 'Show all checks';
    const toggleBtn = '<button onclick="toggleDecisionLogFilter()" ' +
        'class="text-xs text-teal-400 hover:text-teal-300 mb-2">' +
        escapeHtml(toggleLabel) + '</button>';

    container.textContent = '';
    const btnDiv = document.createElement('div');
    btnDiv.innerHTML = toggleBtn;
    container.appendChild(btnDiv);

    const tableDiv = document.createElement('div');
    tableDiv.innerHTML = renderDecisionLogTable(entries);
    container.appendChild(tableDiv);
}

function toggleDecisionLogFilter() {
    const container = document.getElementById('decision-log-container');
    if (!container) return;
    container.dataset.showAll = container.dataset.showAll === 'true' ? 'false' : 'true';
    renderDecisionLog('decision-log-container');
}

// ---------------------------------------------------------------------------
// Exhaustion banner (persistent, replaces disappearing toast)
// ---------------------------------------------------------------------------

function renderExhaustionBanner() {
    const el = document.getElementById('swap-exhaustion-banner');
    if (!el) return;
    const data = window.jackedState._exhaustionData;
    if (!data) {
        el.innerHTML = '';
        el.classList.add('hidden');
        return;
    }
    let recoveryText = '';
    if (data.next_recovery_at) {
        const recoverAt = new Date(data.next_recovery_at);
        const now = Date.now();
        const diffMin = Math.max(0, Math.ceil((recoverAt.getTime() - now) / 60000));
        if (diffMin > 60) {
            recoveryText = `Next 5h window opens in ${Math.floor(diffMin/60)}h ${diffMin%60}m`;
        } else if (diffMin > 0) {
            recoveryText = `Next 5h window opens in ${diffMin}m`;
        } else {
            recoveryText = 'A 5h window should be opening soon';
        }
    }
    el.classList.remove('hidden');
    el.innerHTML = `
        <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200 flex items-center gap-3">
            <svg class="w-5 h-5 shrink-0 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
            <div>
                <div class="font-medium">All accounts at capacity</div>
                <div class="text-xs text-red-300 mt-0.5">${recoveryText}</div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Auto-swap stall banner (silent-stall watchdog)
// ---------------------------------------------------------------------------

function showStallBanner(data) {
    const banner = document.getElementById('auto-swap-stall-banner');
    if (!banner) return;
    const ageMin = Math.round((data.last_fetch_age_seconds || 0) / 60);
    banner.classList.remove('hidden');
    const textEl = banner.querySelector('.stall-text');
    if (textEl) {
        // Pattern (d): a candidate exists every tick but the same-tier
        // rule keeps the loop on stay. Surface that distinct case.
        if (data.best_account_id != null && data.best_deficit != null) {
            const def = Number(data.best_deficit).toFixed(1);
            textEl.textContent = (
                `Auto-swap holding on same-tier candidate: account ` +
                `${data.best_account_id} is ${def}% behind tier target ` +
                `but rules say stay (${data.consecutive_ticks} ticks).`
            );
        } else {
            textEl.textContent = (
                `Auto-swap stalled: ${data.consecutive_ticks} consecutive ` +
                `ticks with no eligible candidate. Active account data is ` +
                `${ageMin} min old. Try refreshing usage manually.`
            );
        }
    }
}

function hideStallBanner(_data) {
    const banner = document.getElementById('auto-swap-stall-banner');
    if (!banner) return;
    banner.classList.add('hidden');
}

// ---------------------------------------------------------------------------
// Drain-advisor banner (expiring account with stranded 7d capacity)
// ---------------------------------------------------------------------------

// Mirrors the backend's _DRAIN_ADVISOR_STRANDING_THRESHOLD: a newer event
// at or below this means the account is draining fine again.
const DRAIN_ADVISOR_CLEAR_THRESHOLD = 2;
const _drainAdvisorResetTimers = {};  // accountId -> setTimeout id

function _formatTimeUntil(iso) {
    if (!iso) return 'soon';
    const ms = new Date(iso).getTime() - Date.now();
    if (!isFinite(ms) || ms <= 0) return 'soon';
    const min = Math.ceil(ms / 60000);
    if (min >= 1440) return `in ${Math.floor(min / 1440)}d ${Math.floor((min % 1440) / 60)}h`;
    if (min >= 60) return `in ${Math.floor(min / 60)}h ${min % 60}m`;
    return `in ${min}m`;
}

function handleDrainAdvisorEvent(data) {
    if (!data || data.account_id == null) return;
    const id = data.account_id;
    if (data.stranding != null && Number(data.stranding) <= DRAIN_ADVISOR_CLEAR_THRESHOLD) {
        clearDrainAdvisor(id);
        return;
    }
    showDrainAdvisorBanner(data);
    // Stranded capacity is moot once the 7d window rolls -- auto-clear then.
    if (_drainAdvisorResetTimers[id] !== undefined) {
        clearTimeout(_drainAdvisorResetTimers[id]);
        delete _drainAdvisorResetTimers[id];
    }
    if (data.resets_at) {
        const delay = new Date(data.resets_at).getTime() - Date.now();
        if (isFinite(delay) && delay > 0) {
            _drainAdvisorResetTimers[id] = setTimeout(() => clearDrainAdvisor(id), delay);
        }
    }
}

function showDrainAdvisorBanner(data) {
    const existing = document.getElementById('drain-advisor-banner');
    if (existing) existing.remove();
    const banner = document.createElement('div');
    banner.id = 'drain-advisor-banner';
    banner.dataset.accountId = String(data.account_id);
    // top-16 sits below the fixed stall banner (top-4) so the red stall
    // state is never obscured by an advisory.
    banner.className = 'fixed top-16 left-1/2 -translate-x-1/2 z-50 bg-amber-900/95 border border-amber-600 rounded-lg px-5 py-3 shadow-lg max-w-lg flex items-center gap-3';
    // Info icon built via DOM API (same pattern as _usageCreateIcon).
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'w-5 h-5 shrink-0 text-amber-400');
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    const iconPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    iconPath.setAttribute('stroke-linecap', 'round');
    iconPath.setAttribute('stroke-linejoin', 'round');
    iconPath.setAttribute('stroke-width', '2');
    iconPath.setAttribute('d', 'M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z');
    icon.appendChild(iconPath);
    banner.appendChild(icon);
    const text = document.createElement('span');
    text.className = 'text-sm text-amber-100';
    const label = data.label || data.email || `account ${data.account_id}`;
    const stranding = data.stranding != null ? Math.round(Number(data.stranding)) : '?';
    text.textContent = `Account ${label} expires ${_formatTimeUntil(data.resets_at)} ` +
        `with ~${stranding}% capacity that won't be drained at current pace — ` +
        `route work there now`;
    const close = document.createElement('button');
    close.className = 'text-amber-400 hover:text-white text-lg leading-none ml-auto';
    close.textContent = '×';
    close.setAttribute('aria-label', 'Dismiss');
    close.onclick = function() { banner.remove(); };
    banner.appendChild(text);
    banner.appendChild(close);
    document.body.appendChild(banner);
}

function clearDrainAdvisor(accountId) {
    if (_drainAdvisorResetTimers[accountId] !== undefined) {
        clearTimeout(_drainAdvisorResetTimers[accountId]);
        delete _drainAdvisorResetTimers[accountId];
    }
    const banner = document.getElementById('drain-advisor-banner');
    if (banner && banner.dataset.accountId === String(accountId)) banner.remove();
}

// ---------------------------------------------------------------------------
// Same-tier deficit advisory (informational -- intended-behavior stay)
// ---------------------------------------------------------------------------

function showSameTierAdvisory(data) {
    if (typeof showToast !== 'function' || !data) return;
    const tier = data.best_tier != null ? `T${data.best_tier}` : 'same-tier';
    const deficit = data.best_deficit != null ? `${Number(data.best_deficit).toFixed(1)}%` : '?%';
    const acct = data.best_account_id != null ? data.best_account_id : '?';
    showToast(
        `Same-tier advisory: account ${acct} (${tier}) is ${deficit} behind its ` +
        `tier target — staying on the active account by design`,
        'info', 8000
    );
}

// ---------------------------------------------------------------------------
// Event binding
// ---------------------------------------------------------------------------

function bindAutoSwapEvents() {
    // Collapsible panel toggle
    const toggleBtn = document.getElementById('btn-toggle-swap-panel');
    const panelBody = document.getElementById('swap-panel-body');
    const panelArrow = document.getElementById('swap-panel-arrow');
    if (toggleBtn && panelBody) {
        toggleBtn.addEventListener('click', () => {
            const isHidden = panelBody.classList.contains('hidden');
            panelBody.classList.toggle('hidden');
            if (panelArrow) {
                panelArrow.style.transform = isHidden ? 'rotate(180deg)' : '';
            }
        });
    }

    // Pause / Resume
    const pauseSel = document.getElementById('sel-swap-pause');
    if (pauseSel) {
        pauseSel.addEventListener('change', async () => {
            const minutes = parseInt(pauseSel.value);
            if (!minutes) return;
            try {
                const res = await api.post(`/api/settings/swap-pause?minutes=${minutes}`);
                window.jackedState.swapSettings.auto_swap_paused_until = res.paused_until;
                window.jackedState._uiPauseUntil = res.paused_until;
                showToast(`Auto-swap paused for ${minutes} min`, 'success', 3000);
                if (typeof renderPage === 'function') renderPage();
            } catch (e) {
                showToast(e.message || 'Failed to pause', 'error');
            }
        });
    }
    const resumeBtn = document.getElementById('btn-swap-resume');
    if (resumeBtn) {
        resumeBtn.addEventListener('click', async () => {
            try {
                await api.post('/api/settings/swap-resume');
                window.jackedState.swapSettings.auto_swap_paused_until = null;
                window.jackedState._uiPauseUntil = null;
                showToast('Auto-swap resumed', 'success', 2000);
                if (typeof renderPage === 'function') renderPage();
            } catch (e) {
                showToast(e.message || 'Failed to resume', 'error');
            }
        });
    }

    // Slider live labels
    _bindSliderLabel('rng-warn-5h', 'lbl-warn-5h');
    _bindSliderLabel('rng-crit-5h', 'lbl-crit-5h');
    _bindSliderLabel('rng-thresh-7d', 'lbl-thresh-7d');

    // Save on any change (debounced)
    const inputIds = [
        'chk-auto-swap', 'rng-warn-5h', 'rng-crit-5h', 'rng-thresh-7d',
        'sel-check-interval', 'chk-window-keeper',
        'inp-active-start', 'inp-active-end', 'inp-pre-wake',
    ];
    let saveTimer = null;
    for (const id of inputIds) {
        const el = document.getElementById(id);
        if (!el) continue;
        el.addEventListener('change', () => {
            clearTimeout(saveTimer);
            saveTimer = setTimeout(() => _saveSwapSettings(), 400);
        });
    }
}

function _bindSliderLabel(sliderId, labelId) {
    const slider = document.getElementById(sliderId);
    const label = document.getElementById(labelId);
    if (slider && label) {
        slider.addEventListener('input', () => {
            label.textContent = slider.value + '%';
        });
    }
}

async function _saveSwapSettings() {
    const settings = {
        auto_swap_enabled: document.getElementById('chk-auto-swap')?.checked || false,
        auto_swap_5h_warning: parseInt(document.getElementById('rng-warn-5h')?.value) || 80,
        auto_swap_5h_critical: parseInt(document.getElementById('rng-crit-5h')?.value) || 90,
        auto_swap_7d_threshold: parseInt(document.getElementById('rng-thresh-7d')?.value) || 85,
        usage_check_interval: parseInt(document.getElementById('sel-check-interval')?.value) || 300,
        window_keeper_enabled: document.getElementById('chk-window-keeper')?.checked || false,
        window_keeper_active_start: document.getElementById('inp-active-start')?.value || '06:00',
        window_keeper_active_end: document.getElementById('inp-active-end')?.value || '23:00',
        window_keeper_prewake: document.getElementById('inp-pre-wake')?.value || '04:00',
    };

    try {
        await api.put('/api/settings/swap-settings', settings);
        window.jackedState.swapSettings = settings;
        showToast('Settings saved', 'success', 2000);
    } catch (e) {
        console.error('Failed to save swap settings:', e);
        showToast(e.message || 'Failed to save settings', 'error');
    }
}

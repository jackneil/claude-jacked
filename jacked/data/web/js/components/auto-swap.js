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
        const data = await api.get('/api/settings/swap-log?limit=10');
        return data.entries || data || [];
    } catch (e) {
        console.error('Failed to load swap log:', e);
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
    let isPaused = false;
    if (pausedUntil) {
        const pauseEnd = new Date(pausedUntil);
        const nowMs = Date.now();
        const remainMs = pauseEnd.getTime() - nowMs;
        if (remainMs > 0) {
            isPaused = true;
            const remainMin = Math.ceil(remainMs / 60000);
            pauseLabel = remainMin >= 60
                ? `${Math.floor(remainMin / 60)}h ${remainMin % 60}m`
                : `${remainMin}m`;
        }
    }

    const wkEnabled = s.window_keeper_enabled || false;
    const activeStart = s.window_keeper_active_start || '06:00';
    const activeEnd = s.window_keeper_active_end || '23:00';
    const preWake = s.window_keeper_prewake || '04:00';

    return `
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
                                    <span class="text-slate-400 text-sm">Enable Auto-Swap</span>
                                    <label class="toggle-switch">
                                        <input type="checkbox" id="chk-auto-swap" ${autoSwapEnabled ? 'checked' : ''}>
                                        <span class="toggle-slider"></span>
                                    </label>
                                </div>

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

                        <!-- Section 3: Recent Swaps (collapsed by default) -->
                        <div class="border-t border-slate-700/50 pt-4">
                            <button id="btn-toggle-swap-log" class="flex items-center gap-2 text-sm text-slate-400 hover:text-slate-200 transition-colors">
                                <span>Recent Swaps</span>
                                <svg id="swap-log-arrow" class="w-3 h-3 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                            </button>
                            <div id="swap-log-container" class="hidden mt-3">
                                <div class="text-xs text-slate-500">Loading...</div>
                            </div>
                        </div>

                    </div>
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Swap log table
// ---------------------------------------------------------------------------

function renderSwapLogTable(entries) {
    if (!entries || entries.length === 0) {
        return '<div class="text-xs text-slate-500">No recent swaps</div>';
    }

    const rows = entries.map(e => {
        const ts = e.timestamp
            ? new Date(e.timestamp * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
            : '\u2014';
        const from = escapeHtml(e.from_email || '\u2014');
        const to = escapeHtml(e.to_email || '\u2014');
        const reason = escapeHtml(e.reason || '\u2014');
        return `
            <tr class="border-t border-slate-700/30">
                <td class="py-1.5 pr-3 text-xs text-slate-400 whitespace-nowrap">${ts}</td>
                <td class="py-1.5 pr-3 text-xs text-slate-300 whitespace-nowrap">${from} \u2192 ${to}</td>
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
                    <th class="pb-1 text-xs text-slate-500 font-medium">Reason</th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>
    `;
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

    // Recent swaps sub-collapse
    const logToggle = document.getElementById('btn-toggle-swap-log');
    const logContainer = document.getElementById('swap-log-container');
    const logArrow = document.getElementById('swap-log-arrow');
    let logLoaded = false;
    if (logToggle && logContainer) {
        logToggle.addEventListener('click', async () => {
            const isHidden = logContainer.classList.contains('hidden');
            logContainer.classList.toggle('hidden');
            if (logArrow) {
                logArrow.style.transform = isHidden ? 'rotate(180deg)' : '';
            }
            // Lazy-load log on first expand
            if (isHidden && !logLoaded) {
                logLoaded = true;
                const entries = await loadSwapLog();
                logContainer.textContent = '';
                const temp = document.createElement('div');
                temp.insertAdjacentHTML('afterbegin', renderSwapLogTable(entries));
                while (temp.firstChild) logContainer.appendChild(temp.firstChild);
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

/**
 * jacked web dashboard — log viewer
 * Sub-tabbed view: Gatekeeper | Hooks | Version Checks
 */

// ---------------------------------------------------------------------------
// Gatekeeper state (preserved from original)
// ---------------------------------------------------------------------------
let logsAutoRefresh = true;
let logsRefreshTimer = null;
let logsFilter = 'ALL';
let logsSearch = '';
let logsActiveSession = 'ALL';
let logsSessions = [];

// ---------------------------------------------------------------------------
// Active sub-tab
// ---------------------------------------------------------------------------
let logsSubTab = localStorage.getItem('jacked_logs_subtab') || 'gatekeeper';

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function formatDuration(ms) {
    if (ms == null) return '-';
    if (ms < 1) return '<1ms';
    if (ms < 1000) return Math.round(ms) + 'ms';
    return (ms / 1000).toFixed(1) + 's';
}

function successBadge(val) {
    if (val === true || val === 1) return '<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-green-700 text-green-100">OK</span>';
    if (val === false || val === 0) return '<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-red-700 text-red-100">FAIL</span>';
    return '<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-slate-700 text-slate-300">-</span>';
}

function boolBadge(val, trueLabel, falseLabel) {
    if (val === true || val === 1) return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-yellow-700 text-yellow-100">${escapeHtml(trueLabel)}</span>`;
    return `<span class="inline-block px-2 py-0.5 rounded text-xs font-medium bg-slate-700 text-slate-300">${escapeHtml(falseLabel)}</span>`;
}

function repoShort(path) {
    if (!path) return '';
    return path.replace(/\\/g, '/').split('/').filter(Boolean).pop() || path;
}

function formatLogTs(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z');
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        return `${date} ${time}`;
    } catch { return isoStr; }
}

// ---------------------------------------------------------------------------
// Render top-level logs page shell with sub-tab bar
// ---------------------------------------------------------------------------

function renderLogs() {
    const tabs = [
        { id: 'gatekeeper', label: 'Gatekeeper' },
        { id: 'hooks', label: 'Hooks' },
        { id: 'version-checks', label: 'Version Checks' },
    ];

    const tabBar = tabs.map(t => `
        <button class="logs-subtab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors
            ${logsSubTab === t.id
                ? 'bg-slate-800 text-white border-t-2 border-x border-t-blue-500 border-x-slate-700 -mb-px'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/50'}"
            data-subtab="${t.id}">
            ${t.label}
        </button>
    `).join('');

    return `
        <div class="max-w-6xl">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold text-white">Logs</h2>
            </div>
            <div class="flex items-end gap-1 border-b border-slate-700 mb-0">
                ${tabBar}
            </div>
            <div id="logs-subtab-content" class="bg-slate-800/30 border border-slate-700 border-t-0 rounded-b-lg p-4">
                <div class="flex items-center justify-center py-12">
                    <div class="spinner"></div>
                    <span class="ml-3 text-slate-400 text-sm">Loading...</span>
                </div>
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Event binding — called after renderLogs() HTML is in the DOM
// ---------------------------------------------------------------------------

function bindLogsEvents() {
    // Sub-tab click handlers
    document.querySelectorAll('.logs-subtab').forEach(btn => {
        btn.addEventListener('click', () => {
            logsSubTab = btn.dataset.subtab;
            localStorage.setItem('jacked_logs_subtab', logsSubTab);
            // Re-highlight tabs
            document.querySelectorAll('.logs-subtab').forEach(b => {
                if (b.dataset.subtab === logsSubTab) {
                    b.className = b.className
                        .replace('text-slate-400 hover:text-slate-200 hover:bg-slate-800/50', '')
                        + ' bg-slate-800 text-white border-t-2 border-x border-t-blue-500 border-x-slate-700 -mb-px';
                } else {
                    b.className = 'logs-subtab px-4 py-2 text-sm font-medium rounded-t-lg transition-colors text-slate-400 hover:text-slate-200 hover:bg-slate-800/50';
                }
            });
            renderSubTab();
        });
    });

    renderSubTab();
}

function renderSubTab() {
    const container = document.getElementById('logs-subtab-content');
    if (!container) return;

    // Stop existing timer when switching sub-tabs (preserve preference)
    if (logsRefreshTimer) {
        clearInterval(logsRefreshTimer);
        logsRefreshTimer = null;
    }

    switch (logsSubTab) {
        case 'gatekeeper': renderGatekeeperSubTab(container); break;
        case 'hooks':      renderHookLogs(container); break;
        case 'version-checks': renderVersionCheckLogs(container); break;
        default: container.innerHTML = '<div class="text-slate-500 p-4">Unknown log type</div>';
    }

    // Restart auto-refresh if enabled (handles initial load + tab switches)
    if (logsAutoRefresh) {
        toggleLogsAutoRefresh(true);
    }
}

// ============================================================================
// GATEKEEPER SUB-TAB (existing functionality, refactored into sub-tab)
// ============================================================================

function renderGatekeeperSubTab(container) {
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="text-sm text-slate-400">Security gatekeeper decisions</div>
            <div class="flex flex-wrap items-center gap-2">
                <input id="logs-search" type="text" placeholder="Search commands..."
                    class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500 w-full sm:w-48"
                    value="${escapeHtml(logsSearch)}">
                <select id="logs-filter" class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-blue-500">
                    <option value="ALL" ${logsFilter === 'ALL' ? 'selected' : ''}>All Decisions</option>
                    <option value="ALLOW" ${logsFilter === 'ALLOW' ? 'selected' : ''}>Allowed</option>
                    <option value="ASK_USER" ${logsFilter === 'ASK_USER' ? 'selected' : ''}>Asked User</option>
                </select>
                <label class="flex items-center gap-2 text-sm text-slate-400 cursor-pointer select-none">
                    <input id="logs-auto-refresh" type="checkbox" class="rounded" ${logsAutoRefresh ? 'checked' : ''}>
                    Auto-refresh
                </label>
                <button id="logs-export-btn" title="Export as JSON"
                    class="p-1.5 rounded-lg bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition-colors">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
                    </svg>
                </button>
                <button id="logs-purge-btn" title="Purge old logs"
                    class="p-1.5 rounded-lg bg-slate-900 border border-slate-700 hover:border-red-500 text-slate-400 hover:text-red-300 transition-colors">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                    </svg>
                </button>
            </div>
        </div>

        <div id="logs-purge-bar" class="hidden mb-4"></div>

        <div id="logs-sessions" class="mb-4">
            <div class="flex items-center gap-2 text-xs text-slate-500">
                <div class="spinner" style="width:14px;height:14px"></div> Loading sessions...
            </div>
        </div>

        <div id="logs-content">
            <div class="flex items-center justify-center py-12">
                <div class="spinner"></div>
                <span class="ml-3 text-slate-400 text-sm">Loading logs...</span>
            </div>
        </div>
    `;

    bindGatekeeperLogsEvents();
    loadSessions().then(() => loadLogsData());
}

function bindGatekeeperLogsEvents() {
    const filterEl = document.getElementById('logs-filter');
    if (filterEl) {
        filterEl.addEventListener('change', () => {
            logsFilter = filterEl.value;
            loadLogsData();
        });
    }

    const searchEl = document.getElementById('logs-search');
    if (searchEl) {
        let debounce = null;
        searchEl.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                logsSearch = searchEl.value;
                loadLogsData();
            }, 300);
        });
    }

    const autoRefreshEl = document.getElementById('logs-auto-refresh');
    if (autoRefreshEl) {
        autoRefreshEl.addEventListener('change', () => {
            toggleLogsAutoRefresh(autoRefreshEl.checked);
        });
    }

    const exportBtn = document.getElementById('logs-export-btn');
    if (exportBtn) exportBtn.addEventListener('click', doExport);

    const purgeBtn = document.getElementById('logs-purge-btn');
    if (purgeBtn) purgeBtn.addEventListener('click', showPurgeBar);
}

// ---------------------------------------------------------------------------
// Session cards (unchanged)
// ---------------------------------------------------------------------------

function getRepoName(repoPath) {
    if (!repoPath) return '';
    return repoPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || repoPath;
}

function isSessionActive(lastSeen) {
    if (!lastSeen) return false;
    try {
        const d = new Date(lastSeen + 'Z');
        return (Date.now() - d.getTime()) < 5 * 60 * 1000;
    } catch { return false; }
}

function renderSessionCards(sessions, activeId) {
    const totalDecisions = sessions.reduce((sum, s) => sum + (s.total || 0), 0);

    const allCard = `
        <button class="session-card flex-shrink-0 rounded-lg px-3 py-2.5 text-left transition-all cursor-pointer min-w-[80px]
            ${activeId === 'ALL'
                ? 'bg-blue-900/40 border-2 border-blue-500 ring-1 ring-blue-500/30'
                : 'bg-slate-800 border border-slate-700 hover:border-slate-500'}"
            data-session="ALL">
            <div class="text-xs font-semibold text-slate-300 uppercase tracking-wider">All</div>
            <div class="text-lg font-bold text-white mt-0.5">${totalDecisions}</div>
            <div class="text-xs text-slate-500">decisions</div>
        </button>
    `;

    const sessionCards = sessions.map(s => {
        const sid = s.session_id || '';
        const shortId = sid.substring(0, 8);
        const repo = getRepoName(s.repo_path);
        const active = isSessionActive(s.last_seen);
        const isSelected = activeId === sid;
        const firstTime = formatLogTimestamp(s.first_seen);
        const lastTime = formatLogTimestamp(s.last_seen);

        return `
            <button class="session-card flex-shrink-0 rounded-lg px-3 py-2.5 text-left transition-all cursor-pointer min-w-[180px] max-w-[240px]
                ${isSelected
                    ? 'bg-blue-900/40 border-2 border-blue-500 ring-1 ring-blue-500/30'
                    : 'bg-slate-800 border border-slate-700 hover:border-slate-500'}"
                data-session="${escapeHtml(sid)}">
                <div class="flex items-center gap-2">
                    <span class="font-mono text-xs font-semibold ${isSelected ? 'text-blue-300' : 'text-slate-300'}">${shortId}</span>
                    ${active ? '<span class="w-2 h-2 rounded-full bg-green-400 animate-pulse"></span>' : ''}
                </div>
                <div class="text-xs text-slate-400 mt-1 truncate" title="${escapeHtml(s.repo_path || '')}">${escapeHtml(repo)}</div>
                <div class="flex items-center gap-2 mt-1.5">
                    <span class="text-xs font-medium text-white">${s.total || 0}</span>
                    ${(s.allowed || 0) > 0 ? `<span class="inline-block px-1.5 py-0 rounded text-[10px] font-medium bg-green-800/60 text-green-300">${s.allowed}A</span>` : ''}
                    ${(s.asked || 0) > 0 ? `<span class="inline-block px-1.5 py-0 rounded text-[10px] font-medium bg-yellow-800/60 text-yellow-300">${s.asked}U</span>` : ''}
                </div>
                <div class="text-[10px] text-slate-500 mt-1">${firstTime} → ${lastTime}</div>
            </button>
        `;
    }).join('');

    return `
        <div class="flex gap-2 overflow-x-auto pb-2 scrollbar-thin" style="scrollbar-width: thin;">
            ${allCard}
            ${sessionCards}
        </div>
    `;
}

async function loadSessions() {
    try {
        logsSessions = await api.get('/api/logs/sessions');
    } catch (e) {
        console.error('Failed to load sessions:', e);
        logsSessions = [];
    }

    const container = document.getElementById('logs-sessions');
    if (!container) return;

    if (logsSessions.length === 0) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = renderSessionCards(logsSessions, logsActiveSession);

    container.querySelectorAll('.session-card').forEach(card => {
        card.addEventListener('click', () => {
            logsActiveSession = card.dataset.session;
            container.innerHTML = renderSessionCards(logsSessions, logsActiveSession);
            bindSessionCardClicks(container);
            loadLogsData();
        });
    });
}

function bindSessionCardClicks(container) {
    container.querySelectorAll('.session-card').forEach(card => {
        card.addEventListener('click', () => {
            logsActiveSession = card.dataset.session;
            container.innerHTML = renderSessionCards(logsSessions, logsActiveSession);
            bindSessionCardClicks(container);
            loadLogsData();
        });
    });
}

// ---------------------------------------------------------------------------
// Decision colors & formatting (gatekeeper-specific)
// ---------------------------------------------------------------------------

function getDecisionColors(decision, method) {
    if (decision === 'ALLOW') {
        if (method === 'PERMS' || method === 'LOCAL') {
            return { bg: 'bg-green-900/20', badge: 'bg-green-700 text-green-100' };
        }
        return { bg: 'bg-emerald-900/20', badge: 'bg-emerald-700 text-emerald-100' };
    }
    if (method === 'DENY_PATTERN') {
        return { bg: 'bg-red-900/20', badge: 'bg-red-700 text-red-100' };
    }
    return { bg: 'bg-yellow-900/20', badge: 'bg-yellow-700 text-yellow-100' };
}

function formatLogTimestamp(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr + 'Z');
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        return `${date} ${time}`;
    } catch { return isoStr; }
}

function truncateCommand(cmd, maxLen) {
    if (!cmd) return '';
    return cmd.length <= maxLen ? cmd : cmd.substring(0, maxLen) + '...';
}

// ---------------------------------------------------------------------------
// Gatekeeper decision table
// ---------------------------------------------------------------------------

async function loadLogsData() {
    const container = document.getElementById('logs-content');
    if (!container) return;

    try {
        let url = '/api/logs/gatekeeper?limit=500';
        if (logsFilter !== 'ALL') url += `&decision=${logsFilter}`;
        if (logsActiveSession !== 'ALL') url += `&session_id=${encodeURIComponent(logsActiveSession)}`;

        const rows = await api.get(url);

        let filtered = rows;
        if (logsSearch) {
            const q = logsSearch.toLowerCase();
            filtered = rows.filter(r => (r.command || '').toLowerCase().includes(q));
        }

        if (filtered.length === 0) {
            container.innerHTML = `
                <div class="bg-slate-900 border border-slate-700 rounded-lg px-6 py-12 text-center">
                    <svg class="w-10 h-10 text-slate-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
                    </svg>
                    <div class="text-slate-400 text-sm">No gatekeeper decisions${logsActiveSession !== 'ALL' ? ' for this session' : ''}</div>
                    <div class="text-slate-500 text-xs mt-1">Decisions appear here as the gatekeeper evaluates commands</div>
                </div>
            `;
            return;
        }

        const showRepo = logsActiveSession === 'ALL';

        const rowsHtml = filtered.map((r, idx) => {
            const colors = getDecisionColors(r.decision, r.method);
            const cmd = escapeHtml(truncateCommand(r.command, 100));
            const fullCmd = escapeHtml(r.command || '');
            const method = escapeHtml(r.method || '-');
            const reason = r.reason ? escapeHtml(r.reason) : '';
            const elapsed = formatDuration(r.elapsed_ms);
            const ts = formatLogTimestamp(r.timestamp);
            const repo = getRepoName(r.repo_path);
            const fullRepo = escapeHtml(r.repo_path || '');
            const session = r.session_id ? r.session_id.substring(0, 8) : '';
            const fullSession = escapeHtml(r.session_id || '');
            const colSpan = showRepo ? 6 : 5;

            return `
                <tr class="${colors.bg} hover:bg-slate-700/50 transition-colors cursor-pointer log-row" data-row="${idx}">
                    <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap font-mono">${ts}</td>
                    <td class="px-3 py-2">
                        <span class="inline-block px-2 py-0.5 rounded text-xs font-medium ${colors.badge}">${escapeHtml(r.decision)}</span>
                    </td>
                    <td class="px-3 py-2 text-xs text-slate-300 whitespace-nowrap">${method}</td>
                    <td class="px-3 py-2 text-sm font-mono text-slate-200 max-w-md truncate">${cmd}</td>
                    <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap text-right">${elapsed}</td>
                    ${showRepo
                        ? `<td class="px-3 py-2 text-xs text-slate-500 whitespace-nowrap font-mono">${escapeHtml(repo || session)}</td>`
                        : ''}
                </tr>
                <tr class="log-detail hidden" data-detail="${idx}">
                    <td colspan="${colSpan}" class="px-4 py-3 ${colors.bg} border-t border-slate-700/30">
                        <div class="space-y-2">
                            <div>
                                <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Full Command</div>
                                <pre class="text-xs font-mono text-slate-200 whitespace-pre-wrap break-all bg-slate-900/50 rounded px-3 py-2 max-h-40 overflow-y-auto">${fullCmd}</pre>
                            </div>
                            ${reason ? `
                            <div>
                                <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Reason</div>
                                <div class="text-xs text-slate-300 italic">${reason}</div>
                            </div>` : ''}
                            <div class="flex gap-6 text-xs text-slate-400">
                                <div><span class="text-slate-500">Session:</span> <span class="font-mono">${fullSession}</span></div>
                                <div><span class="text-slate-500">Repo:</span> <span class="font-mono">${fullRepo}</span></div>
                                <div><span class="text-slate-500">Elapsed:</span> ${elapsed}</div>
                            </div>
                        </div>
                    </td>
                </tr>
            `;
        }).join('');

        container.innerHTML = `
            <div class="text-xs text-slate-500 mb-2">${filtered.length} decision${filtered.length !== 1 ? 's' : ''}${logsActiveSession !== 'ALL' ? ' in session ' + logsActiveSession.substring(0, 8) : ''}</div>
            <div class="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-slate-700">
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Time</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Decision</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Method</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Command</th>
                            <th class="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Elapsed</th>
                            ${showRepo ? '<th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Repo</th>' : ''}
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/50">
                        ${rowsHtml}
                    </tbody>
                </table>
            </div>
        `;

        container.querySelectorAll('.log-row').forEach(row => {
            row.addEventListener('click', () => {
                const detail = container.querySelector(`.log-detail[data-detail="${row.dataset.row}"]`);
                if (detail) detail.classList.toggle('hidden');
            });
        });
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load logs: ${escapeHtml(e.message)}
            </div>
        `;
    }
}

// ---------------------------------------------------------------------------
// Purge & Export (gatekeeper-specific, unchanged)
// ---------------------------------------------------------------------------

function showPurgeBar() {
    const bar = document.getElementById('logs-purge-bar');
    if (!bar) return;

    const sessionBtn = logsActiveSession !== 'ALL'
        ? `<button id="purge-session-btn" class="px-3 py-1 rounded text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">
               Purge this session (${logsActiveSession.substring(0, 8)})
           </button>`
        : '';

    bar.innerHTML = `
        <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3">
            <div class="flex items-center gap-3 flex-wrap">
                <svg class="w-4 h-4 text-red-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z"/>
                </svg>
                <span class="text-sm text-red-200">Purge logs older than</span>
                <select id="purge-age" class="bg-slate-800 border border-red-700 rounded px-2 py-1 text-xs text-slate-200 focus:outline-none">
                    <option value="1">1 day</option>
                    <option value="7" selected>7 days</option>
                    <option value="30">30 days</option>
                    <option value="all">All</option>
                </select>
                <button id="purge-confirm-btn" class="px-3 py-1 rounded text-xs font-medium bg-red-700 hover:bg-red-600 text-white transition-colors">
                    Purge
                </button>
                ${sessionBtn}
                <button id="purge-cancel-btn" class="px-3 py-1 rounded text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-300 transition-colors">
                    Cancel
                </button>
            </div>
            <div class="text-[11px] text-red-300/70 mt-1.5">Purged logs are also removed from Analytics.</div>
        </div>
    `;
    bar.classList.remove('hidden');

    document.getElementById('purge-confirm-btn').addEventListener('click', () => {
        const ageVal = document.getElementById('purge-age').value;
        if (ageVal === 'all') {
            doPurge(null, null);
        } else {
            doPurge(parseInt(ageVal, 10), null);
        }
    });

    document.getElementById('purge-cancel-btn').addEventListener('click', hidePurgeBar);

    const sessionPurgeBtn = document.getElementById('purge-session-btn');
    if (sessionPurgeBtn) {
        sessionPurgeBtn.addEventListener('click', () => doPurge(null, logsActiveSession));
    }
}

function hidePurgeBar() {
    const bar = document.getElementById('logs-purge-bar');
    if (bar) {
        bar.classList.add('hidden');
        bar.innerHTML = '';
    }
}

async function doPurge(olderThanDays, sessionId) {
    try {
        let url = '/api/logs/gatekeeper';
        const params = [];
        if (olderThanDays != null) params.push(`older_than_days=${olderThanDays}`);
        if (sessionId) params.push(`session_id=${encodeURIComponent(sessionId)}`);
        if (params.length) url += '?' + params.join('&');

        const res = await api.delete(url);
        const count = res.purged || 0;
        hidePurgeBar();
        showLogsToast(`${count} decision${count !== 1 ? 's' : ''} purged`);
        await loadSessions();
        await loadLogsData();
    } catch (e) {
        showLogsToast('Purge failed: ' + e.message, true);
    }
}

async function doExport() {
    try {
        let url = '/api/logs/gatekeeper/export?';
        const params = [];
        if (logsActiveSession !== 'ALL') params.push(`session_id=${encodeURIComponent(logsActiveSession)}`);
        if (logsFilter !== 'ALL') params.push(`decision=${logsFilter}`);
        url += params.join('&');

        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const disposition = resp.headers.get('Content-Disposition') || '';
        const match = disposition.match(/filename="(.+?)"/);
        const filename = match ? match[1] : 'gatekeeper-logs.json';

        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);

        showLogsToast('Exported ' + filename);
    } catch (e) {
        showLogsToast('Export failed: ' + e.message, true);
    }
}

function showLogsToast(msg, isError) {
    const existing = document.getElementById('logs-toast');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'logs-toast';
    toast.className = `fixed bottom-2 right-2 md:bottom-6 md:right-6 left-2 md:left-auto max-w-[calc(100vw-1rem)] md:max-w-sm px-4 py-2.5 rounded-lg text-sm font-medium shadow-lg z-50 transition-opacity duration-300 ${
        isError ? 'bg-red-800 text-red-100 border border-red-600' : 'bg-green-800 text-green-100 border border-green-600'
    }`;
    toast.textContent = msg;
    document.body.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 300); }, 3000);
}

// ---------------------------------------------------------------------------
// Auto-refresh (gatekeeper-specific)
// ---------------------------------------------------------------------------

function toggleLogsAutoRefresh(enabled) {
    logsAutoRefresh = enabled;
    if (logsRefreshTimer) {
        clearInterval(logsRefreshTimer);
        logsRefreshTimer = null;
    }
    if (enabled) {
        logsRefreshTimer = setInterval(() => {
            if (window.jackedState.activeRoute !== 'logs') {
                clearInterval(logsRefreshTimer);
                logsRefreshTimer = null;
                return;
            }
            switch (logsSubTab) {
                case 'gatekeeper': loadSessions(); loadLogsData(); break;
                case 'hooks': loadHookLogsData(); break;
                case 'version-checks': loadVersionCheckLogsData(); break;
            }
        }, 5000);
    }
}

// ============================================================================
// HOOKS SUB-TAB
// ============================================================================

async function renderHookLogs(container) {
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="text-sm text-slate-400">Hook execution history</div>
            <div class="flex flex-wrap items-center gap-2">
                <label class="flex items-center gap-2 text-sm text-slate-400 cursor-pointer select-none">
                    <input id="hook-auto-refresh" type="checkbox" class="rounded" ${logsAutoRefresh ? 'checked' : ''}>
                    Auto-refresh
                </label>
                <button id="hook-logs-refresh" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition-colors">
                    Refresh
                </button>
            </div>
        </div>
        <div id="hook-logs-content">
            <div class="flex items-center justify-center py-12">
                <div class="spinner"></div>
                <span class="ml-3 text-slate-400 text-sm">Loading hook logs...</span>
            </div>
        </div>
    `;

    document.getElementById('hook-logs-refresh')?.addEventListener('click', () => renderHookLogs(container));
    document.getElementById('hook-auto-refresh')?.addEventListener('change', (e) => toggleLogsAutoRefresh(e.target.checked));
    await loadHookLogsData();
}

async function loadHookLogsData() {
    const container = document.getElementById('hook-logs-content');
    if (!container) return;

    try {
        const data = await api.get('/api/logs/hooks?limit=300');
        const logs = data.logs || [];

        if (logs.length === 0) {
            container.innerHTML = `
                <div class="bg-slate-900 border border-slate-700 rounded-lg px-6 py-12 text-center">
                    <svg class="w-10 h-10 text-slate-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/>
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>
                    </svg>
                    <div class="text-slate-400 text-sm">No hook executions yet</div>
                    <div class="text-slate-500 text-xs mt-1">Hook runs will appear here as the gatekeeper fires hooks</div>
                </div>
            `;
            return;
        }

        const rowsHtml = logs.map((r, idx) => {
            const hasError = r.error_msg && r.error_msg.trim();
            return `
                <tr class="${hasError ? 'bg-red-900/10' : ''} hover:bg-slate-700/50 transition-colors ${hasError ? 'cursor-pointer hook-row' : ''}" data-row="${idx}">
                    <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap font-mono">${formatLogTs(r.timestamp)}</td>
                    <td class="px-3 py-2 text-sm font-mono text-slate-200">${escapeHtml(r.hook_name || '-')}</td>
                    <td class="px-3 py-2 text-xs text-slate-300">${escapeHtml(r.hook_type || '-')}</td>
                    <td class="px-3 py-2">${successBadge(r.success)}</td>
                    <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap text-right">${formatDuration(r.duration_ms)}</td>
                    <td class="px-3 py-2 text-xs text-slate-500 font-mono">${escapeHtml(r.session_id ? r.session_id.substring(0, 8) : '-')}</td>
                    <td class="px-3 py-2 text-xs text-slate-500 font-mono" title="${escapeHtml(r.repo_path || '')}">${escapeHtml(repoShort(r.repo_path))}</td>
                </tr>
                ${hasError ? `
                <tr class="hook-detail hidden" data-detail="${idx}">
                    <td colspan="7" class="px-4 py-3 bg-red-900/10 border-t border-slate-700/30">
                        <div class="text-[10px] uppercase tracking-wider text-slate-500 mb-1">Error</div>
                        <pre class="text-xs font-mono text-red-300 whitespace-pre-wrap break-all bg-slate-900/50 rounded px-3 py-2 max-h-40 overflow-y-auto">${escapeHtml(r.error_msg)}</pre>
                    </td>
                </tr>` : ''}
            `;
        }).join('');

        container.innerHTML = `
            <div class="text-xs text-slate-500 mb-2">${logs.length} execution${logs.length !== 1 ? 's' : ''}</div>
            <div class="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-slate-700">
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Time</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Hook</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Type</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Status</th>
                            <th class="px-3 py-2 text-right text-xs font-medium text-slate-400 uppercase">Duration</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Session</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Repo</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/50">
                        ${rowsHtml}
                    </tbody>
                </table>
            </div>
        `;

        // Bind click-to-expand on hook rows with errors
        container.querySelectorAll('.hook-row').forEach(row => {
            row.addEventListener('click', () => {
                const detail = container.querySelector(`.hook-detail[data-detail="${row.dataset.row}"]`);
                if (detail) detail.classList.toggle('hidden');
            });
        });
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load hook logs: ${escapeHtml(e.message)}
            </div>
        `;
    }
}

// ============================================================================
// VERSION CHECKS SUB-TAB
// ============================================================================

async function renderVersionCheckLogs(container) {
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="text-sm text-slate-400">Version check history</div>
            <div class="flex flex-wrap items-center gap-2">
                <label class="flex items-center gap-2 text-sm text-slate-400 cursor-pointer select-none">
                    <input id="ver-auto-refresh" type="checkbox" class="rounded" ${logsAutoRefresh ? 'checked' : ''}>
                    Auto-refresh
                </label>
                <button id="ver-logs-refresh" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition-colors">
                    Refresh
                </button>
            </div>
        </div>
        <div id="ver-logs-content">
            <div class="flex items-center justify-center py-12">
                <div class="spinner"></div>
                <span class="ml-3 text-slate-400 text-sm">Loading version checks...</span>
            </div>
        </div>
    `;

    document.getElementById('ver-logs-refresh')?.addEventListener('click', () => renderVersionCheckLogs(container));
    document.getElementById('ver-auto-refresh')?.addEventListener('change', (e) => toggleLogsAutoRefresh(e.target.checked));
    await loadVersionCheckLogsData();
}

async function loadVersionCheckLogsData() {
    const container = document.getElementById('ver-logs-content');
    if (!container) return;

    try {
        const data = await api.get('/api/logs/version-checks?limit=100');
        const logs = data.logs || [];

        if (logs.length === 0) {
            container.innerHTML = `
                <div class="bg-slate-900 border border-slate-700 rounded-lg px-6 py-12 text-center">
                    <svg class="w-10 h-10 text-slate-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M9 19l3 3m0 0l3-3m-3 3V10"/>
                    </svg>
                    <div class="text-slate-400 text-sm">No version checks recorded</div>
                    <div class="text-slate-500 text-xs mt-1">Version checks happen at startup</div>
                </div>
            `;
            return;
        }

        const rowsHtml = logs.map(r => `
            <tr class="hover:bg-slate-700/50 transition-colors">
                <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap font-mono">${formatLogTs(r.timestamp)}</td>
                <td class="px-3 py-2 text-sm font-mono text-slate-200">${escapeHtml(r.current_version || '-')}</td>
                <td class="px-3 py-2 text-sm font-mono text-slate-200">${escapeHtml(r.latest_version || '-')}</td>
                <td class="px-3 py-2">${boolBadge(r.outdated, 'Outdated', 'Current')}</td>
                <td class="px-3 py-2">${boolBadge(r.cache_hit, 'Cached', 'Fresh')}</td>
            </tr>
        `).join('');

        container.innerHTML = `
            <div class="text-xs text-slate-500 mb-2">${logs.length} check${logs.length !== 1 ? 's' : ''}</div>
            <div class="bg-slate-900 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
                <table class="w-full">
                    <thead>
                        <tr class="border-b border-slate-700">
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Time</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Current</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Latest</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Status</th>
                            <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Cache</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-700/50">
                        ${rowsHtml}
                    </tbody>
                </table>
            </div>
        `;
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load version check logs: ${escapeHtml(e.message)}
            </div>
        `;
    }
}

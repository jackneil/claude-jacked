/**
 * jacked web dashboard — log viewer (shell + shared helpers)
 * Sub-tabbed view: Gatekeeper | Hooks | Version Checks
 *
 * Sub-tab implementations in logs-gatekeeper.js, logs-hooks.js, logs-version-checks.js
 */

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------
let logsFilter = 'ALL';
let logsSearch = '';
let logsActiveSession = 'ALL';
let logsActiveRepo = 'ALL';
let logsSessions = [];
let logsSubTab = localStorage.getItem('jacked_logs_subtab') || 'gatekeeper';

// Pagination state per sub-tab
let gkPage = 0, gkPageSize = 50, gkTotal = 0;
let hookPage = 0, hookPageSize = 50, hookTotal = 0;
let vcPage = 0, vcPageSize = 25, vcTotal = 0;

// Fallback polling timer (used when WS disconnected)
let _logsFallbackTimer = null;

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
        const d = parseUTCDate(isoStr);
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        return `${date} ${time}`;
    } catch { return isoStr; }
}

function formatLogTimestamp(isoStr) {
    if (!isoStr) return '';
    try {
        const d = parseUTCDate(isoStr);
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        return `${date} ${time}`;
    } catch { return isoStr; }
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
// Pause / Resume
// ---------------------------------------------------------------------------

function toggleLogsPause() {
    window.jackedState.logsPaused = !window.jackedState.logsPaused;
    updatePauseButtons();
    if (!window.jackedState.logsPaused) {
        // Unpaused — snap to page 1 and refresh to catch up
        gkPage = 0; hookPage = 0; vcPage = 0;
        refreshCurrentLogsSubTab();
    }
}

function updatePauseButtons() {
    const paused = window.jackedState.logsPaused;
    document.querySelectorAll('.logs-pause-btn').forEach(btn => {
        const dot = btn.querySelector('.pause-dot');
        const label = btn.querySelector('.pause-label');
        if (dot) {
            dot.className = paused
                ? 'pause-dot w-2 h-2 rounded-full bg-amber-400'
                : 'pause-dot w-2 h-2 rounded-full bg-green-400 logs-live-indicator';
        }
        if (label) label.textContent = paused ? 'Paused' : 'Live';
    });
}

function renderPauseButton() {
    const paused = window.jackedState.logsPaused;
    return `<button class="logs-pause-btn flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-900 border border-slate-700 hover:border-blue-500 text-slate-400 hover:text-blue-300 transition-colors" onclick="toggleLogsPause()">
        <span class="pause-dot w-2 h-2 rounded-full ${paused ? 'bg-amber-400' : 'bg-green-400 logs-live-indicator'}"></span>
        <span class="pause-label">${paused ? 'Paused' : 'Live'}</span>
    </button>`;
}

// ---------------------------------------------------------------------------
// Pagination
// ---------------------------------------------------------------------------

function renderPagination(prefix, page, pageSize, total) {
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const start = total > 0 ? page * pageSize + 1 : 0;
    const end = Math.min((page + 1) * pageSize, total);

    return `<div class="pagination-controls flex flex-wrap items-center gap-2 text-xs text-slate-400 mt-3">
        <span>Showing ${start}-${end} of ${total}</span>
        <select class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200" onchange="${prefix}ChangePageSize(this.value)">
            <option value="25" ${pageSize === 25 ? 'selected' : ''}>25</option>
            <option value="50" ${pageSize === 50 ? 'selected' : ''}>50</option>
            <option value="100" ${pageSize === 100 ? 'selected' : ''}>100</option>
            <option value="200" ${pageSize === 200 ? 'selected' : ''}>200</option>
        </select>
        <button class="px-2 py-1 rounded bg-slate-800 border border-slate-700 hover:border-blue-500 disabled:opacity-30 disabled:cursor-not-allowed" onclick="${prefix}PrevPage()" ${page === 0 ? 'disabled' : ''}>Prev</button>
        <span>Page ${page + 1} of ${totalPages}</span>
        <button class="px-2 py-1 rounded bg-slate-800 border border-slate-700 hover:border-blue-500 disabled:opacity-30 disabled:cursor-not-allowed" onclick="${prefix}NextPage()" ${page >= totalPages - 1 ? 'disabled' : ''}>Next</button>
    </div>`;
}

// ---------------------------------------------------------------------------
// Refresh dispatcher (called by WS handler + fallback polling)
// ---------------------------------------------------------------------------

function refreshCurrentLogsSubTab(changedTables) {
    if (window.jackedState.activeRoute !== 'logs') return;
    if (window.jackedState.logsPaused) return;
    if (window.jackedState.logsInFlight) return;

    switch (logsSubTab) {
        case 'gatekeeper':
            if (!changedTables || changedTables.includes('gatekeeper_decisions')) {
                loadSessions(); loadLogsData();
            }
            break;
        case 'hooks':
            if (!changedTables || changedTables.includes('hook_executions')) {
                loadHookLogsData();
            }
            break;
        case 'version-checks':
            if (!changedTables || changedTables.includes('version_checks')) {
                loadVersionCheckLogsData();
            }
            break;
    }
}

// ---------------------------------------------------------------------------
// Fallback polling (used when WS disconnected)
// ---------------------------------------------------------------------------

function startLogsFallbackPolling() {
    stopLogsFallbackPolling();
    _logsFallbackTimer = setInterval(() => {
        if (window.jackedState.activeRoute !== 'logs') return;
        refreshCurrentLogsSubTab();
    }, 10000);
}

function stopLogsFallbackPolling() {
    if (_logsFallbackTimer) {
        clearInterval(_logsFallbackTimer);
        _logsFallbackTimer = null;
    }
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

function bindLogsEvents() {
    document.querySelectorAll('.logs-subtab').forEach(btn => {
        btn.addEventListener('click', () => {
            logsSubTab = btn.dataset.subtab;
            localStorage.setItem('jacked_logs_subtab', logsSubTab);
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

    switch (logsSubTab) {
        case 'gatekeeper': renderGatekeeperSubTab(container); break;
        case 'hooks':      renderHookLogs(container); break;
        case 'version-checks': renderVersionCheckLogs(container); break;
        default: container.innerHTML = '<div class="text-slate-500 p-4">Unknown log type</div>';
    }
}

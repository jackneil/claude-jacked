/**
 * jacked web dashboard — Version Checks sub-tab
 * Depends on shared state/helpers from logs.js
 */

// ---------------------------------------------------------------------------
// Version Checks sub-tab renderer
// ---------------------------------------------------------------------------

function renderVersionCheckLogs(container) {
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="text-sm text-slate-400">Version check history</div>
            <div class="flex flex-wrap items-center gap-2">
                ${renderPauseButton()}
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

    document.getElementById('ver-logs-refresh')?.addEventListener('click', () => {
        vcPage = 0;
        loadVersionCheckLogsData();
    });
    loadVersionCheckLogsData();
}

// ---------------------------------------------------------------------------
// Version check logs data (server-side paginated)
// ---------------------------------------------------------------------------

async function loadVersionCheckLogsData() {
    const container = document.getElementById('ver-logs-content');
    if (!container) return;

    window.jackedState.logsInFlight = true;
    try {
        let url = `/api/logs/version-checks?limit=${vcPageSize}&offset=${vcPage * vcPageSize}`;
        const data = await api.get(url);
        const logs = data.logs || [];
        vcTotal = data.total || 0;

        // Auto-clamp page if total dropped
        const maxPage = Math.max(0, Math.ceil(vcTotal / vcPageSize) - 1);
        if (vcPage > maxPage) {
            vcPage = maxPage;
            window.jackedState.logsInFlight = false;
            return loadVersionCheckLogsData();
        }

        if (logs.length === 0) {
            container.innerHTML = `
                <div class="bg-slate-900 border border-slate-700 rounded-lg px-6 py-12 text-center">
                    <svg class="w-10 h-10 text-slate-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M9 19l3 3m0 0l3-3m-3 3V10"/>
                    </svg>
                    <div class="text-slate-400 text-sm">No version checks recorded</div>
                    <div class="text-slate-500 text-xs mt-1">Version checks happen at startup</div>
                </div>
                ${vcTotal > 0 ? renderPagination('vc', vcPage, vcPageSize, vcTotal) : ''}
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
            ${renderPagination('vc', vcPage, vcPageSize, vcTotal)}
        `;
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load version check logs: ${escapeHtml(e.message)}
            </div>
        `;
    } finally {
        window.jackedState.logsInFlight = false;
    }
}

// Version check pagination handlers
function vcChangePageSize(val) {
    vcPageSize = parseInt(val, 10) || 25;
    vcPage = 0;
    loadVersionCheckLogsData();
}
function vcPrevPage() { if (vcPage > 0) { vcPage--; loadVersionCheckLogsData(); } }
function vcNextPage() {
    if ((vcPage + 1) * vcPageSize < vcTotal) { vcPage++; loadVersionCheckLogsData(); }
}

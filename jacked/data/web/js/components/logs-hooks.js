/**
 * jacked web dashboard — Hooks sub-tab
 * Depends on shared state/helpers from logs.js
 */

// ---------------------------------------------------------------------------
// Hooks sub-tab renderer
// ---------------------------------------------------------------------------

function renderHookLogs(container) {
    container.innerHTML = `
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div class="text-sm text-slate-400">Hook execution history</div>
            <div class="flex flex-wrap items-center gap-2">
                ${renderPauseButton()}
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

    document.getElementById('hook-logs-refresh')?.addEventListener('click', () => {
        hookPage = 0;
        loadHookLogsData();
    });
    loadHookLogsData();
}

// ---------------------------------------------------------------------------
// Hook logs data (server-side paginated)
// ---------------------------------------------------------------------------

async function loadHookLogsData() {
    const container = document.getElementById('hook-logs-content');
    if (!container) return;

    window.jackedState.logsInFlight = true;
    try {
        let url = `/api/logs/hooks?limit=${hookPageSize}&offset=${hookPage * hookPageSize}`;
        const data = await api.get(url);
        const logs = data.logs || [];
        hookTotal = data.total || 0;

        // Auto-clamp page if total dropped (e.g., after purge)
        const maxPage = Math.max(0, Math.ceil(hookTotal / hookPageSize) - 1);
        if (hookPage > maxPage) {
            hookPage = maxPage;
            window.jackedState.logsInFlight = false;
            return loadHookLogsData();
        }

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
                ${hookTotal > 0 ? renderPagination('hook', hookPage, hookPageSize, hookTotal) : ''}
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
            ${renderPagination('hook', hookPage, hookPageSize, hookTotal)}
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
    } finally {
        window.jackedState.logsInFlight = false;
    }
}

// Hook pagination handlers
function hookChangePageSize(val) {
    hookPageSize = parseInt(val, 10) || 50;
    hookPage = 0;
    loadHookLogsData();
}
function hookPrevPage() { if (hookPage > 0) { hookPage--; loadHookLogsData(); } }
function hookNextPage() {
    if ((hookPage + 1) * hookPageSize < hookTotal) { hookPage++; loadHookLogsData(); }
}

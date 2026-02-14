/** jacked web dashboard — Gatekeeper sub-tab (depends on logs.js) */

// --- Gatekeeper sub-tab renderer ---
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
                <select id="logs-repo-filter" class="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:border-blue-500">
                    <option value="ALL">All Repos</option>
                </select>
                ${renderPauseButton()}
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

// --- Event binding ---
function bindGatekeeperLogsEvents() {
    const filterEl = document.getElementById('logs-filter');
    if (filterEl) {
        filterEl.addEventListener('change', () => {
            logsFilter = filterEl.value;
            gkPage = 0;
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
                gkPage = 0;
                loadLogsData();
            }, 300);
        });
    }

    const repoFilterEl = document.getElementById('logs-repo-filter');
    if (repoFilterEl) {
        repoFilterEl.addEventListener('change', () => {
            logsActiveRepo = repoFilterEl.value;
            logsActiveSession = 'ALL';
            gkPage = 0;
            renderFilteredSessions();
            loadLogsData();
        });
    }

    const exportBtn = document.getElementById('logs-export-btn');
    if (exportBtn) exportBtn.addEventListener('click', doExport);

    const purgeBtn = document.getElementById('logs-purge-btn');
    if (purgeBtn) purgeBtn.addEventListener('click', showPurgeBar);
}

// --- Session cards ---
function getRepoName(repoPath) {
    if (!repoPath) return '';
    return repoPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || repoPath;
}
function getUniqueRepos(sessions) {
    const seen = new Map();
    for (const s of sessions) {
        if (s.repo_path) {
            const key = s.repo_path.toLowerCase();
            if (!seen.has(key)) seen.set(key, s.repo_path);
        }
    }
    return [...seen.values()].sort((a, b) =>
        getRepoName(a).localeCompare(getRepoName(b))
    );
}
function isSessionActive(lastSeen) {
    if (!lastSeen) return false;
    try {
        const d = parseUTCDate(lastSeen);
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
    renderFilteredSessions();
}

function renderFilteredSessions() {
    const container = document.getElementById('logs-sessions');
    if (!container) return;

    if (logsSessions.length === 0) {
        container.innerHTML = '';
        return;
    }

    if (logsActiveRepo !== 'ALL') {
        const repoExists = logsSessions.some(s =>
            (s.repo_path || '').toLowerCase() === logsActiveRepo
        );
        if (!repoExists) logsActiveRepo = 'ALL';
    }

    let filtered = logsSessions;
    if (logsActiveRepo !== 'ALL') {
        filtered = logsSessions.filter(s =>
            (s.repo_path || '').toLowerCase() === logsActiveRepo
        );
    }

    container.innerHTML = renderSessionCards(filtered, logsActiveSession);
    bindSessionCardClicks(container);
    updateRepoDropdown();
}

function updateRepoDropdown() {
    const select = document.getElementById('logs-repo-filter');
    if (!select) return;
    const uniqueRepos = getUniqueRepos(logsSessions);
    const options = ['<option value="ALL"' + (logsActiveRepo === 'ALL' ? ' selected' : '') + '>All Repos</option>']
        .concat(uniqueRepos.map(r =>
            `<option value="${escapeHtml(r.toLowerCase())}"${logsActiveRepo === r.toLowerCase() ? ' selected' : ''}>${escapeHtml(getRepoName(r))}</option>`
        ));
    select.innerHTML = options.join('');
}

function bindSessionCardClicks(container) {
    container.querySelectorAll('.session-card').forEach(card => {
        card.addEventListener('click', () => {
            logsActiveSession = card.dataset.session;
            gkPage = 0;
            renderFilteredSessions();
            loadLogsData();
        });
    });
}

// --- Decision colors & formatting ---
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
function truncateCommand(cmd, maxLen) {
    if (!cmd) return '';
    return cmd.length <= maxLen ? cmd : cmd.substring(0, maxLen) + '...';
}

// --- Gatekeeper decision table (server-side paginated) ---
async function loadLogsData() {
    const container = document.getElementById('logs-content');
    if (!container) return;

    window.jackedState.logsInFlight = true;
    try {
        let url = `/api/logs/gatekeeper?limit=${gkPageSize}&offset=${gkPage * gkPageSize}`;
        if (logsFilter !== 'ALL') url += `&decision=${logsFilter}`;
        if (logsActiveSession !== 'ALL') url += `&session_id=${encodeURIComponent(logsActiveSession)}`;
        if (logsSearch) url += `&command_search=${encodeURIComponent(logsSearch)}`;
        if (logsActiveRepo !== 'ALL') url += `&repo_path=${encodeURIComponent(logsActiveRepo)}`;

        const data = await api.get(url);
        const rows = data.rows || [];
        gkTotal = data.total || 0;

        // Auto-clamp page if total dropped (e.g., after purge)
        const maxPage = Math.max(0, Math.ceil(gkTotal / gkPageSize) - 1);
        if (gkPage > maxPage) {
            gkPage = maxPage;
            window.jackedState.logsInFlight = false;
            return loadLogsData();
        }

        if (rows.length === 0) {
            container.innerHTML = `
                <div class="bg-slate-900 border border-slate-700 rounded-lg px-6 py-12 text-center">
                    <svg class="w-10 h-10 text-slate-600 mx-auto mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/>
                    </svg>
                    <div class="text-slate-400 text-sm">No gatekeeper decisions${logsActiveSession !== 'ALL' ? ' for this session' : ''}</div>
                    <div class="text-slate-500 text-xs mt-1">Decisions appear here as the gatekeeper evaluates commands</div>
                </div>
                ${gkTotal > 0 ? renderPagination('gk', gkPage, gkPageSize, gkTotal) : ''}
            `;
            return;
        }

        const showRepo = logsActiveSession === 'ALL';

        const rowsHtml = rows.map((r, idx) => {
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
                    <td class="px-3 py-2 text-sm font-mono text-slate-200 max-w-[200px] md:max-w-md truncate">${cmd}</td>
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
            ${renderPagination('gk', gkPage, gkPageSize, gkTotal)}
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
    } finally {
        window.jackedState.logsInFlight = false;
    }
}

function gkChangePageSize(val) { gkPageSize = parseInt(val, 10) || 50; gkPage = 0; loadLogsData(); }
function gkPrevPage() { if (gkPage > 0) { gkPage--; loadLogsData(); } }
function gkNextPage() { if ((gkPage + 1) * gkPageSize < gkTotal) { gkPage++; loadLogsData(); } }

// --- Purge & Export ---
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
        gkPage = 0;
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

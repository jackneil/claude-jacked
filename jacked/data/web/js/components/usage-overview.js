/**
 * jacked web dashboard — token usage overview tab
 *
 * Diagnosis-first view: health banner, anomaly flags, project breakdown.
 * Exports: renderUsageOverview(container)
 */

async function renderUsageOverview(container) {
    // 1. Check scan status first
    try {
        const status = await api.get('/api/analytics/usage-scan-status');
        if (!status.ready) {
            container.innerHTML = _renderScanProgress(status);
            return;
        }
    } catch (e) {
        container.innerHTML = _renderScanProgress({ status: 'scanning' });
        return;
    }

    // 2. Fetch overview data
    const [overviewResp, flagsResp] = await Promise.all([
        api.get('/api/analytics/usage-overview?days=1').catch(() => null),
        api.get('/api/analytics/usage-flags').catch(() => null),
    ]);

    const overview = overviewResp?.overview || {};
    const flags = flagsResp?.flags || [];

    // 3. Render
    container.innerHTML = `
        ${_renderHealthBanner(overview)}
        ${_renderFlags(flags)}
        ${_renderProjectBreakdown(overview.projects || [])}
        ${_renderTodaySummary(overview)}
    `;

    // Bind flag dismiss/snooze buttons
    _bindFlagActions();
}

// --- Health Banner ---

function _renderHealthBanner(overview) {
    const cacheRatio = overview.cache_hit_ratio || 0;
    const grade = cacheRatio >= 90 ? 'A' : cacheRatio >= 80 ? 'B' : cacheRatio >= 70 ? 'C' : cacheRatio >= 50 ? 'D' : 'F';
    const gradeColor = grade <= 'B' ? 'text-teal-400' : grade <= 'C' ? 'text-yellow-400' : 'text-red-400';
    const bgColor = grade <= 'B' ? 'bg-teal-900/30 border-teal-800' : grade <= 'C' ? 'bg-yellow-900/30 border-yellow-800' : 'bg-red-900/30 border-red-800';

    const totalTokens = (overview.total_input || 0) + (overview.total_output || 0) + (overview.total_cache_read || 0) + (overview.total_cache_create || 0);
    const tokenStr = totalTokens > 1000000 ? (totalTokens / 1000000).toFixed(1) + 'M' : totalTokens > 1000 ? (totalTokens / 1000).toFixed(0) + 'K' : totalTokens;
    const costStr = '$' + (overview.total_cost || 0).toFixed(2);

    return `
        <div class="flex items-center gap-4 p-4 ${bgColor} border rounded-lg mb-4">
            <div class="text-4xl font-black ${gradeColor}">${grade}</div>
            <div class="flex-1">
                <div class="text-sm font-medium text-slate-200">Cache health: ${cacheRatio.toFixed(0)}% hit rate</div>
                <div class="text-xs text-slate-400">${tokenStr} tokens today &middot; ${costStr} estimated &middot; ${overview.session_count || 0} sessions</div>
            </div>
            <div id="usage-live-pulse"></div>
        </div>`;
}

// --- Flags Section ---

function _renderFlags(flags) {
    if (!flags || flags.length === 0) {
        return '<div class="text-sm text-slate-500 mb-4 p-3 bg-slate-800/30 rounded-lg">No anomalies detected &mdash; usage looks healthy.</div>';
    }

    const flagHtml = flags.map(f => {
        const icon = f.severity === 'critical' ? '&#x1F534;' : '&#x1F7E1;';
        return `
            <div class="flex items-center gap-3 p-3 bg-slate-800/50 rounded-lg mb-2">
                <span class="text-lg">${icon}</span>
                <div class="flex-1">
                    <div class="text-sm text-slate-200">${escapeHtml(f.message)}</div>
                    <div class="text-xs text-slate-500">${escapeHtml(f.flag_type || '')} &middot; ${_usageTimeAgo(f.created_at)}</div>
                </div>
                <button class="text-xs text-slate-400 hover:text-white px-2 py-1" data-dismiss-flag="${f.id}">dismiss</button>
                <button class="text-xs text-slate-400 hover:text-white px-2 py-1" data-snooze-flag="${escapeHtml(f.flag_type || '')}">snooze 24h</button>
            </div>`;
    }).join('');

    return `
        <div class="mb-4">
            <div class="text-xs text-slate-500 uppercase tracking-wider mb-2">Flags (${flags.length})</div>
            ${flagHtml}
        </div>`;
}

// --- Project Breakdown ---

function _renderProjectBreakdown(projects) {
    if (!projects || projects.length === 0) return '';

    // Sort by tokens descending
    const sorted = [...projects].sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0));
    const maxTokens = Math.max(...sorted.map(p => p.total_tokens || 0));

    const rows = sorted.map(p => {
        const name = _decodeProjectName(p.project_hash);
        const tokens = p.total_tokens || 0;
        const pct = maxTokens > 0 ? (tokens / maxTokens * 100) : 0;
        const cacheStr = (p.cache_hit_ratio || 0).toFixed(0) + '%';
        const cacheColor = p.cache_hit_ratio >= 90 ? 'text-teal-400' : p.cache_hit_ratio >= 70 ? 'text-yellow-400' : 'text-red-400';
        const tokenStr = tokens > 1000000 ? (tokens / 1000000).toFixed(1) + 'M' : (tokens / 1000).toFixed(0) + 'K';

        return `
            <div class="grid grid-cols-[2fr_1fr_1fr_0.5fr] gap-2 py-2 px-3 border-t border-slate-700/30 items-center">
                <span class="text-sm text-slate-200 truncate">${escapeHtml(name)}</span>
                <span class="text-xs text-slate-400 text-right">${tokenStr}</span>
                <div class="h-1.5 bg-slate-700 rounded-full overflow-hidden">
                    <div class="h-full bg-teal-500 rounded-full" style="width:${pct}%"></div>
                </div>
                <span class="text-xs ${cacheColor} text-right">${cacheStr}</span>
            </div>`;
    }).join('');

    return `
        <div class="mb-4">
            <div class="text-xs text-slate-500 uppercase tracking-wider mb-2">By Project (today)</div>
            <div class="bg-slate-800/30 rounded-lg overflow-hidden">
                <div class="grid grid-cols-[2fr_1fr_1fr_0.5fr] gap-2 py-1.5 px-3 text-xs text-slate-500">
                    <span>Project</span><span class="text-right">Tokens</span><span></span><span class="text-right">Cache</span>
                </div>
                ${rows}
            </div>
        </div>`;
}

// --- Today Summary (simple stats row) ---

function _renderTodaySummary(overview) {
    const input = overview.total_input || 0;
    const output = overview.total_output || 0;
    const cacheRead = overview.total_cache_read || 0;
    const cacheCreate = overview.total_cache_create || 0;
    const fmt = (n) => n > 1000000 ? (n / 1000000).toFixed(1) + 'M' : n > 1000 ? (n / 1000).toFixed(0) + 'K' : String(n);

    return `
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div class="stat-card">
                <div class="text-lg font-bold text-blue-400">${fmt(input)}</div>
                <div class="text-xs text-slate-400 mt-1">Input Tokens</div>
            </div>
            <div class="stat-card">
                <div class="text-lg font-bold text-purple-400">${fmt(output)}</div>
                <div class="text-xs text-slate-400 mt-1">Output Tokens</div>
            </div>
            <div class="stat-card">
                <div class="text-lg font-bold text-teal-400">${fmt(cacheRead)}</div>
                <div class="text-xs text-slate-400 mt-1">Cache Read</div>
            </div>
            <div class="stat-card">
                <div class="text-lg font-bold text-amber-400">${fmt(cacheCreate)}</div>
                <div class="text-xs text-slate-400 mt-1">Cache Create</div>
            </div>
        </div>`;
}

// --- Helpers ---

function _decodeProjectName(hash) {
    if (!hash) return 'Unknown';
    // Strip leading dash, split, skip common prefixes, take last 2-3 segments
    const segments = hash.replace(/^-/, '').split('-');
    const skip = new Set(['users', 'home', 'documents', 'desktop', 'github', 'repos', 'projects', 'code', 'dev', 'src', 'conductor', 'workspaces']);
    let start = 0;
    for (let i = 0; i < segments.length; i++) {
        if (skip.has(segments[i].toLowerCase())) start = i + 1;
        else break;
    }
    // Also skip username (segment after 'users')
    const meaningful = segments.slice(Math.max(start, segments.length - 3));
    return meaningful.join('-') || hash.slice(0, 20);
}

function _usageTimeAgo(isoStr) {
    if (!isoStr) return '';
    const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
}

function _renderScanProgress(status) {
    const pct = status.projects_total > 0 ? Math.round((status.projects_scanned || 0) / status.projects_total * 100) : 0;
    const projectInfo = status.current_project ? '<p class="text-slate-600 text-xs mt-1">' + escapeHtml(_decodeProjectName(status.current_project)) + '</p>' : '';
    const progressBar = status.projects_total > 0
        ? '<div class="w-48 h-1 bg-slate-700 rounded-full mt-3"><div class="h-full bg-teal-500 rounded-full transition-all" style="width:' + pct + '%"></div></div>'
        : '';

    return '<div class="flex flex-col items-center justify-center py-16">'
        + '<div class="spinner w-8 h-8 mb-4"></div>'
        + '<p class="text-slate-300 text-sm mb-1">Scanning your Claude Code history...</p>'
        + '<p class="text-slate-500 text-xs">' + (status.projects_scanned || 0) + ' / ' + (status.projects_total || '?') + ' projects &middot; ' + (status.messages_parsed || 0).toLocaleString() + ' messages</p>'
        + projectInfo
        + progressBar
        + '</div>';
}

// --- WebSocket update handlers ---

function updateAnalyticsScanProgress(data) {
    const container = document.getElementById('usage-tab-content');
    if (container) container.innerHTML = _renderScanProgress(data);
}

function onAnalyticsScanComplete() {
    const container = document.getElementById('usage-tab-content');
    if (container && typeof renderUsageOverview === 'function') renderUsageOverview(container);
}

function onAnalyticsLiveUpdate(data) {
    // Re-render overview if visible
    const container = document.getElementById('usage-tab-content');
    const tab = localStorage.getItem('jacked_usage_tab') || 'overview';
    if (container && tab === 'overview' && typeof renderUsageOverview === 'function') {
        renderUsageOverview(container);
    }
}

function onAnalyticsFlagRaised(flag) { onAnalyticsLiveUpdate(); }
function onAnalyticsFlagResolved(data) { onAnalyticsLiveUpdate(); }

function _bindFlagActions() {
    document.querySelectorAll('[data-dismiss-flag]').forEach(btn => {
        btn.addEventListener('click', async () => {
            await api.post('/api/analytics/usage-flag-dismiss/' + btn.dataset.dismissFlag).catch(() => null);
            const container = document.getElementById('usage-tab-content');
            if (container) renderUsageOverview(container);
        });
    });
    document.querySelectorAll('[data-snooze-flag]').forEach(btn => {
        btn.addEventListener('click', async () => {
            await api.post('/api/analytics/usage-flag-snooze/' + btn.dataset.snoozeFlag + '?hours=24').catch(() => null);
            const container = document.getElementById('usage-tab-content');
            if (container) renderUsageOverview(container);
        });
    });
}

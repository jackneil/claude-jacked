/**
 * jacked web dashboard — token usage sessions tab
 *
 * Ranked session list with expandable detail rows.
 * Exports: renderUsageSessions(container)
 */

async function renderUsageSessions(container) {
    const status = await api.get('/api/analytics/usage-scan-status').catch(() => null);
    if (!status?.ready) {
        container.innerHTML = '<p class="text-slate-500 text-sm py-8 text-center">Analytics scan in progress...</p>';
        return;
    }

    const data = await api.get('/api/analytics/usage-sessions?days=1').catch(() => null);
    const sessions = data?.sessions || [];

    if (sessions.length === 0) {
        container.innerHTML = '<p class="text-slate-500 text-sm py-8 text-center">No sessions recorded today.</p>';
        return;
    }

    const rows = sessions.map(s => {
        const name = typeof _decodeProjectName === 'function' ? _decodeProjectName(s.project_hash) : (s.project_hash || 'Unknown');
        const sid = (s.session_id || '').slice(0, 8);
        const tokens = (s.total_tokens || 0);
        const tokenStr = tokens > 1000000 ? (tokens / 1000000).toFixed(1) + 'M' : (tokens / 1000).toFixed(0) + 'K';
        const cost = '$' + (s.total_cost || 0).toFixed(2);
        const cache = (s.cache_hit_ratio || 0).toFixed(0) + '%';
        const cacheColor = s.cache_hit_ratio >= 90 ? 'text-teal-400' : s.cache_hit_ratio >= 70 ? 'text-yellow-400' : 'text-red-400';
        const flagCount = s.flag_count || 0;
        const dot = flagCount > 0 ? (s.has_critical ? '&#x1F534;' : '&#x1F7E1;') : '&#x1F7E2;';
        const duration = s.duration_minutes ? Math.round(s.duration_minutes) + 'min' : '&mdash;';

        return '<div class="grid grid-cols-[auto_2fr_1fr_1fr_1fr_1fr_1fr] gap-2 py-2 px-3 border-t border-slate-700/30 items-center text-xs cursor-pointer hover:bg-slate-800/50" onclick="this.nextElementSibling?.classList.toggle(\'hidden\')">'
            + '<span>' + dot + '</span>'
            + '<span class="text-slate-200 truncate">' + escapeHtml(name) + ' <span class="text-slate-500">' + escapeHtml(sid) + '</span></span>'
            + '<span class="text-right text-slate-400 tabular-nums">' + duration + '</span>'
            + '<span class="text-right text-slate-300 tabular-nums">' + tokenStr + '</span>'
            + '<span class="text-right text-slate-300 tabular-nums">' + cost + '</span>'
            + '<span class="text-right ' + cacheColor + ' tabular-nums">' + cache + '</span>'
            + '<span class="text-right text-slate-500 tabular-nums">' + (s.message_count || 0) + ' msgs</span>'
            + '</div>'
            + '<div class="hidden px-3 py-2 bg-slate-900/50 border-t border-slate-700/30 text-xs text-slate-400">'
            + 'Session ' + escapeHtml(s.session_id || '') + ' &middot; ' + escapeHtml(name)
            + '</div>';
    }).join('');

    container.innerHTML = '<div class="bg-slate-800/30 rounded-lg overflow-hidden">'
        + '<div class="grid grid-cols-[auto_2fr_1fr_1fr_1fr_1fr_1fr] gap-2 py-1.5 px-3 text-xs text-slate-500">'
        + '<span></span><span>Session</span><span class="text-right">Duration</span><span class="text-right">Tokens</span><span class="text-right">Cost</span><span class="text-right">Cache</span><span class="text-right">Messages</span>'
        + '</div>'
        + rows
        + '</div>';
}

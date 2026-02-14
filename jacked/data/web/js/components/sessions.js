/**
 * jacked web dashboard — session display component
 * Controls bar, repo grouping, session lookup, and active session rendering.
 */

// Staleness dropdown options: label → milliseconds
const STALENESS_OPTIONS = [
    { label: '5m', ms: 300000 },
    { label: '15m', ms: 900000 },
    { label: '30m', ms: 1800000 },
    { label: '1h', ms: 3600000 },
    { label: '2h', ms: 7200000 },
];

// ---------------------------------------------------------------------------
// Session controls bar (above account cards)
// ---------------------------------------------------------------------------
function renderSessionControls() {
    const st = window.jackedState;
    const stalenessOpts = STALENESS_OPTIONS.map(o =>
        `<option value="${o.ms}" ${st.sessionStalenessMs === o.ms ? 'selected' : ''}>${escapeHtml(o.label)}</option>`
    ).join('');

    const groupChecked = st.sessionGroupByRepo ? 'checked' : '';
    const subagentChecked = st.sessionShowSubagents ? 'checked' : '';

    return `
        <div class="flex flex-wrap items-center gap-3 bg-slate-800/50 border border-slate-700 rounded-lg px-4 py-2.5 mb-4">
            <span class="text-xs text-slate-400 font-medium">Sessions</span>

            <div class="flex items-center gap-1.5">
                <span class="text-xs text-slate-500">Hide after</span>
                <select id="sel-session-staleness"
                    class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 cursor-pointer">
                    ${stalenessOpts}
                </select>
            </div>

            <div class="flex items-center gap-1.5">
                <span class="text-xs text-slate-500">Group repos</span>
                <label class="toggle-switch toggle-sm">
                    <input type="checkbox" id="chk-session-group" ${groupChecked}>
                    <span class="toggle-slider"></span>
                </label>
            </div>

            <div class="flex items-center gap-1.5">
                <span class="text-xs text-slate-500">Sub-agents</span>
                <label class="toggle-switch toggle-sm">
                    <input type="checkbox" id="chk-session-subagents" ${subagentChecked}>
                    <span class="toggle-slider"></span>
                </label>
            </div>

            <div class="flex items-center gap-1.5 ml-auto">
                <input type="text" id="inp-session-lookup"
                    class="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 w-40 placeholder-slate-500"
                    placeholder="Find session ID...">
            </div>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Session lookup result banner
// ---------------------------------------------------------------------------
function renderSessionLookupResult() {
    const result = window.jackedState.sessionLookupResult;
    if (!result) return '';

    if (result.loading) {
        return `<div class="bg-slate-800/50 border border-slate-700 rounded-lg px-4 py-2.5 mb-4 text-xs text-slate-400 flex items-center gap-2">
            <div class="spinner" style="width:14px;height:14px;border-width:2px;"></div> Searching...
        </div>`;
    }

    if (!result.records || result.records.length === 0) {
        return `<div class="bg-yellow-900/30 border border-yellow-700 rounded-lg px-4 py-2.5 mb-4 text-xs text-yellow-200 flex items-center justify-between">
            <span>No session found for "${escapeHtml(result.query)}"</span>
            <button id="btn-dismiss-lookup" class="text-yellow-400 hover:text-yellow-200 ml-3 text-sm">&times;</button>
        </div>`;
    }

    const rows = result.records.map(r => {
        const repo = (r.repo_path || 'unknown').replace(/\\/g, '/').split('/').filter(Boolean).pop() || r.repo_path;
        const email = r.email || 'unknown';
        const status = r.ended_at ? 'Ended' : 'Active';
        const statusClass = r.ended_at ? 'text-slate-500' : 'text-green-400';
        const sid = r.session_id ? '...' + r.session_id.slice(-8) : '';
        const ago = r.detected_at ? formatTimeAgo(r.detected_at) : '';
        return `<div class="flex items-center gap-2">
            <span class="font-medium text-blue-300">${escapeHtml(email)}</span>
            <span class="text-slate-500">${escapeHtml(repo)}</span>
            <span class="text-slate-600">${escapeHtml(sid)}</span>
            <span class="${statusClass}">${status}</span>
            ${ago ? `<span class="text-slate-600">${escapeHtml(ago)}</span>` : ''}
        </div>`;
    }).join('');

    return `<div class="bg-blue-900/20 border border-blue-700/50 rounded-lg px-4 py-2.5 mb-4 text-xs flex items-center justify-between">
        <div class="flex flex-col gap-1">${rows}</div>
        <button id="btn-dismiss-lookup" class="text-blue-400 hover:text-blue-200 ml-3 text-sm">&times;</button>
    </div>`;
}

// ---------------------------------------------------------------------------
// Active sessions rendering (replaces old renderActiveSessions in accounts.js)
// ---------------------------------------------------------------------------
function renderActiveSessions(acct) {
    const allSessions = (window.jackedState.activeSessions || {})[String(acct.id)];
    if (!allSessions || allSessions.length === 0) return '';

    // Filter subagents if toggle is off
    const sessions = window.jackedState.sessionShowSubagents
        ? allSessions
        : allSessions.filter(s => !s.is_subagent);

    if (sessions.length === 0) return '';

    if (window.jackedState.sessionGroupByRepo) {
        return renderGroupedSessions(sessions, acct.id);
    }
    return renderFlatSessions(sessions);
}

function renderFlatSessions(sessions) {
    const mainSessions = sessions.filter(s => !s.is_subagent);
    const subSessions = sessions.filter(s => s.is_subagent);
    let html = '';

    html += mainSessions.map(s => {
        const fullPath = s.repo_path || 'unknown';
        const name = fullPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || fullPath;
        const ago = s.last_activity_at ? formatTimeAgo(s.last_activity_at) : (s.detected_at ? formatTimeAgo(s.detected_at) : '');
        const sid = s.session_id || '';
        const sidTag = sid ? ` (${sid})` : '';
        const label = ago ? `${name}${sidTag} \u2014 ${ago}` : `${name}${sidTag}`;
        const tooltip = sid ? `${fullPath}\nSession: ...${sid}` : fullPath;
        return `<span class="active-repo-tag" title="${escapeHtml(tooltip)}">${escapeHtml(label)}</span>`;
    }).join('');

    if (subSessions.length > 0) {
        const MAX_VISIBLE = 3;
        const visible = subSessions.slice(0, MAX_VISIBLE);
        const overflow = subSessions.length - MAX_VISIBLE;

        html += visible.map(s => {
            const fullPath = s.repo_path || 'unknown';
            const name = fullPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || fullPath;
            const agentLabel = s.agent_type || 'agent';
            const sid = s.session_id || '';
            const tooltip = `Subagent: ${agentLabel}\n${fullPath}${sid ? '\nSession: ...' + sid : ''}`;
            return `<span class="active-repo-tag subagent-tag" title="${escapeHtml(tooltip)}">${escapeHtml(agentLabel)} \u00b7 ${escapeHtml(name)}</span>`;
        }).join('');

        if (overflow > 0) {
            html += `<span class="active-repo-tag subagent-tag" title="${overflow} more subagent sessions">+${overflow} agents</span>`;
        }
    }

    return `<div class="active-repo-tags mb-2">${html}</div>`;
}

// ---------------------------------------------------------------------------
// Grouped sessions (by repo)
// ---------------------------------------------------------------------------
function renderGroupedSessions(sessions, acctId) {
    // Group by normalized repo path
    const groups = {};
    for (const s of sessions) {
        const key = (s.repo_path || 'unknown').replace(/\\/g, '/').toLowerCase();
        if (!groups[key]) groups[key] = { path: s.repo_path || 'unknown', sessions: [] };
        groups[key].sessions.push(s);
    }

    // Sort groups by most recent activity
    const sorted = Object.entries(groups).sort((a, b) => {
        const aMax = Math.max(...a[1].sessions.map(s => new Date(s.last_activity_at || s.detected_at || 0).getTime()));
        const bMax = Math.max(...b[1].sessions.map(s => new Date(s.last_activity_at || s.detected_at || 0).getTime()));
        return bMax - aMax;
    });

    let html = '';
    for (const [key, group] of sorted) {
        const name = group.path.replace(/\\/g, '/').split('/').filter(Boolean).pop() || group.path;
        const count = group.sessions.length;
        const groupKey = `${acctId}:${key}`;
        const isExpanded = window.jackedState.expandedRepoGroups.has(groupKey);

        html += `
            <div class="mb-1">
                <div class="repo-group-header flex items-center gap-2 px-2 py-1 rounded cursor-pointer hover:bg-cyan-900/10 transition-colors" data-group-key="${escapeHtml(groupKey)}">
                    <span class="repo-group-chevron text-slate-500 text-xs ${isExpanded ? 'expanded' : ''}" data-group-key="${escapeHtml(groupKey)}">&#9654;</span>
                    <span class="text-xs font-medium text-cyan-300">${escapeHtml(name)}</span>
                    <span class="text-xs text-slate-500">${count} session${count !== 1 ? 's' : ''}</span>
                </div>
                <div class="repo-group-sessions pl-5 ${isExpanded ? '' : 'hidden'}" data-group-key="${escapeHtml(groupKey)}">
                    <div class="active-repo-tags mb-1 mt-1">
                        ${group.sessions.map(s => _renderSessionPill(s)).join('')}
                    </div>
                </div>
            </div>
        `;
    }

    return `<div class="mb-2">${html}</div>`;
}

function _renderSessionPill(s) {
    const fullPath = s.repo_path || 'unknown';
    const sid = s.session_id || '';
    const ago = s.last_activity_at ? formatTimeAgo(s.last_activity_at) : (s.detected_at ? formatTimeAgo(s.detected_at) : '');

    if (s.is_subagent) {
        const name = fullPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || fullPath;
        const agentLabel = s.agent_type || 'agent';
        const tooltip = `Subagent: ${agentLabel}\n${fullPath}${sid ? '\nSession: ...' + sid : ''}`;
        return `<span class="active-repo-tag subagent-tag" title="${escapeHtml(tooltip)}">${escapeHtml(agentLabel)} \u00b7 ${escapeHtml(name)}</span>`;
    }

    const pill = sid ? `(${sid}) \u2014 ${ago}` : ago;
    const tooltip = sid ? `${fullPath}\nSession: ...${sid}` : fullPath;
    return `<span class="active-repo-tag" title="${escapeHtml(tooltip)}">${escapeHtml(pill || 'session')}</span>`;
}

// ---------------------------------------------------------------------------
// Session control event bindings
// ---------------------------------------------------------------------------
let _lookupDebounce = null;
let _lookupAbort = null;

function bindSessionControlEvents() {
    // Staleness dropdown
    const selStaleness = document.getElementById('sel-session-staleness');
    if (selStaleness) {
        selStaleness.addEventListener('change', async () => {
            window.jackedState.sessionStalenessMs = parseInt(selStaleness.value);
            localStorage.setItem('jacked_session_staleness', selStaleness.value);
            await loadActiveSessions();
            rerenderAccountsView();
        });
    }

    // Group by repo toggle
    const chkGroup = document.getElementById('chk-session-group');
    if (chkGroup) {
        chkGroup.addEventListener('change', () => {
            window.jackedState.sessionGroupByRepo = chkGroup.checked;
            localStorage.setItem('jacked_session_group_repo', chkGroup.checked ? '1' : '0');
            rerenderAccountsView();
        });
    }

    // Sub-agents toggle
    const chkSub = document.getElementById('chk-session-subagents');
    if (chkSub) {
        chkSub.addEventListener('change', () => {
            window.jackedState.sessionShowSubagents = chkSub.checked;
            localStorage.setItem('jacked_session_show_subagents', chkSub.checked ? '1' : '0');
            rerenderAccountsView();
        });
    }

    // Session lookup input (debounced 300ms)
    const inpLookup = document.getElementById('inp-session-lookup');
    if (inpLookup) {
        inpLookup.addEventListener('input', () => {
            clearTimeout(_lookupDebounce);
            const val = inpLookup.value.trim();
            if (!val) {
                window.jackedState.sessionLookupResult = null;
                rerenderAccountsView();
                return;
            }
            if (val.length < 8) return;
            _lookupDebounce = setTimeout(() => _doSessionLookup(val), 300);
        });
        inpLookup.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                inpLookup.value = '';
                window.jackedState.sessionLookupResult = null;
                rerenderAccountsView();
            }
        });
    }

    // Dismiss lookup result
    const btnDismiss = document.getElementById('btn-dismiss-lookup');
    if (btnDismiss) {
        btnDismiss.addEventListener('click', () => {
            window.jackedState.sessionLookupResult = null;
            const inp = document.getElementById('inp-session-lookup');
            if (inp) inp.value = '';
            rerenderAccountsView();
        });
    }

    // Repo group expand/collapse headers
    document.querySelectorAll('.repo-group-header').forEach(hdr => {
        hdr.addEventListener('click', () => {
            const key = hdr.dataset.groupKey;
            if (window.jackedState.expandedRepoGroups.has(key)) {
                window.jackedState.expandedRepoGroups.delete(key);
            } else {
                window.jackedState.expandedRepoGroups.add(key);
            }
            // Toggle visibility directly (avoid full re-render for snappiness)
            // Use dataset matching instead of querySelector interpolation (safe for paths with quotes)
            const sessions = [...document.querySelectorAll('.repo-group-sessions')].find(el => el.dataset.groupKey === key);
            const chevron = [...document.querySelectorAll('.repo-group-chevron')].find(el => el.dataset.groupKey === key);
            if (sessions) sessions.classList.toggle('hidden');
            if (chevron) chevron.classList.toggle('expanded');
        });
    });
}

async function _doSessionLookup(query) {
    // Cancel any in-flight lookup
    if (_lookupAbort) _lookupAbort.abort();
    _lookupAbort = new AbortController();
    const signal = _lookupAbort.signal;

    window.jackedState.sessionLookupResult = { loading: true, query };
    rerenderAccountsView();

    try {
        const data = await api.get(`/api/auth/session-account?session_id=${encodeURIComponent(query)}`);
        if (signal.aborted) return;  // Superseded by a newer lookup
        window.jackedState.sessionLookupResult = {
            query,
            records: data.records || [],
        };
    } catch (e) {
        if (signal.aborted) return;
        window.jackedState.sessionLookupResult = { query, records: [] };
    }

    rerenderAccountsView();

    // Highlight matching account cards
    const result = window.jackedState.sessionLookupResult;
    if (result && result.records) {
        const matchedIds = new Set(result.records.map(r => String(r.account_id)));
        matchedIds.forEach(id => {
            const card = document.querySelector(`[data-account-id="${id}"]`);
            if (card) {
                card.classList.add('account-card-highlight');
                setTimeout(() => card.classList.remove('account-card-highlight'), 3000);
            }
        });
    }
}

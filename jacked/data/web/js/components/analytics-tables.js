/**
 * jacked web dashboard — analytics tables companion
 * Renders session risk table, suggested rules, and hot rules
 * into containers created by the coordinator.
 *
 * Exports: renderAnalyticsTables(sessionsData, rulesData)
 * Depends: escapeHtml(), api, Swal (SweetAlert2)
 */

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

function renderAnalyticsTables(sessionsData, rulesData) {
    _renderSessionRiskTable(sessionsData);
    _renderRulesTables(rulesData);
}

// ---------------------------------------------------------------------------
// Session risk table
// ---------------------------------------------------------------------------

function _renderSessionRiskTable(data) {
    const container = document.getElementById('analytics-sessions-container');
    if (!container) return;

    const sessions = data && data.sessions;
    if (!sessions || sessions.length === 0) {
        container.innerHTML = '<div class="text-sm text-slate-500 py-6 text-center">No session data available</div>';
        return;
    }

    const rows = sessions.map(s => {
        const risk = s.risk_score || 0;
        let riskClass = 'text-green-400';
        if (risk >= 10) riskClass = 'text-red-400';
        else if (risk >= 5) riskClass = 'text-yellow-400';

        const repo = s.repo_path
            ? s.repo_path.replace(/\\/g, '/').split('/').filter(Boolean).pop()
            : '';
        const firstSeen = _formatTs(s.first_seen);
        const sid = s.session_id || '';
        const shortId = sid.substring(0, 8);

        return `
            <tr class="hover:bg-slate-700/50 transition-colors cursor-pointer analytics-session-row"
                data-session="${escapeHtml(sid)}">
                <td class="px-3 py-2 text-xs text-slate-400 whitespace-nowrap">${firstSeen}</td>
                <td class="px-3 py-2 text-xs font-mono text-slate-300" title="${escapeHtml(s.repo_path || '')}">
                    ${escapeHtml(repo || shortId)}
                </td>
                <td class="px-3 py-2 text-xs text-slate-200 text-center">${(s.total_decisions || 0).toLocaleString()}</td>
                <td class="px-3 py-2 text-xs text-red-300 text-center">${(s.denials || 0).toLocaleString()}</td>
                <td class="px-3 py-2 text-xs font-semibold text-center ${riskClass}">${risk.toFixed(1)}</td>
            </tr>`;
    }).join('');

    container.innerHTML = `
        <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
            <table class="w-full">
                <thead>
                    <tr class="border-b border-slate-700">
                        <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">First Seen</th>
                        <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Repo</th>
                        <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Decisions</th>
                        <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Denials</th>
                        <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Risk Score</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-slate-700/50">
                    ${rows}
                </tbody>
            </table>
        </div>`;

    // Bind click → drill to logs for that session
    container.querySelectorAll('.analytics-session-row').forEach(row => {
        row.addEventListener('click', () => {
            const sid = row.dataset.session;
            if (sid) {
                const qs = new URLSearchParams({ session_id: sid, from: 'analytics' });
                window.location.hash = 'logs?' + qs.toString();
            }
        });
    });
}

// ---------------------------------------------------------------------------
// Rules tables (suggested + hot)
// ---------------------------------------------------------------------------

function _renderRulesTables(data) {
    const container = document.getElementById('analytics-rules-container');
    if (!container) return;

    const suggested = data && data.suggested;
    const hot = data && data.hot;
    const hasSuggested = suggested && suggested.length > 0;
    const hasHot = hot && hot.length > 0;

    if (!hasSuggested && !hasHot) {
        container.innerHTML = '<div class="text-sm text-slate-500 py-6 text-center">No rule intelligence data available</div>';
        return;
    }

    let html = '';

    // Suggested rules
    if (hasSuggested) {
        const suggestedRows = suggested.map(s => {
            const cmd = escapeHtml(s.command || '');
            const decision = escapeHtml(s.decision || '');
            const count = s.count || 0;

            // Determine badge color for decision
            let badgeClass = 'bg-slate-700 text-slate-300';
            if (s.decision === 'ALLOW') badgeClass = 'bg-green-700 text-green-100';
            else if (s.decision === 'ASK_USER') badgeClass = 'bg-yellow-700 text-yellow-100';
            else if (s.decision === 'DENY') badgeClass = 'bg-red-700 text-red-100';

            return `
                <tr class="hover:bg-slate-700/30 transition-colors">
                    <td class="px-3 py-2 text-xs font-mono text-slate-200 max-w-xs truncate" title="${cmd}">${cmd}</td>
                    <td class="px-3 py-2 text-center">
                        <span class="inline-block px-2 py-0.5 rounded text-xs font-medium ${badgeClass}">${decision}</span>
                    </td>
                    <td class="px-3 py-2 text-xs text-slate-300 text-center">${count.toLocaleString()}</td>
                    <td class="px-3 py-2 text-center">
                        <button class="analytics-add-rule-btn px-2 py-1 rounded text-xs font-medium bg-blue-700 hover:bg-blue-600 text-white transition-colors"
                            data-command="${cmd}" data-decision="${decision}">
                            Add Rule
                        </button>
                    </td>
                </tr>`;
        }).join('');

        html += `
            <div class="mb-4">
                <h4 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Suggested Rules</h4>
                <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
                    <table class="w-full">
                        <thead>
                            <tr class="border-b border-slate-700">
                                <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Command</th>
                                <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Decision</th>
                                <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Hits</th>
                                <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Action</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-700/50">
                            ${suggestedRows}
                        </tbody>
                    </table>
                </div>
            </div>`;
    }

    // Hot rules
    if (hasHot) {
        const hotRows = hot.map(h => {
            return `
                <tr class="hover:bg-slate-700/30 transition-colors">
                    <td class="px-3 py-2 text-xs font-mono text-slate-200">${escapeHtml(h.method || '')}</td>
                    <td class="px-3 py-2 text-xs text-slate-300 text-center">${(h.count || 0).toLocaleString()}</td>
                </tr>`;
        }).join('');

        html += `
            <div>
                <h4 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2">Hot Methods</h4>
                <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden overflow-x-auto">
                    <table class="w-full">
                        <thead>
                            <tr class="border-b border-slate-700">
                                <th class="px-3 py-2 text-left text-xs font-medium text-slate-400 uppercase">Method</th>
                                <th class="px-3 py-2 text-center text-xs font-medium text-slate-400 uppercase">Count</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-700/50">
                            ${hotRows}
                        </tbody>
                    </table>
                </div>
            </div>`;
    }

    container.innerHTML = html;

    // Bind "Add Rule" buttons
    container.querySelectorAll('.analytics-add-rule-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const command = btn.dataset.command;
            const decision = btn.dataset.decision;
            _handleAddRule(command, decision);
        });
    });
}

// ---------------------------------------------------------------------------
// Add Rule handler with broad-pattern warning
// ---------------------------------------------------------------------------

async function _handleAddRule(command, decision) {
    // Determine pattern and list based on the decision
    const pattern = `Bash(${command}:*)`;
    const listName = decision === 'DENY' ? 'deny' : 'allow';

    // Warn on broad patterns (single-word commands, very short)
    const isBroad = !command.includes(' ') && command.length <= 5;

    if (isBroad && typeof Swal !== 'undefined') {
        const result = await Swal.fire({
            title: 'Broad Pattern Warning',
            html: `<p class="text-sm">The pattern <code class="bg-slate-700 px-1 rounded">${escapeHtml(pattern)}</code> is quite broad and will match <strong>all</strong> invocations of <code>${escapeHtml(command)}</code>.</p><p class="text-sm mt-2">Are you sure you want to add this rule?</p>`,
            icon: 'warning',
            showCancelButton: true,
            confirmButtonText: 'Add Rule',
            cancelButtonText: 'Cancel',
        });
        if (!result.isConfirmed) return;
    }

    try {
        await api.post('/api/claude-settings/permissions/rule', {
            pattern,
            list_name: listName,
        });
        if (typeof showLogsToast === 'function') {
            showLogsToast(`Added ${listName} rule: ${pattern}`);
        } else if (typeof showToast === 'function') {
            showToast(`Added ${listName} rule: ${pattern}`, 'success');
        }
    } catch (e) {
        const msg = 'Failed to add rule: ' + (e.message || e);
        if (typeof showLogsToast === 'function') {
            showLogsToast(msg, true);
        } else if (typeof showToast === 'function') {
            showToast(msg, 'error');
        }
    }
}

// ---------------------------------------------------------------------------
// Timestamp formatting helper
// ---------------------------------------------------------------------------

function _formatTs(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        const now = new Date();
        const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (d.toDateString() === now.toDateString()) return time;
        const date = d.toLocaleDateString([], { month: 'short', day: 'numeric' });
        return `${date} ${time}`;
    } catch {
        return isoStr;
    }
}

/**
 * jacked web dashboard — analytics component
 * Dashboard with stat cards for gatekeeper, agents, hooks, lessons.
 */

// Active date range for analytics queries
let analyticsRange = '7d';

/**
 * Render the analytics dashboard.
 */
function renderAnalytics() {
    return `
        <div class="max-w-4xl">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Analytics</h2>
                <div class="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded-lg p-1">
                    <button class="analytics-range-btn text-xs px-3 py-1.5 rounded ${analyticsRange === '7d' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-white'}" data-range="7d">7d</button>
                    <button class="analytics-range-btn text-xs px-3 py-1.5 rounded ${analyticsRange === '30d' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-white'}" data-range="30d">30d</button>
                    <button class="analytics-range-btn text-xs px-3 py-1.5 rounded ${analyticsRange === '90d' ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-white'}" data-range="90d">90d</button>
                </div>
            </div>

            <div id="analytics-content" class="space-y-6">
                <div class="flex items-center justify-center py-12">
                    <div class="spinner"></div>
                    <span class="ml-3 text-slate-400 text-sm">Loading analytics...</span>
                </div>
            </div>
        </div>
    `;
}

/**
 * Load and render all analytics sections.
 */
async function loadAnalyticsData() {
    const container = document.getElementById('analytics-content');
    if (!container) return;

    const rangeParam = `?days=${parseInt(analyticsRange)}`;

    try {
        const [gatekeeper, agents, hooks, lessons] = await Promise.all([
            api.get(`/api/analytics/gatekeeper${rangeParam}`).catch(() => null),
            api.get(`/api/analytics/agents${rangeParam}`).catch(() => null),
            api.get(`/api/analytics/hooks${rangeParam}`).catch(() => null),
            api.get(`/api/analytics/lessons${rangeParam}`).catch(() => null),
        ]);

        container.innerHTML = `
            ${renderGatekeeperStats(gatekeeper)}
            ${renderAgentStats(agents)}
            ${renderHookStats(hooks)}
            ${renderLessonStats(lessons)}
        `;
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load analytics: ${escapeHtml(e.message)}
            </div>
        `;
    }
}

/**
 * Render gatekeeper section.
 */
function renderGatekeeperStats(data) {
    if (!data) return renderAnalyticsPlaceholder('Gatekeeper');

    const total = data.total_decisions || 0;
    const approvalRate = data.approval_rate != null ? data.approval_rate.toFixed(1) : '0.0';
    const methods = data.method_breakdown || {};

    let methodsHtml = '';
    if (Object.keys(methods).length > 0) {
        methodsHtml = Object.entries(methods).map(([method, count]) => `
            <div class="flex items-center justify-between text-sm">
                <span class="text-slate-400">${escapeHtml(method)}</span>
                <span class="text-white font-mono">${count}</span>
            </div>
        `).join('');
    } else {
        methodsHtml = '<div class="text-sm text-slate-500">No data</div>';
    }

    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Security Gatekeeper</h3>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-3">
                <div class="stat-card">
                    <div class="text-2xl font-bold text-white">${total.toLocaleString()}</div>
                    <div class="text-xs text-slate-400 mt-1">Total Decisions</div>
                </div>
                <div class="stat-card">
                    <div class="text-2xl font-bold ${Number(approvalRate) >= 90 ? 'text-green-400' : Number(approvalRate) >= 70 ? 'text-yellow-400' : 'text-red-400'}">${approvalRate}%</div>
                    <div class="text-xs text-slate-400 mt-1">Approval Rate</div>
                </div>
                <div class="stat-card">
                    <div class="space-y-1">${methodsHtml}</div>
                    <div class="text-xs text-slate-400 mt-2">Method Breakdown</div>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render agent stats section.
 */
function renderAgentStats(data) {
    if (!data) return renderAnalyticsPlaceholder('Agents');

    const agents = data.agent_breakdown || [];
    if (agents.length === 0) {
        return `
            <div>
                <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Agents</h3>
                <div class="stat-card text-sm text-slate-500">No agent invocation data</div>
            </div>
        `;
    }

    const rowsHtml = agents.slice(0, 5).map(agent => `
        <tr>
            <td class="font-mono">${escapeHtml(agent.agent)}</td>
            <td class="text-center">${(agent.count || 0).toLocaleString()}</td>
            <td class="text-center">${agent.avg_duration_ms != null ? (agent.avg_duration_ms / 1000).toFixed(1) + 's' : '-'}</td>
        </tr>
    `).join('');

    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Top Agents</h3>
            <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-x-auto">
                <table class="data-table">
                    <thead><tr><th class="text-left">Agent</th><th class="text-center">Invocations</th><th class="text-center">Avg Duration</th></tr></thead>
                    <tbody>${rowsHtml}</tbody>
                </table>
            </div>
        </div>
    `;
}

/**
 * Render hook stats section.
 */
function renderHookStats(data) {
    if (!data) return renderAnalyticsPlaceholder('Hooks');

    const hooks = data.hook_breakdown || [];
    if (hooks.length === 0) {
        return `
            <div>
                <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Hooks</h3>
                <div class="stat-card text-sm text-slate-500">No hook execution data</div>
            </div>
        `;
    }

    const rowsHtml = hooks.map(hook => {
        const rate = hook.success_rate != null ? hook.success_rate.toFixed(1) + '%' : '-';
        const rateColor = hook.success_rate >= 95 ? 'text-green-400' : hook.success_rate >= 80 ? 'text-yellow-400' : 'text-red-400';
        return `
            <tr>
                <td class="font-mono">${escapeHtml(hook.hook)}</td>
                <td class="text-center">${(hook.count || 0).toLocaleString()}</td>
                <td class="text-center ${rateColor}">${rate}</td>
                <td class="text-center">${hook.avg_duration_ms != null ? hook.avg_duration_ms.toFixed(0) + 'ms' : '-'}</td>
            </tr>
        `;
    }).join('');

    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Hook Health</h3>
            <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-x-auto">
                <table class="data-table">
                    <thead><tr><th class="text-left">Hook</th><th class="text-center">Executions</th><th class="text-center">Success Rate</th><th class="text-center">Avg Duration</th></tr></thead>
                    <tbody>${rowsHtml}</tbody>
                </table>
            </div>
        </div>
    `;
}

/**
 * Render lesson stats section.
 */
function renderLessonStats(data) {
    if (!data) return renderAnalyticsPlaceholder('Lessons');

    const active = data.active || 0;
    const graduated = data.graduated || 0;
    const archived = data.archived || 0;
    const total = active + graduated + archived;

    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Lessons</h3>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div class="stat-card">
                    <div class="text-2xl font-bold text-blue-400">${active}</div>
                    <div class="text-xs text-slate-400 mt-1">Active (Learning)</div>
                </div>
                <div class="stat-card">
                    <div class="text-2xl font-bold text-green-400">${graduated}</div>
                    <div class="text-xs text-slate-400 mt-1">Graduated</div>
                </div>
                <div class="stat-card">
                    <div class="text-2xl font-bold text-slate-400">${total}</div>
                    <div class="text-xs text-slate-400 mt-1">Total</div>
                </div>
            </div>
        </div>
    `;
}

/**
 * Placeholder for analytics sections that failed to load.
 */
function renderAnalyticsPlaceholder(title) {
    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">${escapeHtml(title)}</h3>
            <div class="stat-card text-sm text-slate-500">No data available</div>
        </div>
    `;
}

/**
 * Bind analytics events.
 */
function bindAnalyticsEvents() {
    // Date range buttons
    document.querySelectorAll('.analytics-range-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            analyticsRange = btn.dataset.range;
            // Re-render the whole analytics view
            renderRoute('analytics');
        });
    });

    // Load data after rendering
    loadAnalyticsData();
}

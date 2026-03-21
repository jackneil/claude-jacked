/**
 * jacked web dashboard — analytics dashboard coordinator
 * KPI cards, collapsible sections, delegates to charts + tables companions.
 */

let analyticsRange = '7d';
const RANGE_OPTIONS = { '24h': 1, '7d': 7, '30d': 30, '90d': 90, '1y': 365 };

// --- Collapsed state persistence ---

function _getCollapsed() {
    try { return JSON.parse(localStorage.getItem('jacked_analytics_collapsed') || '{}'); }
    catch { return {}; }
}
function _setCollapsed(state) {
    localStorage.setItem('jacked_analytics_collapsed', JSON.stringify(state));
}
function _isOpen(id, defaultOpen) {
    const s = _getCollapsed();
    return s[id] !== undefined ? !s[id] : defaultOpen;
}

// --- Chart.js lazy loader ---

function ensureChartJs() {
    return new Promise((resolve) => {
        if (window.Chart) { resolve(); return; }
        const s = document.createElement('script');
        s.src = '/js/vendor/chart.umd.min.js';
        s.onload = () => {
            const s2 = document.createElement('script');
            s2.src = '/js/vendor/chartjs-chart-matrix.min.js';
            s2.onload = resolve;
            s2.onerror = resolve; // proceed without matrix if it fails
            document.head.appendChild(s2);
        };
        s.onerror = resolve; // proceed without charts
        document.head.appendChild(s);
    });
}

// --- Collapsible section helper ---

function _section(id, title, defaultOpen, contentHtml) {
    const open = _isOpen(id, defaultOpen);
    return `
        <div class="mb-4">
            <button class="analytics-collapse-btn flex items-center justify-between w-full text-left py-2 group" data-section="${id}">
                <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider group-hover:text-white">${title}</h3>
                <svg class="w-4 h-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                </svg>
            </button>
            <div id="analytics-section-${id}" class="${open ? '' : 'hidden'}">
                ${contentHtml}
            </div>
        </div>`;
}

function _skeleton(h) {
    return `<div class="bg-slate-700 animate-pulse rounded" style="height:${h}px"></div>`;
}

// --- KPI cards ---

function _renderKpis(kpi, tokenCosts) {
    if (!kpi || kpi.total_decisions === 0) {
        return `
            <div class="flex flex-col items-center justify-center py-16 text-center">
                <svg class="w-16 h-16 text-slate-600 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/>
                </svg>
                <p class="text-slate-400 text-sm">No gatekeeper data yet — decisions will appear here<br>once the security gatekeeper processes commands.</p>
            </div>`;
    }

    const rate = kpi.approval_rate != null ? kpi.approval_rate.toFixed(1) : '0.0';
    const rateColor = Number(rate) >= 90 ? 'text-green-400' : Number(rate) >= 70 ? 'text-yellow-400' : 'text-red-400';
    const cov = kpi.rule_coverage != null ? kpi.rule_coverage.toFixed(1) : '0.0';

    // Cost card — only show if there's actual cost data
    const cost = tokenCosts && tokenCosts.total_cost_usd > 0 ? tokenCosts.total_cost_usd : 0;
    const totalTokens = tokenCosts ? (tokenCosts.total_input_tokens || 0) + (tokenCosts.total_output_tokens || 0) : 0;
    const costDisplay = cost > 0 ? `$${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}` : '$0.00';
    const tokenDisplay = totalTokens > 0 ? totalTokens.toLocaleString() : '0';

    return `
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-6">
            <div class="stat-card">
                <div class="text-2xl font-bold text-white">${(kpi.total_decisions || 0).toLocaleString()}</div>
                <div class="text-xs text-slate-400 mt-1">Total Decisions</div>
            </div>
            <div class="stat-card">
                <div class="text-2xl font-bold ${rateColor}">${rate}%</div>
                <div class="text-xs text-slate-400 mt-1">Approval Rate</div>
            </div>
            <div class="stat-card">
                <div class="text-2xl font-bold text-red-400">${(kpi.denials || 0).toLocaleString()}</div>
                <div class="text-xs text-slate-400 mt-1">Denials</div>
            </div>
            <div class="stat-card">
                <div class="text-2xl font-bold text-blue-400">${cov}%</div>
                <div class="text-xs text-slate-400 mt-1">Rule Coverage</div>
            </div>
            <div class="stat-card">
                <div class="text-2xl font-bold text-amber-400">${(kpi.api_evaluations || 0).toLocaleString()}</div>
                <div class="text-xs text-slate-400 mt-1">API Evaluations</div>
            </div>
            <div class="stat-card">
                <div class="text-2xl font-bold text-emerald-400">${costDisplay}</div>
                <div class="text-xs text-slate-400 mt-1">API Cost <span class="text-slate-500">(${tokenDisplay} tok)</span></div>
            </div>
        </div>`;
}

// --- Main render ---

function renderAnalytics() {
    const rangeButtons = Object.keys(RANGE_OPTIONS).map(r => `
        <button class="analytics-range-btn text-xs px-3 py-1.5 rounded ${analyticsRange === r ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-white'}" data-range="${r}">${r}</button>
    `).join('');

    return `
        <div class="max-w-6xl">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Analytics</h2>
                <div class="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded-lg p-1">
                    ${rangeButtons}
                </div>
            </div>
            <div id="analytics-content" class="space-y-2">
                <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 mb-6">${_skeleton(80)} ${_skeleton(80)} ${_skeleton(80)} ${_skeleton(80)} ${_skeleton(80)}</div>
                ${_section('charts', 'Charts', true, _skeleton(250))}
                ${_section('heatmap', 'Activity Heatmap', false, _skeleton(200))}
                ${_section('sessions', 'Session Risk', false, _skeleton(150))}
                ${_section('rules', 'Rule Intelligence', false, _skeleton(150))}
                ${_section('agents', 'Agents', false, _skeleton(100))}
                ${_section('hooks', 'Hook Health', false, _skeleton(100))}
                ${_section('lessons', 'Lessons', false, _skeleton(100))}
            </div>
        </div>`;
}

// --- Data loading ---

async function loadAnalyticsData() {
    const container = document.getElementById('analytics-content');
    if (!container) return;

    const days = RANGE_OPTIONS[analyticsRange] || 7;
    const q = `?days=${days}`;

    try {
        const [dashboard, heatmap, sessions, rules, agents, hooks, lessons] = await Promise.all([
            api.get(`/api/analytics/gatekeeper-dashboard${q}`).catch(() => null),
            api.get(`/api/analytics/gatekeeper-heatmap${q}`).catch(() => null),
            api.get(`/api/analytics/gatekeeper-sessions${q}`).catch(() => null),
            api.get(`/api/analytics/gatekeeper-rules${q}`).catch(() => null),
            api.get(`/api/analytics/agents${q}`).catch(() => null),
            api.get(`/api/analytics/hooks${q}`).catch(() => null),
            api.get(`/api/analytics/lessons${q}`).catch(() => null),
        ]);

        const kpi = dashboard ? dashboard.kpi : null;
        const tokenCosts = dashboard ? dashboard.token_costs : null;
        const hasData = kpi && kpi.total_decisions > 0;

        // KPI cards (or empty state)
        let html = _renderKpis(kpi, tokenCosts);

        if (hasData) {
            // Charts section — needs Chart.js
            let chartsContent = _skeleton(250);
            if (typeof renderAnalyticsCharts === 'function') {
                await ensureChartJs();
                chartsContent = '<div id="analytics-charts-container"></div>';
            }
            html += _section('charts', 'Charts', true, chartsContent);

            // Heatmap
            let heatmapContent = _skeleton(200);
            if (typeof renderAnalyticsCharts === 'function') {
                heatmapContent = '<div id="analytics-heatmap-container"></div>';
            }
            html += _section('heatmap', 'Activity Heatmap', false, heatmapContent);

            html += _section('sessions', 'Session Risk', false,
                '<div id="analytics-sessions-container"></div>');
            html += _section('rules', 'Rule Intelligence', false,
                '<div id="analytics-rules-container"></div>');
        }

        // Legacy sections
        html += _section('agents', 'Agents', false, renderAgentStats(agents));
        html += _section('hooks', 'Hook Health', false, renderHookStats(hooks));
        html += _section('lessons', 'Lessons', false, renderLessonStats(lessons));

        container.innerHTML = html;

        // Bind collapse toggles
        _bindCollapseButtons();

        // Render charts + tables into their containers (after DOM is ready)
        if (hasData && typeof renderAnalyticsCharts === 'function' && window.Chart) {
            renderAnalyticsCharts(dashboard, heatmap);
        }
        if (hasData && typeof renderAnalyticsTables === 'function') {
            renderAnalyticsTables(sessions, rules);
        }
    } catch (e) {
        container.innerHTML = `
            <div class="bg-red-900/30 border border-red-700 rounded-lg px-4 py-3 text-sm text-red-200">
                Failed to load analytics: ${escapeHtml(e.message)}
            </div>`;
    }
}

function _bindCollapseButtons() {
    document.querySelectorAll('.analytics-collapse-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const id = btn.dataset.section;
            const el = document.getElementById(`analytics-section-${id}`);
            if (!el) return;
            const isHidden = el.classList.toggle('hidden');
            const svg = btn.querySelector('svg');
            if (svg) svg.classList.toggle('rotate-180', !isHidden);
            const state = _getCollapsed();
            state[id] = isHidden;
            _setCollapsed(state);
        });
    });
}

// --- Legacy section renderers (kept from original) ---

function renderAgentStats(data) {
    if (!data) return '<div class="stat-card text-sm text-slate-500">No data available</div>';
    const agents = data.agent_breakdown || [];
    if (agents.length === 0) return '<div class="stat-card text-sm text-slate-500">No agent invocation data</div>';
    const rows = agents.slice(0, 5).map(a => `
        <tr><td class="font-mono">${escapeHtml(a.agent)}</td><td class="text-center">${(a.count||0).toLocaleString()}</td><td class="text-center">${a.avg_duration_ms != null ? (a.avg_duration_ms/1000).toFixed(1)+'s' : '-'}</td></tr>
    `).join('');
    return `<div class="bg-slate-800 border border-slate-700 rounded-lg overflow-x-auto"><table class="data-table"><thead><tr><th class="text-left">Agent</th><th class="text-center">Invocations</th><th class="text-center">Avg Duration</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderHookStats(data) {
    if (!data) return '<div class="stat-card text-sm text-slate-500">No data available</div>';
    const hooks = data.hook_breakdown || [];
    if (hooks.length === 0) return '<div class="stat-card text-sm text-slate-500">No hook execution data</div>';
    const rows = hooks.map(h => {
        const rate = h.success_rate != null ? h.success_rate.toFixed(1)+'%' : '-';
        const c = h.success_rate >= 95 ? 'text-green-400' : h.success_rate >= 80 ? 'text-yellow-400' : 'text-red-400';
        return `<tr><td class="font-mono">${escapeHtml(h.hook)}</td><td class="text-center">${(h.count||0).toLocaleString()}</td><td class="text-center ${c}">${rate}</td><td class="text-center">${h.avg_duration_ms != null ? h.avg_duration_ms.toFixed(0)+'ms' : '-'}</td></tr>`;
    }).join('');
    return `<div class="bg-slate-800 border border-slate-700 rounded-lg overflow-x-auto"><table class="data-table"><thead><tr><th class="text-left">Hook</th><th class="text-center">Executions</th><th class="text-center">Success Rate</th><th class="text-center">Avg Duration</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderLessonStats(data) {
    if (!data) return '<div class="stat-card text-sm text-slate-500">No data available</div>';
    const active = data.active||0, graduated = data.graduated||0, total = active + graduated + (data.archived||0);
    return `<div class="grid grid-cols-1 sm:grid-cols-3 gap-3"><div class="stat-card"><div class="text-2xl font-bold text-blue-400">${active}</div><div class="text-xs text-slate-400 mt-1">Active</div></div><div class="stat-card"><div class="text-2xl font-bold text-green-400">${graduated}</div><div class="text-xs text-slate-400 mt-1">Graduated</div></div><div class="stat-card"><div class="text-2xl font-bold text-slate-400">${total}</div><div class="text-xs text-slate-400 mt-1">Total</div></div></div>`;
}

function renderAnalyticsPlaceholder(title) {
    return `<div class="stat-card text-sm text-slate-500">No ${escapeHtml(title)} data</div>`;
}

// --- Bind events ---

function bindAnalyticsEvents() {
    document.querySelectorAll('.analytics-range-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            analyticsRange = btn.dataset.range;
            renderRoute('analytics');
        });
    });
    loadAnalyticsData();
}

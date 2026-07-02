/**
 * jacked web dashboard — analytics dashboard coordinator
 * KPI cards, collapsible sections, delegates to charts + tables companions.
 */

let analyticsRange = '7d';
const RANGE_OPTIONS = { '24h': 1, '7d': 7, '30d': 30, '90d': 90, '1y': 365 };

let analyticsSubTab = localStorage.getItem('jacked_analytics_subtab') || 'usage';
// The Gatekeeper sub-tab became Activity in 0.70.0 — migrate stale choices.
if (analyticsSubTab === 'gatekeeper') analyticsSubTab = 'activity';

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
            <button class="analytics-collapse-btn flex items-center justify-between w-full text-left py-2 group rounded-t-lg" data-section="${id}">
                <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider group-hover:text-white">${title}</h3>
                <svg class="w-4 h-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                </svg>
            </button>
            <div id="analytics-section-${id}" class="${open ? 'rounded-b-lg' : 'rounded-lg hidden'}">
                ${contentHtml}
            </div>
        </div>`;
}

function _skeleton(h) {
    return `<div class="bg-slate-700 animate-pulse rounded" style="height:${h}px"></div>`;
}

// --- Main render ---

function renderAnalytics() {
    const tabs = [
        { id: 'usage', label: 'Token Usage' },
        { id: 'activity', label: 'Activity' },
    ];

    const tabHtml = tabs.map(t => `
        <button class="analytics-subtab text-sm px-4 py-2 rounded-lg ${analyticsSubTab === t.id
            ? 'bg-teal-600/20 text-teal-300 font-medium'
            : 'text-slate-400 hover:text-white hover:bg-slate-800'}"
            data-subtab="${t.id}">${t.label}</button>
    `).join('');

    return `
        <div class="max-w-6xl">
            <div class="flex items-center gap-3 mb-5">
                <h2 class="text-xl font-semibold text-white">Analytics</h2>
                <div class="flex gap-1 bg-slate-800/50 rounded-lg p-1">${tabHtml}</div>
            </div>
            <div id="analytics-subtab-content"></div>
        </div>`;
}

// --- Sub-tab loading ---

async function loadAnalyticsSubTab() {
    const container = document.getElementById('analytics-subtab-content');
    if (!container) return;

    if (analyticsSubTab === 'activity') {
        await _loadActivityAnalytics(container);
    } else if (analyticsSubTab === 'usage') {
        await _loadUsageAnalytics(container);
    }
}

async function _loadUsageAnalytics(container) {
    const usageTab = localStorage.getItem('jacked_usage_tab') || 'overview';

    const usageTabHtml = [
        { id: 'overview', label: 'Overview' },
        { id: 'sessions', label: 'Sessions' },
        { id: 'trends', label: 'Trends' },
    ].map(t => `
        <button class="usage-tab text-xs px-3 py-1.5 rounded ${usageTab === t.id
            ? 'bg-blue-600 text-white'
            : 'text-slate-400 hover:text-white'}"
            data-usage-tab="${t.id}">${t.label}</button>
    `).join('');

    container.innerHTML = `
        <div class="flex items-center gap-2 mb-4">
            <div class="flex gap-1 bg-slate-800 border border-slate-700 rounded-lg p-1">${usageTabHtml}</div>
            <div id="usage-live-indicator" class="ml-auto"></div>
        </div>
        <div id="usage-tab-content"></div>`;

    // Bind tab clicks
    container.querySelectorAll('.usage-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            localStorage.setItem('jacked_usage_tab', btn.dataset.usageTab);
            _loadUsageAnalytics(container);
        });
    });

    // Load the active sub-tab
    const tabContent = document.getElementById('usage-tab-content');
    if (usageTab === 'overview' && typeof renderUsageOverview === 'function') {
        await renderUsageOverview(tabContent);
    } else if (usageTab === 'sessions' && typeof renderUsageSessions === 'function') {
        await renderUsageSessions(tabContent);
    } else if (usageTab === 'trends' && typeof renderUsageTrends === 'function') {
        await renderUsageTrends(tabContent);
    }
}

// --- Activity data loading (agents / hook health / lessons) ---

async function _loadActivityAnalytics(container) {
    const rangeButtons = Object.keys(RANGE_OPTIONS).map(r => `
        <button class="analytics-range-btn text-xs px-3 py-1.5 rounded ${analyticsRange === r ? 'bg-blue-600 text-white' : 'text-slate-400 hover:text-white'}" data-range="${r}">${r}</button>
    `).join('');

    container.innerHTML = `
        <div class="flex items-center justify-end mb-4">
            <div class="flex items-center gap-1 bg-slate-800 border border-slate-700 rounded-lg p-1">
                ${rangeButtons}
            </div>
        </div>
        <div id="analytics-content" class="space-y-2">
            ${_section('agents', 'Agents', true, _skeleton(100))}
            ${_section('hooks', 'Hook Health', false, _skeleton(100))}
            ${_section('lessons', 'Lessons', false, _skeleton(100))}
        </div>`;

    // Bind range buttons inside the activity sub-tab
    container.querySelectorAll('.analytics-range-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            analyticsRange = btn.dataset.range;
            _loadActivityAnalytics(container);
        });
    });

    await _loadActivityData();
}

async function _loadActivityData() {
    const container = document.getElementById('analytics-content');
    if (!container) return;

    const days = RANGE_OPTIONS[analyticsRange] || 7;
    const q = `?days=${days}`;

    try {
        const [agents, hooks, lessons] = await Promise.all([
            api.get(`/api/analytics/agents${q}`).catch(() => null),
            api.get(`/api/analytics/hooks${q}`).catch(() => null),
            api.get(`/api/analytics/lessons${q}`).catch(() => null),
        ]);

        let html = _section('agents', 'Agents', true, renderAgentStats(agents));
        html += _section('hooks', 'Hook Health', false, renderHookStats(hooks));
        html += _section('lessons', 'Lessons', false, renderLessonStats(lessons));

        container.innerHTML = html;
        _bindCollapseButtons();
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
    // Bind top-level sub-tab clicks (Token Usage | Activity)
    document.querySelectorAll('.analytics-subtab').forEach(btn => {
        btn.addEventListener('click', () => {
            analyticsSubTab = btn.dataset.subtab;
            localStorage.setItem('jacked_analytics_subtab', analyticsSubTab);
            renderRoute('analytics');
        });
    });
    loadAnalyticsSubTab();
}

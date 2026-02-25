/**
 * jacked web dashboard — analytics charts companion
 * Renders line chart (decision trends), doughnut (method breakdown),
 * and activity heatmap into containers created by the coordinator.
 *
 * Exports: renderAnalyticsCharts(dashboardData, heatmapData)
 * Depends: Chart.js (lazy-loaded by coordinator), escapeHtml(), utils
 */

// -- Color palette --
const _CHART_COLORS = {
    ALLOW:    'rgba(74,222,128,0.8)',
    DENY:     'rgba(248,113,113,0.8)',
    ASK_USER: 'rgba(250,204,21,0.8)',
};

const _METHOD_COLORS = [
    '#60a5fa', '#f472b6', '#34d399', '#fbbf24', '#a78bfa',
    '#fb923c', '#2dd4bf', '#e879f9', '#38bdf8', '#f87171',
];

// Keep chart instances for cleanup
let _trendChart = null;
let _doughnutChart = null;

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

function renderAnalyticsCharts(dashboardData, heatmapData) {
    _renderTrendAndDoughnut(dashboardData);
    _renderHeatmap(heatmapData);
}

// ---------------------------------------------------------------------------
// Trend line + doughnut
// ---------------------------------------------------------------------------

function _renderTrendAndDoughnut(data) {
    const container = document.getElementById('analytics-charts-container');
    if (!container) return;

    const ts = data && data.time_series;
    const mb = data && data.method_breakdown;
    const hasTrend = ts && ts.buckets && ts.buckets.length > 0;
    const hasMethods = mb && Object.keys(mb).length > 0;

    if (!hasTrend && !hasMethods) {
        container.innerHTML = '<div class="text-sm text-slate-500 py-6 text-center">No chart data available</div>';
        return;
    }

    container.innerHTML = `
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div class="lg:col-span-2 bg-slate-800 border border-slate-700 rounded-lg p-4">
                <h4 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Decision Trend</h4>
                <div style="position:relative;height:260px"><canvas id="analytics-trend-canvas"></canvas></div>
            </div>
            <div class="bg-slate-800 border border-slate-700 rounded-lg p-4">
                <h4 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Method Breakdown</h4>
                <div style="position:relative;height:260px"><canvas id="analytics-doughnut-canvas"></canvas></div>
            </div>
        </div>`;

    if (hasTrend) _buildTrendChart(ts);
    if (hasMethods) _buildDoughnutChart(mb);
}

function _buildTrendChart(ts) {
    const canvas = document.getElementById('analytics-trend-canvas');
    if (!canvas || !window.Chart) return;

    if (_trendChart) { _trendChart.destroy(); _trendChart = null; }

    const labels = ts.buckets.map(b => _formatBucketLabel(b.bucket, ts.granularity));
    const allowData = ts.buckets.map(b => b.allow || 0);
    const denyData  = ts.buckets.map(b => b.deny || 0);
    const askData   = ts.buckets.map(b => b.ask_user || 0);

    _trendChart = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: 'Allow',
                    data: allowData,
                    borderColor: _CHART_COLORS.ALLOW,
                    backgroundColor: _CHART_COLORS.ALLOW.replace('0.8', '0.15'),
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2,
                },
                {
                    label: 'Deny',
                    data: denyData,
                    borderColor: _CHART_COLORS.DENY,
                    backgroundColor: _CHART_COLORS.DENY.replace('0.8', '0.15'),
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2,
                },
                {
                    label: 'Ask User',
                    data: askData,
                    borderColor: _CHART_COLORS.ASK_USER,
                    backgroundColor: _CHART_COLORS.ASK_USER.replace('0.8', '0.15'),
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            onClick: (_evt, elements) => {
                if (elements.length === 0) return;
                const dsIndex = elements[0].datasetIndex;
                const decision = ['ALLOW', 'DENY', 'ASK_USER'][dsIndex];
                if (decision) _drillToLogs({ decision });
            },
            plugins: {
                legend: { labels: { color: '#94a3b8', font: { size: 11 } } },
                tooltip: { backgroundColor: '#1e293b', titleColor: '#e2e8f0', bodyColor: '#cbd5e1' },
            },
            scales: {
                x: {
                    ticks: { color: '#64748b', maxRotation: 45, font: { size: 10 } },
                    grid: { color: 'rgba(51,65,85,0.5)' },
                },
                y: {
                    stacked: true,
                    beginAtZero: true,
                    ticks: { color: '#64748b', font: { size: 10 } },
                    grid: { color: 'rgba(51,65,85,0.5)' },
                },
            },
        },
    });
}

function _buildDoughnutChart(mb) {
    const canvas = document.getElementById('analytics-doughnut-canvas');
    if (!canvas || !window.Chart) return;

    if (_doughnutChart) { _doughnutChart.destroy(); _doughnutChart = null; }

    const methods = Object.keys(mb).sort((a, b) => mb[b] - mb[a]);
    const counts = methods.map(m => mb[m]);
    const colors = methods.map((_, i) => _METHOD_COLORS[i % _METHOD_COLORS.length]);

    _doughnutChart = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: methods,
            datasets: [{ data: counts, backgroundColor: colors, borderWidth: 0 }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '55%',
            onClick: (_evt, elements) => {
                if (elements.length === 0) return;
                const method = methods[elements[0].index];
                if (method) _drillToLogs({ method });
            },
            plugins: {
                legend: {
                    position: 'right',
                    labels: { color: '#94a3b8', font: { size: 11 }, padding: 8 },
                },
                tooltip: { backgroundColor: '#1e293b', titleColor: '#e2e8f0', bodyColor: '#cbd5e1' },
            },
        },
    });
}

// ---------------------------------------------------------------------------
// Activity heatmap (pure HTML grid — no matrix plugin needed)
// ---------------------------------------------------------------------------

function _renderHeatmap(heatmapData) {
    const container = document.getElementById('analytics-heatmap-container');
    if (!container) return;

    const timestamps = heatmapData && heatmapData.timestamps;
    if (!timestamps || timestamps.length === 0) {
        container.innerHTML = '<div class="text-sm text-slate-500 py-6 text-center">No heatmap data available</div>';
        return;
    }

    // Build 7x24 grid: grid[day][hour] = count
    const grid = Array.from({ length: 7 }, () => new Array(24).fill(0));
    let maxCount = 0;
    for (const entry of timestamps) {
        const d = new Date(entry.timestamp);
        const day = d.getDay();   // 0=Sun .. 6=Sat
        const hour = d.getHours();
        grid[day][hour]++;
        if (grid[day][hour] > maxCount) maxCount = grid[day][hour];
    }

    const dayLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    // Build HTML grid
    let html = '<div class="bg-slate-800 border border-slate-700 rounded-lg p-4 overflow-x-auto">';
    html += '<div class="min-w-[600px]">';

    // Hour header row
    html += '<div class="flex items-center gap-px mb-1 ml-10">';
    for (let h = 0; h < 24; h++) {
        if (h % 3 === 0) {
            html += `<div class="flex-1 text-center text-[10px] text-slate-500">${h}:00</div>`;
        } else {
            html += '<div class="flex-1"></div>';
        }
    }
    html += '</div>';

    // Day rows
    for (let d = 0; d < 7; d++) {
        html += '<div class="flex items-center gap-px mb-px">';
        html += `<div class="w-10 text-xs text-slate-400 text-right pr-2 flex-shrink-0">${dayLabels[d]}</div>`;
        for (let h = 0; h < 24; h++) {
            const count = grid[d][h];
            const color = _heatmapColor(count, maxCount);
            const title = `${dayLabels[d]} ${h}:00 — ${count} decision${count !== 1 ? 's' : ''}`;
            html += `<div class="flex-1 aspect-square rounded-sm cursor-default" style="background:${color};min-width:12px;min-height:12px" title="${escapeHtml(title)}"></div>`;
        }
        html += '</div>';
    }

    // Legend
    html += '<div class="flex items-center gap-2 mt-3 ml-10">';
    html += '<span class="text-[10px] text-slate-500">Less</span>';
    const legendSteps = [0, 0.25, 0.5, 0.75, 1.0];
    for (const pct of legendSteps) {
        const c = _heatmapColor(pct * maxCount, maxCount);
        html += `<div class="w-3 h-3 rounded-sm" style="background:${c}"></div>`;
    }
    html += '<span class="text-[10px] text-slate-500">More</span>';
    html += '</div>';

    html += '</div></div>';
    container.innerHTML = html;
}

function _heatmapColor(count, max) {
    if (max === 0 || count === 0) return '#1e293b'; // slate-800
    const ratio = Math.min(count / max, 1);
    // Interpolate: slate-800 (30,41,59) → blue-500 (59,130,246) → red-500 (239,68,68)
    if (ratio <= 0.5) {
        const t = ratio * 2;
        const r = Math.round(30 + (59 - 30) * t);
        const g = Math.round(41 + (130 - 41) * t);
        const b = Math.round(59 + (246 - 59) * t);
        return `rgb(${r},${g},${b})`;
    } else {
        const t = (ratio - 0.5) * 2;
        const r = Math.round(59 + (239 - 59) * t);
        const g = Math.round(130 + (68 - 130) * t);
        const b = Math.round(246 + (68 - 246) * t);
        return `rgb(${r},${g},${b})`;
    }
}

// ---------------------------------------------------------------------------
// Bucket label formatting
// ---------------------------------------------------------------------------

function _formatBucketLabel(bucket, granularity) {
    if (!bucket) return '';
    try {
        const d = new Date(bucket);
        if (granularity === 'hour') {
            return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        }
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    } catch {
        return bucket;
    }
}

// ---------------------------------------------------------------------------
// Drill-down navigation
// ---------------------------------------------------------------------------

function _drillToLogs(params) {
    const qs = new URLSearchParams(params);
    qs.set('from', 'analytics');
    window.location.hash = 'logs?' + qs.toString();
}

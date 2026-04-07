/**
 * jacked web dashboard — token usage trends tab
 *
 * Chart.js daily bar + line chart for tokens and cache hit %.
 * Exports: renderUsageTrends(container)
 */

let _usageTrendsChart = null;

async function renderUsageTrends(container) {
    const status = await api.get('/api/analytics/usage-scan-status').catch(() => null);
    if (!status?.ready) {
        container.innerHTML = '<p class="text-slate-500 text-sm py-8 text-center">Analytics scan in progress...</p>';
        return;
    }

    await ensureChartJs();

    const data = await api.get('/api/analytics/usage-trends?days=30').catch(() => null);
    const summaries = data?.summaries || [];

    if (summaries.length === 0) {
        container.innerHTML = '<p class="text-slate-500 text-sm py-8 text-center">No trend data available yet.</p>';
        return;
    }

    // Group by date
    const byDate = {};
    for (const s of summaries) {
        if (!byDate[s.date]) byDate[s.date] = { tokens: 0, cost: 0, cache_ratio: 0, count: 0 };
        const d = byDate[s.date];
        d.tokens += (s.input_tokens || 0) + (s.output_tokens || 0) + (s.cache_read_tokens || 0) + (s.cache_create_tokens || 0);
        d.cost += s.estimated_cost_usd || 0;
        d.cache_ratio += (s.cache_hit_ratio || 0);
        d.count++;
    }

    const dates = Object.keys(byDate).sort();
    const tokenData = dates.map(d => byDate[d].tokens);
    const cacheData = dates.map(d => byDate[d].count > 0 ? byDate[d].cache_ratio / byDate[d].count : 0);
    const labels = dates.map(d => d.slice(5)); // MM-DD

    container.innerHTML = '<div class="bg-slate-800/30 rounded-lg p-4"><canvas id="usage-trends-chart" height="200"></canvas></div>';

    const ctx = document.getElementById('usage-trends-chart');
    if (!ctx || !window.Chart) return;

    // Destroy previous instance to prevent memory leak
    if (_usageTrendsChart) {
        _usageTrendsChart.destroy();
        _usageTrendsChart = null;
    }

    _usageTrendsChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Tokens',
                    data: tokenData,
                    backgroundColor: 'rgba(94, 234, 212, 0.5)',
                    borderColor: 'rgb(94, 234, 212)',
                    borderWidth: 1,
                    yAxisID: 'y',
                },
                {
                    label: 'Cache Hit %',
                    data: cacheData,
                    type: 'line',
                    borderColor: 'rgb(129, 140, 248)',
                    borderWidth: 2,
                    pointRadius: 2,
                    fill: false,
                    yAxisID: 'y1',
                },
            ],
        },
        options: {
            responsive: true,
            interaction: { mode: 'index', intersect: false },
            scales: {
                y: {
                    type: 'linear',
                    position: 'left',
                    grid: { color: 'rgba(148,163,184,0.1)' },
                    ticks: { color: '#94a3b8' },
                },
                y1: {
                    type: 'linear',
                    position: 'right',
                    min: 0,
                    max: 100,
                    grid: { display: false },
                    ticks: { color: '#818cf8', callback: v => v + '%' },
                },
                x: {
                    grid: { display: false },
                    ticks: { color: '#64748b' },
                },
            },
            plugins: {
                legend: { labels: { color: '#94a3b8' } },
            },
        },
    });
}

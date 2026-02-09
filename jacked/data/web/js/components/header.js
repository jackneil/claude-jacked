/**
 * jacked web dashboard — header / version display
 */

function _timeAgo(isoStr) {
    if (!isoStr) return null;
    const diff = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
}

function _timeUntil(isoStr) {
    if (!isoStr) return null;
    const diff = (new Date(isoStr).getTime() - Date.now()) / 1000;
    if (diff <= 0) return 'soon';
    if (diff < 3600) return `in ${Math.round(diff / 60)}m`;
    if (diff < 86400) return `in ${Math.round(diff / 3600)}h`;
    return `in ${Math.round(diff / 86400)}d`;
}

function _buildTooltip(current, latest, outdated, ahead, checkedAt, nextCheckAt) {
    const lines = [];
    if (outdated && latest) {
        lines.push(`Update available: v${current} \u2192 v${latest}`);
        lines.push('pip install -U claude-jacked');
    } else if (ahead && latest) {
        lines.push(`v${current} — ahead of PyPI (v${latest})`);
        lines.push('Running unreleased build');
    } else if (latest) {
        lines.push(`v${current} \u2014 latest`);
    } else {
        lines.push('Could not reach PyPI');
    }
    const ago = _timeAgo(checkedAt);
    if (ago) lines.push(`Checked: ${ago}`);
    const until = _timeUntil(nextCheckAt);
    if (until) lines.push(`Next check: ${until}`);
    return lines.join('\n');
}

async function _refreshVersion() {
    const btn = document.getElementById('version-refresh-btn');
    if (!btn || btn.classList.contains('version-refresh--spinning')) return;
    btn.classList.add('version-refresh--spinning');
    try {
        const data = await api.post('/api/version/refresh');
        window.jackedState.version = data;
        updateVersionDisplay(data);
    } catch (e) {
        console.error('Version refresh failed:', e);
    } finally {
        btn.classList.remove('version-refresh--spinning');
    }
}

function updateVersionDisplay(versionData) {
    const el = document.getElementById('version-info');
    if (!el || !versionData) return;

    const current = versionData.current_version || versionData.current || '';
    const latest = versionData.latest_version || versionData.latest || '';
    const outdated = versionData.outdated || false;
    const ahead = versionData.ahead || false;
    const checkedAt = versionData.checked_at || '';
    const nextCheckAt = versionData.next_check_at || '';

    const tooltip = _buildTooltip(current, latest, outdated, ahead, checkedAt, nextCheckAt);

    const refresh = `<button id="version-refresh-btn" class="version-refresh" onclick="_refreshVersion()" title="Check now">\u21bb</button>`;

    let badge;
    if (outdated && latest) {
        badge = `<span class="version-badge version-badge--outdated" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} \u2192 v${escapeHtml(latest)} ${refresh}</span>`;
    } else if (ahead && latest) {
        badge = `<span class="version-badge version-badge--ahead" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} dev ${refresh}</span>`;
    } else if (latest) {
        badge = `<span class="version-badge version-badge--current" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} \u2713 ${refresh}</span>`;
    } else {
        badge = `<span class="version-badge version-badge--unknown" title="${escapeHtml(tooltip)}">v${escapeHtml(current)} ${refresh}</span>`;
    }

    el.innerHTML = badge;
}

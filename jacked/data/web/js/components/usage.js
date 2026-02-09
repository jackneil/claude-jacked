/**
 * jacked web dashboard — usage bar component
 * Reusable usage bar with percentage fill, color coding, and elapsed-time marker.
 */

/**
 * Render a usage bar.
 * @param {number} percentage - Usage percentage (0-100).
 * @param {string} resetTime - ISO timestamp or human-readable reset time string.
 * @param {number|null} elapsedFraction - 0-1 fraction of time elapsed in the window.
 * @param {string} label - Label like "5h limit" or "7d limit".
 * @returns {string} HTML string.
 */
function renderUsageBar(percentage, resetTime, elapsedFraction, label) {
    const pct = Math.max(0, Math.min(100, percentage || 0));
    const colorClass = pct >= 90 ? 'red' : (pct >= 71 ? 'yellow' : 'green');

    let markerHtml = '';
    if (elapsedFraction !== null && elapsedFraction !== undefined && elapsedFraction >= 0 && elapsedFraction <= 1) {
        const markerPos = (elapsedFraction * 100).toFixed(1);
        markerHtml = `<div class="elapsed-marker" style="left: ${markerPos}%"></div>`;
    }

    const resetDisplay = resetTime ? formatResetTime(resetTime) : '';

    return `
        <div class="flex items-center gap-3 mb-1.5">
            <span class="text-xs text-slate-400 w-14 shrink-0">${escapeHtml(label || '')}</span>
            <div class="usage-bar flex-1">
                <div class="fill ${colorClass}" style="width: ${pct.toFixed(1)}%"></div>
                ${markerHtml}
            </div>
            <span class="text-xs font-mono w-10 text-right ${colorClass === 'red' ? 'text-red-400' : colorClass === 'yellow' ? 'text-yellow-400' : 'text-slate-300'}">${Math.round(pct)}%</span>
            <span class="text-xs text-slate-500 w-28 text-right">${escapeHtml(resetDisplay)}</span>
        </div>
    `;
}

/**
 * Compute elapsed fraction for a 5-hour window.
 * @param {string} resetsAt - ISO timestamp when the window resets.
 * @returns {number} 0-1 fraction elapsed.
 */
function computeElapsedFraction5h(resetsAt) {
    if (!resetsAt) return null;
    try {
        const resetMs = new Date(resetsAt).getTime();
        const windowMs = 5 * 60 * 60 * 1000; // 5 hours
        const startMs = resetMs - windowMs;
        const nowMs = Date.now();
        const elapsed = (nowMs - startMs) / windowMs;
        return Math.max(0, Math.min(1, elapsed));
    } catch {
        return null;
    }
}

/**
 * Compute elapsed fraction for a 7-day window.
 * @param {string} resetsAt - ISO timestamp when the window resets.
 * @returns {number} 0-1 fraction elapsed.
 */
function computeElapsedFraction7d(resetsAt) {
    if (!resetsAt) return null;
    try {
        const resetMs = new Date(resetsAt).getTime();
        const windowMs = 7 * 24 * 60 * 60 * 1000; // 7 days
        const startMs = resetMs - windowMs;
        const nowMs = Date.now();
        const elapsed = (nowMs - startMs) / windowMs;
        return Math.max(0, Math.min(1, elapsed));
    } catch {
        return null;
    }
}

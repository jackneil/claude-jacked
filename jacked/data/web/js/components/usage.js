/**
 * jacked web dashboard — usage bar component
 * Reusable usage bar with percentage fill, color coding, and elapsed-time marker.
 */

/**
 * Color class for a usage percentage. Single source of truth for the
 * green/yellow(amber)/red thresholds, shared by renderUsageBar and any other
 * surface (menu-bar pill, side panel) so a bar and its pill can never disagree.
 * Mirror of jacked/service/menubar_summary.py::usage_color_class (Python side).
 * @param {number} percentage - Usage percentage (0-100).
 * @returns {'green'|'yellow'|'red'}
 */
function usageColorClass(percentage) {
    const pct = Math.max(0, Math.min(100, percentage || 0));
    return pct >= 90 ? 'red' : (pct >= 71 ? 'yellow' : 'green');
}

/**
 * Tailwind text-color utility for a usage percentage's color class. The bar fill
 * itself is themed purely in CSS (.usage-bar .fill.<class>), but the percentage
 * label uses a Tailwind utility class emitted here, so the America 250 theme has
 * to pick a readable tone per scheme. The semantic class names (green/yellow/red)
 * stay intact — only the presentation shifts.
 *   america250: green→slate-300 (blue bar), yellow→slate-100 (white bar reads on
 *               dark text elsewhere; light label sits beside it), red→red-400.
 *   classic:    the original green→slate-300 / yellow→yellow-400 / red→red-400.
 * @param {'green'|'yellow'|'red'} colorClass - Semantic usage color class.
 * @returns {string} Tailwind text-color utility class.
 */
function usageTextClass(colorClass) {
    // Guard: the node-eval test harness (tests/unit/test_web_js_*.py) runs this
    // file without a DOM; theme detection is inherently browser-only, so no
    // document means the classic mapping.
    if (typeof document !== 'undefined'
        && document.documentElement.classList.contains('theme-america250')) {
        return colorClass === 'red' ? 'text-red-400' : colorClass === 'yellow' ? 'text-slate-100' : 'text-slate-300';
    }
    return colorClass === 'red' ? 'text-red-400' : colorClass === 'yellow' ? 'text-yellow-400' : 'text-slate-300';
}

/**
 * Render a usage bar.
 * @param {number} percentage - Usage percentage (0-100).
 * @param {string} resetTime - ISO timestamp or human-readable reset time string.
 * @param {number|null} elapsedFraction - 0-1 fraction of time elapsed in the window.
 * @param {string} label - Label like "5h limit" or "7d limit".
 * @param {{compact?: boolean, reason?: ({text: string, tone?: string, muted?: boolean}|null)}} [opts]
 *   compact drops the fixed reset-time column
 *   (it's moved to the row's hover title) and narrows the label so the bar gets
 *   the width back — for tight surfaces like the menu-bar panel popover. The
 *   bar/marker/threshold logic is identical, so the two surfaces never diverge.
 *   reason (from windowReason) replaces the bare "no active window" when the
 *   window has no future reset: plain muted text in the reset column (warning
 *   tone for a re-auth), the whole row dimmed when the reading is stale.
 * @returns {string} HTML string.
 */
function renderUsageBar(percentage, resetTime, elapsedFraction, label, opts) {
    const compact = !!(opts && opts.compact);
    const pct = Math.max(0, Math.min(100, percentage || 0));
    const colorClass = usageColorClass(pct);
    const pctColor = usageTextClass(colorClass);

    let markerHtml = '';
    if (elapsedFraction !== null && elapsedFraction !== undefined && elapsedFraction >= 0 && elapsedFraction <= 1) {
        const markerPos = (elapsedFraction * 100).toFixed(1);
        markerHtml = `<div class="elapsed-marker" style="left: ${markerPos}%"></div>`;
    }

    let resetDisplay = resetTime ? formatResetTime(resetTime) : '';
    const reason = opts && opts.reason;
    let resetTone = 'text-slate-500';
    let rowMuted = '';
    if (reason && reason.text && !/^resets\s+/i.test(resetDisplay)) {
        resetDisplay = reason.text;
        if (reason.tone === 'warn') resetTone = 'text-amber-400';
        if (reason.muted) rowMuted = ' opacity-60';
    }

    if (compact) {
        // Narrow label + bar + percent, then a short reset caption so the
        // dropdown answers "when does this window reset?" inline (the full
        // dashboard already shows this per bar). The "resets " prefix is dropped
        // to stay tight; same-day shows the time ("3:45 PM"), other days add the
        // date ("Jun 30 3:45 PM"). Full text still rides the row title on hover.
        // No extra row → the 6-account no-scroll panel fit is preserved.
        // Only the "resets <time>" form gets a caption; a past/expired window
        // ("no active window") clamps to a short em-dash so the widest case
        // can't squeeze the bar — the full text still rides the hover title.
        const isReset = /^resets\s+/i.test(resetDisplay);
        const resetShort = isReset ? resetDisplay.replace(/^resets\s+/i, '') : (resetDisplay ? '—' : '');
        const resetCaption = resetShort
            ? `<span class="reset-caption text-[10px] text-slate-500 shrink-0 text-right tabular-nums whitespace-nowrap" title="${escapeHtml(resetDisplay)}">${escapeHtml(resetShort)}</span>`
            : '';
        return `
        <div class="flex items-center gap-2 mb-0.5${rowMuted}" title="${escapeHtml(label || '')}${resetDisplay ? ' · ' + escapeHtml(resetDisplay) : ''}">
            <span class="text-[10px] text-slate-400 w-10 shrink-0 truncate">${escapeHtml(label || '')}</span>
            <div class="usage-bar flex-1">
                <div class="fill ${colorClass}" style="width: ${pct.toFixed(1)}%"></div>
                ${markerHtml}
            </div>
            <span class="text-[11px] font-mono w-9 text-right tabular-nums ${pctColor}">${Math.round(pct)}%</span>
            ${resetCaption}
        </div>
    `;
    }

    return `
        <div class="flex items-center gap-3 mb-1${rowMuted}">
            <span class="text-xs text-slate-400 w-14 shrink-0 truncate" title="${escapeHtml(label || '')}">${escapeHtml(label || '')}</span>
            <div class="usage-bar flex-1">
                <div class="fill ${colorClass}" style="width: ${pct.toFixed(1)}%"></div>
                ${markerHtml}
            </div>
            <span class="text-xs font-mono w-10 text-right tabular-nums ${pctColor}">${Math.round(pct)}%</span>
            <span class="reset-text text-xs ${resetTone} w-28 text-right" title="${escapeHtml(resetDisplay)}">${escapeHtml(resetDisplay)}</span>
        </div>
    `;
}

/**
 * Compute elapsed fraction for a 5-hour window.
 * @param {string} resetsAt - ISO timestamp when the window resets.
 * @returns {number|null} 0-1 fraction elapsed; null when there is no reset or
 *   the reset is already past (a lapsed window has no "now" marker to draw).
 */
function computeElapsedFraction5h(resetsAt) {
    if (!resetsAt) return null;
    try {
        const resetMs = new Date(resetsAt).getTime();
        const windowMs = 5 * 60 * 60 * 1000; // 5 hours
        const startMs = resetMs - windowMs;
        const nowMs = Date.now();
        const elapsed = (nowMs - startMs) / windowMs;
        if (!Number.isFinite(elapsed) || elapsed >= 1) return null;
        return Math.max(0, elapsed);
    } catch {
        return null;
    }
}

/**
 * Compute elapsed fraction for a 7-day window.
 * @param {string} resetsAt - ISO timestamp when the window resets.
 * @returns {number|null} 0-1 fraction elapsed; null when there is no reset or
 *   the reset is already past (a lapsed window has no "now" marker to draw).
 */
function computeElapsedFraction7d(resetsAt) {
    if (!resetsAt) return null;
    try {
        const resetMs = new Date(resetsAt).getTime();
        const windowMs = 7 * 24 * 60 * 60 * 1000; // 7 days
        const startMs = resetMs - windowMs;
        const nowMs = Date.now();
        const elapsed = (nowMs - startMs) / windowMs;
        if (!Number.isFinite(elapsed) || elapsed >= 1) return null;
        return Math.max(0, elapsed);
    } catch {
        return null;
    }
}

// ---------------------------------------------------------------------------
// Why a window has no future reset (window keeper state)
// ---------------------------------------------------------------------------

/** Usage older than this reads as stale: three 10-minute poll intervals. */
const USAGE_STALE_SECS = 30 * 60;

/** Short local time ("3:45 PM"), with the date when it is not today. */
function _keeperWhen(epochSecs) {
    const d = new Date(epochSecs * 1000);
    const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (d.toDateString() === new Date().toDateString()) return time;
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
}

/** Short local date of a reading ("3:45 PM" today, else "Sep 5"). */
function _keeperReadDate(epochSecs) {
    const d = new Date(epochSecs * 1000);
    if (d.toDateString() === new Date().toDateString()) {
        return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

/** The configured keeper start (HH:MM), else the keeper default. */
function _keeperActiveStart() {
    const s = (typeof window !== 'undefined' && window.jackedState && window.jackedState.settings) || {};
    let v = s.window_keeper_active_start;
    if (typeof v === 'string') v = v.replace(/^"|"$/g, '');
    return (typeof v === 'string' && /^\d{1,2}:\d{2}$/.test(v)) ? v : '06:00';
}

/**
 * Explain a usage window that has no future reset, from the account's
 * keeper_state and usage freshness. Returns null when the window is open
 * (the normal "resets ..." text applies) or when nothing better than the
 * plain "no active window" is known.
 * @param {object} acct - Account payload (keeper_state, usage_cached_at, provider).
 * @param {string|null} resetsAt - ISO reset timestamp of this window.
 * @param {number} [nowMs] - Current time in ms (tests).
 * @returns {{text: string, tone: ('warn'|'muted'), muted: boolean}|null}
 */
function windowReason(acct, resetsAt, nowMs) {
    if (!acct) return null;
    const now = (nowMs === undefined || nowMs === null) ? Date.now() : nowMs;
    const resetMs = resetsAt ? new Date(resetsAt).getTime() : NaN;
    if (Number.isFinite(resetMs) && resetMs > now) return null;

    const reason = (text, tone, muted) => ({ text, tone: tone || 'muted', muted: !!muted });
    const ks = acct.keeper_state || null;
    // A dead token also stops usage reads, so the actionable state wins
    // over "last read"; the re-auth control on the card is the fix.
    if (ks && ks.last_status === 'needs_reauth') return reason('needs re-auth', 'warn');

    const cachedAt = Number(acct.usage_cached_at) || 0;
    const stale = cachedAt > 0 && (now / 1000 - cachedAt) > USAGE_STALE_SECS;
    if (cachedAt > 0 && (acct.provider === 'codex' || stale)) {
        return reason('last read ' + _keeperReadDate(cachedAt), 'muted', true);
    }

    if (!ks) return null;
    switch (ks.skip_reason) {
        case 'keeper_disabled': return reason('keeper off');
        case 'no_token': return reason('no Claude Code token');
        case 'outside_active_hours': return reason('paused until ' + _keeperActiveStart());
        case 'auto_swap_disabled': return reason('auto-swap off');
        default: break;
    }
    const next = Number(ks.next_attempt_at) || 0;
    if (ks.last_status === 'rate_limited') {
        return reason(next ? 'rate limited until ' + _keeperWhen(next) : 'rate limited');
    }
    if (next && ['transport', 'auth_failed', 'error', 'ping_ok_no_window'].includes(ks.last_status)) {
        return reason('retrying ' + _keeperWhen(next));
    }
    if (ks.last_status === 'ok' || ks.last_status === 'ping_ok_no_window') {
        return reason('window lapsed');
    }
    return null;
}

/** renderUsageBar opts for an account window: merges the window reason. */
function usageBarOpts(acct, resetsAt, base) {
    return Object.assign({}, base || {}, { reason: windowReason(acct, resetsAt) });
}

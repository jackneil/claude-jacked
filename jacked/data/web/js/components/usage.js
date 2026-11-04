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
 * @param {{compact?: boolean, hint?: string, reason?: ({text: string, tone?: string, muted?: boolean, absent?: boolean, none?: boolean, hint?: string}|null)}} [opts]
 *   compact drops the fixed reset-time column
 *   (it's moved to the row's hover title) and narrows the label so the bar gets
 *   the width back — for tight surfaces like the menu-bar panel popover. The
 *   bar/marker/threshold logic is identical, so the two surfaces never diverge.
 *   reason (from windowReason) replaces the bare "no active window" when the
 *   window has no future reset: plain muted text in the reset column (warning
 *   tone for a re-auth). When the reading is stale, the bar and the percent
 *   are dimmed; the reason text that explains the dimming stays readable.
 *   reason.absent marks a window the provider does not report: the bar has
 *   no fill and the percent reads "n/a". reason.none marks a window the
 *   plan does not have: the same empty track, the percent reads "none", and
 *   the reason replaces any reset time. reason.hint, when set, is the hover
 *   text of the reset column. hint (a temporary window's note) is added to
 *   the hover text of the row and of the reset column.
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
    // A window the plan does not have ("none") has no reset to show either.
    const none = !!(reason && reason.none);
    let resetTone = 'text-slate-500';
    let staleDim = '';
    let hasReason = false;
    if (reason && reason.text && (none || !/^resets\s+/i.test(resetDisplay))) {
        resetDisplay = reason.text;
        hasReason = true;
        if (reason.tone === 'warn') resetTone = 'text-amber-400';
        if (reason.muted) staleDim = ' opacity-60';
    }
    // A window the provider does not report ("n/a") or the plan does not
    // have ("none") has no reading: an empty track, never a 0% bar that
    // reads as free headroom.
    const absent = none || !!(reason && reason.absent);
    // Hover text: the reason's own explanation when it has one, then the
    // window's note (a temporary 5-hour limit, for example).
    let resetTitle = hasReason && reason.hint ? reason.hint : resetDisplay;
    const hint = opts && opts.hint ? String(opts.hint) : '';
    if (hint && hint !== resetTitle) resetTitle = resetTitle ? resetTitle + ' · ' + hint : hint;
    const rowTitle = (label || '') + (resetTitle ? ' · ' + resetTitle : '');
    const fillHtml = absent ? '' : `<div class="fill ${colorClass}" style="width: ${pct.toFixed(1)}%"></div>`;
    const pctText = absent ? (none ? 'none' : 'n/a') : Math.round(pct) + '%';
    const pctTone = absent ? 'text-slate-500' : pctColor;
    if (absent) markerHtml = '';
    // An empty track at full strength reads as a 0% bar at a glance: dim the
    // track of a window with no reading so "not applicable" shows first.
    const trackDim = absent && !staleDim ? ' opacity-40' : '';

    if (compact) {
        // Narrow label + bar + percent, then a short reset caption so the
        // dropdown answers "when does this window reset?" inline (the full
        // dashboard already shows this per bar). The "resets " prefix is dropped
        // to stay tight; same-day shows the time ("3:45 PM"), other days add the
        // date ("Jun 30 3:45 PM"). Full text still rides the row title on hover.
        // No extra row → the 6-account no-scroll panel fit is preserved.
        // The "resets <time>" form drops its prefix; a keeper reason shows
        // its short form (the reason strings are at most 14 characters, the
        // width the panel already accepts for "Oct 27 3:45 PM"); a window
        // with no reason reads "no window". Full text rides the hover title.
        const isReset = /^resets\s+/i.test(resetDisplay);
        let resetShort = '';
        if (isReset) resetShort = resetDisplay.replace(/^resets\s+/i, '');
        else if (hasReason) resetShort = reason.short || resetDisplay;
        else if (resetDisplay) resetShort = 'no window';
        const resetCaption = resetShort
            ? `<span class="reset-caption text-[10px] ${resetTone} shrink-0 text-right tabular-nums whitespace-nowrap" title="${escapeHtml(resetTitle)}">${escapeHtml(resetShort)}</span>`
            : '';
        return `
        <div class="flex items-center gap-2 mb-0.5" title="${escapeHtml(rowTitle)}">
            <span class="text-[10px] text-slate-400 w-10 shrink-0 truncate">${escapeHtml(label || '')}</span>
            <div class="usage-bar flex-1${staleDim}${trackDim}">
                ${fillHtml}
                ${markerHtml}
            </div>
            <span class="text-[11px] font-mono w-9 text-right tabular-nums ${pctTone}${staleDim}">${pctText}</span>
            ${resetCaption}
        </div>
    `;
    }

    // The dashboard row has a hover title only when the window has a note: the
    // label and the reset column carry their own titles.
    const rowTitleAttr = hint ? ` title="${escapeHtml(rowTitle)}"` : '';
    return `
        <div class="flex items-center gap-3 mb-1"${rowTitleAttr}>
            <span class="text-xs text-slate-400 w-14 shrink-0 truncate" title="${escapeHtml(label || '')}">${escapeHtml(label || '')}</span>
            <div class="usage-bar flex-1${staleDim}${trackDim}">
                ${fillHtml}
                ${markerHtml}
            </div>
            <span class="text-xs font-mono w-10 text-right tabular-nums ${pctTone}${staleDim}">${pctText}</span>
            <span class="reset-text text-xs ${resetTone} w-32 text-right whitespace-nowrap truncate" title="${escapeHtml(resetTitle)}">${escapeHtml(resetDisplay)}</span>
        </div>
    `;
}

// ---------------------------------------------------------------------------
// Window lengths and the elapsed ("now") marker
// ---------------------------------------------------------------------------

/**
 * The account fields of each usage window, keyed like the server's
 * acct.windows object. seconds is the length a payload from an older server
 * implies (it sends no windows object): 5 hours and 7 days.
 */
const USAGE_WINDOW_FIELDS = {
    five_hour: { value: 'cached_usage_5h', reset: 'cached_5h_resets_at', seconds: 5 * 3600, short: '5h', long: '5-hour' },
    seven_day: { value: 'cached_usage_7d', reset: 'cached_7d_resets_at', seconds: 7 * 86400, short: '7d', long: '7-day' },
};

/** The fields of a window key, or null for a key that is not a window. */
function _usageWindowFields(windowKey) {
    return Object.prototype.hasOwnProperty.call(USAGE_WINDOW_FIELDS, windowKey)
        ? USAGE_WINDOW_FIELDS[windowKey]
        : null;
}

/** The server's facts about one window of an account (acct.windows[key]):
 *  {state, temporary, seconds, note}, or null when the payload has none. */
function _usageWindowInfo(acct, windowKey) {
    const win = acct && acct.windows && acct.windows[windowKey];
    return win && typeof win === 'object' ? win : null;
}

/**
 * Fraction of a usage window that has elapsed, for the white "now" marker.
 * @param {string} resetsAt - ISO timestamp when the window resets.
 * @param {number} windowSeconds - Length of the window in seconds.
 * @param {number} [nowMs] - Current time in ms (tests).
 * @returns {number|null} 0-1 fraction elapsed; null when there is no reset,
 *   the reset is already past (a lapsed window has no "now" marker to draw),
 *   or the window length is missing or not positive.
 */
function computeElapsedFraction(resetsAt, windowSeconds, nowMs) {
    if (!resetsAt) return null;
    const seconds = Number(windowSeconds);
    if (!Number.isFinite(seconds) || seconds <= 0) return null;
    const resetMs = new Date(resetsAt).getTime();
    if (!Number.isFinite(resetMs)) return null;
    const windowMs = seconds * 1000;
    const now = (nowMs === undefined || nowMs === null) ? Date.now() : nowMs;
    const elapsed = (now - (resetMs - windowMs)) / windowMs;
    if (!Number.isFinite(elapsed) || elapsed >= 1) return null;
    return Math.max(0, elapsed);
}

/**
 * Elapsed fraction of an account's 5h or 7d window: its cached reset and the
 * window length the server sent (acct.windows[windowKey].seconds). A payload
 * without a usable length (an older server) uses 5 hours or 7 days.
 * @param {object} acct - Account payload.
 * @param {'five_hour'|'seven_day'} windowKey - The window.
 * @returns {number|null} 0-1 fraction elapsed, or null (no marker).
 */
function windowElapsed(acct, windowKey) {
    const fields = _usageWindowFields(windowKey);
    if (!acct || !fields) return null;
    const win = _usageWindowInfo(acct, windowKey);
    const sent = win ? Number(win.seconds) : NaN;
    const seconds = Number.isFinite(sent) && sent > 0 ? sent : fields.seconds;
    return computeElapsedFraction(acct[fields.reset], seconds);
}

/**
 * Render the bar of one model-scoped limit: a per-model entry or the binding
 * model (the Fable weekly limit, a Codex named limit). This is the one path
 * for every such bar, so each one gets the elapsed marker of its own window
 * (model.window_seconds). A payload without window_seconds draws no marker.
 * @param {{utilization: number, resets_at: (string|null), window_seconds?: number, label?: string}} model
 * @param {object} [opts] - renderUsageBar opts (compact).
 * @returns {string} HTML string.
 */
function renderModelBar(model, opts) {
    const m = model || {};
    return renderUsageBar(m.utilization, m.resets_at, computeElapsedFraction(m.resets_at, m.window_seconds), m.label || 'model', opts);
}

// ---------------------------------------------------------------------------
// Why a window has no future reset (window keeper state)
// ---------------------------------------------------------------------------

/** Usage older than this reads as stale: six 5-minute poll intervals (usage_check_interval default 300s). */
const USAGE_STALE_SECS = 30 * 60;

/** Short local time ("3:45 PM"); the date is added only when the moment is
 *  more than a day away (a time within the next 24 hours is unambiguous
 *  even past midnight, and the column is sized for the short form). */
function _keeperWhen(epochSecs, nowMs) {
    const d = new Date(epochSecs * 1000);
    const now = (nowMs === undefined || nowMs === null) ? Date.now() : nowMs;
    const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (Math.abs(d.getTime() - now) < 24 * 3600 * 1000) return time;
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

/** Longest reason caption the compact panel column holds ("Oct 27 3:45 PM"). */
const USAGE_REASON_SHORT_MAX = 14;

/**
 * Explain a usage window that the plan does not have, that the provider did
 * not report, or that has no future reset (window keeper state and usage
 * freshness). Returns null when the window is open (the normal "resets ..."
 * text applies) or when nothing better than the plain "no active window" is
 * known.
 * @param {object} acct - Account payload (windows, plan, keeper_state,
 *   usage_cached_at, provider, and the cached value and reset of the window).
 * @param {'five_hour'|'seven_day'} windowKey - The window.
 * @param {number} [nowMs] - Current time in ms (tests).
 * @returns {{text: string, short: string, tone: ('warn'|'muted'), muted: boolean, absent?: boolean, none?: boolean, hint?: string}|null}
 *   text is the dashboard column copy; short is the compact-panel caption
 *   (at most 14 characters); muted marks a stale reading (the bar dims);
 *   absent marks a window with no reading; none marks a window the plan does
 *   not have; hint is the hover text.
 */
function windowReason(acct, windowKey, nowMs) {
    const fields = _usageWindowFields(windowKey);
    if (!acct || !fields) return null;
    const win = _usageWindowInfo(acct, windowKey);
    // The plan has no such window (a verified Ultra account has no 5-hour
    // limit). The server decides this from the plan catalog and the payload.
    if (win && win.state === 'absent') {
        // "not on <Plan>" only when the catalog knows this plan: on catalog
        // drift (an unlisted tier, say) the plan name can be a family name
        // such as "Max", and "not on Max" would wrongly describe every Max
        // account. Then the generic text applies.
        const drift = acct.plan && Array.isArray(acct.plan.drift) && acct.plan.drift.length > 0;
        const plan = !drift && acct.plan && typeof acct.plan.label === 'string' ? acct.plan.label.trim() : '';
        const generic = 'no ' + fields.short + ' limit';
        const text = windowKey === 'five_hour' && plan ? 'not on ' + plan : generic;
        return {
            text,
            short: text.length <= USAGE_REASON_SHORT_MAX ? text : generic,
            tone: 'muted', muted: false, absent: true, none: true,
            hint: win.note || ('This plan has no ' + fields.long + ' limit.'),
        };
    }
    // A window the provider did not report has no reading: "not reported",
    // not 0% used. That is a window in the "unknown" state, and a Codex window
    // in any state (Codex never turns a missing reading into 0%, and an older
    // server sends no windows). A Claude window that the plan has and that
    // has no reading is "no active window", which is 0% used, so it keeps its
    // bar.
    const value = acct[fields.value];
    const unreported = (win && win.state === 'unknown') || acct.provider === 'codex';
    if (unreported && (value === null || value === undefined) && Number(acct.usage_cached_at) > 0) {
        const provider = acct.provider === 'codex' ? 'Codex' : 'Anthropic';
        return {
            text: 'not reported', short: 'not reported', tone: 'muted', muted: false, absent: true,
            hint: provider + ' does not report this limit for this account.',
        };
    }
    const resetsAt = acct[fields.reset];
    const now = (nowMs === undefined || nowMs === null) ? Date.now() : nowMs;
    const resetMs = resetsAt ? new Date(resetsAt).getTime() : NaN;
    if (Number.isFinite(resetMs) && resetMs > now) return null;

    const reason = (text, tone, muted, short) => ({
        text, short: short || text, tone: tone || 'muted', muted: !!muted,
    });
    const ks = acct.keeper_state || null;
    // A dead token also stops usage reads, so the actionable state wins
    // over "last read"; the re-auth control on the card is the fix.
    if (ks && ks.last_status === 'needs_reauth') return reason('needs re-auth', 'warn');

    // A reading older than the stale threshold dims the bar. The keeper never
    // pings a Codex account, so a lapsed Codex window has only its last read.
    const cachedAt = Number(acct.usage_cached_at) || 0;
    const stale = cachedAt > 0 && (now / 1000 - cachedAt) > USAGE_STALE_SECS;
    // The provider accepted the ping and a usage read has not shown the new
    // window yet (#238i): the window is opening. It is not a retry and not
    // a lapsed window, and it holds when the last reading is stale too (a
    // read-back that did not land leaves the reading old): the caption says
    // what the keeper knows, the stale dim stays.
    if (ks && (ks.last_status === 'ping_pending' || ks.skip_reason === 'confirming')) {
        return reason('window opening', 'muted', stale, 'opening');
    }
    if (cachedAt > 0 && (acct.provider === 'codex' || stale)) {
        const when = _keeperReadDate(cachedAt);
        return reason('last read ' + when, 'muted', stale, 'read ' + when);
    }

    if (!ks) return null;
    // Epoch seconds the server attached to the state (next retry, the next
    // active start): rendered in the viewer's own zone and locale.
    const next = Number(ks.next_attempt_at) || 0;
    const due = next > 0 && next * 1000 <= now;
    switch (ks.skip_reason) {
        case 'keeper_disabled': return reason('keeper off');
        case 'no_token': return reason('no token, re-auth', 'warn', false, 'no token');
        case 'outside_active_hours': {
            // The next active start rides its own field: next_attempt_at is
            // a retry deadline the keeper honors across restarts.
            const until = Number(ks.paused_until) || 0;
            return until && until * 1000 > now
                ? reason('paused until ' + _keeperWhen(until, now), 'muted', false, 'paused')
                : reason('paused', 'muted', false, 'paused');
        }
        case 'auto_swap_disabled': return reason('auto-swap off');
        default: break;
    }
    if (ks.last_status === 'rate_limited') {
        if (!next) return reason('rate limited', 'muted', false, 'rate limited');
        return due
            ? reason('limit lifting', 'muted', false, 'rate limited')
            : reason('limited until ' + _keeperWhen(next, now), 'muted', false, 'rate limited');
    }
    if (next && ['transport', 'auth_failed', 'error', 'ping_ok_no_window'].includes(ks.last_status)) {
        return due
            ? reason('retry due', 'muted', false, 'retry due')
            : reason('retry ' + _keeperWhen(next, now), 'muted', false, 'retrying');
    }
    if (ks.last_status === 'ok' || ks.last_status === 'ping_ok_no_window') {
        return reason('window lapsed', 'muted', false, 'lapsed');
    }
    return null;
}

/**
 * renderUsageBar opts for an account window: base plus the window reason,
 * plus the window's note as hint when the window is temporary (an Ultra
 * account that is not verified yet has a temporary 5-hour limit).
 * @param {object} acct - Account payload.
 * @param {'five_hour'|'seven_day'} windowKey - The window.
 * @param {object} [base] - Other renderUsageBar opts (compact).
 * @returns {object} renderUsageBar opts.
 */
function usageBarOpts(acct, windowKey, base) {
    const opts = Object.assign({}, base || {}, { reason: windowReason(acct, windowKey) });
    const win = _usageWindowInfo(acct, windowKey);
    if (win && win.temporary && win.note) opts.hint = String(win.note);
    return opts;
}

/**
 * Label and hover text for a provider-reported usage block (acct.usage_block),
 * shared by the dashboard badge and the panel marker. The server drops a block
 * whose reset time has passed. A reading older than the stale threshold adds
 * when it was read, because the verdict is only as new as that reading.
 * @param {object} acct - Account payload.
 * @param {number} [nowMs] - Current time in ms (tests).
 * @returns {{label: string, title: string}|null}
 */
function usageBlockText(acct, nowMs) {
    const block = acct && acct.usage_block;
    if (!block) return null;
    const label = block.reason === 'spend_limit' ? 'Spend limit reached'
        : block.reason === 'limit_reached' ? 'Limit reached' : 'Usage blocked';
    const provider = acct.provider === 'codex' ? 'Codex' : 'The provider';
    const reset = block.resets_at ? formatResetTime(block.resets_at) : '';
    let title = label + '. ' + provider + ' blocks new requests for this account.';
    if (/^resets\s+/i.test(reset)) title += ' The limit ' + reset + '.';
    const now = (nowMs === undefined || nowMs === null) ? Date.now() : nowMs;
    const readAt = Number(block.read_at) || 0;
    if (readAt > 0 && (now / 1000 - readAt) > USAGE_STALE_SECS) {
        title += ' Last usage read ' + _keeperReadDate(readAt) + '.';
    }
    return { label, title };
}

/**
 * The card badge for a provider-reported usage block (acct.usage_block), or ''.
 * @param {object} acct - Account payload.
 * @returns {string} HTML string.
 */
function renderUsageBlockBadge(acct) {
    const text = usageBlockText(acct);
    if (!text) return '';
    return `<span class="badge badge-danger" title="${escapeHtml(text.title)}">${escapeHtml(text.label)}</span>`;
}

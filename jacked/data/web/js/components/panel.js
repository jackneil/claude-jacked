/**
 * jacked — compact usage panel
 *
 * One component rendered into BOTH the menu-bar dropdown (NSPopover) and the
 * pinned side panel (NSPanel). Reuses the dashboard's bar component (compact
 * mode of renderUsageBar + .elapsed-marker, usage.js) and the shared grouping
 * util (groupAccountsByLogin, account-grouping.js) so the panel can never
 * diverge from the dashboard. Self-contained fetch — deliberately does NOT pull
 * the full dashboard app.js.
 *
 * Layout is tuned for a narrow (~360px) popover with many accounts: the EMAIL
 * is the primary identifier, single-org logins collapse to one identity line,
 * only multi-org logins get a header + connecting rail, each account shows how
 * fresh its numbers are, and the ACTIVE account shows a live countdown to its
 * next refresh (the backend's real poll schedule, from /api/menubar-summary).
 */

const PANEL_REFRESH_MS = 15000;

/** Minimal JSON fetch wrapper (panel is standalone; no app.js `api`). */
async function panelFetch(path) {
    const resp = await fetch(path, { headers: { Accept: 'application/json' } });
    if (!resp.ok) throw new Error('HTTP ' + resp.status + ' for ' + path);
    return resp.json();
}

/** Compact "Max 20x" style plan badge from subscription_type + rate_limit_tier. */
function planLabel(org) {
    const sub = (org.subscription_type || '').toString();
    const tierMatch = (org.rate_limit_tier || '').match(/(\d+)x/);
    if (!sub) return tierMatch ? tierMatch[1] + 'x' : '';
    const label = sub.charAt(0).toUpperCase() + sub.slice(1);
    return tierMatch ? label + ' ' + tierMatch[1] + 'x' : label;
}

/** Short data-freshness label from usage_cached_at (unix seconds): now/5m/2h/3d. */
function freshnessLabel(org) {
    const ts = org.usage_cached_at;
    if (!ts) return '';
    const secs = Math.floor(Date.now() / 1000) - ts;
    if (secs < 60) return 'now';
    if (secs < 3600) return Math.floor(secs / 60) + 'm';
    if (secs < 86400) return Math.floor(secs / 3600) + 'h';
    return Math.floor(secs / 86400) + 'd';
}

/** Countdown to the next scheduled refresh (unix seconds): "next 4m" / "next 45s"
 * / "refreshing…" once due. */
function formatCountdown(nextAt) {
    if (!nextAt) return '';
    const secs = Math.round(nextAt - Date.now() / 1000);
    if (secs <= 0) return 'refreshing…';
    if (secs < 60) return 'next ' + secs + 's';
    return 'next ' + Math.floor(secs / 60) + 'm';
}

/** The two compact usage bars (5h + 7d) for an account/org. */
function orgBarsHtml(org) {
    const elapsed5h = computeElapsedFraction5h(org.cached_5h_resets_at);
    const elapsed7d = computeElapsedFraction7d(org.cached_7d_resets_at);
    return (
        renderUsageBar(org.cached_usage_5h, org.cached_5h_resets_at, elapsed5h, '5h', { compact: true }) +
        renderUsageBar(org.cached_usage_7d, org.cached_7d_resets_at, elapsed7d, '7d', { compact: true })
    );
}

/** Trailing meta on an identity line: plan badge, active marker, freshness age,
 * and — for the active account — a live "next refresh" countdown. */
function orgMetaHtml(org, nextRefreshAt) {
    const plan = planLabel(org);
    const planHtml = plan ? `<span class="plan-badge">${escapeHtml(plan)}</span>` : '';
    const activeHtml = org.isActive
        ? '<span class="active-badge" title="Active in Claude Code">active</span>'
        : '';
    const age = freshnessLabel(org);
    const ageHtml = age
        ? `<span class="acct-age" title="Usage updated ${escapeHtml(age)} ago">${escapeHtml(age)}</span>`
        : '';
    let nextHtml = '';
    if (org.isActive && nextRefreshAt) {
        nextHtml =
            `<span class="next-refresh" data-next-at="${nextRefreshAt}"` +
            ` title="Next automatic usage refresh">${escapeHtml(formatCountdown(nextRefreshAt))}</span>`;
    }
    return `${planHtml}${activeHtml}<span class="meta-spacer"></span>${ageHtml}${nextHtml}`;
}

/** Single-org login → one compact identity line (email-primary) + bars. */
function buildSingleAccountHtml(group, nextRefreshAt) {
    const org = group.orgs[0];
    // Show a real org name inline; "Personal" is implied by a lone account, omit it.
    const orgTag =
        org.orgLabel && org.orgLabel !== 'Personal'
            ? `<span class="org-tag" title="${escapeHtml(org.orgLabel)}">${escapeHtml(org.orgLabel)}</span>`
            : '';
    const provider = org.provider || 'claude';
    return `
        <section class="acct provider-${provider}${org.isActive ? ' is-active' : ''}" data-account-id="${org.id}">
            <div class="acct-head">
                ${providerBadge(provider)}
                <span class="acct-email" title="${escapeHtml(group.email)}">${escapeHtml(group.email)}</span>
                ${orgTag}
                ${orgMetaHtml(org, nextRefreshAt)}
            </div>
            <div class="org-bars">${orgBarsHtml(org)}</div>
        </section>`;
}

/** Multi-org login → email header (+ "N orgs" chip + rail) and per-org rows. */
function buildMultiOrgLoginHtml(group, nextRefreshAt) {
    const rows = group.orgs
        .map(
            (org) => `
            <div class="org-row provider-${org.provider || 'claude'}${org.isActive ? ' is-active' : ''}" data-account-id="${org.id}">
                <div class="org-row-head">
                    ${providerBadge(org.provider || 'claude')}
                    <span class="org-name" title="${escapeHtml(org.orgLabel)}">${escapeHtml(org.orgLabel)}</span>
                    ${orgMetaHtml(org, nextRefreshAt)}
                </div>
                <div class="org-bars">${orgBarsHtml(org)}</div>
            </div>`
        )
        .join('');
    return `
        <section class="login-group has-rail${group.hasActive ? ' has-active' : ''}">
            <header class="login-header">
                <span class="login-name" title="${escapeHtml(group.email)}">${escapeHtml(group.email)}</span>
                <span class="org-chip">${group.orgCount} orgs</span>
            </header>
            <div class="org-rows">${rows}</div>
        </section>`;
}

/** One login group — collapsed when single-org, header+rail when multi-org. */
function buildLoginGroupHtml(group, nextRefreshAt) {
    return group.orgCount > 1
        ? buildMultiOrgLoginHtml(group, nextRefreshAt)
        : buildSingleAccountHtml(group, nextRefreshAt);
}

/** Full panel body from grouped accounts (empty state when none). nextRefreshAt
 * is the active account's next scheduled refresh (unix seconds) or null. */
function buildPanelHtml(groups, nextRefreshAt) {
    if (!groups || groups.length === 0) return panelEmptyHtml();
    return groups.map((g) => buildLoginGroupHtml(g, nextRefreshAt)).join('');
}

function panelEmptyHtml() {
    return `
        <div class="panel-empty">
            <div class="panel-empty-title">No accounts connected</div>
            <div class="panel-empty-sub">Add one from the dashboard to see usage here.</div>
        </div>`;
}

function panelErrorHtml(message) {
    return `
        <div class="panel-error">
            <div class="panel-error-title">Can't reach jacked</div>
            <div class="panel-error-sub">${escapeHtml(message || 'Service unavailable')}</div>
            <button id="panel-retry" class="panel-retry">Retry</button>
        </div>`;
}

/** Fetch accounts + the menu-bar summary (active id + next-refresh), group, render.
 * Multiple triggers can race (interval, native reload nudge, visibilitychange) —
 * a sequence counter drops any fetch that finished after a newer one started, so
 * a slow stale response never overwrites fresher data. */
let _loadSeq = 0;
async function loadPanel(container) {
    const seq = ++_loadSeq;
    try {
        const [accounts, summary] = await Promise.all([
            panelFetch('/api/auth/accounts'),
            panelFetch('/api/menubar-summary').catch(() => ({})),
        ]);
        if (seq !== _loadSeq) return; // superseded by a newer load
        const activeId =
            summary && summary.active_account_id != null ? summary.active_account_id : null;
        const nextRefreshAt =
            summary && summary.active && summary.active.next_refresh_at != null
                ? summary.active.next_refresh_at
                : null;
        const groups = groupAccountsByLogin(accounts, activeId);
        container.innerHTML = buildPanelHtml(groups, nextRefreshAt);
    } catch (err) {
        if (seq !== _loadSeq) return; // superseded — don't clobber a good render
        container.innerHTML = panelErrorHtml(err && err.message ? err.message : String(err));
        const retry = document.getElementById('panel-retry');
        if (retry) retry.addEventListener('click', () => loadPanel(container));
    }
}

/** Tick the "next refresh" countdown(s) every second so it rolls down smoothly
 * between the 15s data reloads. */
function startCountdownTicker() {
    setInterval(() => {
        const els = document.querySelectorAll('[data-next-at]');
        els.forEach((el) => {
            const at = parseFloat(el.getAttribute('data-next-at'));
            if (at) el.textContent = formatCountdown(at);
        });
    }, 1000);
}

/**
 * Wire the in-panel "⋯" button to open the native actions menu (the right-click
 * equivalent), for users who don't realize they can right-click the icon. It is
 * only revealed when the WKWebView→native bridge is present, so a plain browser
 * (QA / the dashboard) never shows a button that would do nothing.
 */
function setupMenuButton() {
    const btn = document.getElementById('panel-menu-btn');
    if (!btn) return;
    const bridge =
        typeof window !== 'undefined' &&
        window.webkit &&
        window.webkit.messageHandlers &&
        window.webkit.messageHandlers.jacked;
    if (!bridge) return; // not inside the native panel — leave it hidden
    btn.hidden = false;
    btn.addEventListener('click', () => {
        try {
            window.webkit.messageHandlers.jacked.postMessage('show-menu');
        } catch (e) {
            /* bridge went away — nothing to do */
        }
    });
}

// Anthropic complains if you refresh usage more than once a minute, so the
// manual "refresh all" button is rate-limited to one use per 60s — persisted in
// localStorage so closing/reopening the popover can't bypass it.
const REFRESH_COOLDOWN_S = 60;

function _lastManualRefreshAgo() {
    try {
        const t = parseFloat(localStorage.getItem('jacked_panel_last_refresh') || '0');
        return Date.now() / 1000 - t;
    } catch (e) {
        return Infinity;
    }
}

function startRefreshCooldown(btn, secs) {
    btn.disabled = true;
    let rem = Math.ceil(secs);
    const tick = () => {
        if (rem <= 0) {
            btn.disabled = false;
            btn.title = 'Refresh all accounts now';
            return;
        }
        btn.title = 'Refreshed — wait ' + rem + 's';
        rem -= 1;
        setTimeout(tick, 1000);
    };
    tick();
}

async function doManualRefresh(btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add('spinning');
    try {
        await fetch('/api/auth/accounts/refresh-all-usage?user_initiated=true', {
            method: 'POST',
            headers: { Accept: 'application/json' },
        });
    } catch (e) {
        /* the server self-throttles (and fetch_usage caps per account); ignore */
    }
    try {
        localStorage.setItem('jacked_panel_last_refresh', String(Date.now() / 1000));
    } catch (e) {
        /* private mode / no storage — the in-memory cooldown still applies */
    }
    btn.classList.remove('spinning');
    const c = document.getElementById('panel-root');
    if (c) loadPanel(c); // pull the freshly-refreshed numbers
    startRefreshCooldown(btn, REFRESH_COOLDOWN_S);
}

/** Wire the "⟳ refresh all now" button, honoring a persisted 60s cooldown. */
function setupRefreshButton() {
    const btn = document.getElementById('panel-refresh-btn');
    if (!btn) return;
    btn.addEventListener('click', () => doManualRefresh(btn));
    const ago = _lastManualRefreshAgo();
    if (ago < REFRESH_COOLDOWN_S) startRefreshCooldown(btn, REFRESH_COOLDOWN_S - ago);
}

function startPanel() {
    const container = document.getElementById('panel-root');
    if (!container) return;
    setupMenuButton();
    setupRefreshButton();
    loadPanel(container);
    setInterval(() => loadPanel(container), PANEL_REFRESH_MS);
    startCountdownTicker();
    // WKWebView suspends this page's timers while the popover/panel is
    // offscreen, so the interval alone goes stale. The native agent calls
    // __panelReload on show and whenever usage data changes in-process;
    // visibilitychange covers plain browser tabs regaining focus.
    window.__panelReload = () => loadPanel(container);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) loadPanel(container);
    });
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', startPanel);
}

// CommonJS export for the node test harness; no-op in the browser.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        buildPanelHtml,
        buildLoginGroupHtml,
        buildSingleAccountHtml,
        buildMultiOrgLoginHtml,
        orgBarsHtml,
        orgMetaHtml,
        planLabel,
        freshnessLabel,
        formatCountdown,
        panelEmptyHtml,
        panelErrorHtml,
        setupMenuButton,
        setupRefreshButton,
        doManualRefresh,
    };
}

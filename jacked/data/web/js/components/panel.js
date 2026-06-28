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
 * is the primary identifier (it's what distinguishes accounts; display names
 * collide), single-org logins collapse to one identity line, only multi-org
 * logins get a header + connecting rail, and each account shows how fresh its
 * numbers are.
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

/** The two compact usage bars (5h + 7d) for an account/org. */
function orgBarsHtml(org) {
    const elapsed5h = computeElapsedFraction5h(org.cached_5h_resets_at);
    const elapsed7d = computeElapsedFraction7d(org.cached_7d_resets_at);
    return (
        renderUsageBar(org.cached_usage_5h, org.cached_5h_resets_at, elapsed5h, '5h', { compact: true }) +
        renderUsageBar(org.cached_usage_7d, org.cached_7d_resets_at, elapsed7d, '7d', { compact: true })
    );
}

/** Trailing meta on an identity line: plan badge, active marker, freshness age. */
function orgMetaHtml(org) {
    const plan = planLabel(org);
    const planHtml = plan ? `<span class="plan-badge">${escapeHtml(plan)}</span>` : '';
    const activeHtml = org.isActive
        ? '<span class="active-badge" title="Active in Claude Code">active</span>'
        : '';
    const age = freshnessLabel(org);
    const ageHtml = age
        ? `<span class="acct-age" title="Usage updated ${escapeHtml(age)} ago">${escapeHtml(age)}</span>`
        : '';
    return `${planHtml}${activeHtml}<span class="meta-spacer"></span>${ageHtml}`;
}

/** Single-org login → one compact identity line (email-primary) + bars. */
function buildSingleAccountHtml(group) {
    const org = group.orgs[0];
    // Show a real org name inline; "Personal" is implied by a lone account, omit it.
    const orgTag =
        org.orgLabel && org.orgLabel !== 'Personal'
            ? `<span class="org-tag" title="${escapeHtml(org.orgLabel)}">${escapeHtml(org.orgLabel)}</span>`
            : '';
    return `
        <section class="acct${org.isActive ? ' is-active' : ''}" data-account-id="${org.id}">
            <div class="acct-head">
                <span class="acct-email" title="${escapeHtml(group.email)}">${escapeHtml(group.email)}</span>
                ${orgTag}
                ${orgMetaHtml(org)}
            </div>
            <div class="org-bars">${orgBarsHtml(org)}</div>
        </section>`;
}

/** Multi-org login → email header (+ "N orgs" chip + rail) and per-org rows. */
function buildMultiOrgLoginHtml(group) {
    const rows = group.orgs
        .map(
            (org) => `
            <div class="org-row${org.isActive ? ' is-active' : ''}" data-account-id="${org.id}">
                <div class="org-row-head">
                    <span class="org-name" title="${escapeHtml(org.orgLabel)}">${escapeHtml(org.orgLabel)}</span>
                    ${orgMetaHtml(org)}
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
function buildLoginGroupHtml(group) {
    return group.orgCount > 1 ? buildMultiOrgLoginHtml(group) : buildSingleAccountHtml(group);
}

/** Full panel body from grouped accounts (empty state when none). */
function buildPanelHtml(groups) {
    if (!groups || groups.length === 0) return panelEmptyHtml();
    return groups.map(buildLoginGroupHtml).join('');
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

/** Fetch accounts + active credential, group, and render into container. */
async function loadPanel(container) {
    try {
        const [accounts, active] = await Promise.all([
            panelFetch('/api/auth/accounts'),
            panelFetch('/api/auth/active-credential').catch(() => ({})),
        ]);
        const activeId = active && active.account_id != null ? active.account_id : null;
        const groups = groupAccountsByLogin(accounts, activeId);
        container.innerHTML = buildPanelHtml(groups);
    } catch (err) {
        container.innerHTML = panelErrorHtml(err && err.message ? err.message : String(err));
        const retry = document.getElementById('panel-retry');
        if (retry) retry.addEventListener('click', () => loadPanel(container));
    }
}

function startPanel() {
    const container = document.getElementById('panel-root');
    if (!container) return;
    loadPanel(container);
    setInterval(() => loadPanel(container), PANEL_REFRESH_MS);
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
        panelEmptyHtml,
        panelErrorHtml,
    };
}

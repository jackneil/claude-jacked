/**
 * jacked — compact usage panel
 *
 * One component rendered into BOTH the menu-bar dropdown (NSPopover) and the
 * pinned side panel (NSPanel). Reuses the dashboard's bar component verbatim
 * (renderUsageBar + .elapsed-marker, usage.js) and the shared grouping util
 * (groupAccountsByLogin, account-grouping.js) so the panel can never diverge
 * from the dashboard. Self-contained fetch — deliberately does NOT pull the
 * full dashboard app.js.
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

/** One org sub-row: name + plan + active marker + the two reused usage bars. */
function buildOrgRowHtml(org) {
    const elapsed5h = computeElapsedFraction5h(org.cached_5h_resets_at);
    const elapsed7d = computeElapsedFraction7d(org.cached_7d_resets_at);
    const bar5h = renderUsageBar(org.cached_usage_5h, org.cached_5h_resets_at, elapsed5h, '5h');
    const bar7d = renderUsageBar(org.cached_usage_7d, org.cached_7d_resets_at, elapsed7d, '7d');
    const plan = planLabel(org);
    const planHtml = plan ? `<span class="plan-badge">${escapeHtml(plan)}</span>` : '';
    const activeHtml = org.isActive
        ? '<span class="active-badge" title="Active in Claude Code">active</span>'
        : '';
    return `
        <div class="org-row${org.isActive ? ' is-active' : ''}" data-account-id="${org.id}">
            <div class="org-row-head">
                <span class="org-name" title="${escapeHtml(org.orgLabel)}">${escapeHtml(org.orgLabel)}</span>
                ${planHtml}
                ${activeHtml}
            </div>
            <div class="org-bars">
                ${bar5h}
                ${bar7d}
            </div>
        </div>`;
}

/** One login group: header (+ "N orgs" chip + rail when multi-org) and its org rows. */
function buildLoginGroupHtml(group) {
    const orgChip =
        group.orgCount > 1 ? `<span class="org-chip">${group.orgCount} orgs</span>` : '';
    const title = group.displayName || group.email;
    const subEmail =
        group.displayName ? `<div class="login-email">${escapeHtml(group.email)}</div>` : '';
    const orgsHtml = group.orgs.map(buildOrgRowHtml).join('');
    const multi = group.orgCount > 1 ? ' has-rail' : '';
    return `
        <section class="login-group${multi}${group.hasActive ? ' has-active' : ''}">
            <header class="login-header">
                <span class="login-name" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
                ${orgChip}
            </header>
            ${subEmail}
            <div class="org-rows">${orgsHtml}</div>
        </section>`;
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
        buildOrgRowHtml,
        planLabel,
        panelEmptyHtml,
        panelErrorHtml,
    };
}

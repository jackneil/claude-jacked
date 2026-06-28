/**
 * jacked — account grouping util
 *
 * Single source of truth for "same login, different org" grouping. Several
 * Claude accounts can share one email (one login) while differing by
 * organization (personal vs. one or more workspaces). The dashboard, the
 * menu-bar dropdown, and the pinned side panel must all group them the same
 * way, so that logic lives here and nowhere else.
 *
 * Pure, DOM-free, and dependency-free so it can be unit-tested under node and
 * reused verbatim by every surface. Loaded as a plain browser script (exposes
 * globals) and, when present, also exported for CommonJS test harnesses.
 */

/**
 * Human-readable label for an account's organization.
 * Empty/missing org (the "" personal sentinel from the API) → "Personal".
 * @param {object} acct - Account row (AccountResponse shape).
 * @returns {string}
 */
function orgLabel(acct) {
    const name = acct.organization_name;
    // Anthropic auto-names a personal org "<email>'s Organization" — that's noise
    // (it just restates the email and overflows narrow UIs), so collapse it to
    // "Personal". Only a real, user-meaningful org name is shown verbatim.
    if (name && !/'s Organization$/i.test(name.trim())) return name;
    if (!name && acct.organization_uuid) return acct.organization_uuid.slice(0, 8) + '…';
    return 'Personal';
}

/**
 * Group accounts by login (email), collapsing same-email/different-org
 * accounts under one header with per-org sub-rows.
 *
 * @param {Array<object>} accounts - Account rows (AccountResponse shape).
 * @param {number|null} activeAccountId - id of the account currently active in
 *   Claude Code, used to mark the active org. May be null/undefined.
 * @returns {Array<object>} Login groups, each:
 *   {
 *     email,         // the login email (original case preserved)
 *     displayName,   // a custom label if any org carries one, else ''
 *     orgCount,      // number of orgs under this login
 *     hasActive,     // true if the active account is in this group
 *     bestPriority,  // lowest priority among the group's orgs (for sorting)
 *     orgs: [        // one entry per org, sorted by priority then label
 *       { ...acct, orgLabel, isActive }
 *     ]
 *   }
 *   Groups are sorted by best (lowest) priority, then email.
 */
function groupAccountsByLogin(accounts, activeAccountId) {
    const list = Array.isArray(accounts) ? accounts.filter((a) => a && !a.is_deleted) : [];
    const groups = new Map(); // key: normalized email (or per-id fallback)

    for (const acct of list) {
        const email = (acct.email || '').trim();
        // Blank emails must NOT all collapse into one phantom login — fall back
        // to a per-id key so each shows as its own group.
        const key = email ? email.toLowerCase() : '__noemail_' + acct.id;
        let grp = groups.get(key);
        if (!grp) {
            grp = { email: email || 'Unknown', displayName: '', hasActive: false, orgs: [] };
            groups.set(key, grp);
        }
        const isActive = activeAccountId != null && acct.id === activeAccountId;
        if (isActive) grp.hasActive = true;
        grp.orgs.push(Object.assign({}, acct, { orgLabel: orgLabel(acct), isActive }));
    }

    const result = [];
    for (const grp of groups.values()) {
        grp.orgs.sort(
            (a, b) =>
                Number(b.isActive) - Number(a.isActive) ||  // active org first within its login
                (a.priority || 0) - (b.priority || 0) ||
                a.orgLabel.localeCompare(b.orgLabel)
        );
        grp.orgCount = grp.orgs.length;
        grp.bestPriority = grp.orgs.reduce(
            (min, o) => Math.min(min, o.priority || 0),
            Number.POSITIVE_INFINITY
        );
        const named = grp.orgs.find((o) => o.display_name && o.display_name !== o.email);
        grp.displayName = named ? named.display_name : '';
        result.push(grp);
    }

    // The active account's login is always pinned to the TOP of the dropdown,
    // then the rest by priority, then email.
    result.sort(
        (a, b) =>
            Number(b.hasActive) - Number(a.hasActive) ||
            a.bestPriority - b.bestPriority ||
            a.email.localeCompare(b.email)
    );
    return result;
}

// CommonJS export for the node test harness; harmless no-op in the browser
// where `module` is undefined.
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { groupAccountsByLogin, orgLabel };
}

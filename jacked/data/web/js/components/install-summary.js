/**
 * jacked web dashboard — one-shot "what changed in this update" panel.
 *
 * On app init (and after a post-upgrade reload, which re-runs init), fetches
 * GET /api/install/summary. If a fresh install/upgrade record exists with any
 * added/changed/removed artifacts and it hasn't been seen before
 * (localStorage 'jacked:lastInstallSeen' tracks the record's `at` marker),
 * renders a dismissible slate/blue panel listing the changes. Shows once.
 *
 * Record shape (from jacked/install_summary.py via /api/install/summary):
 *   { at, from_version, to_version,
 *     changes: { skills|commands|agents|lenses|templates:
 *                  { added:[], changed:[], removed:[] } },
 *     unchanged_count }
 */

const INSTALL_SUMMARY_SEEN_KEY = 'jacked:lastInstallSeen';

// Category key -> display label. Mirrors jacked/install_summary.py _LABELS.
const _INSTALL_SUMMARY_CATEGORIES = [
    ['skills', 'Skills'],
    ['commands', 'Commands'],
    ['agents', 'Agents'],
    ['lenses', 'Lenses'],
    ['templates', 'Templates'],
];

// Change kind -> { badge class, label }. green/yellow/red via existing badges.
const _INSTALL_SUMMARY_KINDS = {
    added: { cls: 'badge-success', word: 'new' },
    changed: { cls: 'badge-warning', word: 'updated' },
    removed: { cls: 'badge-danger', word: 'removed' },
};

function _installSummaryHasChanges(summary) {
    const changes = (summary && summary.changes) || {};
    return Object.values(changes).some(
        (ch) => ch && (
            (ch.added && ch.added.length) ||
            (ch.changed && ch.changed.length) ||
            (ch.removed && ch.removed.length)
        ),
    );
}

function _installSummaryTitle(summary) {
    const to = summary.to_version || '';
    if (summary.from_version == null) {
        return `Installed — ${to}`;
    }
    return `Updated ${summary.from_version} → ${to}`;
}

// Build one row: a colored badge + "category: name".
function _installSummaryRow(label, name, kind) {
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 text-sm';

    const meta = _INSTALL_SUMMARY_KINDS[kind] || _INSTALL_SUMMARY_KINDS.added;
    const badge = document.createElement('span');
    badge.className = `badge ${meta.cls}`;
    badge.textContent = meta.word;
    row.appendChild(badge);

    const text = document.createElement('span');
    text.className = 'text-slate-300';
    text.textContent = `${label}: ${name}`;
    row.appendChild(text);

    return row;
}

function renderInstallSummaryPanel(summary) {
    // Remove any prior panel (idempotent if init runs twice).
    const existing = document.getElementById('install-summary-panel');
    if (existing) existing.remove();

    const panel = document.createElement('div');
    panel.id = 'install-summary-panel';
    // Fixed banner over the content area. <body> is display:flex with a fixed
    // w-56 sidebar; #content offsets itself by md:ml-56. We mirror that offset
    // (left-0 mobile / md:left-56) and pin top/right so the panel floats above
    // the content without becoming an in-flow flex column. z-50 keeps it over
    // content but below the upgrade modal (z-9999). Cap height + scroll so a
    // long change list never runs off-screen.
    panel.className =
        'stat-card fixed top-2 md:top-4 right-2 md:right-4 left-2 md:left-56 z-50 '
        + 'max-w-none md:max-w-md md:ml-4 max-h-[80vh] overflow-y-auto shadow-xl';
    panel.style.borderColor = '#3b82f6';

    // Header: title + dismiss button.
    const head = document.createElement('div');
    head.className = 'flex items-start justify-between gap-3 mb-3';

    const titleWrap = document.createElement('div');
    const title = document.createElement('div');
    title.className = 'text-base font-semibold text-white';
    title.textContent = _installSummaryTitle(summary);
    titleWrap.appendChild(title);

    const sub = document.createElement('div');
    sub.className = 'text-xs text-slate-400 mt-0.5';
    sub.textContent = 'What changed in this update';
    titleWrap.appendChild(sub);
    head.appendChild(titleWrap);

    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'text-slate-400 hover:text-white text-lg leading-none shrink-0';
    dismiss.setAttribute('aria-label', 'Dismiss');
    dismiss.textContent = '×';
    dismiss.addEventListener('click', () => panel.remove());
    head.appendChild(dismiss);

    panel.appendChild(head);

    // Body: one row per changed artifact.
    const body = document.createElement('div');
    body.className = 'flex flex-col gap-1.5';
    for (const [catKey, label] of _INSTALL_SUMMARY_CATEGORIES) {
        const ch = (summary.changes && summary.changes[catKey]) || {};
        for (const kind of ['added', 'changed', 'removed']) {
            for (const name of (ch[kind] || [])) {
                body.appendChild(_installSummaryRow(label, name, kind));
            }
        }
    }
    panel.appendChild(body);

    // Footer: unchanged count.
    const unchanged = Number(summary.unchanged_count) || 0;
    const foot = document.createElement('div');
    foot.className = 'text-xs text-slate-500 mt-3';
    foot.textContent = `${unchanged} unchanged`;
    panel.appendChild(foot);

    // Mount on <body>, OUTSIDE #content, so the router's #content.innerHTML
    // overwrite on every route change can't wipe it. #content IS the <main>
    // flex child; inserting as its sibling would make the panel a second
    // in-flow column under the fixed sidebar (hidden/broken), so we attach to
    // <body> and rely on the panel's own `position: fixed` (see className) to
    // float it over the content area, offset for the sidebar.
    document.body.appendChild(panel);
}

async function loadInstallSummary() {
    let data;
    try {
        data = await api.get('/api/install/summary');
    } catch (e) {
        console.error('Failed to load install summary:', e);
        return;
    }

    const summary = data && data.summary;
    if (!summary || !summary.at) return;
    if (localStorage.getItem(INSTALL_SUMMARY_SEEN_KEY) === summary.at) return;
    if (!_installSummaryHasChanges(summary)) return;

    // Mark seen first so the panel shows exactly once (reload -> gone).
    localStorage.setItem(INSTALL_SUMMARY_SEEN_KEY, summary.at);
    renderInstallSummaryPanel(summary);
}

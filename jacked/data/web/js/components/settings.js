/**
 * jacked web dashboard — settings component
 * Tabbed layout: Agents | Commands | Features | Plugins | Claude Code | Advanced
 */

const SETTINGS_TAB_KEY = 'jacked_settings_tab';
const DEFAULT_TAB = 'agents';

// Color theme for account-usage bars + labels ('america250' | 'classic';
// unset means 'america250', the red/white/blue semiquincentennial scheme).
//
// The SERVER is the source of truth: the value lives in the jacked settings
// table under 'color_theme' (GET /api/settings, PUT /api/settings/color_theme).
// localStorage is only a per-webview PRE-PAINT CACHE so the <head> snippet in
// index.html/panel.html can set the html class before first paint without
// waiting on a fetch.
//
// This split is not optional. The dashboard runs in the user's browser, while
// the menu-bar dropdown and side panel are WKWebViews created inside the jacked
// Python process (service/menubar_mac.py) that load /panel. A WKWebView has its
// OWN localStorage store, completely separate from Chrome's — a theme written
// only to localStorage in the browser is invisible to the tray forever. The
// server is the one store both surfaces can read, so it arbitrates; each
// surface then refreshes its own cache for the next pre-paint.
const COLOR_THEME_KEY = 'jacked_color_theme';   // localStorage pre-paint cache
const COLOR_THEME_SETTING = 'color_theme';      // server settings key (authority)
const DEFAULT_COLOR_THEME = 'america250';

// --- Main render ---

function _resolveSettingsTab() {
    let tab = localStorage.getItem(SETTINGS_TAB_KEY) || DEFAULT_TAB;
    // Gatekeeper + Profiles tabs were removed in 0.70.0 — migrate stale choices.
    if (tab === 'gatekeeper' || tab === 'profiles') {
        tab = DEFAULT_TAB;
        localStorage.setItem(SETTINGS_TAB_KEY, tab);
    }
    return tab;
}

// --- Color theme (server-authoritative, localStorage = pre-paint cache) ---

/** Coerce anything to a known theme name. Only 'classic' opts out of the default. */
function _normalizeColorTheme(value) {
    return String(value == null ? '' : value) === 'classic' ? 'classic' : DEFAULT_COLOR_THEME;
}

/** The cached (pre-paint) theme for THIS webview, or null when never cached. */
function _cachedColorTheme() {
    try {
        const raw = localStorage.getItem(COLOR_THEME_KEY);
        return raw === null ? null : _normalizeColorTheme(raw);
    } catch (e) {
        return null;   // private mode / storage disabled
    }
}

/**
 * Pull 'color_theme' out of a GET /api/settings payload.
 * Accepts the raw list of {key, value} rows or a plain key→value map.
 * Returns null when the server has no opinion yet — that null is what drives
 * the one-time migration of an existing localStorage-only choice.
 */
function colorThemeFromSettings(settings) {
    if (!settings) return null;
    let raw;
    if (Array.isArray(settings)) {
        const row = settings.find(r => r && r.key === COLOR_THEME_SETTING);
        raw = row ? row.value : undefined;
    } else if (typeof settings === 'object') {
        raw = settings[COLOR_THEME_SETTING];
    }
    if (raw === undefined || raw === null) return null;
    // GET /api/settings JSON-decodes values, but a row written by an older
    // client can still arrive as a quoted string — tolerate both.
    const value = String(raw).replace(/^"(.*)"$/, '$1');
    return value === 'classic' || value === 'america250' ? value : null;
}

/**
 * The theme that should be applied: the server value when it has one, else this
 * webview's cache, else the default. Pure — the reconcile paths call it.
 */
function resolveColorTheme(settings, cached) {
    const remote = colorThemeFromSettings(settings);
    if (remote) return remote;
    return cached ? _normalizeColorTheme(cached) : DEFAULT_COLOR_THEME;
}

/** The theme currently painted on this page (the html class is the live truth). */
function _appliedColorTheme() {
    try {
        if (document.documentElement.classList.contains('theme-america250')) return 'america250';
        return 'classic';
    } catch (e) {
        return _cachedColorTheme() || DEFAULT_COLOR_THEME;
    }
}

/** Paint a theme and refresh this webview's pre-paint cache. Returns the theme. */
function applyColorTheme(value) {
    const theme = _normalizeColorTheme(value);
    document.documentElement.classList.toggle('theme-america250', theme !== 'classic');
    try {
        localStorage.setItem(COLOR_THEME_KEY, theme);
    } catch (e) {
        /* no storage — the next pre-paint just falls back to the default */
    }
    return theme;
}

/** Write the theme to the server so the tray WKWebView can read it. */
async function persistColorTheme(theme) {
    await api.put(`/api/settings/${encodeURIComponent(COLOR_THEME_SETTING)}`, { value: theme });
}

/**
 * Reconcile this page against the server exactly once per page load.
 *
 * - Server has a value → paint it and refresh the cache (the pre-paint class may
 *   have been wrong if the choice was made in another browser/webview).
 * - Server has none but this device does → push the local choice up ONCE, so an
 *   existing user's theme reaches the tray without re-clicking it.
 *
 * The one-shot flag is set BEFORE any await, so a repeated call (poll, re-render,
 * route change) can never loop or re-issue the migration PUT.
 */
let _colorThemeSynced = false;

// Set the instant the user picks a theme in the Appearance tab, BEFORE the paint.
// The reconcile GET can take seconds (busy DB; the api client allows up to 60s),
// and a pick made while it is in flight must not be silently reverted by a value
// that was already stale when it was read: the click's own PUT is what the server
// ends up holding, so applying the older remote value would leave the painted
// class, the localStorage cache, the picker's Active badge and the server all
// disagreeing until a reload. The user's choice always wins.
let _colorThemeUserPicked = false;

/**
 * Rebuild the surfaces that bake theme classes into their HTML.
 *
 * Flipping the html class restyles everything CSS drives (the usage bars) at
 * once, but the percent LABELS are Tailwind classes chosen in JS at render time
 * (usageTextClass in js/components/usage.js), so they keep the old palette until
 * something re-renders — up to a full poll cycle. The tray panel already forces
 * this; the dashboard must too.
 *
 * Every call is guarded: settings.js is loaded standalone by the node test
 * harness, where neither the accounts view nor a settings container exists.
 */
function _repaintThemedSurfaces() {
    try {
        // Only when the accounts view is really on screen. The reconcile resolves
        // on DOMContentLoaded, possibly BEFORE app.js has finished its own first
        // render — rendering an empty accounts list there would just be a flash,
        // and that first render already picks up the theme we just painted.
        if (typeof rerenderAccountsView === 'function'
                && document.getElementById('accounts-list')) {
            rerenderAccountsView();
        }
    } catch (e) {
        /* the accounts view is not mounted (or mid-navigation) — nothing to repaint */
    }
    try {
        if (typeof renderAppearanceTab !== 'function') return;
        // Only when Appearance is the tab actually showing: its ring + Active
        // badge are rendered from the painted theme, so a reconcile that changed
        // the theme leaves them marking the wrong option.
        if (_resolveSettingsTab() !== 'appearance') return;
        const container = document.getElementById('settings-tab-content');
        if (container) renderAppearanceTab(container);
    } catch (e) {
        /* no settings container on this page — nothing to repaint */
    }
}

async function syncColorThemeFromServer() {
    if (_colorThemeSynced) return null;
    _colorThemeSynced = true;

    let settings;
    try {
        settings = await api.get('/api/settings');
    } catch (e) {
        return null;   // server unreachable — the cached pre-paint class stands
    }

    const remote = colorThemeFromSettings(settings);
    if (remote) {
        // A pick made while the GET was in flight is newer than this response.
        if (_colorThemeUserPicked) return null;
        const before = _appliedColorTheme();
        const applied = applyColorTheme(remote);
        // Only when the reconcile actually CHANGED the painted theme: re-rendering
        // the accounts view on every page load for a value that already matches is
        // pointless churn (and would fight the user's scroll/expansion state).
        if (applied !== before) _repaintThemedSurfaces();
        return applied;
    }

    const cached = _cachedColorTheme();
    if (!cached) return null;   // nobody has an opinion — leave the default alone
    try {
        await persistColorTheme(cached);
    } catch (e) {
        /* migration is best-effort; the next page load retries it */
    }
    return cached;
}

if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('DOMContentLoaded', () => { syncColorThemeFromServer(); });
}

function renderSettings(settings) {
    const savedTab = _resolveSettingsTab();

    return `
        <div class="max-w-4xl">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white text-balance">Settings</h2>
            </div>

            <!-- Tab Bar -->
            <div class="flex gap-1 border-b border-slate-700 mb-6 overflow-x-auto">
                <button class="settings-tab ${savedTab === 'agents' ? 'active' : ''}" data-tab="agents">Agents</button>
                <button class="settings-tab ${savedTab === 'commands' ? 'active' : ''}" data-tab="commands">Commands</button>
                <button class="settings-tab ${savedTab === 'features' ? 'active' : ''}" data-tab="features">Features</button>
                <button class="settings-tab ${savedTab === 'plugins' ? 'active' : ''}" data-tab="plugins">Plugins</button>
                <button class="settings-tab ${savedTab === 'claude-code' ? 'active' : ''}" data-tab="claude-code">Claude Code</button>
                <button class="settings-tab ${savedTab === 'appearance' ? 'active' : ''}" data-tab="appearance">Appearance</button>
                <button class="settings-tab ${savedTab === 'advanced' ? 'active' : ''}" data-tab="advanced">Advanced</button>
            </div>

            <!-- Tab Content -->
            <div id="settings-tab-content">
                <div class="flex items-center justify-center py-12">
                    <div class="spinner"></div>
                    <span class="ml-3 text-slate-400 text-sm">Loading...</span>
                </div>
            </div>
        </div>
    `;
}

// --- Tab switching ---

function bindSettingsEvents() {
    const tabs = document.querySelectorAll('.settings-tab');
    tabs.forEach(tab => {
        tab.addEventListener('click', async () => {
            // Guard against losing unsaved changes when switching sub-tabs
            if (window._settingsDirty) {
                const result = await Swal.fire({
                    title: 'Unsaved Changes',
                    text: 'You have unsaved settings changes. Leave without saving?',
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonText: 'Leave',
                    cancelButtonText: 'Stay',
                    focusCancel: true,
                });
                if (!result.isConfirmed) return;
                window._settingsDirty = false;
            }
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const tabName = tab.dataset.tab;
            localStorage.setItem(SETTINGS_TAB_KEY, tabName);
            renderSettingsTab(tabName);
        });
    });

    // Render initial tab
    renderSettingsTab(_resolveSettingsTab());
}

async function renderSettingsTab(tabName) {
    const container = document.getElementById('settings-tab-content');
    if (!container) return;

    switch (tabName) {
        case 'agents':
            await renderAgentsTab(container);
            break;
        case 'commands':
            await renderCommandsTab(container);
            break;
        case 'features':
            await renderFeaturesTab(container);
            break;
        case 'plugins':
            await renderPluginsTab(container);
            break;
        case 'claude-code':
            await renderClaudeCodeTab(container);
            break;
        case 'appearance':
            renderAppearanceTab(container);
            break;
        case 'advanced':
            renderAdvancedTab(container);
            break;
        default:
            container.innerHTML = '<div class="text-slate-500">Unknown tab</div>';
    }
}

// --- Feature data loading ---

async function loadFeatures() {
    if (!window.jackedState.features) {
        window.jackedState.features = await api.get('/api/features');
    }
    return window.jackedState.features;
}

async function refreshFeatures() {
    window.jackedState.features = null;
    return await loadFeatures();
}

// --- Skill packs data loading ---

// Enabling/disabling a pack runs `npx skills add/remove`, which typically takes
// 10-60s (longer for big packs). The default 60s api timeout would abort a
// legitimate slow install; wait comfortably past the server's own 600s
// subprocess cap so the browser never gives up before the server reports back.
const PACK_TOGGLE_TIMEOUT_MS = 660000;

// Packs whose install/remove PUT is currently running, keyed by pack name to
// 'enable' | 'disable'. This is MODULE-level (not per-render) on purpose: a
// pack op takes 10-60s, and any Features-tab re-render that lands mid-op (a
// sibling feature toggle, a background refresh) rebuilds the section from
// scratch. Without a durable record, that re-render resurrects a live-looking
// toggle and erases the spinner. _renderPacksSection and _bindPackToggleEvents
// both consult this so an in-flight row stays disabled + "Installing skills..."
// across re-renders until the op's own finally clears it.
const _packsInFlight = new Map();

async function loadPacks() {
    if (!window.jackedState.packs) {
        window.jackedState.packs = await api.get('/api/packs');
    }
    return window.jackedState.packs;
}

async function refreshPacks() {
    window.jackedState.packs = null;
    return await loadPacks();
}

// --- DCR review engine data loading ---

// Effort levels the Codex CLI accepts, weakest to strongest.
const DCR_EFFORT_LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];

// A PUT is running: the card renders its controls disabled and refuses a second
// concurrent write. Module-level (not per-render) so a re-render landing mid-save
// cannot resurrect live-looking controls.
let _dcrEngineSaving = false;
// Message from the last failed PUT, shown inline in the card with a Retry link.
let _dcrEngineSaveError = null;
// Whether that failure is worth re-firing. A 4xx validation rejection can only
// fail the identical way a second time, so it renders without a Retry link.
let _dcrEngineSaveRetryable = true;
// Body of the last PUT, re-fired by that Retry link.
let _dcrEngineLastPayload = null;
// A manual "Check again" GET is running: a second click is a no-op until it lands.
let _dcrEngineRechecking = false;

// Codex readiness (CLI installed? signed in?) is LIVE external state, not static
// config, so this never caches: a user who runs `codex login` in a terminal and
// comes back to the Features tab must see the new state without a full reload.
// The fetched value is still published to jackedState so the rest of the card
// (payload defaults, the in-place re-render) reads one shared latest copy.
async function loadDcrEngine() {
    const data = await api.get('/api/dcr-engine');
    window.jackedState.dcrEngine = data;
    return data;
}

// --- Claude Code settings data loading ---

async function loadClaudeSettings() {
    if (!window.jackedState.claudeSettings) {
        window.jackedState.claudeSettings = await api.get('/api/claude-settings');
    }
    return window.jackedState.claudeSettings;
}

async function refreshClaudeSettings() {
    window.jackedState.claudeSettings = null;
    return await loadClaudeSettings();
}

// --- Toggle helper ---

function renderToggle(name, category, checked, sourceAvailable) {
    if (!sourceAvailable) {
        return `<span class="text-xs text-yellow-400">Source missing</span>`;
    }
    return `
        <label class="toggle-switch" data-name="${escapeHtml(name)}" data-category="${escapeHtml(category)}">
            <input type="checkbox" ${checked ? 'checked' : ''}>
            <span class="toggle-slider"></span>
        </label>
    `;
}

function bindToggleEvents(container) {
    // Scope to feature toggles (agents/commands/hooks/knowledge), which carry
    // data-category. Skill-pack toggles share the .toggle-switch styling but
    // have no data-category and are bound separately in _bindPackToggleEvents —
    // this selector keeps the two handlers from colliding.
    container.querySelectorAll('.toggle-switch[data-category]').forEach(toggle => {
        const input = toggle.querySelector('input');
        if (!input) return;
        input.addEventListener('change', async () => {
            const name = toggle.dataset.name;
            const category = toggle.dataset.category;
            const enabled = input.checked;

            toggle.classList.add('pending');
            input.disabled = true;

            try {
                const res = await api.put(`/api/features/${encodeURIComponent(category)}/${encodeURIComponent(name)}`, { enabled });
                const displayName = toggle.closest('[data-feature-row]')?.dataset.displayName || name;
                showToast(`${displayName} ${enabled ? 'enabled' : 'disabled'}`, 'success');
                // Memory Vault enable can surface legacy .remember dirs still
                // importable into the vault; nudge the user toward the migrate CLI.
                if (enabled && name === 'memory_vault' && res && res.migration_available > 0) {
                    const n = res.migration_available;
                    showToast(`${n} legacy .remember dir(s) found; run jacked memory migrate to import`, 'info');
                }
                // Some mapped repos may have refused the post-merge git hook (a
                // foreign hook, a husky/pre-commit framework, no .git). Surface the
                // count so the skip isn't silent; details live in memory status.
                if (enabled && name === 'memory_vault' && res && res.git_hooks) {
                    const skipped = Object.values(res.git_hooks).filter(g => g && g.skipped).length;
                    if (skipped > 0) {
                        showToast(`${skipped} repo(s) skipped post-merge hook install; run jacked memory status for details`, 'warning');
                    }
                }
                // Statusline enable can replace a pre-existing statusline; the
                // engine saves it, and disable restores it. Say so out loud.
                if (name === 'statusline' && res && res.took_over_foreign) {
                    showToast('Your previous statusline was saved. Disable to restore it.', 'info');
                }
                if (name === 'statusline' && res && res.restored_previous) {
                    showToast('Your previous statusline is back.', 'info');
                }
                await refreshFeatures();
                // Re-render the current tab to reflect changes
                const activeTab = localStorage.getItem(SETTINGS_TAB_KEY) || DEFAULT_TAB;
                await renderSettingsTab(activeTab);
            } catch (e) {
                // Revert toggle
                input.checked = !enabled;
                showToast(e.message || 'Toggle failed', 'error');
            } finally {
                toggle.classList.remove('pending');
                input.disabled = false;
            }
        });
    });
}

// --- Tab: Agents ---

async function renderAgentsTab(container) {
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading agents...</span>
        </div>
    `;

    try {
        const features = await loadFeatures();
        const agents = features.agents || [];

        if (agents.length === 0) {
            container.innerHTML = `<div class="text-center py-12 text-slate-500 text-sm">No agents available.</div>`;
            return;
        }

        const cardsHtml = agents.map(a => {
            const modelBadge = a.model && a.model !== 'haiku'
                ? `<span class="badge badge-info ml-2">${escapeHtml(a.model)}</span>`
                : '';
            return `
                <div class="feature-card ${a.installed ? '' : 'disabled'}" data-feature-row data-display-name="${escapeHtml(a.display_name || a.name)}">
                    <div class="flex items-start justify-between gap-3">
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center">
                                <span class="text-sm font-medium text-white truncate">${escapeHtml(a.display_name || a.name)}</span>
                                ${modelBadge}
                            </div>
                            <p class="text-xs text-slate-400 mt-1 line-clamp-2">${escapeHtml(a.description || '')}</p>
                        </div>
                        ${renderToggle(a.name, 'agents', a.installed, a.source_available)}
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = `
            <p class="text-xs text-slate-500 mb-4 text-pretty">Specialized agents installed to <code class="text-slate-300">~/.claude/agents/</code>. Toggle to enable or disable individual agents.</p>
            ${_renderFeatureFilter('Filter agents...')}
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        bindToggleEvents(container);
        _bindFeatureFilter(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load agents: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('agents')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
    }
}

// --- Tab: Commands ---

async function renderCommandsTab(container) {
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading commands...</span>
        </div>
    `;

    try {
        const features = await loadFeatures();
        const commands = features.commands || [];

        if (commands.length === 0) {
            container.innerHTML = `<div class="text-center py-12 text-slate-500 text-sm">No commands available.</div>`;
            return;
        }

        const cardsHtml = commands.map(c => `
            <div class="feature-card ${c.installed ? '' : 'disabled'}" data-feature-row data-display-name="${escapeHtml(c.display_name || c.name)}">
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0 flex-1">
                        <span class="text-sm font-medium text-white font-mono">${escapeHtml(c.display_name || c.name)}</span>
                        <p class="text-xs text-slate-400 mt-1 line-clamp-2">${escapeHtml(c.description || '')}</p>
                    </div>
                    ${renderToggle(c.name, 'commands', c.installed, c.source_available)}
                </div>
            </div>
        `).join('');

        container.innerHTML = `
            <p class="text-xs text-slate-500 mb-4 text-pretty">Slash commands installed to <code class="text-slate-300">~/.claude/commands/</code>. Use these with <code class="text-slate-300">/command-name</code> in Claude Code. Skills are slash-invocable too and live under Features.</p>
            ${_renderFeatureFilter('Filter commands...')}
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        bindToggleEvents(container);
        _bindFeatureFilter(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load commands: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('commands')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
    }
}

// --- Shared: filter over feature rows ---
//
// Agents (10), Commands (20) and the knowledge/skills lists (30) are long
// enough that finding one item meant scrolling the whole tab. Every row in
// every tab already carries `data-feature-row`, so one filter works across all
// of them without touching the row markup.
//
// Matching is on the row's rendered textContent, which covers the display name
// AND the description, so "browser" finds the QA skill without needing to know
// its name. Sections tagged `data-feature-section` collapse when every row
// inside them is filtered out, so a heading never hangs over empty space.

// The `knowledge` category the API returns is a grab bag: the behavioral rules
// and the jacked reference doc, PLUS every bundled skill as `skill_<name>`.
// This is the one rule that decides which half a row belongs to, kept at module
// scope so it is directly testable rather than buried in an async renderer.
function _isSkillFeature(k) {
    return !!k && typeof k.name === 'string' && k.name.startsWith('skill_');
}

function _renderFeatureFilter(placeholder) {
    return `
        <div class="mb-4">
            <input type="text" data-feature-filter
                   class="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500"
                   placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
            <div data-feature-filter-empty class="text-xs text-slate-500 mt-3 hidden">No matches.</div>
        </div>
    `;
}

function _bindFeatureFilter(container) {
    const input = container.querySelector('[data-feature-filter]');
    if (!input) return;
    const empty = container.querySelector('[data-feature-filter-empty]');
    const rows = [...container.querySelectorAll('[data-feature-row]')];
    const sections = [...container.querySelectorAll('[data-feature-section]')];

    input.addEventListener('input', () => {
        const q = input.value.trim().toLowerCase();
        let visible = 0;
        rows.forEach(row => {
            const hit = !q || row.textContent.toLowerCase().includes(q);
            row.classList.toggle('hidden', !hit);
            if (hit) visible++;
        });
        sections.forEach(sec => {
            const any = [...sec.querySelectorAll('[data-feature-row]')]
                .some(r => !r.classList.contains('hidden'));
            sec.classList.toggle('hidden', !any);
        });
        if (empty) empty.classList.toggle('hidden', visible !== 0);
    });
}

// --- Tab: Features ---

async function renderFeaturesTab(container) {
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading features...</span>
        </div>
    `;

    try {
        const features = await loadFeatures();
        const hooks = features.hooks || [];
        const knowledge = features.knowledge || [];

        // A present-but-corrupt settings.json makes every toggle read as OFF
        // and every mutation refuse with 503; without a banner that reads as
        // "everything mysteriously disabled". Say what is actually wrong.
        const settingsWarning = features.settings_unreadable
            ? `<div class="mb-4 p-3 rounded border border-yellow-600/50 bg-yellow-900/20 text-yellow-300 text-sm">
                   ~/.claude/settings.json is unreadable (corrupt JSON). Toggle states shown here may be wrong and changes are refused until the file is fixed.
               </div>`
            : '';

        // Skill packs come from a separate endpoint. A hiccup fetching them
        // must not blank out hooks/knowledge, so its failure is captured here
        // and rendered as a small inline error with its own retry.
        let packsData = null;
        let packsError = null;
        try {
            packsData = await loadPacks();
        } catch (e) {
            packsError = e.message || 'Failed to load skill packs';
        }
        const packsSection = packsError
            ? _renderPacksError(packsError)
            : _renderPacksSection(packsData);

        // The review-engine card has its own endpoint too. Same isolation rule as
        // packs: a failed fetch renders the card's own inline error with a retry
        // instead of blanking hooks/knowledge.
        // The save-failure state is module-level, so without this a rejected PUT
        // would resurface, error and all, every later time the user opens this
        // tab. A save genuinely still in flight keeps its state.
        if (!_dcrEngineSaving) {
            _dcrEngineSaveError = null;
            _dcrEngineSaveRetryable = true;
            _dcrEngineLastPayload = null;
        }
        let dcrEngineData = null;
        let dcrEngineError = null;
        try {
            dcrEngineData = await loadDcrEngine();
        } catch (e) {
            dcrEngineError = e.message || 'Failed to load review engine';
        }
        const dcrEngineSection = dcrEngineError
            ? _renderDcrEngineError(dcrEngineError)
            : _renderDcrEngineSection(dcrEngineData);

        const hookRows = hooks.map(h => {
            // Memory Vault: when installed and reporting a capture/sync failure,
            // surface a small muted status line under the description (mirrors the
            // rules corrupt-marker note pattern).
            let healthNote = '';
            if (h.name === 'memory_vault' && h.installed && h.health) {
                if (h.health.last_capture_error) {
                    healthNote = `<div class="text-xs text-yellow-400 mt-1">capture failing: ${escapeHtml(h.health.last_capture_error)}</div>`;
                } else if (h.health.last_sync_error) {
                    healthNote = `<div class="text-xs text-yellow-400 mt-1">sync failing: ${escapeHtml(h.health.last_sync_error)}</div>`;
                }
            }
            return `
            <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50" data-feature-row data-display-name="${escapeHtml(h.display_name)}">
                <div class="min-w-0 flex-1">
                    <div class="text-sm text-white">${escapeHtml(h.display_name)}</div>
                    <div class="text-xs text-slate-400">${escapeHtml(h.description || '')}</div>
                    ${healthNote}
                </div>
                ${renderToggle(h.name, 'hooks', h.installed, h.source_available)}
            </div>
        `;
        }).join('');

        // The knowledge category is a grab bag: the behavioral rules and the
        // jacked reference doc, PLUS every bundled skill as a `skill_<name>`
        // entry. Rendering all of it in one list under "Documents and rules"
        // buried 28 skills in a section whose own description does not describe
        // them, and disagreed with the Installations page, which already treats
        // Skills as a first-class group. Split on the prefix the API hands us.
        //
        // The toggle category stays `knowledge` for BOTH halves: that is what
        // PUT /api/features/{category}/{name} accepts, and its Literal has no
        // `skills` member. This is a presentation split, not an API change.
        const skillEntries = knowledge.filter(_isSkillFeature);
        const docEntries = knowledge.filter(k => !_isSkillFeature(k));

        const knowledgeRow = (k) => {
            let note = '';
            if (k.name === 'rules' && k.corrupt) {
                note = '<span class="text-xs text-red-400 ml-2">Corrupt markers detected</span>';
            }
            return `
                <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50" data-feature-row data-display-name="${escapeHtml(k.display_name)}">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">${escapeHtml(k.display_name)}${note}</div>
                        <div class="text-xs text-slate-400">${escapeHtml(k.description || '')}</div>
                    </div>
                    ${renderToggle(k.name, 'knowledge', k.installed, k.source_available)}
                </div>
            `;
        };
        const knowledgeRows = docEntries.map(knowledgeRow).join('');
        const skillRows = skillEntries.map(knowledgeRow).join('');

        container.innerHTML = `
            <div class="space-y-6">
                ${settingsWarning}
                ${_renderFeatureFilter('Filter hooks, knowledge and skills...')}
                <div data-feature-section>
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Hooks</h3>
                    <p class="text-xs text-slate-500 mb-3">Background hooks that run automatically during Claude Code sessions.</p>
                    <div class="space-y-2">
                        ${hookRows}
                    </div>
                </div>

                ${dcrEngineSection}

                <div data-feature-section>
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Knowledge</h3>
                    <p class="text-xs text-slate-500 mb-3">Documents and rules that Claude reads for context and behavior. Installed to <code class="text-slate-300">~/.claude/</code>.</p>
                    <div class="space-y-2">
                        ${knowledgeRows}
                    </div>
                </div>

                <div data-feature-section>
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Skills</h3>
                    <p class="text-xs text-slate-500 mb-3">Skills bundled with jacked, installed to <code class="text-slate-300">~/.claude/skills/</code>. Toggle one off to stop Claude loading it.</p>
                    <div class="space-y-2">
                        ${skillRows}
                    </div>
                </div>

                ${packsSection}
            </div>
        `;

        bindToggleEvents(container);
        _bindPackToggleEvents(container);
        _bindDcrEngineEvents(container);
        _bindFeatureFilter(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load features: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('features')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
    }
}

// --- Skill packs section (rendered inside the Features tab) ---

function _renderPacksError(message) {
    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Skill Packs</h3>
            <div class="text-xs text-red-400">
                Failed to load skill packs: ${escapeHtml(message)}
                <button onclick="renderSettingsTab('features')" class="text-blue-400 hover:text-blue-300 ml-2 transition active:scale-[0.96]">Retry</button>
            </div>
        </div>
    `;
}

function _renderPacksSection(packsData) {
    const packs = (packsData && packsData.packs) || [];
    if (packs.length === 0) return '';

    const npxAvailable = !!(packsData && packsData.npx_available);
    const npxNote = npxAvailable
        ? ''
        : `<p id="packs-npx-note" class="text-xs text-yellow-400 mb-3">Requires Node.js 18+ (npx). Install Node to enable skill packs.</p>`;

    const rows = packs.map(p => {
        const displayName = p.display_name || p.name;
        // 'enable' | 'disable' | undefined — a PUT for this pack is running.
        const inflight = _packsInFlight.get(p.name);
        const partial = !inflight && p.enabled && p.installed_count < p.total;
        const countText = `${p.installed_count} of ${p.total} skills installed`;

        let statusLine;
        if (inflight) {
            // Mid-op: show progress, never the (stale) count. Survives any
            // re-render that lands while npx is still running. role=status so
            // assistive tech hears the minute-long operation begin.
            const busyText = inflight === 'enable' ? 'Installing skills...' : 'Removing skills...';
            statusLine = `<div class="text-xs text-slate-400 mt-1 pack-status" role="status" aria-live="polite">${busyText}</div>`;
        } else if (partial) {
            // Enabled but short: some skills failed to land. Flag it amber and
            // offer a one-click repair that re-fires the enable PUT. The link
            // needs npx just like the toggle, so it disappears with it.
            const retryLink = npxAvailable
                ? `<a href="#" class="pack-retry text-xs text-blue-400 hover:text-blue-300 ml-2 transition-colors" data-pack-retry="${escapeHtml(p.name)}">Retry install</a>`
                : '';
            statusLine = `<div class="text-xs text-amber-400 mt-1 pack-status" role="status" aria-live="polite">${escapeHtml(countText)}${retryLink}</div>`;
        } else {
            statusLine = `<div class="text-xs text-slate-400 mt-1 pack-status" role="status" aria-live="polite">${escapeHtml(countText)}</div>`;
        }

        const homeLink = p.homepage
            ? `<a href="${escapeHtml(p.homepage)}" target="_blank" rel="noopener" class="text-xs text-blue-400 hover:text-blue-300 transition-colors flex-shrink-0">Source</a>`
            : '';

        // Label carries `disabled` (npx missing → CSS dims + kills pointer) and
        // `pending` (mid-op spinner). Input is disabled in either case.
        const labelClasses = ['toggle-switch', 'pack-toggle', 'flex-shrink-0'];
        if (!npxAvailable) labelClasses.push('disabled');
        if (inflight) labelClasses.push('pending');
        const inputDisabled = (!npxAvailable || inflight) ? 'disabled' : '';
        const describedBy = npxAvailable ? '' : ' aria-describedby="packs-npx-note"';
        const ariaBusy = inflight ? ' aria-busy="true"' : '';
        // Mid-op the checkbox reflects the user's INTENT (the op in flight),
        // never the stale cached enabled flag: a re-render during a disable
        // must not snap the track back to ON under a "Removing skills..." label.
        const checked = (inflight ? inflight === 'enable' : p.enabled) ? 'checked' : '';

        // A default pack the user hasn't explicitly toggled is on because it
        // ships default-on; label it so "why is this enabled?" answers itself.
        const defaultChip = (p.default && !p.explicit)
            ? '<span class="text-[10px] uppercase tracking-wider text-slate-500 border border-slate-700 rounded px-1 py-0.5 flex-shrink-0">Default</span>'
            : '';

        return `
            <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50 gap-3" data-pack-row="${escapeHtml(p.name)}">
                <div class="min-w-0 flex-1">
                    <div class="flex items-center gap-2">
                        <span class="text-sm text-white">${escapeHtml(displayName)}</span>
                        ${defaultChip}
                        ${homeLink}
                    </div>
                    <div class="text-xs text-slate-400 mt-1">${escapeHtml(p.description || '')}</div>
                    ${statusLine}
                </div>
                <label class="${labelClasses.join(' ')}" data-pack="${escapeHtml(p.name)}" data-display-name="${escapeHtml(displayName)}"${ariaBusy}>
                    <input type="checkbox" ${checked} ${inputDisabled} aria-label="${escapeHtml(displayName)} skill pack"${describedBy}>
                    <span class="toggle-slider"></span>
                </label>
            </div>
        `;
    }).join('');

    return `
        <div>
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Skill Packs</h3>
            <p class="text-xs text-slate-500 mb-3">Curated skill bundles installed live from upstream GitHub repos via the skills CLI. Toggling a pack runs npx and can take up to a minute. Skills are instructions your agents will follow. Enabling a pack installs content from its upstream repo; review it via the Source link.</p>
            ${npxNote}
            <div class="space-y-2">${rows}</div>
        </div>
    `;
}

async function _runPackToggle(toggle, input, name, displayName, enabled) {
    // Re-entry guard: a stale Retry link (or any handler bound before this op
    // started) must never fire a second concurrent npx run for the same pack.
    if (_packsInFlight.has(name)) return;
    // Optimistic pending + disable: these ops take 10-60s, and the instant
    // feature toggles don't, so guard against double-clicks firing a second
    // npx run mid-install. The in-flight record is what makes that guard
    // survive an unrelated re-render (see _packsInFlight).
    _packsInFlight.set(name, enabled ? 'enable' : 'disable');
    toggle.classList.add('pending');
    if (input) input.disabled = true;

    try {
        const res = await api.put(
            `/api/packs/${encodeURIComponent(name)}`,
            { enabled },
            { timeout: PACK_TOGGLE_TIMEOUT_MS },
        );
        if (res && res.ok) {
            showToast(res.message || `${displayName} ${enabled ? 'enabled' : 'disabled'}`, 'success');
        } else {
            // HTTP 200 with ok=false: the op ran but did not fully succeed. The
            // server persists the requested intent even on partial failure, so
            // surface the reason and fall through to the re-render below; the
            // authoritative state decides what the toggle shows, never a manual
            // revert.
            showToast((res && res.message) || `${displayName} ${enabled ? 'enable' : 'disable'} failed`, 'error');
        }
    } catch (e) {
        showToast(e.message || 'Toggle failed', 'error');
    } finally {
        // Op is done: drop the in-flight marker BEFORE re-rendering so the row
        // rebuilds from the real on-disk status (enabled intent, the "N of M
        // installed" line, partial-repair link), not the spinner.
        _packsInFlight.delete(name);
        try {
            await refreshPacks();
            // Don't hijack the user back to Features if they navigated away
            // mid-op; only re-render when Features is still the saved tab. The
            // packs cache is refreshed above regardless, so the next Features
            // visit shows fresh state.
            const activeTab = localStorage.getItem(SETTINGS_TAB_KEY) || DEFAULT_TAB;
            if (activeTab === 'features') {
                await renderSettingsTab('features');
            }
        } catch (_) {
            // Re-render failed (e.g. server restarting): restore the live toggle
            // so the user can retry manually.
            console.warn('packs re-render failed', _);
            toggle.classList.remove('pending');
            if (input) input.disabled = false;
        }
    }
}

function _bindPackToggleEvents(container) {
    container.querySelectorAll('.pack-toggle').forEach(toggle => {
        const input = toggle.querySelector('input');
        const name = toggle.dataset.pack;
        // A disabled input (npx unavailable) gets no handler — the section
        // stays read-only until Node is installed. An in-flight pack likewise
        // gets no fresh binding: its op owns the row until its finally clears
        // the marker and re-renders.
        if (!input || input.disabled) return;
        if (_packsInFlight.has(name)) return;
        input.addEventListener('change', () => {
            const displayName = toggle.dataset.displayName || name;
            _runPackToggle(toggle, input, name, displayName, input.checked);
        });
    });

    // Partial-install repair: the amber "Retry install" link re-fires the
    // enable PUT for a pack that landed short. Skip in-flight packs (their op
    // is already running).
    container.querySelectorAll('.pack-retry').forEach(link => {
        const name = link.dataset.packRetry;
        if (_packsInFlight.has(name)) return;
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const row = link.closest('[data-pack-row]');
            const toggle = row ? row.querySelector('.pack-toggle') : null;
            const input = toggle ? toggle.querySelector('input') : null;
            const displayName = (toggle && toggle.dataset.displayName) || name;
            if (!toggle) return;
            _runPackToggle(toggle, input, name, displayName, true);
        });
    });
}

// --- DCR review engine section (rendered inside the Features tab) ---

const DCR_ENGINE_URL = '/api/dcr-engine';

function _renderDcrEngineError(message) {
    return `
        <div id="dcr-engine-section">
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Review Engine</h3>
            <div class="text-xs text-red-400">
                Failed to load review engine: ${escapeHtml(message)}
                <button onclick="renderSettingsTab('features')" class="text-blue-400 hover:text-blue-300 ml-2 transition active:scale-[0.96]">Retry</button>
            </div>
        </div>
    `;
}

function _renderDcrEngineSection(data) {
    const engine = (data && data.engine === 'codex') ? 'codex' : 'claude';
    const isCodex = engine === 'codex';
    // Mid-save every control is inert, and the flag is module-level so a
    // re-render landing during the PUT keeps them that way.
    const disabledAttr = _dcrEngineSaving ? ' disabled' : '';

    const engineOptions = [
        ['claude', 'Claude (default)'],
        ['codex', 'Codex (OpenAI)'],
    ].map(([value, label]) =>
        `<option value="${value}" ${engine === value ? 'selected' : ''}>${label}</option>`
    ).join('');

    // The API sanitizes stored values, so `effort` is always a known level here.
    // The fallback is belt-and-braces: an unrecognized value must not render a
    // select with nothing selected, which would silently PUT the first option.
    const rawEffort = (data && data.effort) || '';
    const effort = DCR_EFFORT_LEVELS.includes(rawEffort) ? rawEffort : 'xhigh';
    const effortOptions = DCR_EFFORT_LEVELS.map(level =>
        `<option value="${level}" ${effort === level ? 'selected' : ''}>${level}</option>`
    ).join('');

    // Model + effort only apply to Codex, so they are absent entirely on Claude
    // rather than sitting there dead.
    const codexFields = isCodex ? `
                <div class="flex items-center justify-between gap-3">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">Model</div>
                        <div class="text-xs text-slate-400">Any Codex model name works. gpt-5.6-luna is fast and cheap; gpt-5.6-terra is stronger.</div>
                    </div>
                    <input type="text" id="dcr-engine-model" class="w-44 flex-shrink-0 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500"
                           placeholder="gpt-5.6-luna" value="${escapeHtml((data && data.model) || '')}" aria-label="Codex model"${disabledAttr}>
                </div>
                <div class="flex items-center justify-between gap-3">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">Effort</div>
                        <div class="text-xs text-slate-400">How hard the model thinks. xhigh is the sweet spot for reviews.</div>
                    </div>
                    <select id="dcr-engine-effort" class="flex-shrink-0 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500" aria-label="Codex effort"${disabledAttr}>
                        ${effortOptions}
                    </select>
                </div>
    ` : '';

    // Readiness only means something for Codex: on Claude there is nothing to be
    // signed in to. `reason` is server-provided text, so it goes through escapeHtml.
    let statusLine = '';
    if (isCodex) {
        statusLine = (data && data.usable)
            ? `
                <div class="flex items-center gap-2" role="status" aria-live="polite">
                    <span class="w-2 h-2 rounded-full bg-green-400 flex-shrink-0"></span>
                    <span class="text-xs text-green-400">Codex is ready</span>
                </div>
            `
            : `
                <div role="status" aria-live="polite">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0"></span>
                        <span class="text-xs text-amber-400">${escapeHtml((data && data.reason) || 'Codex is not available.')}</span>
                        <a href="#" id="dcr-engine-recheck" class="text-xs text-blue-400 hover:text-blue-300 transition-colors">Check again</a>
                    </div>
                    <div class="text-xs text-slate-500 mt-1">Reviews fall back to Claude until this is fixed.</div>
                </div>
            `;
    }

    // A failed PUT reports here, next to the controls that caused it. Retry
    // re-fires the very same request, so it appears only when repeating it could
    // plausibly succeed: a 4xx rejection of this exact body cannot.
    const retryLink = _dcrEngineSaveRetryable
        ? '<a href="#" id="dcr-engine-retry" class="text-blue-400 hover:text-blue-300 ml-2 transition-colors">Retry</a>'
        : '';
    const saveError = _dcrEngineSaveError
        ? `
                <div class="text-xs text-red-400">
                    ${escapeHtml(_dcrEngineSaveError)}
                    ${retryLink}
                </div>
        `
        : '';

    // The PUT re-runs the Codex preflight and can take several seconds. Without
    // a progress line the inert controls read as a broken card.
    const savingLine = _dcrEngineSaving
        ? '<div id="dcr-engine-saving" class="text-xs text-slate-400">Saving...</div>'
        : '';

    // Which lenses stay on Claude is server state, not a constant: the CLI can
    // set the list to anything, including nothing. Rendering a fixed sentence
    // here would tell the user that Security is protected when it may not be.
    const keepOnClaude = (data && Array.isArray(data.keep_on_claude)) ? data.keep_on_claude : [];
    let carveOutNote = '';
    if (keepOnClaude.length) {
        const names = keepOnClaude.map(name => escapeHtml(String(name))).join(' and ');
        carveOutNote = `<div id="dcr-engine-carveout" class="text-xs text-slate-500">${names} reviews always stay on Claude for the highest quality judgment.</div>`;
    } else if (isCodex) {
        carveOutNote = '<div id="dcr-engine-carveout" class="text-xs text-amber-400">The keep-on-Claude list is empty: every review lens, including Security, runs on Codex. Restore it with: jacked dcr engine set codex --keep-on-claude "Security,Frontend Design"</div>';
    }

    return `
        <div id="dcr-engine-section">
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Review Engine</h3>
            <p class="text-xs text-slate-500 mb-3">Choose which AI runs your /dcr code reviews. Claude is the default and uses your Anthropic plan. Codex sends the review work to OpenAI instead, which saves your Anthropic usage and costs less.</p>
            <div class="p-3 bg-slate-900/50 rounded border border-slate-700/50 space-y-3">
                <div class="flex items-center justify-between gap-3">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">Engine</div>
                    </div>
                    <select id="dcr-engine-select" class="flex-shrink-0 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500" aria-label="Review engine"${disabledAttr}>
                        ${engineOptions}
                    </select>
                </div>
                ${codexFields}
                ${statusLine}
                ${saveError}
                ${savingLine}
                ${carveOutNote}
            </div>
        </div>
    `;
}

// Swap the card for a freshly rendered one and rebind. Scoped to the card so a
// save never disturbs the hooks/knowledge/packs sections around it.
function _rerenderDcrEngineCard() {
    const el = document.getElementById('dcr-engine-section');
    if (!el) return;
    el.outerHTML = _renderDcrEngineSection(window.jackedState.dcrEngine);
    const fresh = document.getElementById('dcr-engine-section');
    if (fresh) _bindDcrEngineEvents(fresh);
}

// Mark the card as saving WITHOUT rebuilding it. Re-rendering here would replace
// the very element the user's pointer is over: editing the model field and then
// clicking the effort select fires the input's `change` on blur, and a rebuild
// mid-gesture destroys the select before the click lands, silently swallowing it.
function _markDcrEngineSaving() {
    const el = document.getElementById('dcr-engine-section');
    if (!el) return;
    const controls = el.querySelectorAll('select, input');
    if (controls && controls.forEach) {
        controls.forEach(node => { node.disabled = true; });
    }
    if (el.querySelector('#dcr-engine-saving')) return;
    const line = document.createElement('div');
    line.id = 'dcr-engine-saving';
    line.className = 'text-xs text-slate-400';
    line.textContent = 'Saving...';
    // Same slot the renderer uses, so the in-place card and a re-rendered one
    // put the progress line in the same place.
    const anchor = el.querySelector('#dcr-engine-carveout');
    if (anchor && anchor.parentNode) {
        anchor.parentNode.insertBefore(line, anchor);
    } else {
        // No carve-out note to anchor to (Claude engine, empty list): land in the
        // card body rather than outside the bordered box.
        (el.querySelector('.space-y-3') || el).appendChild(line);
    }
}

async function _saveDcrEngine(payload) {
    // Re-entry guard: a stale Retry link (or a fast second change) must not fire
    // a second concurrent write.
    if (_dcrEngineSaving) return;
    _dcrEngineSaving = true;
    _dcrEngineLastPayload = payload;
    _dcrEngineSaveError = null;
    _dcrEngineSaveRetryable = true;
    // Inert controls plus a progress line, applied to the live nodes. The full
    // re-render happens only once the PUT settles.
    _markDcrEngineSaving();

    try {
        const fresh = await api.put(DCR_ENGINE_URL, payload);
        window.jackedState.dcrEngine = fresh;
        _dcrEngineSaveError = null;
        _dcrEngineLastPayload = null;
        showToast('Review engine updated', 'success');
    } catch (e) {
        // The cached state still holds the last known-good server values, so the
        // re-render below shows those plus this error, never a half-applied UI.
        _dcrEngineSaveError = e.message || 'Failed to save review engine';
        // A 4xx means the server judged THIS body invalid, and the inline message
        // names the offending value. Re-sending it can only fail identically, so
        // no Retry: the user edits the field and the change handler re-fires.
        // Timeouts (status 0) and 5xx are transient, so those keep Retry.
        const status = (e && typeof e.status === 'number') ? e.status : 0;
        _dcrEngineSaveRetryable = !(status >= 400 && status < 500);
    } finally {
        _dcrEngineSaving = false;
        _rerenderDcrEngineCard();
    }
}

function _bindDcrEngineEvents(container) {
    const engineSelect = container.querySelector('#dcr-engine-select');
    if (engineSelect && !engineSelect.disabled) {
        engineSelect.addEventListener('change', () => {
            _saveDcrEngine({ engine: engineSelect.value });
        });
    }

    // Current engine for the codex-only fields: read the live select so a payload
    // can never disagree with what the user is looking at.
    const currentEngine = () => (engineSelect && engineSelect.value) ||
        ((window.jackedState.dcrEngine && window.jackedState.dcrEngine.engine) || 'claude');

    // `change` on a text input fires on blur once the value actually differs, so
    // this covers both the change and blur cases without a redundant PUT.
    const modelInput = container.querySelector('#dcr-engine-model');
    if (modelInput && !modelInput.disabled) {
        modelInput.addEventListener('change', () => {
            const model = modelInput.value.trim();
            const saved = (window.jackedState.dcrEngine && window.jackedState.dcrEngine.model) || '';
            if (model === saved) return;
            _saveDcrEngine({ engine: currentEngine(), model });
        });
    }

    const effortSelect = container.querySelector('#dcr-engine-effort');
    if (effortSelect && !effortSelect.disabled) {
        effortSelect.addEventListener('change', () => {
            _saveDcrEngine({ engine: currentEngine(), effort: effortSelect.value });
        });
    }

    // Codex readiness can change out from under the page (the user runs
    // `codex login` in a terminal), so the amber block offers an explicit
    // re-check that refetches and redraws the card in place.
    const recheckLink = container.querySelector('#dcr-engine-recheck');
    if (recheckLink) {
        recheckLink.addEventListener('click', async (e) => {
            e.preventDefault();
            // A running PUT owns the card; racing a GET into the cache behind it
            // would show state the save is about to replace.
            if (_dcrEngineSaving || _dcrEngineRechecking) return;
            _dcrEngineRechecking = true;
            try {
                window.jackedState.dcrEngine = await api.get(DCR_ENGINE_URL);
                _rerenderDcrEngineCard();
            } catch (err) {
                showToast(err.message || 'Failed to re-check Codex', 'error');
            } finally {
                _dcrEngineRechecking = false;
            }
        });
    }

    // Retry re-fires the exact request that failed, not a generic refetch.
    const retryLink = container.querySelector('#dcr-engine-retry');
    if (retryLink) {
        retryLink.addEventListener('click', (e) => {
            e.preventDefault();
            if (!_dcrEngineLastPayload) return;
            _saveDcrEngine(_dcrEngineLastPayload);
        });
    }
}

// --- Tab: Plugins ---

async function renderPluginsTab(container) {
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading plugins...</span>
        </div>
    `;

    try {
        const data = await loadClaudeSettings();
        const plugins = data.plugins || [];

        if (plugins.length === 0) {
            container.innerHTML = `
                <div class="mb-3">
                    <p class="text-xs text-slate-500">Plugins from Claude Code's <code class="text-slate-300">enabledPlugins</code> in <code class="text-slate-300">~/.claude/settings.json</code>.</p>
                </div>
                <div class="flex flex-col items-center justify-center py-12 bg-slate-800/50 rounded-lg border border-slate-700/50">
                    <div class="text-sm text-slate-400 mb-1">No plugins configured</div>
                    <p class="text-xs text-slate-500">Add plugins via Claude Code or edit settings.json directly.</p>
                </div>
            `;
            return;
        }

        const cardsHtml = plugins.map(p => {
            const parts = p.name.split('@');
            const displayName = parts[0] || p.name;
            const marketplace = parts.length > 1 ? parts.slice(1).join('@') : '';
            const marketplaceBadge = marketplace
                ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-400 ml-2">${escapeHtml(marketplace)}</span>`
                : '';
            return `
                <div class="feature-card ${p.enabled ? '' : 'disabled'}">
                    <div class="flex items-center justify-between gap-3">
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center">
                                <span class="text-sm font-medium text-white truncate">${escapeHtml(displayName)}</span>
                                ${marketplaceBadge}
                            </div>
                        </div>
                        <label class="toggle-switch claude-settings-toggle" data-plugin="${escapeHtml(p.name)}">
                            <input type="checkbox" ${p.enabled ? 'checked' : ''}>
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = `
            <div class="mb-4">
                <p class="text-xs text-slate-500">Claude Code plugins from <code class="text-slate-300">enabledPlugins</code> in <code class="text-slate-300">~/.claude/settings.json</code>. Restart Claude Code after changes.</p>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        // Bind plugin toggles
        container.querySelectorAll('.claude-settings-toggle[data-plugin]').forEach(toggle => {
            const input = toggle.querySelector('input');
            if (!input) return;
            input.addEventListener('change', async () => {
                const name = toggle.dataset.plugin;
                const enabled = input.checked;
                toggle.classList.add('pending');
                input.disabled = true;
                try {
                    await api.put(`/api/claude-settings/plugins/${encodeURIComponent(name)}`, { enabled });
                    showToast(`${name.split('@')[0]} ${enabled ? 'enabled' : 'disabled'}. Restart Claude Code.`, 'warning');
                    await refreshClaudeSettings();
                } catch (e) {
                    input.checked = !enabled;
                    showToast(e.message || 'Toggle failed', 'error');
                } finally {
                    toggle.classList.remove('pending');
                    input.disabled = false;
                }
            });
        });
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load plugins: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('plugins')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
    }
}

// --- Tab: Claude Code ---

let _claudeCodeSaveTimers = {};

function _debouncedSave(key, fn, delay = 800) {
    if (_claudeCodeSaveTimers[key]) clearTimeout(_claudeCodeSaveTimers[key]);
    _claudeCodeSaveTimers[key] = setTimeout(fn, delay);
}

async function renderClaudeCodeTab(container) {
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading Claude Code settings...</span>
        </div>
    `;

    try {
        const data = await loadClaudeSettings();
        const envToggles = data.env_toggles || [];
        const envNumeric = data.env_numeric || [];
        const directSettings = data.direct_settings || [];
        const permissions = data.permissions || { allow: [], deny: [], ask: [], defaultMode: 'default' };

        // Group env toggles by section
        const experimental = envToggles.filter(t => t.section === 'experimental');
        const privacy = envToggles.filter(t => t.section === 'privacy');

        // Build HTML
        let html = `
            <div class="bg-gradient-to-r from-blue-900/20 to-indigo-900/20 border border-blue-700/30 rounded-lg p-4 mb-6">
                <div class="flex items-center gap-2 mb-1">
                    <svg class="w-4 h-4 text-blue-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                    <span class="text-sm font-semibold text-white">Claude Code Configuration</span>
                </div>
                <p class="text-xs text-slate-400">Settings below control <span class="text-blue-300">Claude Code</span> behavior directly — not jacked features. Changes are written to <code class="text-slate-300">~/.claude/settings.json</code>. Restart Claude Code after making changes.</p>
            </div>
        `;

        // --- Experimental ---
        html += _renderToggleSection('Experimental', experimental);

        // --- Performance ---
        html += _renderNumericSection('Performance', envNumeric);

        // --- Privacy ---
        html += _renderToggleSection('Privacy', privacy);

        // --- Preferences ---
        const boolPrefs = directSettings.filter(s => s.type === 'bool');
        const numPrefs = directSettings.filter(s => s.type === 'number');
        html += `
            <div class="mb-6">
                <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Preferences</h3>
                <div class="space-y-2">
        `;
        for (const pref of boolPrefs) {
            html += `
                <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">${escapeHtml(pref.display_name)}</div>
                        <div class="text-xs text-slate-400">${escapeHtml(pref.description)}</div>
                    </div>
                    <label class="toggle-switch cc-key-toggle" data-key="${escapeHtml(pref.name)}">
                        <input type="checkbox" ${pref.value ? 'checked' : ''}>
                        <span class="toggle-slider"></span>
                    </label>
                </div>
            `;
        }
        for (const pref of numPrefs) {
            html += `
                <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50">
                    <div class="min-w-0 flex-1">
                        <div class="text-sm text-white">${escapeHtml(pref.display_name)}</div>
                        <div class="text-xs text-slate-400">${escapeHtml(pref.description)}</div>
                    </div>
                    <input type="number" class="cc-key-number w-24 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white text-right tabular-nums focus:outline-none focus:border-blue-500"
                           data-key="${escapeHtml(pref.name)}" value="${escapeHtml(String(pref.value))}" data-default="${escapeHtml(String(pref.default))}">
                </div>
            `;
        }
        html += `</div></div>`;

        // --- Permissions ---
        html += _renderPermissionsSection(permissions);

        // --- Chrome DevTools MCP ---
        html += `
            <div class="mb-6 border-t border-slate-700 pt-5">
                <div class="flex items-center gap-2 mb-1">
                    <svg class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9"/></svg>
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Browser Tools</h3>
                </div>
                <p class="text-xs text-slate-500 mb-3">Chrome DevTools MCP powers <code class="text-slate-300">/qa</code> and <code class="text-slate-300">/ux</code> browser testing. Requires <span class="text-emerald-400">Chrome 144+</span> with remote debugging enabled.</p>
                <div id="cdp-mcp-status" class="p-3 bg-slate-900/50 rounded border border-slate-700/50">
                    <div class="flex items-center justify-between">
                        <div class="min-w-0 flex-1">
                            <div class="text-sm text-white">Chrome DevTools MCP</div>
                            <div id="cdp-mcp-status-text" class="text-xs text-slate-400">Checking...</div>
                        </div>
                        <div class="flex items-center gap-3">
                            <select id="cdp-mcp-mode" class="bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500" disabled>
                                <option value="autoConnect">Auto-connect (Chrome 144+)</option>
                                <option value="browserUrl">Browser URL (:9222)</option>
                                <option value="launch">Launch new Chrome</option>
                                <option value="headless">Headless (no UI)</option>
                            </select>
                            <label class="toggle-switch" id="cdp-mcp-toggle">
                                <input type="checkbox" disabled>
                                <span class="toggle-slider"></span>
                            </label>
                        </div>
                    </div>
                    <div id="cdp-mcp-help" class="hidden mt-3 p-2 bg-slate-800/50 rounded border border-slate-700/30">
                        <p class="text-xs text-slate-400 mb-1 font-semibold">Setup instructions:</p>
                        <ol class="text-xs text-slate-500 list-decimal ml-4 space-y-1">
                            <li>Install Chrome 144 or newer — check at <code class="text-slate-300">chrome://version</code></li>
                            <li>Enable remote debugging at <code class="text-slate-300">chrome://inspect/#remote-debugging</code></li>
                            <li>Restart Claude Code after enabling</li>
                        </ol>
                    </div>
                </div>
            </div>
        `;

        // --- Raw JSON Editor ---
        html += `
            <div class="mb-6 border-t border-slate-700 pt-5">
                <div id="raw-editor-header" class="flex items-center justify-between cursor-pointer select-none">
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Raw settings.json</h3>
                    <svg id="raw-editor-chevron" class="w-5 h-5 text-slate-400 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                </div>
                <div id="raw-editor-content" class="hidden mt-3">
                    <p class="text-xs text-slate-500 mb-2">Direct JSON editor for <code class="text-slate-300">~/.claude/settings.json</code>. Be careful — invalid JSON will break Claude Code.</p>
                    <textarea id="raw-settings-textarea"
                        class="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-slate-200 font-mono leading-relaxed focus:outline-none focus:border-blue-500 resize-y"
                        rows="18" spellcheck="false"></textarea>
                    <div id="raw-json-error" class="mt-1 text-xs text-red-400 hidden"></div>
                    <div class="flex items-center justify-between mt-2">
                        <button id="btn-raw-revert" class="text-xs text-slate-400 hover:text-slate-300 transition-colors">Revert</button>
                        <div class="flex items-center gap-2">
                            <span id="raw-save-status" class="text-xs text-slate-500"></span>
                            <button id="btn-raw-save" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition active:scale-[0.96] disabled:opacity-40 disabled:cursor-not-allowed" disabled>Save</button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        container.innerHTML = html;
        _bindClaudeCodeEvents(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load Claude Code settings: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('claude-code')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
    }
}

function _renderToggleSection(title, items) {
    if (!items.length) return '';
    const rows = items.map(item => `
        <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50">
            <div class="min-w-0 flex-1">
                <div class="text-sm text-white">${escapeHtml(item.display_name)}</div>
                <div class="text-xs text-slate-400">${escapeHtml(item.description)}</div>
            </div>
            <label class="toggle-switch cc-env-toggle" data-env="${escapeHtml(item.name)}">
                <input type="checkbox" ${item.enabled ? 'checked' : ''}>
                <span class="toggle-slider"></span>
            </label>
        </div>
    `).join('');
    return `
        <div class="mb-6">
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">${escapeHtml(title)}</h3>
            <div class="space-y-2">${rows}</div>
        </div>
    `;
}

function _renderNumericSection(title, items) {
    if (!items.length) return '';
    const rows = items.map(item => `
        <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50">
            <div class="min-w-0 flex-1">
                <div class="text-sm text-white">${escapeHtml(item.display_name)}</div>
                <div class="text-xs text-slate-400">${escapeHtml(item.description)}</div>
            </div>
            <input type="number" class="cc-env-number w-28 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white text-right tabular-nums focus:outline-none focus:border-blue-500"
                   data-env="${escapeHtml(item.name)}" value="${escapeHtml(item.value)}"
                   min="${item.min}" max="${item.max}" data-default="${escapeHtml(item.default)}">
        </div>
    `).join('');
    return `
        <div class="mb-6">
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">${escapeHtml(title)}</h3>
            <div class="space-y-2">${rows}</div>
        </div>
    `;
}

// --- Permissions scope state ---
let _permScope = 'global';
let _permProjectRepo = '';
let _permProjectRepos = [];
let _permProjectData = null;  // cached project permissions

const _PERM_TEMPLATES = [
    { label: 'WebFetch', pattern: 'WebFetch' },
    { label: 'WebSearch', pattern: 'WebSearch' },
    { label: 'curl', pattern: 'Bash(curl:*)' },
    { label: 'wget', pattern: 'Bash(wget:*)' },
    { label: 'git push', pattern: 'Bash(git push:*)' },
    { label: 'git commit', pattern: 'Bash(git commit:*)' },
    { label: 'npm install', pattern: 'Bash(npm install:*)' },
    { label: 'pip install', pattern: 'Bash(pip install:*)' },
    { label: 'docker', pattern: 'Bash(docker:*)' },
    { label: 'make', pattern: 'Bash(make:*)' },
];

function _renderPermissionsSection(permissions) {
    const modeOptions = ['default', 'plan', 'bypassPermissions', 'acceptEdits'].map(m =>
        `<option value="${m}" ${permissions.defaultMode === m ? 'selected' : ''}>${m}</option>`
    ).join('');

    function renderList(label, items, listName) {
        const itemsHtml = items.length > 0
            ? items.map((rule, i) => `
                <div class="flex items-center justify-between py-1.5 px-2 bg-slate-800 rounded text-sm group">
                    <code class="text-slate-200 text-xs font-mono truncate">${escapeHtml(rule)}</code>
                    <button class="perm-remove-btn opacity-0 group-hover:opacity-100 text-red-400 hover:text-red-300 transition-opacity transition-colors ml-2 flex-shrink-0"
                            data-list="${listName}" data-index="${i}" title="Remove">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                    </button>
                </div>
            `).join('')
            : '<div class="text-xs text-slate-500 italic py-1">empty</div>';

        return `
            <div class="mb-3">
                <div class="flex items-center justify-between mb-1.5">
                    <span class="text-xs font-medium text-slate-400 uppercase">${label}</span>
                    <span class="text-[10px] text-slate-600">${items.length} rule${items.length !== 1 ? 's' : ''}</span>
                </div>
                <div class="space-y-1">${itemsHtml}</div>
                <div class="flex items-center gap-2 mt-2">
                    <input type="text" class="perm-add-input flex-1 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-xs text-white font-mono placeholder-slate-500 focus:outline-none focus:border-blue-500"
                           data-list="${listName}" placeholder="e.g. Bash(git status:*)">
                    <button class="perm-add-btn px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-white rounded transition active:scale-[0.96]" data-list="${listName}">Add</button>
                </div>
            </div>
        `;
    }

    // Templates row (only for allow list)
    const templatesHtml = _PERM_TEMPLATES.map(t =>
        `<button class="perm-template-btn px-2 py-0.5 text-[11px] bg-slate-700/60 hover:bg-blue-700/60 text-slate-300 hover:text-white rounded transition-colors font-mono" data-pattern="${escapeHtml(t.pattern)}">${escapeHtml(t.label)}</button>`
    ).join('');

    // Scope tabs
    const globalActive = _permScope === 'global';
    const projectActive = _permScope === 'project';

    // Project repo dropdown
    const repoOptions = _permProjectRepos.map(r => {
        const name = r.replace(/\\/g, '/').split('/').filter(Boolean).pop() || r;
        return `<option value="${escapeHtml(r)}" ${_permProjectRepo === r ? 'selected' : ''}>${escapeHtml(name)}</option>`;
    }).join('');

    // Decide which permissions to display
    const displayPerms = projectActive && _permProjectData ? _permProjectData : permissions;

    return `
        <div class="mb-6">
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Permissions</h3>

            <div class="flex items-center gap-2 mb-3">
                <button class="perm-scope-btn px-3 py-1 rounded-lg text-xs font-medium transition-colors ${globalActive ? 'bg-blue-700 text-white' : 'bg-slate-700 text-slate-400 hover:text-white'}" data-scope="global">Global</button>
                <button class="perm-scope-btn px-3 py-1 rounded-lg text-xs font-medium transition-colors ${projectActive ? 'bg-blue-700 text-white' : 'bg-slate-700 text-slate-400 hover:text-white'}" data-scope="project">Project</button>
                ${projectActive ? `
                    <select id="perm-project-repo" class="flex-1 bg-slate-900 border border-slate-600 rounded-lg px-2 py-1 text-xs text-slate-200 focus:outline-none focus:border-blue-500">
                        <option value="">Select project...</option>
                        ${repoOptions}
                    </select>` : ''}
                ${projectActive ? '<div class="text-[10px] text-slate-500">settings.local.json</div>' : '<div class="text-[10px] text-slate-500">~/.claude/settings.json</div>'}
            </div>

            ${projectActive && !_permProjectRepo ? `
                <div class="text-xs text-slate-500 italic py-4 text-center">Select a project to view its permissions</div>
            ` : `
                <div class="p-3 bg-slate-900/50 rounded border border-slate-700/50 mb-3">
                    <div class="flex items-center justify-between">
                        <div>
                            <div class="text-sm text-white">Default Mode</div>
                            <div class="text-xs text-slate-400">Permission mode Claude Code starts in</div>
                        </div>
                        <select id="perm-default-mode" class="bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-blue-500">
                            ${modeOptions}
                        </select>
                    </div>
                </div>
                ${renderList('Allow', displayPerms.allow || [], 'allow')}
                <div class="mb-3">
                    <div class="text-[10px] text-slate-500 uppercase tracking-wider mb-1.5">Quick-add templates</div>
                    <div class="flex flex-wrap gap-1">${templatesHtml}</div>
                </div>
                ${renderList('Deny', displayPerms.deny || [], 'deny')}
                ${renderList('Ask', displayPerms.ask || [], 'ask')}
            `}
        </div>
    `;
}

function _bindClaudeCodeEvents(container) {
    // Env toggle switches
    container.querySelectorAll('.cc-env-toggle').forEach(toggle => {
        const input = toggle.querySelector('input');
        if (!input) return;
        input.addEventListener('change', async () => {
            const name = toggle.dataset.env;
            const enabled = input.checked;
            toggle.classList.add('pending');
            input.disabled = true;
            try {
                await api.put(`/api/claude-settings/env/${encodeURIComponent(name)}`, { enabled });
                showToast(`Updated. Restart Claude Code for changes to take effect.`, 'warning');
                await refreshClaudeSettings();
            } catch (e) {
                input.checked = !enabled;
                showToast(e.message || 'Save failed', 'error');
            } finally {
                toggle.classList.remove('pending');
                input.disabled = false;
            }
        });
    });

    // Env numeric inputs (debounced)
    container.querySelectorAll('.cc-env-number').forEach(input => {
        input.addEventListener('input', () => {
            const name = input.dataset.env;
            _debouncedSave(`env-${name}`, async () => {
                try {
                    await api.put(`/api/claude-settings/env/${encodeURIComponent(name)}`, { value: input.value });
                    showToast(`Updated. Restart Claude Code for changes to take effect.`, 'warning');
                    await refreshClaudeSettings();
                } catch (e) {
                    showToast(e.message || 'Save failed', 'error');
                }
            });
        });
    });

    // Direct settings bool toggles
    container.querySelectorAll('.cc-key-toggle').forEach(toggle => {
        const input = toggle.querySelector('input');
        if (!input) return;
        input.addEventListener('change', async () => {
            const key = toggle.dataset.key;
            const value = input.checked;
            toggle.classList.add('pending');
            input.disabled = true;
            try {
                await api.put(`/api/claude-settings/key/${encodeURIComponent(key)}`, { value });
                showToast(`Updated. Restart Claude Code for changes to take effect.`, 'warning');
                await refreshClaudeSettings();
            } catch (e) {
                input.checked = !value;
                showToast(e.message || 'Save failed', 'error');
            } finally {
                toggle.classList.remove('pending');
                input.disabled = false;
            }
        });
    });

    // Direct settings number inputs (debounced)
    container.querySelectorAll('.cc-key-number').forEach(input => {
        input.addEventListener('input', () => {
            const key = input.dataset.key;
            _debouncedSave(`key-${key}`, async () => {
                try {
                    await api.put(`/api/claude-settings/key/${encodeURIComponent(key)}`, { value: parseInt(input.value) || 0 });
                    showToast(`Updated. Restart Claude Code for changes to take effect.`, 'warning');
                    await refreshClaudeSettings();
                } catch (e) {
                    showToast(e.message || 'Save failed', 'error');
                }
            });
        });
    });

    // Permissions — scope tabs
    container.querySelectorAll('.perm-scope-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            _permScope = btn.dataset.scope;
            _permProjectData = null;
            if (_permScope === 'project' && _permProjectRepos.length === 0) {
                try {
                    const sessions = await api.get('/api/logs/sessions');
                    const seen = new Map();
                    for (const s of sessions) {
                        if (s.repo_path) {
                            const key = s.repo_path.toLowerCase();
                            if (!seen.has(key)) seen.set(key, s.repo_path);
                        }
                    }
                    _permProjectRepos = [...seen.values()].sort();
                } catch (e) { _permProjectRepos = []; }
            }
            await renderClaudeCodeTab(container);
        });
    });

    // Permissions — project repo dropdown
    const repoSelect = document.getElementById('perm-project-repo');
    if (repoSelect) {
        repoSelect.addEventListener('change', async () => {
            _permProjectRepo = repoSelect.value;
            if (_permProjectRepo) {
                try {
                    _permProjectData = await api.get(`/api/claude-settings/project-permissions?repo_path=${encodeURIComponent(_permProjectRepo)}`);
                } catch (e) {
                    _permProjectData = null;
                    showToast(e.message || 'Failed to load project permissions', 'error');
                }
            } else {
                _permProjectData = null;
            }
            await renderClaudeCodeTab(container);
        });
    }

    // Permissions — default mode dropdown
    const modeSelect = document.getElementById('perm-default-mode');
    if (modeSelect) {
        modeSelect.addEventListener('change', async () => {
            try {
                if (_permScope === 'project' && _permProjectRepo) {
                    await api.put('/api/claude-settings/project-permissions', {
                        repo_path: _permProjectRepo,
                        defaultMode: modeSelect.value,
                    });
                } else {
                    await api.put('/api/claude-settings/permissions', { defaultMode: modeSelect.value });
                }
                showToast(`Default mode set to "${modeSelect.value}". Restart Claude Code.`, 'warning');
                await refreshClaudeSettings();
            } catch (e) {
                showToast(e.message || 'Save failed', 'error');
            }
        });
    }

    // Permissions — remove rule (scope-aware)
    container.querySelectorAll('.perm-remove-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const listName = btn.dataset.list;
            const index = parseInt(btn.dataset.index);
            try {
                if (_permScope === 'project' && _permProjectRepo && _permProjectData) {
                    const arr = [...(_permProjectData[listName] || [])];
                    arr.splice(index, 1);
                    const payload = { repo_path: _permProjectRepo };
                    payload[listName] = arr;
                    await api.put('/api/claude-settings/project-permissions', payload);
                    _permProjectData[listName] = arr;
                } else {
                    const current = await loadClaudeSettings();
                    const perms = { ...current.permissions };
                    const arr = [...(perms[listName] || [])];
                    arr.splice(index, 1);
                    perms[listName] = arr;
                    await api.put('/api/claude-settings/permissions', perms);
                    await refreshClaudeSettings();
                }
                showToast(`Rule removed. Restart Claude Code.`, 'warning');
                await renderClaudeCodeTab(container);
            } catch (e) {
                showToast(e.message || 'Remove failed', 'error');
            }
        });
    });

    // Permissions — add rule (scope-aware)
    container.querySelectorAll('.perm-add-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const listName = btn.dataset.list;
            const input = container.querySelector(`.perm-add-input[data-list="${listName}"]`);
            const rule = input ? input.value.trim() : '';
            if (!rule) return;
            try {
                const payload = { pattern: rule, list_name: listName, scope: _permScope };
                if (_permScope === 'project' && _permProjectRepo) {
                    payload.repo_path = _permProjectRepo;
                }
                await api.post('/api/claude-settings/permissions/rule', payload);
                showToast(`Rule added. Restart Claude Code.`, 'warning');
                if (_permScope === 'project' && _permProjectRepo) {
                    _permProjectData = await api.get(`/api/claude-settings/project-permissions?repo_path=${encodeURIComponent(_permProjectRepo)}`);
                } else {
                    await refreshClaudeSettings();
                }
                await renderClaudeCodeTab(container);
            } catch (e) {
                showToast(e.message || 'Add failed', 'error');
            }
        });
    });

    // Permissions — add on Enter
    container.querySelectorAll('.perm-add-input').forEach(input => {
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                const btn = container.querySelector(`.perm-add-btn[data-list="${input.dataset.list}"]`);
                if (btn) btn.click();
            }
        });
    });

    // Permissions — template buttons
    container.querySelectorAll('.perm-template-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const pattern = btn.dataset.pattern;
            try {
                const payload = { pattern, list_name: 'allow', scope: _permScope };
                if (_permScope === 'project' && _permProjectRepo) {
                    payload.repo_path = _permProjectRepo;
                }
                await api.post('/api/claude-settings/permissions/rule', payload);
                showToast(`Added ${pattern}. Restart Claude Code.`, 'warning');
                if (_permScope === 'project' && _permProjectRepo) {
                    _permProjectData = await api.get(`/api/claude-settings/project-permissions?repo_path=${encodeURIComponent(_permProjectRepo)}`);
                } else {
                    await refreshClaudeSettings();
                }
                await renderClaudeCodeTab(container);
            } catch (e) {
                showToast(e.message || 'Add failed', 'error');
            }
        });
    });

    // Chrome DevTools MCP — load status then bind events (once only)
    _initChromeDevToolsMCP(container);

    // Raw JSON editor — collapsible
    const rawHeader = document.getElementById('raw-editor-header');
    const rawContent = document.getElementById('raw-editor-content');
    const rawChevron = document.getElementById('raw-editor-chevron');

    if (rawHeader) {
        rawHeader.addEventListener('click', () => {
            const isHidden = rawContent.classList.toggle('hidden');
            rawChevron.style.transform = isHidden ? '' : 'rotate(180deg)';
            if (!isHidden) {
                // Always re-fetch when expanding to avoid stale data
                _loadRawEditor();
            }
        });
    }
}

async function _refreshChromeDevToolsStatus() {
    const statusText = document.getElementById('cdp-mcp-status-text');
    const modeSelect = document.getElementById('cdp-mcp-mode');
    const toggleInput = document.getElementById('cdp-mcp-toggle')?.querySelector('input');
    const helpDiv = document.getElementById('cdp-mcp-help');
    if (!statusText || !modeSelect || !toggleInput) return;

    try {
        const data = await api.get('/api/chrome-devtools-mcp');
        if (data.installed) {
            statusText.textContent = `Configured (${data.mode || 'default'})`;
            statusText.className = 'text-xs text-emerald-400';
            modeSelect.value = data.mode || 'autoConnect';
            modeSelect.disabled = false;
            toggleInput.checked = true;
            toggleInput.disabled = false;
            if (helpDiv) helpDiv.classList.add('hidden');
        } else {
            statusText.textContent = 'Not configured';
            statusText.className = 'text-xs text-amber-400';
            modeSelect.disabled = true;
            toggleInput.checked = false;
            toggleInput.disabled = false;
            if (helpDiv) helpDiv.classList.remove('hidden');
        }
    } catch (e) {
        statusText.textContent = 'Error loading status';
        statusText.className = 'text-xs text-red-400';
        if (helpDiv) helpDiv.classList.remove('hidden');
    }
}

function _initChromeDevToolsMCP(container) {
    const toggleInput = document.getElementById('cdp-mcp-toggle')?.querySelector('input');
    const modeSelect = document.getElementById('cdp-mcp-mode');
    if (!toggleInput || !modeSelect) return;

    // Load initial status
    _refreshChromeDevToolsStatus();

    // Bind events once only
    toggleInput.addEventListener('change', async () => {
        toggleInput.disabled = true;
        try {
            if (toggleInput.checked) {
                const mode = modeSelect.value || 'autoConnect';
                await api.put('/api/chrome-devtools-mcp', { mode });
                showToast(`Chrome DevTools MCP enabled (${mode}). Restart Claude Code.`, 'warning');
            } else {
                await api.delete('/api/chrome-devtools-mcp');
                showToast('Chrome DevTools MCP removed. Restart Claude Code.', 'warning');
            }
            await _refreshChromeDevToolsStatus();
        } catch (e) {
            toggleInput.checked = !toggleInput.checked;
            showToast(e.message || 'Failed to update Chrome DevTools MCP', 'error');
        } finally {
            toggleInput.disabled = false;
        }
    });

    modeSelect.addEventListener('change', async () => {
        if (!toggleInput.checked) return;
        modeSelect.disabled = true;
        try {
            await api.put('/api/chrome-devtools-mcp', { mode: modeSelect.value });
            showToast(`Mode changed to ${modeSelect.value}. Restart Claude Code.`, 'warning');
            await _refreshChromeDevToolsStatus();
        } catch (e) {
            showToast(e.message || 'Failed to change mode', 'error');
        } finally {
            modeSelect.disabled = false;
        }
    });
}

async function _loadRawEditor() {
    const textarea = document.getElementById('raw-settings-textarea');
    const saveBtn = document.getElementById('btn-raw-save');
    const revertBtn = document.getElementById('btn-raw-revert');
    const errorEl = document.getElementById('raw-json-error');
    const statusEl = document.getElementById('raw-save-status');
    if (!textarea) return;

    try {
        const data = await api.get('/api/claude-settings/raw');
        const jsonStr = JSON.stringify(data.content, null, 2);
        textarea.value = jsonStr;
        textarea.dataset.original = jsonStr;

        // Input validation
        textarea.addEventListener('input', () => {
            const changed = textarea.value !== textarea.dataset.original;
            statusEl.textContent = changed ? 'Unsaved changes' : '';
            try {
                JSON.parse(textarea.value);
                errorEl.classList.add('hidden');
                saveBtn.disabled = !changed;
            } catch (e) {
                errorEl.textContent = `Invalid JSON: ${e.message}`;
                errorEl.classList.remove('hidden');
                saveBtn.disabled = true;
            }
        });

        // Save
        saveBtn.addEventListener('click', async () => {
            try {
                const parsed = JSON.parse(textarea.value);
                saveBtn.disabled = true;
                saveBtn.textContent = 'Saving...';
                await api.put('/api/claude-settings/raw', { content: parsed, confirm_overwrite: true });
                textarea.dataset.original = textarea.value;
                statusEl.textContent = '';
                showToast('Settings saved. Restart Claude Code for changes to take effect.', 'warning');
                await refreshClaudeSettings();
            } catch (e) {
                showToast(e.message || 'Save failed', 'error');
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save';
            }
        });

        // Revert
        revertBtn.addEventListener('click', () => {
            textarea.value = textarea.dataset.original;
            statusEl.textContent = '';
            errorEl.classList.add('hidden');
            saveBtn.disabled = true;
        });
    } catch (e) {
        textarea.value = `Error loading settings: ${e.message}`;
        textarea.disabled = true;
    }
}

// --- Tab: Appearance ---

// Two-option segmented picker for the account-usage color scheme. Renders from
// the theme currently painted on the page (which syncColorThemeFromServer has
// already reconciled against the server), so it stays synchronous.
function renderAppearanceTab(container) {
    const theme = _appliedColorTheme();

    // Each option previews its usage-bar palette so the choice is obvious; the
    // selected one carries a blue ring + "Active" badge.
    const option = (value, title, desc, swatches) => {
        const active = theme === value;
        const swatchHtml = swatches
            .map(c => `<span class="inline-block w-6 h-2.5 rounded-sm border border-slate-600/50" style="background:${c}"></span>`)
            .join('');
        return `
            <button type="button" class="appearance-option feature-card text-left w-full ${active ? 'ring-2 ring-blue-500 border-blue-500' : ''}" data-theme="${value}" aria-pressed="${active}">
                <div class="flex items-center gap-2">
                    <span class="text-sm font-medium text-white">${title}</span>
                    ${active ? '<span class="badge badge-primary">Active</span>' : ''}
                </div>
                <p class="text-xs text-slate-400 mt-1">${desc}</p>
                <div class="flex items-center gap-1.5 mt-2">${swatchHtml}</div>
            </button>
        `;
    };

    container.innerHTML = `
        <p class="text-xs text-slate-500 mb-4 text-pretty">Color scheme for account-usage bars and their percentages. Applies instantly here and to the menu-bar panel.</p>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
            ${option('america250', 'America 250', 'Red, white &amp; blue for the 2026 U.S. semiquincentennial: healthy is blue, warning white, critical red.', ['#3b82f6', '#ffffff', '#ef4444'])}
            ${option('classic', 'Classic', 'The original green / amber / red usage palette.', ['#22c55e', '#eab308', '#ef4444'])}
        </div>
    `;

    _bindAppearanceEvents(container);
}

function _bindAppearanceEvents(container) {
    container.querySelectorAll('.appearance-option').forEach(btn => {
        btn.addEventListener('click', async () => {
            // Claim the theme for the user BEFORE painting: a reconcile GET that
            // was issued before this click can still be in flight, and it must
            // not overwrite the choice being made right now.
            _colorThemeUserPicked = true;
            // Paint + cache first so the choice is instant and survives a reload,
            // then persist to the server so the tray WKWebView (its own separate
            // localStorage) picks it up on its next refresh. Bars restyle live via
            // CSS the moment the class flips; the JS-emitted percent-label classes
            // catch up on the next render of accounts/panel.
            const theme = applyColorTheme(btn.dataset.theme);
            showToast(theme === 'classic' ? 'Classic theme applied' : 'America 250 theme applied', 'success');
            renderAppearanceTab(container);
            try {
                await persistColorTheme(theme);
            } catch (e) {
                showToast(
                    'Saved on this device only'
                    + (e && e.message ? ': ' + e.message : '')
                    + '. The menu-bar panel may keep the old colors.',
                    'warning',
                );
            }
        });
    });
}

// --- Tab: Advanced ---

function renderAdvancedTab(container) {
    const settings = window.jackedState.settings;
    const entries = settingsToEntries(settings);

    let tableHtml = '';
    if (entries.length > 0) {
        const rowsHtml = entries.map(([key, value]) => renderSettingRow(key, value)).join('');
        tableHtml = `
            <table class="data-table">
                <thead>
                    <tr>
                        <th class="text-left w-1/3">Key</th>
                        <th class="text-left">Value</th>
                        <th class="w-24">Actions</th>
                    </tr>
                </thead>
                <tbody id="settings-tbody">
                    ${rowsHtml}
                </tbody>
            </table>
        `;
    } else {
        tableHtml = `
            <div class="text-center py-12 text-slate-500 text-sm">
                No settings configured.
            </div>
        `;
    }

    container.innerHTML = `
        <div id="remote-access-card" class="bg-slate-800 border border-slate-700 rounded-lg p-4 mb-4"></div>
        <div id="oauth-browser-card" class="bg-slate-800 border border-slate-700 rounded-lg p-4 mb-4"></div>

        <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-x-auto">
            ${tableHtml}
        </div>

        <div class="mt-4 bg-slate-800 border border-slate-700 rounded-lg p-4">
            <h3 class="text-sm font-medium text-slate-300 mb-3 text-balance">Add Setting</h3>
            <div class="flex items-center gap-3">
                <input id="new-setting-key" type="text" placeholder="Key" class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500">
                <input id="new-setting-value" type="text" placeholder="Value" class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500">
                <button id="btn-add-setting" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition active:scale-[0.96]">Add</button>
            </div>
        </div>
    `;

    // Remote-access (network bind) card sits at the top of Advanced. It fetches
    // its own live state and manages its own loading/error/populated rendering.
    const raCard = document.getElementById('remote-access-card');
    if (raCard && typeof renderRemoteAccessCard === 'function') {
        renderRemoteAccessCard(raCard);
    }

    const browserCard = document.getElementById('oauth-browser-card');
    if (browserCard) {
        renderOAuthBrowserCard(browserCard);
    }

    bindAdvancedTabEvents();
}

// Where re-auth opens the Claude login. A dedicated profile per account is the
// point of the feature, so it is the default and the recommended option.
const OAUTH_BROWSER_MODES = [
    ['profile', 'Dedicated profile per account (recommended)'],
    ['incognito', 'Private window'],
    ['default', 'System default browser'],
];
const OAUTH_BROWSER_SELECT_CLASS = 'w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500';

function _oauthBrowserModeValue(settings) {
    const pairs = Array.isArray(settings)
        ? settings.map(s => [s.key, s.value])
        : Object.entries(settings || {});
    const hit = pairs.find(([key]) => key === 'oauth_browser_mode');
    const value = hit ? String(hit[1] == null ? '' : hit[1]) : '';
    return OAUTH_BROWSER_MODES.some(([mode]) => mode === value) ? value : 'profile';
}

// Built node by node rather than with innerHTML — same posture as the OAuth
// banner: no interpolated data ever reaches an HTML parser.
function _oauthBrowserText(tag, className, text) {
    const el = document.createElement(tag);
    el.className = className;
    el.textContent = text;
    return el;
}

function renderOAuthBrowserCard(card) {
    const current = _oauthBrowserModeValue(window.jackedState.settings);

    card.textContent = '';
    card.appendChild(_oauthBrowserText(
        'h3', 'text-sm font-medium text-slate-300 mb-3 text-balance', 'Re-auth browser'));
    card.appendChild(_oauthBrowserText(
        'p', 'text-xs text-slate-400 mb-3 text-pretty',
        'Where jacked opens the Claude login when you add or re-authenticate an account.'));

    const select = document.createElement('select');
    select.id = 'oauth-browser-mode';
    select.className = OAUTH_BROWSER_SELECT_CLASS;
    select.setAttribute('aria-label', 'Re-auth browser');
    OAUTH_BROWSER_MODES.forEach(([mode, label]) => {
        const option = document.createElement('option');
        option.value = mode;
        option.textContent = label;
        if (mode === current) option.selected = true;
        select.appendChild(option);
    });
    card.appendChild(select);

    card.appendChild(_oauthBrowserText(
        'p', 'text-xs text-slate-500 mt-2 text-pretty',
        'A dedicated profile keeps each account signed in to claude.ai on its own '
        + 'cookies, so re-auth is one click. Profiles live in '
        + '~/.claude/jacked-browser-profiles.'));

    select.addEventListener('change', async () => {
        const value = select.value;
        const match = OAUTH_BROWSER_MODES.find(([mode]) => mode === value);
        const label = match ? match[1] : value;
        try {
            await api.put('/api/settings/oauth_browser_mode', { value });
            _rememberOAuthBrowserMode(value);
            showToast('Re-auth browser: ' + label, 'success');
        } catch (e) {
            showToast('Could not save: ' + e.message, 'error');
        }
    });
}

// Keep the in-memory settings in step with the server so a re-render of the
// Advanced tab shows the mode that was just chosen.
function _rememberOAuthBrowserMode(value) {
    const settings = window.jackedState.settings;
    if (Array.isArray(settings)) {
        const row = settings.find(s => s.key === 'oauth_browser_mode');
        if (row) row.value = value;
        else settings.push({ key: 'oauth_browser_mode', value });
    } else if (settings && typeof settings === 'object') {
        settings.oauth_browser_mode = value;
    } else {
        window.jackedState.settings = { oauth_browser_mode: value };
    }
}

function bindAdvancedTabEvents() {
    // Show save button when value changes
    document.querySelectorAll('.setting-value-input').forEach(input => {
        input.addEventListener('input', () => {
            const row = input.closest('tr');
            const saveBtn = row.querySelector('.btn-save-setting');
            if (input.value !== input.dataset.original) {
                saveBtn.classList.remove('hidden');
            } else {
                saveBtn.classList.add('hidden');
            }
        });
    });

    // Save setting
    document.querySelectorAll('.btn-save-setting').forEach(btn => {
        btn.addEventListener('click', async () => {
            const key = btn.dataset.key;
            const row = document.querySelector(`tr[data-key="${key}"]`);
            const input = row.querySelector('.setting-value-input');
            const value = input.value;
            try {
                await api.put(`/api/settings/${encodeURIComponent(key)}`, { value });
                input.dataset.original = value;
                btn.classList.add('hidden');
                showToast(`Setting "${key}" saved`, 'success');
                await loadSettings();
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    });

    // Delete setting
    document.querySelectorAll('.btn-delete-setting').forEach(btn => {
        btn.addEventListener('click', async () => {
            const key = btn.dataset.key;
            try {
                await api.delete(`/api/settings/${encodeURIComponent(key)}`);
                showToast(`Setting "${key}" removed`, 'success');
                await loadSettings();
                renderSettingsTab('advanced');
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    });

    // Add setting
    const addBtn = document.getElementById('btn-add-setting');
    if (addBtn) {
        addBtn.addEventListener('click', async () => {
            const keyInput = document.getElementById('new-setting-key');
            const valInput = document.getElementById('new-setting-value');
            const key = keyInput.value.trim();
            const value = valInput.value.trim();
            if (!key) {
                showToast('Key is required', 'warning');
                return;
            }
            try {
                await api.put(`/api/settings/${encodeURIComponent(key)}`, { value });
                showToast(`Setting "${key}" added`, 'success');
                keyInput.value = '';
                valInput.value = '';
                await loadSettings();
                renderSettingsTab('advanced');
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    }
}

// --- Shared helpers ---

function renderSettingRow(key, value) {
    const displayVal = typeof value === 'object' ? JSON.stringify(value) : String(value);
    return `
        <tr data-key="${escapeHtml(key)}">
            <td class="font-mono text-sm">${escapeHtml(key)}</td>
            <td>
                <input type="text" class="setting-value-input bg-slate-900 border border-slate-700 rounded px-2 py-1 text-sm text-white w-full focus:outline-none focus:border-blue-500" value="${escapeHtml(displayVal)}" data-original="${escapeHtml(displayVal)}">
            </td>
            <td>
                <div class="flex items-center gap-1">
                    <button class="btn-save-setting text-xs px-2 py-1 text-blue-400 hover:text-blue-300 hover:bg-blue-900/30 rounded transition active:scale-[0.96] hidden" data-key="${escapeHtml(key)}" title="Save">Save</button>
                    <button class="btn-delete-setting text-xs px-2 py-1 text-red-400 hover:text-red-300 hover:bg-red-900/30 rounded transition-colors" data-key="${escapeHtml(key)}" title="Delete">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                    </button>
                </div>
            </td>
        </tr>
    `;
}

// Keys managed by a dedicated control on this same Advanced tab (the Remote
// access card). They are protected server-side, so editing them in the raw
// table only dead-ends in a 422; hide them here so the card is the single place
// they are changed. The card renders their live state directly above.
const RAW_TABLE_HIDDEN_KEYS = new Set(['remote_access_enabled', 'remote_access_scope', 'oauth_browser_mode']);

function settingsToEntries(settings) {
    if (!settings) return [];
    const pairs = Array.isArray(settings)
        ? settings.map(s => [s.key, s.value])
        : Object.entries(settings);
    return pairs
        .filter(([key]) => !RAW_TABLE_HIDDEN_KEYS.has(key))
        .sort((a, b) => a[0].localeCompare(b[0]));
}

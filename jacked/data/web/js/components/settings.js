/**
 * jacked web dashboard — settings component
 * Tabbed layout: Agents | Commands | Features | Plugins | Claude Code | Advanced
 */

const SETTINGS_TAB_KEY = 'jacked_settings_tab';
const DEFAULT_TAB = 'agents';

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
    container.querySelectorAll('.toggle-switch').forEach(toggle => {
        const input = toggle.querySelector('input');
        if (!input) return;
        input.addEventListener('change', async () => {
            const name = toggle.dataset.name;
            const category = toggle.dataset.category;
            const enabled = input.checked;

            toggle.classList.add('pending');
            input.disabled = true;

            try {
                await api.put(`/api/features/${encodeURIComponent(category)}/${encodeURIComponent(name)}`, { enabled });
                const displayName = toggle.closest('[data-feature-row]')?.dataset.displayName || name;
                showToast(`${displayName} ${enabled ? 'enabled' : 'disabled'}`, 'success');
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
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        bindToggleEvents(container);
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
            <p class="text-xs text-slate-500 mb-4 text-pretty">Slash commands installed to <code class="text-slate-300">~/.claude/commands/</code>. Use these with <code class="text-slate-300">/command-name</code> in Claude Code.</p>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        bindToggleEvents(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load commands: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('commands')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
    }
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

        const hookRows = hooks.map(h => `
            <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50" data-feature-row data-display-name="${escapeHtml(h.display_name)}">
                <div class="min-w-0 flex-1">
                    <div class="text-sm text-white">${escapeHtml(h.display_name)}</div>
                    <div class="text-xs text-slate-400">${escapeHtml(h.description || '')}</div>
                </div>
                ${renderToggle(h.name, 'hooks', h.installed, h.source_available)}
            </div>
        `).join('');

        const knowledgeRows = knowledge.map(k => {
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
        }).join('');

        container.innerHTML = `
            <div class="space-y-6">
                <div>
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Hooks</h3>
                    <p class="text-xs text-slate-500 mb-3">Background hooks that run automatically during Claude Code sessions.</p>
                    <div class="space-y-2">
                        ${hookRows}
                    </div>
                </div>

                <div>
                    <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Knowledge</h3>
                    <p class="text-xs text-slate-500 mb-3">Documents and rules that Claude reads for context and behavior. Installed to <code class="text-slate-300">~/.claude/</code>.</p>
                    <div class="space-y-2">
                        ${knowledgeRows}
                    </div>
                </div>
            </div>
        `;

        bindToggleEvents(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load features: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('features')" class="text-xs text-blue-400 hover:text-blue-300 transition active:scale-[0.96]">Retry</button>
            </div>
        `;
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

    bindAdvancedTabEvents();
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

function settingsToEntries(settings) {
    if (!settings) return [];
    if (Array.isArray(settings)) {
        return settings.map(s => [s.key, s.value]).sort((a, b) => a[0].localeCompare(b[0]));
    }
    return Object.entries(settings).sort((a, b) => a[0].localeCompare(b[0]));
}

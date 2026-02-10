/**
 * jacked web dashboard — settings component
 * Tabbed layout: Agents | Commands | Gatekeeper | Features | Plugins | Claude Code | Advanced
 */

const SETTINGS_TAB_KEY = 'jacked_settings_tab';
const DEFAULT_TAB = 'agents';

// --- Main render ---

function renderSettings(settings) {
    const savedTab = localStorage.getItem(SETTINGS_TAB_KEY) || DEFAULT_TAB;

    return `
        <div class="max-w-4xl">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Settings</h2>
            </div>

            <!-- Tab Bar -->
            <div class="flex gap-1 border-b border-slate-700 mb-6 overflow-x-auto">
                <button class="settings-tab ${savedTab === 'agents' ? 'active' : ''}" data-tab="agents">Agents</button>
                <button class="settings-tab ${savedTab === 'commands' ? 'active' : ''}" data-tab="commands">Commands</button>
                <button class="settings-tab ${savedTab === 'gatekeeper' ? 'active' : ''}" data-tab="gatekeeper">Gatekeeper</button>
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
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const tabName = tab.dataset.tab;
            localStorage.setItem(SETTINGS_TAB_KEY, tabName);
            renderSettingsTab(tabName);
        });
    });

    // Render initial tab
    const savedTab = localStorage.getItem(SETTINGS_TAB_KEY) || DEFAULT_TAB;
    renderSettingsTab(savedTab);
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
        case 'gatekeeper':
            await renderGatekeeperTab(container);
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
                showToast(`${name} ${enabled ? 'enabled' : 'disabled'}`, 'success');
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
                <div class="feature-card ${a.installed ? '' : 'disabled'}">
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
            <p class="text-xs text-slate-500 mb-4">Specialized agents installed to <code class="text-slate-300">~/.claude/agents/</code>. Toggle to enable or disable individual agents.</p>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        bindToggleEvents(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load agents: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('agents')" class="text-xs text-blue-400 hover:text-blue-300">Retry</button>
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
            <div class="feature-card ${c.installed ? '' : 'disabled'}">
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
            <p class="text-xs text-slate-500 mb-4">Slash commands installed to <code class="text-slate-300">~/.claude/commands/</code>. Use these with <code class="text-slate-300">/command-name</code> in Claude Code.</p>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        `;

        bindToggleEvents(container);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load commands: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('commands')" class="text-xs text-blue-400 hover:text-blue-300">Retry</button>
            </div>
        `;
    }
}

// --- Tab: Gatekeeper ---

async function renderGatekeeperTab(container) {
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="spinner"></div>
            <span class="ml-3 text-slate-400 text-sm">Loading gatekeeper config...</span>
        </div>
    `;

    try {
        const [config, features, pathSafety] = await Promise.all([
            api.get('/api/settings/gatekeeper'),
            loadFeatures(),
            api.get('/api/settings/gatekeeper/path-safety'),
        ]);

        const gkHook = (features.hooks || []).find(h => h.name === 'security_gatekeeper');
        const hookInstalled = gkHook ? gkHook.installed : false;

        container.innerHTML = renderGatekeeperContent(config, hookInstalled, pathSafety);
        bindGatekeeperEvents(config, hookInstalled, pathSafety);
    } catch (e) {
        container.innerHTML = `
            <div class="text-center py-12">
                <div class="text-red-400 text-sm mb-3">Failed to load gatekeeper config: ${escapeHtml(e.message)}</div>
                <button onclick="renderSettingsTab('gatekeeper')" class="text-xs text-blue-400 hover:text-blue-300">Retry</button>
            </div>
        `;
    }
}

function renderGatekeeperContent(config, hookInstalled, pathSafety) {
    // On/off banner
    const bannerClass = hookInstalled ? 'bg-green-900/30 border-green-700/40' : 'bg-yellow-900/20 border-yellow-700/40';
    const bannerIcon = hookInstalled
        ? '<span class="status-dot valid"></span>'
        : '<span class="status-dot disabled"></span>';
    const bannerText = hookInstalled
        ? 'Security Gatekeeper is <span class="text-green-400 font-medium">active</span>'
        : 'Security Gatekeeper is <span class="text-yellow-400 font-medium">disabled</span>';

    const configOpacity = hookInstalled ? '' : 'opacity-40 pointer-events-none';

    const models = [
        { value: 'haiku', label: 'Haiku', desc: 'Fastest (~0.5-1s), cheapest' },
        { value: 'sonnet', label: 'Sonnet', desc: 'Balanced (~1-2s), moderate cost' },
        { value: 'opus', label: 'Opus', desc: 'Best judgment (~2-5s), most expensive' },
    ];

    const methods = [
        { value: 'api_first', label: 'API First', desc: 'Try Anthropic API, fall back to local CLI' },
        { value: 'cli_first', label: 'CLI First', desc: 'Try local claude CLI, fall back to API' },
        { value: 'api_only', label: 'API Only', desc: 'Only use Anthropic API (needs API key)' },
        { value: 'cli_only', label: 'CLI Only', desc: 'Only use local claude CLI (no API key needed)' },
    ];

    const modelOptions = models.map(m =>
        `<option value="${m.value}" ${config.model === m.value ? 'selected' : ''}>
            ${m.label} \u2014 ${m.desc}
        </option>`
    ).join('');

    const methodRadios = methods.map(m =>
        `<label class="flex items-start gap-3 p-2 rounded hover:bg-slate-700/50 cursor-pointer transition-colors">
            <input type="radio" name="gk-eval-method" value="${m.value}"
                   ${config.eval_method === m.value ? 'checked' : ''}
                   class="mt-1 accent-blue-500">
            <div>
                <div class="text-sm text-white">${m.label}</div>
                <div class="text-xs text-slate-400">${m.desc}</div>
            </div>
        </label>`
    ).join('');

    const apiKeyStatus = config.api_key_set
        ? `<span class="inline-flex items-center gap-1 text-green-400 text-xs"><span class="status-dot active"></span>Set (source: ${config.api_key_source})</span>`
        : `<span class="inline-flex items-center gap-1 text-yellow-400 text-xs"><span class="status-dot warning"></span>Not set</span>`;

    const cliStatus = config.cli_available
        ? `<span class="inline-flex items-center gap-1 text-green-400 text-xs"><span class="status-dot active"></span>Available</span>`
        : `<span class="inline-flex items-center gap-1 text-red-400 text-xs"><span class="status-dot error"></span>Not found</span>`;

    return `
        <!-- Hook on/off banner -->
        <div class="flex items-center justify-between p-3 rounded-lg border mb-5 ${bannerClass}">
            <div class="flex items-center gap-2 text-sm">
                ${bannerIcon}
                <span class="text-slate-200">${bannerText}</span>
            </div>
            ${renderToggle('security_gatekeeper', 'hooks', hookInstalled, true)}
        </div>

        <div class="${configOpacity}" id="gk-config-body">
            <div class="space-y-5">
                <!-- Status Row -->
                <div class="flex items-center gap-6 text-sm bg-slate-900/50 rounded p-3">
                    <div class="flex items-center gap-2">
                        <span class="text-slate-400">API Key:</span> ${apiKeyStatus}
                    </div>
                    <div class="flex items-center gap-2">
                        <span class="text-slate-400">Claude CLI:</span> ${cliStatus}
                    </div>
                </div>

                <!-- Evaluation Pipeline -->
                <div>
                    <label class="block text-sm font-medium text-slate-300 mb-3">Evaluation Pipeline</label>
                    <p class="text-xs text-slate-500 mb-3">Every command runs through these tiers in order. The first tier that makes a decision wins.</p>
                    <div class="space-y-2 mb-4">
                        <div class="flex items-start gap-3 p-3 rounded bg-slate-900/50 border border-slate-700/50">
                            <span class="flex-shrink-0 w-6 h-6 rounded-full bg-green-800/60 text-green-300 text-xs font-bold flex items-center justify-center mt-0.5">0</span>
                            <div>
                                <div class="text-sm text-white">Deny Patterns</div>
                                <div class="text-xs text-slate-400">Blocks dangerous commands instantly (rm -rf, reverse shells, etc). <span class="text-green-400">Always on, ~0ms.</span></div>
                            </div>
                        </div>
                        <div class="flex items-start gap-3 p-3 rounded bg-orange-900/20 border border-orange-700/40">
                            <span class="flex-shrink-0 w-6 h-6 rounded-full bg-orange-800/60 text-orange-300 text-xs font-bold flex items-center justify-center mt-0.5">1</span>
                            <div>
                                <div class="text-sm text-white">Path Safety Rules <span class="text-xs text-orange-300 font-normal">(configurable below)</span></div>
                                <div class="text-xs text-slate-400">Blocks access to sensitive files (.env, SSH keys, etc) and paths outside your project. Also guards Read/Edit/Write/Grep tools. Runs before permission rules so broad wildcards don't auto-approve secrets. <span class="text-orange-400">Deterministic, ~0ms.</span></div>
                            </div>
                        </div>
                        <div class="flex items-start gap-3 p-3 rounded bg-slate-900/50 border border-slate-700/50">
                            <span class="flex-shrink-0 w-6 h-6 rounded-full bg-green-800/60 text-green-300 text-xs font-bold flex items-center justify-center mt-0.5">2</span>
                            <div>
                                <div class="text-sm text-white">Permission Rules</div>
                                <div class="text-xs text-slate-400">Checks your Claude Code permission rules for pre-approved commands. <span class="text-green-400">Always on, ~0ms.</span></div>
                            </div>
                        </div>
                        <div class="flex items-start gap-3 p-3 rounded bg-slate-900/50 border border-slate-700/50">
                            <span class="flex-shrink-0 w-6 h-6 rounded-full bg-green-800/60 text-green-300 text-xs font-bold flex items-center justify-center mt-0.5">3</span>
                            <div>
                                <div class="text-sm text-white">Local Pattern Matching</div>
                                <div class="text-xs text-slate-400">Recognizes common safe patterns (git status, ls, cat, etc) without an LLM call. <span class="text-green-400">Always on, ~0ms.</span></div>
                            </div>
                        </div>
                    </div>
                    <div class="flex items-start gap-3 p-3 rounded bg-blue-900/20 border border-blue-700/40">
                        <span class="flex-shrink-0 w-6 h-6 rounded-full bg-blue-800/60 text-blue-300 text-xs font-bold flex items-center justify-center mt-0.5">4</span>
                        <div class="flex-1">
                            <div class="text-sm text-white mb-1">LLM Evaluation <span class="text-xs text-blue-300 font-normal">(configurable below)</span></div>
                            <div class="text-xs text-slate-400">Commands that don't match any pattern above get sent to an LLM for judgment.</div>
                        </div>
                    </div>
                </div>

                <!-- Path Safety Config (collapsible) -->
                <div class="border-t border-slate-700 pt-4 mt-2">
                    ${renderPathSafetySection(pathSafety)}
                </div>

                <!-- LLM Model Selection -->
                <div>
                    <label class="block text-sm font-medium text-slate-300 mb-2">LLM Model</label>
                    <select id="gk-model" class="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500">
                        ${modelOptions}
                    </select>
                    <p class="mt-1 text-xs text-slate-500">Only affects Tier 4 (LLM evaluation). Most commands are resolved by earlier tiers and never hit the LLM.</p>
                </div>

                <!-- LLM Evaluation Method -->
                <div>
                    <label class="block text-sm font-medium text-slate-300 mb-2">LLM Evaluation Method</label>
                    <div class="space-y-1">
                        ${methodRadios}
                    </div>
                    <div class="mt-2 text-xs text-slate-500 space-y-1">
                        <div><span class="text-blue-300 font-medium">API</span> uses your Anthropic API key directly \u2014 fast, typically <span class="text-green-400">~2-3s</span> for Haiku.</div>
                        <div><span class="text-purple-300 font-medium">CLI</span> spawns a local <code class="text-slate-300">claude</code> process \u2014 no API key needed, but slower at <span class="text-yellow-400">~10s+</span> due to startup overhead.</div>
                    </div>
                    <div id="gk-method-warning" class="mt-2 text-xs text-yellow-400 hidden"></div>
                </div>

                <!-- API Key Override -->
                <div>
                    <label class="block text-sm font-medium text-slate-300 mb-2">API Key Override</label>
                    <div class="flex items-center gap-2">
                        <input id="gk-api-key" type="password" placeholder="Leave empty to use ANTHROPIC_API_KEY env var"
                               class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500">
                        <button id="btn-gk-toggle-key" class="px-3 py-2 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded transition-colors" title="Show/hide">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"/></svg>
                        </button>
                        <button id="btn-gk-test-key" class="px-3 py-2 text-xs bg-slate-700 hover:bg-slate-600 text-white rounded transition-colors">Test</button>
                    </div>
                    <div id="gk-key-test-result" class="mt-1 text-xs"></div>
                    <p class="mt-1 text-xs text-slate-500">Overrides the ANTHROPIC_API_KEY environment variable. Clear to revert to env var.</p>
                </div>

                <!-- Save Button -->
                <div class="flex items-center justify-between pt-2 border-t border-slate-700">
                    <button id="btn-gk-clear-key" class="text-xs text-red-400 hover:text-red-300 transition-colors ${config.api_key_source === 'db' ? '' : 'hidden'}">Clear stored API key</button>
                    <button id="btn-gk-save" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition-colors">Save Config</button>
                </div>

                <!-- Prompt Editor (collapsible) -->
                <div class="border-t border-slate-700 pt-4 mt-2">
                    <div id="prompt-editor-header" class="flex items-center justify-between cursor-pointer select-none">
                        <div class="flex items-center gap-3">
                            <span class="text-sm font-medium text-slate-300">Gatekeeper LLM Prompt</span>
                            <span id="prompt-source-badge" class="text-xs px-2 py-0.5 rounded-full bg-slate-700 text-slate-400">Loading...</span>
                        </div>
                        <svg id="prompt-chevron" class="w-5 h-5 text-slate-400 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                        </svg>
                    </div>
                    <div id="prompt-editor-content" class="hidden mt-4">
                        <div class="flex items-center justify-center py-6">
                            <div class="spinner"></div>
                            <span class="ml-3 text-slate-400 text-sm">Loading prompt...</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderPathSafetySection(pathSafety) {
    const enabled = pathSafety.enabled !== false;
    const disabledPatterns = pathSafety.disabled_patterns || [];
    const allowedPaths = pathSafety.allowed_paths || [];
    const rules = pathSafety.available_rules || {};
    const fileRules = rules.file_rules || {};
    const dirRules = rules.dir_rules || {};

    const watchedPaths = pathSafety.watched_paths || [];

    const sectionOpacity = enabled ? '' : 'opacity-40 pointer-events-none';

    const fileCheckboxes = Object.entries(fileRules).map(([key, rule]) => {
        const checked = !disabledPatterns.includes(key);
        return `
            <label class="flex items-start gap-3 p-2 rounded hover:bg-slate-700/30 cursor-pointer transition-colors">
                <input type="checkbox" class="ps-pattern-toggle mt-0.5 accent-orange-500" data-key="${escapeHtml(key)}" ${checked ? 'checked' : ''}>
                <div>
                    <div class="text-sm text-white">${escapeHtml(rule.label)}</div>
                    <div class="text-xs text-slate-400">${escapeHtml(rule.desc)}</div>
                </div>
            </label>
        `;
    }).join('');

    const dirCheckboxes = Object.entries(dirRules).map(([key, rule]) => {
        const checked = !disabledPatterns.includes(key);
        return `
            <label class="flex items-start gap-3 p-2 rounded hover:bg-slate-700/30 cursor-pointer transition-colors">
                <input type="checkbox" class="ps-pattern-toggle mt-0.5 accent-orange-500" data-key="${escapeHtml(key)}" ${checked ? 'checked' : ''}>
                <div>
                    <div class="text-sm text-white">${escapeHtml(rule.label)}</div>
                    <div class="text-xs text-slate-400">${escapeHtml(rule.desc)}</div>
                </div>
            </label>
        `;
    }).join('');

    const pathRows = allowedPaths.map((p, i) => `
        <div class="flex items-center gap-2 p-2 bg-slate-900/50 rounded border border-slate-700/50">
            <code class="text-xs text-slate-300 flex-1 truncate">${escapeHtml(p)}</code>
            <button class="ps-remove-path text-xs text-red-400 hover:text-red-300 px-1" data-index="${i}" title="Remove">&times;</button>
        </div>
    `).join('');

    return `
        <div class="flex items-center justify-between mb-3">
            <div class="flex items-center gap-3">
                <span class="text-sm font-medium text-slate-300">Path Safety Rules</span>
                <span id="ps-status-badge" class="text-xs px-2 py-0.5 rounded-full ${enabled ? 'bg-orange-900/60 text-orange-300' : 'bg-slate-700 text-slate-400'}">${enabled ? 'Active' : 'Disabled'}</span>
            </div>
            <label class="toggle-switch" id="ps-master-toggle">
                <input type="checkbox" ${enabled ? 'checked' : ''}>
                <span class="toggle-slider"></span>
            </label>
        </div>
        <p class="text-xs text-slate-500 mb-4">Blocks reads/writes to files that commonly contain secrets and access outside your project directory. Deterministic checks — no LLM needed.</p>

        <div id="ps-config-body" class="${sectionOpacity}">
            <!-- Sensitive File Patterns -->
            <div class="mb-4">
                <div id="ps-files-header" class="flex items-center justify-between cursor-pointer select-none mb-2">
                    <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Sensitive File Patterns</span>
                    <svg id="ps-files-chevron" class="w-4 h-4 text-slate-500 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                </div>
                <div id="ps-files-content" class="hidden space-y-0.5">
                    ${fileCheckboxes}
                </div>
            </div>

            <!-- Sensitive Directories -->
            <div class="mb-4">
                <div id="ps-dirs-header" class="flex items-center justify-between cursor-pointer select-none mb-2">
                    <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Sensitive Directories</span>
                    <svg id="ps-dirs-chevron" class="w-4 h-4 text-slate-500 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                </div>
                <div id="ps-dirs-content" class="hidden space-y-0.5">
                    ${dirCheckboxes}
                </div>
            </div>

            <!-- Watched Paths -->
            <div class="mb-4">
                <div id="ps-watched-header" class="flex items-center justify-between cursor-pointer select-none mb-2">
                    <span class="text-xs font-medium text-red-400 uppercase tracking-wider">Watched Paths (always require permission)</span>
                    <svg id="ps-watched-chevron" class="w-4 h-4 text-slate-500 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                </div>
                <div id="ps-watched-content" class="hidden">
                    <p class="text-[10px] text-slate-500 mb-2">Files under these paths will always trigger a permission prompt, even inside your project.</p>
                    <div id="ps-watched-list" class="space-y-1 mb-2">
                        ${watchedPaths.length > 0 ? watchedPaths.map((p, i) => `
                            <div class="flex items-center gap-2 p-2 bg-red-900/20 rounded border border-red-800/30">
                                <code class="text-xs text-red-300 flex-1 truncate">${escapeHtml(p)}</code>
                                <button class="ps-remove-watched text-xs text-red-400 hover:text-red-300 px-1" data-index="${i}" title="Remove">&times;</button>
                            </div>
                        `).join('') : '<div class="text-xs text-slate-500 italic p-2">No watched paths. Add paths to always require permission when accessed.</div>'}
                    </div>
                    <div class="flex items-center gap-2">
                        <input id="ps-new-watched" type="text" placeholder="e.g. C:/Users/jack/production-configs"
                               class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-1.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-red-500">
                        <button id="ps-add-watched" class="px-3 py-1.5 text-xs bg-slate-700 hover:bg-slate-600 text-white rounded transition-colors">+ Add</button>
                        <button id="ps-browse-watched" class="px-3 py-1.5 text-xs bg-slate-700 hover:bg-slate-600 text-white rounded transition-colors">Browse</button>
                    </div>
                    <div id="ps-watched-error" class="text-xs text-red-400 mt-1 hidden"></div>
                    <div id="ps-dir-browser" class="hidden mt-2 bg-slate-900 border border-slate-700 rounded-lg p-3"></div>
                </div>
            </div>

            <!-- Allowed Paths -->
            <div class="mb-4">
                <div id="ps-paths-header" class="flex items-center justify-between cursor-pointer select-none mb-2">
                    <span class="text-xs font-medium text-slate-400 uppercase tracking-wider">Allowed Paths (beyond project directory)</span>
                    <svg id="ps-paths-chevron" class="w-4 h-4 text-slate-500 transition-transform duration-200" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
                    </svg>
                </div>
                <div id="ps-paths-content" class="hidden">
                    <div id="ps-paths-list" class="space-y-1 mb-2">
                        ${pathRows || '<div class="text-xs text-slate-500 italic p-2">No extra paths configured. Only your project directory is allowed by default.</div>'}
                    </div>
                    <div class="flex items-center gap-2">
                        <input id="ps-new-path" type="text" placeholder="e.g. C:/Users/jack/.conda/envs"
                               class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-1.5 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-orange-500">
                        <button id="ps-add-path" class="px-3 py-1.5 text-xs bg-slate-700 hover:bg-slate-600 text-white rounded transition-colors">+ Add</button>
                    </div>
                </div>
            </div>

            <!-- Unsaved changes banner -->
            <div id="ps-unsaved-banner" class="hidden mb-3 p-3 bg-amber-900/30 border border-amber-600/50 rounded-lg flex items-center justify-between">
                <span class="text-xs text-amber-300 font-medium">You have unsaved changes</span>
                <div class="flex gap-2">
                    <button id="ps-banner-save" class="px-3 py-1 text-xs bg-amber-600 hover:bg-amber-500 text-white rounded transition-colors">Save Now</button>
                    <button id="ps-banner-discard" class="px-3 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded transition-colors">Discard</button>
                </div>
            </div>

            <!-- Save button -->
            <div class="flex items-center justify-end pt-2">
                <button id="btn-ps-save" class="px-4 py-2 bg-orange-600 hover:bg-orange-500 text-white text-sm rounded transition-colors">Save Path Rules</button>
            </div>
        </div>
    `;
}


function bindPathSafetyEvents(pathSafety) {
    // Store initial state for dirty tracking
    window._psInitialState = {
        enabled: pathSafety.enabled !== undefined ? pathSafety.enabled : true,
        disabled_patterns: [...(pathSafety.disabled_patterns || [])],
        allowed_paths: [...(pathSafety.allowed_paths || [])],
        watched_paths: [...(pathSafety.watched_paths || [])],
    };
    window._settingsDirty = false;

    // Master toggle — saves only enabled state (not unsaved checkbox changes)
    const masterToggle = document.getElementById('ps-master-toggle');
    if (masterToggle) {
        const input = masterToggle.querySelector('input');
        if (input) {
            let _togglePending = false;
            input.addEventListener('change', async () => {
                if (_togglePending) return;
                _togglePending = true;
                const enabled = input.checked;
                const configBody = document.getElementById('ps-config-body');
                if (configBody) {
                    configBody.classList.toggle('opacity-40', !enabled);
                    configBody.classList.toggle('pointer-events-none', !enabled);
                }
                const badge = document.getElementById('ps-status-badge');
                if (badge) {
                    badge.textContent = enabled ? 'Active' : 'Disabled';
                    badge.className = `text-xs px-2 py-0.5 rounded-full ${enabled ? 'bg-orange-900/60 text-orange-300' : 'bg-slate-700 text-slate-400'}`;
                }
                // Save only the enabled flag with the LAST SAVED state (not current UI state)
                try {
                    const saved = {
                        enabled,
                        disabled_patterns: window._psInitialState.disabled_patterns,
                        allowed_paths: window._psInitialState.allowed_paths,
                        watched_paths: window._psInitialState.watched_paths,
                    };
                    await api.put('/api/settings/gatekeeper/path-safety', saved);
                    showToast(`Path safety ${enabled ? 'enabled' : 'disabled'}`, 'success');
                } catch (e) {
                    input.checked = !enabled;
                    // Revert optimistic UI updates
                    if (configBody) {
                        configBody.classList.toggle('opacity-40', enabled);
                        configBody.classList.toggle('pointer-events-none', enabled);
                    }
                    if (badge) {
                        badge.textContent = !enabled ? 'Active' : 'Disabled';
                        badge.className = `text-xs px-2 py-0.5 rounded-full ${!enabled ? 'bg-orange-900/60 text-orange-300' : 'bg-slate-700 text-slate-400'}`;
                    }
                    showToast(e.message || 'Toggle failed', 'error');
                } finally {
                    _togglePending = false;
                }
            });
        }
    }

    // Collapsible sections
    _bindCollapsible('ps-files-header', 'ps-files-content', 'ps-files-chevron');
    _bindCollapsible('ps-dirs-header', 'ps-dirs-content', 'ps-dirs-chevron');
    _bindCollapsible('ps-watched-header', 'ps-watched-content', 'ps-watched-chevron');
    _bindCollapsible('ps-paths-header', 'ps-paths-content', 'ps-paths-chevron');

    // Track dirty state on pattern checkboxes
    document.querySelectorAll('.ps-pattern-toggle').forEach(cb => {
        cb.addEventListener('change', () => _updatePathSafetyDirtyState());
    });

    // Add path
    const addBtn = document.getElementById('ps-add-path');
    const pathInput = document.getElementById('ps-new-path');
    const _addPath = () => {
        const val = (pathInput ? pathInput.value : '').trim();
        if (!val) return;
        const list = document.getElementById('ps-paths-list');
        if (!list) return;

        // Client-side 20-path limit
        const currentPaths = list.querySelectorAll('code');
        if (currentPaths.length >= 20) {
            showToast('Maximum 20 allowed paths', 'warning');
            return;
        }

        // Duplicate detection
        const existingPaths = Array.from(currentPaths).map(el => el.textContent.trim());
        if (existingPaths.includes(val)) {
            showToast('Path already added', 'warning');
            return;
        }

        // Remove placeholder if present
        const placeholder = list.querySelector('.italic');
        if (placeholder) placeholder.remove();

        const idx = list.querySelectorAll('.ps-remove-path').length;
        const row = document.createElement('div');
        row.className = 'flex items-center gap-2 p-2 bg-slate-900/50 rounded border border-slate-700/50';
        row.innerHTML = `
            <code class="text-xs text-slate-300 flex-1 truncate">${escapeHtml(val)}</code>
            <button class="ps-remove-path text-xs text-red-400 hover:text-red-300 px-1" data-index="${idx}" title="Remove">&times;</button>
        `;
        list.appendChild(row);
        pathInput.value = '';
        _rebindRemovePathButtons();
        _updatePathSafetyDirtyState();
    };

    if (addBtn) addBtn.addEventListener('click', _addPath);
    if (pathInput) {
        pathInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') _addPath();
        });
    }

    // Remove path buttons (allowed paths)
    _rebindRemovePathButtons();

    // --- Watched paths ---
    const addWatchedBtn = document.getElementById('ps-add-watched');
    const watchedInput = document.getElementById('ps-new-watched');
    const watchedError = document.getElementById('ps-watched-error');

    const _showWatchedError = (msg) => {
        if (watchedError) {
            watchedError.textContent = msg;
            watchedError.className = 'text-xs text-red-400 mt-1';
        }
    };
    const _showWatchedWarning = (msg) => {
        if (watchedError) {
            watchedError.textContent = msg;
            watchedError.className = 'text-xs text-amber-400 mt-1';
        }
    };
    const _hideWatchedError = () => {
        if (watchedError) {
            watchedError.textContent = '';
            watchedError.className = 'text-xs text-red-400 mt-1 hidden';
        }
    };

    const _addWatchedPath = async () => {
        const val = (watchedInput ? watchedInput.value : '').trim();
        if (!val) return;
        _hideWatchedError();

        const list = document.getElementById('ps-watched-list');
        if (!list) return;

        // Client-side 20-path limit
        const currentPaths = list.querySelectorAll('code');
        if (currentPaths.length >= 20) {
            _showWatchedError('Maximum 20 watched paths');
            return;
        }

        // Duplicate detection
        const existingPaths = Array.from(currentPaths).map(el => el.textContent.trim());
        if (existingPaths.includes(val)) {
            _showWatchedError('Path already watched');
            return;
        }

        // Validate via API
        try {
            const res = await api.post('/api/settings/gatekeeper/validate-path', { path: val });
            if (!res.valid) {
                _showWatchedError(res.reason || 'Invalid path');
                return;
            }
            // Check resolved form for duplicates too
            if (res.resolved && existingPaths.includes(res.resolved)) {
                _showWatchedError('Path already watched (resolves to same location)');
                return;
            }
            _insertWatchedRow(list, res.resolved || val);
            watchedInput.value = '';

            // Auto-save watched paths only — use saved state for other fields to avoid
            // accidentally persisting unsaved pattern/allowed-path changes
            try {
                const freshWatched = [];
                document.querySelectorAll('#ps-watched-list code').forEach(el => {
                    const p = el.textContent.trim();
                    if (p) freshWatched.push(p);
                });
                const init = window._psInitialState || {};
                const saveState = {
                    enabled: init.enabled !== undefined ? init.enabled : true,
                    disabled_patterns: init.disabled_patterns || [],
                    allowed_paths: init.allowed_paths || [],
                    watched_paths: freshWatched,
                };
                await api.put('/api/settings/gatekeeper/path-safety', saveState);
                window._psInitialState.watched_paths = [...freshWatched];
                _updatePathSafetyDirtyState();
            } catch (saveErr) {
                showToast('Path added but failed to save — click Save Path Rules', 'error');
            }

            // Show warnings inline (persistent, not a vanishing toast)
            const warningParts = [];
            if (res.warning) warningParts.push(res.warning);
            const allowedCodes = document.querySelectorAll('#ps-paths-list code');
            const allowedPaths = Array.from(allowedCodes).map(el => el.textContent.trim());
            const resolvedPath = res.resolved || val;
            if (allowedPaths.some(ap => resolvedPath.toLowerCase() === ap.toLowerCase())) {
                warningParts.push('also in Allowed Paths');
            }
            if (warningParts.length > 0) {
                _showWatchedWarning(warningParts.join(' | '));
            }
        } catch (e) {
            _showWatchedError(e.message || 'Validation failed');
        }
    };

    if (addWatchedBtn) addWatchedBtn.addEventListener('click', _addWatchedPath);
    if (watchedInput) {
        watchedInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') _addWatchedPath();
        });
    }

    // Remove watched path buttons
    _rebindRemoveWatchedButtons();

    // Browse button
    const browseBtn = document.getElementById('ps-browse-watched');
    if (browseBtn) {
        browseBtn.addEventListener('click', () => {
            const browser = document.getElementById('ps-dir-browser');
            if (!browser) return;
            if (!browser.classList.contains('hidden')) {
                browser.classList.add('hidden');
                return;
            }
            // Start browsing from user home or the input value
            const startPath = (watchedInput ? watchedInput.value : '').trim() || '';
            _loadDirBrowser(startPath);
        });
    }

    // Save button
    const saveBtn = document.getElementById('btn-ps-save');
    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            try {
                saveBtn.disabled = true;
                saveBtn.textContent = 'Saving...';
                const state = _collectPathSafetyState();
                await api.put('/api/settings/gatekeeper/path-safety', state);
                // Update initial state to new saved values
                window._psInitialState = {
                    disabled_patterns: [...state.disabled_patterns],
                    allowed_paths: [...state.allowed_paths],
                    watched_paths: [...state.watched_paths],
                };
                _updatePathSafetyDirtyState();
                showToast('Path safety rules saved', 'success');
            } catch (e) {
                showToast(e.message || 'Save failed', 'error');
            } finally {
                saveBtn.disabled = false;
                _updatePathSafetyDirtyState();
            }
        });
    }

    // Banner "Save Now" button — triggers same save as main button
    const bannerSave = document.getElementById('ps-banner-save');
    if (bannerSave && saveBtn) {
        bannerSave.addEventListener('click', () => saveBtn.click());
    }

    // Banner "Discard" button — reload gatekeeper tab from server
    const bannerDiscard = document.getElementById('ps-banner-discard');
    if (bannerDiscard) {
        bannerDiscard.addEventListener('click', async () => {
            window._settingsDirty = false;
            await renderSettingsTab('gatekeeper');
        });
    }
}


function _bindCollapsible(headerId, contentId, chevronId) {
    const header = document.getElementById(headerId);
    const content = document.getElementById(contentId);
    const chevron = document.getElementById(chevronId);
    if (header && content) {
        header.addEventListener('click', () => {
            const isHidden = content.classList.toggle('hidden');
            if (chevron) chevron.style.transform = isHidden ? '' : 'rotate(180deg)';
        });
    }
}


function _rebindRemovePathButtons() {
    document.querySelectorAll('.ps-remove-path').forEach(btn => {
        btn.onclick = () => {
            const row = btn.closest('.flex.items-center');
            if (row) row.remove();
            // If list is empty, add placeholder back
            const list = document.getElementById('ps-paths-list');
            if (list && list.children.length === 0) {
                list.innerHTML = '<div class="text-xs text-slate-500 italic p-2">No extra paths configured. Only your project directory is allowed by default.</div>';
            }
            _updatePathSafetyDirtyState();
        };
    });
}


function _insertWatchedRow(list, path) {
    // Remove placeholder if present
    const placeholder = list.querySelector('.italic');
    if (placeholder) placeholder.remove();

    const idx = list.querySelectorAll('.ps-remove-watched').length;
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 p-2 bg-red-900/20 rounded border border-red-800/30';
    row.innerHTML = `
        <code class="text-xs text-red-300 flex-1 truncate">${escapeHtml(path)}</code>
        <button class="ps-remove-watched text-xs text-red-400 hover:text-red-300 px-1" data-index="${idx}" title="Remove">&times;</button>
    `;
    list.appendChild(row);
    _rebindRemoveWatchedButtons();
    _updatePathSafetyDirtyState();
}


function _rebindRemoveWatchedButtons() {
    document.querySelectorAll('.ps-remove-watched').forEach(btn => {
        btn.onclick = async () => {
            const row = btn.closest('.flex.items-center');
            if (row) row.remove();
            const list = document.getElementById('ps-watched-list');
            if (list && list.children.length === 0) {
                list.innerHTML = '<div class="text-xs text-slate-500 italic p-2">No watched paths. Add paths to always require permission when accessed.</div>';
            }
            // Auto-save watched paths only — preserve saved state for other fields
            try {
                const freshWatched = [];
                document.querySelectorAll('#ps-watched-list code').forEach(el => {
                    const p = el.textContent.trim();
                    if (p) freshWatched.push(p);
                });
                const init = window._psInitialState || {};
                const saveState = {
                    enabled: init.enabled !== undefined ? init.enabled : true,
                    disabled_patterns: init.disabled_patterns || [],
                    allowed_paths: init.allowed_paths || [],
                    watched_paths: freshWatched,
                };
                await api.put('/api/settings/gatekeeper/path-safety', saveState);
                window._psInitialState.watched_paths = [...freshWatched];
                _updatePathSafetyDirtyState();
            } catch (e) {
                showToast('Remove failed to save — click Save Path Rules', 'error');
            }
        };
    });
}


async function _loadDirBrowser(startPath) {
    const browser = document.getElementById('ps-dir-browser');
    if (!browser) return;

    browser.classList.remove('hidden');
    browser.innerHTML = '<div class="text-xs text-slate-400">Loading...</div>';

    try {
        const res = await api.post('/api/settings/gatekeeper/browse-path', { path: startPath || '' });
        const current = res.current || '';
        const parent = res.parent || '';
        const dirs = res.directories || [];

        let html = `
            <div class="flex items-center justify-between mb-2">
                <span class="text-xs text-slate-400 font-mono truncate flex-1" title="${escapeHtml(current)}">${escapeHtml(current)}</span>
                <button id="ps-dir-close" class="text-xs text-slate-500 hover:text-slate-300 px-1 ml-2" title="Close">&times;</button>
            </div>
        `;

        if (parent) {
            html += `<div class="ps-dir-entry flex items-center gap-1 px-2 py-1 rounded cursor-pointer hover:bg-slate-800 text-xs text-blue-400" data-path="${escapeHtml(parent)}">
                <span>&#8593;</span> <span>..</span>
            </div>`;
        }

        if (dirs.length === 0) {
            html += '<div class="text-xs text-slate-500 italic px-2 py-1">No subdirectories</div>';
        } else {
            for (const d of dirs) {
                const fullPath = current.replace(/\\/g, '/').replace(/\/$/, '') + '/' + d;
                html += `<div class="ps-dir-entry flex items-center gap-1 px-2 py-1 rounded cursor-pointer hover:bg-slate-800 text-xs text-slate-300" data-path="${escapeHtml(fullPath)}">
                    <span class="text-yellow-500">&#128193;</span> <span>${escapeHtml(d)}</span>
                </div>`;
            }
        }

        html += `<div class="mt-2 pt-2 border-t border-slate-700">
            <button id="ps-dir-select" class="px-3 py-1.5 text-xs bg-red-700 hover:bg-red-600 text-white rounded transition-colors" data-path="${escapeHtml(current)}">Select this folder</button>
        </div>`;

        browser.innerHTML = html;

        // Bind close button
        const closeBtn = document.getElementById('ps-dir-close');
        if (closeBtn) closeBtn.addEventListener('click', () => browser.classList.add('hidden'));

        // Bind directory entries for navigation
        browser.querySelectorAll('.ps-dir-entry').forEach(entry => {
            entry.addEventListener('click', () => {
                const navPath = entry.dataset.path;
                if (navPath) _loadDirBrowser(navPath);
            });
        });

        // Bind select button
        const selectBtn = document.getElementById('ps-dir-select');
        if (selectBtn) {
            selectBtn.addEventListener('click', () => {
                const selectedPath = selectBtn.dataset.path;
                const watchedInput = document.getElementById('ps-new-watched');
                if (watchedInput && selectedPath) {
                    watchedInput.value = selectedPath.replace(/\\/g, '/');
                }
                browser.classList.add('hidden');
            });
        }
    } catch (e) {
        browser.innerHTML = `<div class="text-xs text-red-400">${escapeHtml(e.message || 'Browse failed')}</div>
            <button id="ps-dir-close-err" class="text-xs text-slate-500 hover:text-slate-300 mt-1">Close</button>`;
        const closeBtn = document.getElementById('ps-dir-close-err');
        if (closeBtn) closeBtn.addEventListener('click', () => browser.classList.add('hidden'));
    }
}


function _updatePathSafetyDirtyState() {
    const current = _collectPathSafetyState();
    const initial = window._psInitialState || { disabled_patterns: [], allowed_paths: [], watched_paths: [] };

    const patternsChanged = JSON.stringify([...current.disabled_patterns].sort()) !== JSON.stringify([...initial.disabled_patterns].sort());
    const pathsChanged = JSON.stringify(current.allowed_paths) !== JSON.stringify(initial.allowed_paths);
    const watchedChanged = JSON.stringify(current.watched_paths) !== JSON.stringify(initial.watched_paths || []);
    const isDirty = patternsChanged || pathsChanged || watchedChanged;

    // Global flag for beforeunload and tab switch guards
    window._settingsDirty = isDirty;

    const saveBtn = document.getElementById('btn-ps-save');
    if (saveBtn) {
        saveBtn.textContent = isDirty ? 'Save Path Rules *' : 'Save Path Rules';
        saveBtn.classList.toggle('ring-2', isDirty);
        saveBtn.classList.toggle('ring-orange-400', isDirty);
    }

    // Show/hide unsaved changes banner
    const banner = document.getElementById('ps-unsaved-banner');
    if (banner) {
        banner.classList.toggle('hidden', !isDirty);
    }
}


function _collectPathSafetyState() {
    const masterInput = document.querySelector('#ps-master-toggle input');
    const enabled = masterInput ? masterInput.checked : true;

    // Collect disabled patterns (unchecked = disabled)
    const disabledPatterns = [];
    document.querySelectorAll('.ps-pattern-toggle').forEach(cb => {
        if (!cb.checked) {
            disabledPatterns.push(cb.dataset.key);
        }
    });

    // Collect allowed paths
    const allowedPaths = [];
    document.querySelectorAll('#ps-paths-list code').forEach(el => {
        const path = el.textContent.trim();
        if (path) allowedPaths.push(path);
    });

    // Collect watched paths
    const watchedPaths = [];
    document.querySelectorAll('#ps-watched-list code').forEach(el => {
        const path = el.textContent.trim();
        if (path) watchedPaths.push(path);
    });

    return { enabled, disabled_patterns: disabledPatterns, allowed_paths: allowedPaths, watched_paths: watchedPaths };
}


function bindGatekeeperEvents(config, hookInstalled, pathSafety) {
    // Hook toggle in banner
    const bannerToggle = document.querySelector('.toggle-switch[data-name="security_gatekeeper"][data-category="hooks"]');
    if (bannerToggle) {
        const input = bannerToggle.querySelector('input');
        if (input) {
            input.addEventListener('change', async () => {
                const enabled = input.checked;
                bannerToggle.classList.add('pending');
                input.disabled = true;
                try {
                    await api.put('/api/features/hooks/security_gatekeeper', { enabled });
                    showToast(`Security Gatekeeper ${enabled ? 'enabled' : 'disabled'}`, 'success');
                    await refreshFeatures();
                    await renderSettingsTab('gatekeeper');
                } catch (e) {
                    input.checked = !enabled;
                    showToast(e.message || 'Toggle failed', 'error');
                } finally {
                    bannerToggle.classList.remove('pending');
                    input.disabled = false;
                }
            });
        }
    }

    // Skip config bindings if hook is disabled
    if (!hookInstalled) {
        // Still load prompt badge
        _loadPromptBadge();
        return;
    }

    // Warn if API Only selected but no key
    const methodRadios = document.querySelectorAll('input[name="gk-eval-method"]');
    const warningEl = document.getElementById('gk-method-warning');
    methodRadios.forEach(radio => {
        radio.addEventListener('change', () => {
            if (radio.value === 'api_only' && !config.api_key_set) {
                warningEl.textContent = 'Warning: API Only requires an API key. Set one below or via ANTHROPIC_API_KEY env var.';
                warningEl.classList.remove('hidden');
            } else if (radio.value === 'cli_only' && !config.cli_available) {
                warningEl.textContent = 'Warning: CLI Only requires the claude CLI to be installed and in your PATH.';
                warningEl.classList.remove('hidden');
            } else {
                warningEl.classList.add('hidden');
            }
        });
    });

    // Toggle API key visibility
    const toggleBtn = document.getElementById('btn-gk-toggle-key');
    if (toggleBtn) {
        toggleBtn.addEventListener('click', () => {
            const input = document.getElementById('gk-api-key');
            input.type = input.type === 'password' ? 'text' : 'password';
        });
    }

    // Save config
    const saveBtn = document.getElementById('btn-gk-save');
    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            const model = document.getElementById('gk-model').value;
            const method = document.querySelector('input[name="gk-eval-method"]:checked')?.value || 'api_first';
            const apiKey = document.getElementById('gk-api-key').value.trim();

            const payload = { model, eval_method: method };
            if (apiKey) payload.api_key = apiKey;

            try {
                saveBtn.disabled = true;
                saveBtn.textContent = 'Saving...';
                await api.put('/api/settings/gatekeeper', payload);
                showToast('Gatekeeper config saved', 'success');
                await renderSettingsTab('gatekeeper');
                await loadSettings();
            } catch (e) {
                showToast(e.message, 'error');
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Config';
            }
        });
    }

    // Clear stored API key
    const clearBtn = document.getElementById('btn-gk-clear-key');
    if (clearBtn) {
        clearBtn.addEventListener('click', async () => {
            try {
                await api.put('/api/settings/gatekeeper', {
                    model: document.getElementById('gk-model').value,
                    eval_method: document.querySelector('input[name="gk-eval-method"]:checked')?.value || 'api_first',
                    api_key: '',
                });
                showToast('Stored API key cleared', 'success');
                await renderSettingsTab('gatekeeper');
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
    }

    // Test API key
    const testBtn = document.getElementById('btn-gk-test-key');
    if (testBtn) {
        testBtn.addEventListener('click', async () => {
            const resultEl = document.getElementById('gk-key-test-result');
            testBtn.disabled = true;
            testBtn.textContent = 'Testing...';
            resultEl.innerHTML = '';
            try {
                const result = await api.post('/api/settings/gatekeeper/test-api-key');
                resultEl.innerHTML = result.success
                    ? '<span class="text-green-400">API key works</span>'
                    : `<span class="text-red-400">${escapeHtml(result.error)}</span>`;
            } catch (e) {
                resultEl.innerHTML = `<span class="text-red-400">${escapeHtml(e.message)}</span>`;
            } finally {
                testBtn.disabled = false;
                testBtn.textContent = 'Test';
            }
        });
    }

    // Path safety events
    if (pathSafety) {
        bindPathSafetyEvents(pathSafety);
    }

    // Prompt editor collapsible
    _loadPromptBadge();
    const promptHeader = document.getElementById('prompt-editor-header');
    const promptContent = document.getElementById('prompt-editor-content');
    const promptChevron = document.getElementById('prompt-chevron');
    let promptLoaded = false;

    if (promptHeader) {
        promptHeader.addEventListener('click', () => {
            const isHidden = promptContent.classList.toggle('hidden');
            promptChevron.style.transform = isHidden ? '' : 'rotate(180deg)';
            if (!isHidden && !promptLoaded) {
                promptLoaded = true;
                loadPromptEditor();
            }
        });
    }
}

// --- Prompt Editor ---

async function _loadPromptBadge() {
    try {
        const data = await api.get('/api/settings/gatekeeper/prompt');
        const badge = document.getElementById('prompt-source-badge');
        if (badge) {
            const isCustom = data.source === 'custom';
            badge.textContent = isCustom ? 'Custom' : 'Built-in';
            badge.className = isCustom
                ? 'text-xs px-2 py-0.5 rounded-full bg-yellow-900/60 text-yellow-300'
                : 'text-xs px-2 py-0.5 rounded-full bg-green-900/60 text-green-300';
        }
    } catch (_) {
        const badge = document.getElementById('prompt-source-badge');
        if (badge) {
            badge.textContent = 'Unknown';
            badge.className = 'text-xs px-2 py-0.5 rounded-full bg-slate-700 text-slate-400';
        }
    }
}

async function loadPromptEditor() {
    const content = document.getElementById('prompt-editor-content');
    if (!content) return;

    try {
        const data = await api.get('/api/settings/gatekeeper/prompt');
        const isCustom = data.source === 'custom';

        content.innerHTML = `
            <p class="text-xs text-slate-500 mb-3">The prompt sent to the LLM during Tier 4 (Security Gatekeeper) evaluation. This controls how the gatekeeper judges whether a command is safe to auto-approve. Must contain all three placeholders.</p>
            <textarea id="prompt-textarea"
                class="w-full bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-slate-200 font-mono leading-relaxed focus:outline-none focus:border-blue-500 resize-y"
                rows="14" spellcheck="false">${escapeHtml(data.text)}</textarea>
            <div id="prompt-placeholders" class="flex items-center gap-4 mt-2 text-xs">
                <span id="ph-command" class="flex items-center gap-1"></span>
                <span id="ph-cwd" class="flex items-center gap-1"></span>
                <span id="ph-file_context" class="flex items-center gap-1"></span>
            </div>
            <div id="prompt-validation-error" class="mt-2 text-xs text-red-400 hidden"></div>
            <div class="flex items-center justify-between mt-3 pt-3 border-t border-slate-700">
                <button id="btn-prompt-reset" class="text-xs text-red-400 hover:text-red-300 transition-colors ${isCustom ? '' : 'hidden'}">Reset to Built-in</button>
                <div class="flex items-center gap-2">
                    <span id="prompt-save-status" class="text-xs text-slate-500"></span>
                    <button id="btn-prompt-save" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed" disabled>Save Prompt</button>
                </div>
            </div>
        `;

        content.dataset.defaultText = data.default_text;
        content.dataset.originalText = data.text;

        bindPromptEditorEvents();
        validatePromptPlaceholders();
    } catch (e) {
        content.innerHTML = `<div class="text-sm text-red-400 py-4">Failed to load prompt: ${escapeHtml(e.message)}</div>`;
    }
}

function validatePromptPlaceholders() {
    const textarea = document.getElementById('prompt-textarea');
    if (!textarea) return false;

    const text = textarea.value;
    const placeholders = [
        { id: 'ph-command', token: '{command}' },
        { id: 'ph-cwd', token: '{cwd}' },
        { id: 'ph-file_context', token: '{file_context}' },
    ];

    let allPresent = true;
    for (const p of placeholders) {
        const el = document.getElementById(p.id);
        if (!el) continue;
        const found = text.includes(p.token);
        if (!found) allPresent = false;
        el.innerHTML = found
            ? `<svg class="w-3.5 h-3.5 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"/></svg><code class="text-green-400">${p.token}</code>`
            : `<svg class="w-3.5 h-3.5 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg><code class="text-red-400">${p.token}</code>`;
    }

    const saveBtn = document.getElementById('btn-prompt-save');
    if (saveBtn) saveBtn.disabled = !allPresent;
    return allPresent;
}

function bindPromptEditorEvents() {
    const textarea = document.getElementById('prompt-textarea');
    const saveBtn = document.getElementById('btn-prompt-save');
    const resetBtn = document.getElementById('btn-prompt-reset');
    const content = document.getElementById('prompt-editor-content');
    const statusEl = document.getElementById('prompt-save-status');
    const errorEl = document.getElementById('prompt-validation-error');

    if (textarea) {
        textarea.addEventListener('input', () => {
            const valid = validatePromptPlaceholders();
            errorEl.classList.toggle('hidden', valid);
            if (!valid) errorEl.textContent = 'Save disabled \u2014 all three placeholders are required.';
            const changed = textarea.value !== content.dataset.originalText;
            if (saveBtn) saveBtn.disabled = !valid || !changed;
            if (statusEl) statusEl.textContent = changed ? 'Unsaved changes' : '';
        });
    }

    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            if (!validatePromptPlaceholders()) return;
            try {
                saveBtn.disabled = true;
                saveBtn.textContent = 'Saving...';
                errorEl.classList.add('hidden');
                await api.put('/api/settings/gatekeeper/prompt', { text: textarea.value });
                showToast('Gatekeeper prompt saved', 'success');
                content.dataset.originalText = textarea.value;
                statusEl.textContent = '';
                const badge = document.getElementById('prompt-source-badge');
                if (badge) {
                    badge.textContent = 'Custom';
                    badge.className = 'text-xs px-2 py-0.5 rounded-full bg-yellow-900/60 text-yellow-300';
                }
                if (resetBtn) resetBtn.classList.remove('hidden');
            } catch (e) {
                const msg = e.data?.error?.message || e.message;
                errorEl.textContent = msg;
                errorEl.classList.remove('hidden');
                showToast(msg, 'error');
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Prompt';
            }
        });
    }

    if (resetBtn) {
        resetBtn.addEventListener('click', async () => {
            try {
                await api.delete('/api/settings/gatekeeper/prompt');
                showToast('Prompt reset to built-in default', 'success');
                await loadPromptEditor();
                await _loadPromptBadge();
            } catch (e) {
                showToast(e.message, 'error');
            }
        });
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
        const hooks = (features.hooks || []).filter(h => h.name !== 'security_gatekeeper');
        const knowledge = features.knowledge || [];

        const hookRows = hooks.map(h => `
            <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50">
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
                <div class="flex items-center justify-between p-3 bg-slate-900/50 rounded border border-slate-700/50">
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
                    <p class="text-xs text-slate-500 mb-3">Background hooks that run automatically during Claude Code sessions. Security Gatekeeper is configured in the Gatekeeper tab.</p>
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
                <button onclick="renderSettingsTab('features')" class="text-xs text-blue-400 hover:text-blue-300">Retry</button>
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
                <button onclick="renderSettingsTab('plugins')" class="text-xs text-blue-400 hover:text-blue-300">Retry</button>
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
                    <input type="number" class="cc-key-number w-24 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white text-right focus:outline-none focus:border-blue-500"
                           data-key="${escapeHtml(pref.name)}" value="${escapeHtml(String(pref.value))}" data-default="${escapeHtml(String(pref.default))}">
                </div>
            `;
        }
        html += `</div></div>`;

        // --- Permissions ---
        html += _renderPermissionsSection(permissions);

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
                            <button id="btn-raw-save" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition-colors disabled:opacity-40 disabled:cursor-not-allowed" disabled>Save</button>
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
                <button onclick="renderSettingsTab('claude-code')" class="text-xs text-blue-400 hover:text-blue-300">Retry</button>
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
            <input type="number" class="cc-env-number w-28 bg-slate-900 border border-slate-600 rounded px-2 py-1 text-sm text-white text-right focus:outline-none focus:border-blue-500"
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

function _renderPermissionsSection(permissions) {
    const modeOptions = ['default', 'plan', 'bypassPermissions', 'acceptEdits'].map(m =>
        `<option value="${m}" ${permissions.defaultMode === m ? 'selected' : ''}>${m}</option>`
    ).join('');

    function renderList(label, items, listName) {
        const itemsHtml = items.length > 0
            ? items.map((rule, i) => `
                <div class="flex items-center justify-between py-1.5 px-2 bg-slate-800 rounded text-sm group">
                    <code class="text-slate-200 text-xs font-mono truncate">${escapeHtml(rule)}</code>
                    <button class="perm-remove-btn opacity-0 group-hover:opacity-100 text-red-400 hover:text-red-300 transition-all ml-2 flex-shrink-0"
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
                    <button class="perm-add-btn px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-white rounded transition-colors" data-list="${listName}">Add</button>
                </div>
            </div>
        `;
    }

    return `
        <div class="mb-6">
            <h3 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Permissions</h3>
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
            ${renderList('Allow', permissions.allow, 'allow')}
            ${renderList('Deny', permissions.deny, 'deny')}
            ${renderList('Ask', permissions.ask, 'ask')}
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

    // Permissions — default mode dropdown
    const modeSelect = document.getElementById('perm-default-mode');
    if (modeSelect) {
        modeSelect.addEventListener('change', async () => {
            try {
                await api.put('/api/claude-settings/permissions', { defaultMode: modeSelect.value });
                showToast(`Default mode set to "${modeSelect.value}". Restart Claude Code.`, 'warning');
                await refreshClaudeSettings();
            } catch (e) {
                showToast(e.message || 'Save failed', 'error');
            }
        });
    }

    // Permissions — remove rule
    container.querySelectorAll('.perm-remove-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const listName = btn.dataset.list;
            const index = parseInt(btn.dataset.index);
            try {
                const current = await loadClaudeSettings();
                const perms = { ...current.permissions };
                const arr = [...(perms[listName] || [])];
                arr.splice(index, 1);
                perms[listName] = arr;
                await api.put('/api/claude-settings/permissions', perms);
                showToast(`Rule removed. Restart Claude Code.`, 'warning');
                await refreshClaudeSettings();
                await renderClaudeCodeTab(container);
            } catch (e) {
                showToast(e.message || 'Remove failed', 'error');
            }
        });
    });

    // Permissions — add rule
    container.querySelectorAll('.perm-add-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const listName = btn.dataset.list;
            const input = container.querySelector(`.perm-add-input[data-list="${listName}"]`);
            const rule = input ? input.value.trim() : '';
            if (!rule) return;
            try {
                const current = await loadClaudeSettings();
                const perms = { ...current.permissions };
                const arr = [...(perms[listName] || [])];
                arr.push(rule);
                perms[listName] = arr;
                await api.put('/api/claude-settings/permissions', perms);
                showToast(`Rule added. Restart Claude Code.`, 'warning');
                await refreshClaudeSettings();
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
        <div class="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden">
            ${tableHtml}
        </div>

        <div class="mt-4 bg-slate-800 border border-slate-700 rounded-lg p-4">
            <h3 class="text-sm font-medium text-slate-300 mb-3">Add Setting</h3>
            <div class="flex items-center gap-3">
                <input id="new-setting-key" type="text" placeholder="Key" class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500">
                <input id="new-setting-value" type="text" placeholder="Value" class="flex-1 bg-slate-900 border border-slate-600 rounded px-3 py-2 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-blue-500">
                <button id="btn-add-setting" class="px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm rounded transition-colors">Add</button>
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
                    <button class="btn-save-setting text-xs px-2 py-1 text-blue-400 hover:text-blue-300 hover:bg-blue-900/30 rounded transition-colors hidden" data-key="${escapeHtml(key)}" title="Save">Save</button>
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

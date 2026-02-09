/**
 * jacked web dashboard — installations component
 * Global installation hero card + per-project activity cards.
 */

/**
 * Render the installations page shell with loading spinner.
 * Data loads async after mount via bindInstallationEvents().
 */
function renderInstallations() {
    return `
        <div class="max-w-4xl" id="installations-root">
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Installations</h2>
            </div>
            <div class="flex items-center justify-center py-16">
                <div class="spinner mr-3"></div>
                <span class="text-slate-400">Loading installation data...</span>
            </div>
        </div>
    `;
}

/**
 * Fetch data and render the full page.
 */
async function loadInstallationsData() {
    try {
        const data = await api.get('/api/installations/overview');
        const root = document.getElementById('installations-root');
        if (!root) return;

        let html = `
            <div class="flex items-center justify-between mb-5">
                <h2 class="text-xl font-semibold text-white">Installations</h2>
            </div>
        `;

        html += renderGlobalInstallationCard(data.global_install);
        html += renderProjectActivity(data.projects, data.total_projects);

        root.innerHTML = html;
    } catch (e) {
        console.error('Failed to load installations overview:', e);
        const root = document.getElementById('installations-root');
        if (root) {
            root.innerHTML = `
                <div class="flex items-center justify-between mb-5">
                    <h2 class="text-xl font-semibold text-white">Installations</h2>
                </div>
                <div class="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-300 text-sm">
                    Failed to load installation data. Is the dashboard API running?
                </div>
            `;
        }
    }
}

/**
 * Render the global installation hero card.
 */
function renderGlobalInstallationCard(gi) {
    const agentsInstalled = gi.agents.filter(a => a.installed).length;
    const agentsTotal = gi.agents.length;
    const cmdsInstalled = gi.commands.filter(c => c.installed).length;
    const cmdsTotal = gi.commands.length;

    const hookChips = gi.hooks.map(h => {
        const color = h.installed ? 'bg-green-900/50 text-green-300 border-green-700' : 'bg-slate-800 text-slate-500 border-slate-700';
        const icon = h.installed ? '&#10003;' : '&#10005;';
        return `<span class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border ${color}">${icon} ${escapeHtml(h.display_name)}</span>`;
    }).join(' ');

    const knowledgeChips = gi.knowledge.map(k => {
        const color = k.installed ? 'bg-blue-900/50 text-blue-300 border-blue-700' : 'bg-slate-800 text-slate-500 border-slate-700';
        const icon = k.installed ? '&#10003;' : '&#10005;';
        return `<span class="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded border ${color}">${icon} ${escapeHtml(k.display_name)}</span>`;
    }).join(' ');

    return `
        <div class="bg-gradient-to-r from-indigo-900/40 to-purple-900/40 border border-indigo-700/50 rounded-lg p-5 mb-6">
            <div class="flex items-center justify-between mb-3">
                <div class="flex items-center gap-3">
                    <div class="w-10 h-10 rounded-lg bg-indigo-600/30 flex items-center justify-center">
                        <svg class="w-5 h-5 text-indigo-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
                    </div>
                    <div>
                        <h3 class="text-lg font-semibold text-white">jacked v${escapeHtml(gi.version)}</h3>
                        <p class="text-xs text-slate-400">Global installation</p>
                    </div>
                </div>
            </div>

            <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mt-4">
                <div>
                    <div class="text-xs text-slate-400 mb-1">Agents</div>
                    <div class="text-lg font-bold text-white">${agentsInstalled}<span class="text-sm text-slate-500">/${agentsTotal}</span></div>
                </div>
                <div>
                    <div class="text-xs text-slate-400 mb-1">Commands</div>
                    <div class="text-lg font-bold text-white">${cmdsInstalled}<span class="text-sm text-slate-500">/${cmdsTotal}</span></div>
                </div>
                <div>
                    <div class="text-xs text-slate-400 mb-1">Hooks</div>
                    <div class="mt-1 flex flex-wrap gap-1">${hookChips}</div>
                </div>
                <div>
                    <div class="text-xs text-slate-400 mb-1">Knowledge</div>
                    <div class="mt-1 flex flex-wrap gap-1">${knowledgeChips}</div>
                </div>
            </div>
        </div>
    `;
}

/**
 * Render project activity section.
 */
function renderProjectActivity(projects, total) {
    if (!projects || projects.length === 0) {
        return `
            <div class="mb-4">
                <h3 class="text-base font-semibold text-white mb-3">Project Activity</h3>
                <div class="flex flex-col items-center justify-center py-12 px-8 bg-slate-800/50 rounded-lg border border-slate-700/50">
                    <div class="w-12 h-12 rounded-full bg-slate-800 flex items-center justify-center mb-3">
                        <svg class="w-6 h-6 text-slate-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>
                    </div>
                    <h4 class="text-sm font-medium text-white mb-1">No project activity yet</h4>
                    <p class="text-xs text-slate-400 text-center">Activity will appear here as you use Claude Code with jacked installed.<br>The security gatekeeper, commands, and hooks all log their activity.</p>
                </div>
            </div>
        `;
    }

    const cardsHtml = projects.map(renderProjectCard).join('');

    return `
        <div class="mb-4">
            <div class="flex items-center justify-between mb-3">
                <h3 class="text-base font-semibold text-white">Project Activity</h3>
                <span class="text-xs text-slate-500">${total} project${total !== 1 ? 's' : ''}</span>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                ${cardsHtml}
            </div>
        </div>
    `;
}

/**
 * Render a single project activity card.
 * Only shows stats with non-zero values.
 */
function renderProjectCard(project) {
    const name = escapeHtml(project.repo_name || 'unknown');
    const fullPath = escapeHtml(project.repo_path || '');
    const lastActivity = project.last_activity ? timeAgo(project.last_activity) : 'unknown';

    let statsHtml = '';

    if (project.gatekeeper_decisions > 0) {
        const approvalRate = project.gatekeeper_allowed > 0
            ? ((project.gatekeeper_allowed / project.gatekeeper_decisions) * 100).toFixed(1)
            : '0.0';
        statsHtml += `
            <div class="flex items-center justify-between text-xs">
                <span class="text-slate-400">Gatekeeper</span>
                <span class="text-white">${project.gatekeeper_decisions.toLocaleString()} <span class="text-slate-500">(${approvalRate}% approved)</span></span>
            </div>
        `;
    }

    if (project.commands_run > 0) {
        statsHtml += `
            <div class="flex items-center justify-between text-xs">
                <span class="text-slate-400">Commands</span>
                <span class="text-white">${project.commands_run.toLocaleString()}</span>
            </div>
        `;
    }

    if (project.hook_executions > 0) {
        statsHtml += `
            <div class="flex items-center justify-between text-xs">
                <span class="text-slate-400">Hook runs</span>
                <span class="text-white">${project.hook_executions.toLocaleString()}</span>
            </div>
        `;
    }

    if (project.unique_sessions > 0) {
        statsHtml += `
            <div class="flex items-center justify-between text-xs">
                <span class="text-slate-400">Sessions</span>
                <span class="text-white">${project.unique_sessions.toLocaleString()}</span>
            </div>
        `;
    }

    // Fallback if somehow all stats are zero (shouldn't happen given the query)
    if (!statsHtml) {
        statsHtml = '<div class="text-xs text-slate-500">No detailed stats yet</div>';
    }

    return `
        <div class="bg-slate-800 border border-slate-700 rounded-lg p-4 card-hover">
            <div class="flex items-start justify-between mb-3">
                <div>
                    <h4 class="font-medium text-white text-sm">${name}</h4>
                    <p class="text-[10px] text-slate-600 font-mono mt-0.5 truncate max-w-[250px]" title="${fullPath}">${fullPath}</p>
                </div>
                <span class="text-[10px] text-slate-500 whitespace-nowrap ml-2">Last: ${lastActivity}</span>
            </div>
            <div class="flex flex-col gap-1.5">
                ${statsHtml}
            </div>
        </div>
    `;
}

/**
 * Bind installation events — triggers async data load.
 */
function bindInstallationEvents() {
    loadInstallationsData();
}

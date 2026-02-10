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

    // Setup warning/status badges — use data attributes (not inline onclick) to prevent XSS via repo_path
    let warningBadges = '';
    if (project.has_guardrails) {
        const gFile = project.guardrails_file ? escapeHtml(project.guardrails_file) : 'JACKED_GUARDRAILS.md';
        warningBadges += `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-400 border border-green-700/50" title="${gFile}">Guardrails</span>`;
    } else {
        warningBadges += `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-yellow-900/40 text-yellow-400 border border-yellow-700/50 cursor-pointer jacked-init-guardrails" data-repo="${escapeHtml(project.repo_path)}" title="Click to create JACKED_GUARDRAILS.md">No Guardrails</span>`;
    }
    if (project.has_lint_hook) {
        warningBadges += `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-green-900/40 text-green-400 border border-green-700/50" title="pre-push lint hook installed">Lint Hook</span>`;
    } else {
        warningBadges += `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-orange-900/40 text-orange-400 border border-orange-700/50 cursor-pointer jacked-init-lint-hook" data-repo="${escapeHtml(project.repo_path)}" title="Click to install pre-push lint hook">No Lint Hook</span>`;
    }
    if (project.detected_language) {
        warningBadges += `<span class="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded bg-slate-700/50 text-slate-400">${escapeHtml(project.detected_language)}</span>`;
    }

    // Lessons badge
    if (project.has_lessons && project.lessons_count > 0) {
        warningBadges += `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-purple-900/40 text-purple-400 border border-purple-700/50 cursor-pointer jacked-toggle-lessons" data-repo="${escapeHtml(project.repo_path)}" title="Click to view/edit lessons">Lessons (${project.lessons_count})</span>`;
    } else {
        warningBadges += `<span class="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-500 border border-slate-700">No Lessons</span>`;
    }

    const badgesRow = warningBadges ? `<div class="flex flex-wrap gap-1 mb-2">${warningBadges}</div>` : '';
    const cardId = 'lessons-panel-' + _repoToId(project.repo_path);

    return `
        <div class="bg-slate-800 border border-slate-700 rounded-lg p-4 card-hover">
            <div class="flex items-start justify-between mb-2">
                <div>
                    <h4 class="font-medium text-white text-sm">${name}</h4>
                    <p class="text-[10px] text-slate-600 font-mono mt-0.5 truncate max-w-[250px]" title="${fullPath}">${fullPath}</p>
                </div>
                <span class="text-[10px] text-slate-500 whitespace-nowrap ml-2">Last: ${lastActivity}</span>
            </div>
            ${badgesRow}
            <div class="flex flex-col gap-1.5">
                ${statsHtml}
            </div>
            <div id="${cardId}" class="hidden mt-3"></div>
        </div>
    `;
}

/**
 * One-click: create JACKED_GUARDRAILS.md for a project.
 */
async function _initGuardrails(repoPath) {
    try {
        const result = await api.post('/api/project/guardrails-init', { repo_path: repoPath });
        if (result.created) {
            showToast('Guardrails created: ' + (result.language || 'base'), 'success');
        } else {
            showToast('Guardrails: ' + (result.reason || 'unknown error'), 'warning');
        }
        loadInstallationsData();
    } catch (e) {
        showToast('Failed to create guardrails: ' + e.message, 'error');
    }
}

/**
 * One-click: install pre-push lint hook for a project.
 */
async function _initLintHook(repoPath) {
    try {
        const result = await api.post('/api/project/lint-hook-init', { repo_path: repoPath });
        if (result.installed) {
            showToast('Lint hook installed: ' + (result.language || '?'), 'success');
        } else {
            showToast('Lint hook: ' + (result.reason || 'unknown error'), 'warning');
        }
        loadInstallationsData();
    } catch (e) {
        showToast('Failed to install lint hook: ' + e.message, 'error');
    }
}

/**
 * Convert repo_path to a safe DOM id suffix.
 */
function _repoToId(repoPath) {
    return repoPath.replace(/[^a-zA-Z0-9]/g, '_');
}

/**
 * Toggle lessons panel for a project card.
 */
async function _toggleLessonsPanel(repoPath) {
    const panelId = 'lessons-panel-' + _repoToId(repoPath);
    const panel = document.getElementById(panelId);
    if (!panel) return;

    // Toggle visibility
    if (!panel.classList.contains('hidden')) {
        panel.classList.add('hidden');
        panel.innerHTML = '';
        panel._loading = false;
        return;
    }

    // Prevent double-click race
    if (panel._loading) return;
    panel._loading = true;

    panel.classList.remove('hidden');
    panel.innerHTML = '<div class="flex items-center gap-2 py-2"><div class="spinner" style="width:14px;height:14px;border-width:2px"></div><span class="text-xs text-slate-400">Loading lessons...</span></div>';

    try {
        const data = await api.get('/api/project/lessons?repo_path=' + encodeURIComponent(repoPath));
        if (!data.exists || !data.lessons || data.lessons.length === 0) {
            panel.innerHTML = '<div class="text-xs text-slate-500 py-2">No lessons found</div>';
            return;
        }
        _renderLessonsEditor(panel, repoPath, data.lessons);
    } catch (e) {
        panel.innerHTML = `<div class="text-xs text-red-400 py-2">Failed to load: ${escapeHtml(e.message)}</div>`;
    } finally {
        panel._loading = false;
    }
}

/**
 * Render the inline lessons editor inside a panel element.
 */
function _renderLessonsEditor(panel, repoPath, lessons) {
    let html = '<div class="border-t border-slate-700 pt-2 space-y-1.5">';
    html += '<div class="flex items-center justify-between mb-1"><span class="text-[10px] text-slate-400 uppercase tracking-wider font-semibold">Lessons</span></div>';

    lessons.forEach((lesson, i) => {
        const strikeColor = lesson.strike >= 3 ? 'text-red-400 bg-red-900/30 border-red-700/50'
            : lesson.strike >= 2 ? 'text-yellow-400 bg-yellow-900/30 border-yellow-700/50'
            : 'text-slate-400 bg-slate-700/50 border-slate-600/50';
        html += `
            <div class="flex items-start gap-2 group lesson-row" data-lesson-idx="${i}">
                <span class="inline-flex items-center text-[10px] px-1.5 py-0.5 rounded border ${strikeColor} mt-0.5 shrink-0">${lesson.strike}x</span>
                <textarea class="lesson-text flex-1 bg-slate-900/50 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200 resize-none outline-none focus:border-purple-600 transition-colors" rows="1" data-idx="${i}">${escapeHtml(lesson.text)}</textarea>
                <button class="lesson-delete text-slate-600 hover:text-red-400 transition-colors mt-0.5 shrink-0" data-idx="${i}" title="Delete lesson">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg>
                </button>
            </div>
        `;
    });

    html += `
        <div class="flex items-center gap-2 pt-1">
            <button class="lessons-save text-[10px] px-2.5 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white transition-colors" data-repo="${escapeHtml(repoPath)}">Save</button>
            <span class="lessons-status text-[10px] text-slate-500"></span>
        </div>
    </div>`;

    panel.innerHTML = html;

    // Store lessons data on the panel for save
    panel._lessonsData = lessons.map(l => ({ ...l }));
    panel._repoPath = repoPath;

    // Auto-resize textareas and prevent newlines
    panel.querySelectorAll('.lesson-text').forEach(ta => {
        ta.style.height = 'auto';
        ta.style.height = ta.scrollHeight + 'px';
        ta.addEventListener('input', () => {
            ta.value = ta.value.replace(/\n/g, ' ');
            ta.style.height = 'auto';
            ta.style.height = ta.scrollHeight + 'px';
        });
        ta.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') e.preventDefault();
        });
    });
}

/**
 * Save lessons from the inline editor.
 */
async function _saveLessons(panel) {
    const repoPath = panel._repoPath;
    if (!repoPath) return;

    const rows = panel.querySelectorAll('.lesson-row');
    const lessons = [];
    rows.forEach(row => {
        const ta = row.querySelector('.lesson-text');
        const idx = parseInt(row.dataset.lessonIdx);
        const original = panel._lessonsData ? panel._lessonsData[idx] : null;
        if (ta && original && ta.value.trim()) {
            lessons.push({ index: original.index, strike: original.strike, text: ta.value.trim() });
        }
    });

    const statusEl = panel.querySelector('.lessons-status');
    const saveBtn = panel.querySelector('.lessons-save');
    if (saveBtn) saveBtn.disabled = true;
    if (statusEl) statusEl.textContent = 'Saving...';

    try {
        await api.put('/api/project/lessons', { repo_path: repoPath, lessons });
        if (statusEl) statusEl.textContent = 'Saved';
        showToast('Lessons saved', 'success');
        // Refresh to update badge count
        setTimeout(() => loadInstallationsData(), 500);
    } catch (e) {
        if (statusEl) statusEl.textContent = 'Error: ' + e.message;
        showToast('Save failed: ' + e.message, 'error');
    } finally {
        if (saveBtn) saveBtn.disabled = false;
    }
}

/**
 * Delete a lesson row from the editor (removes from DOM, save persists it).
 */
function _deleteLessonRow(row) {
    const panel = row.closest('[id^="lessons-panel-"]');
    if (panel && panel._lessonsData) {
        const idx = parseInt(row.dataset.lessonIdx);
        panel._lessonsData[idx] = null;
    }
    row.remove();
}

/**
 * Attach click handlers to warning badges via event delegation (prevents XSS).
 */
function _bindBadgeClicks(root) {
    root.addEventListener('click', function(e) {
        const guardrailsBtn = e.target.closest('.jacked-init-guardrails');
        if (guardrailsBtn) {
            _initGuardrails(guardrailsBtn.dataset.repo);
            return;
        }
        const lintBtn = e.target.closest('.jacked-init-lint-hook');
        if (lintBtn) {
            _initLintHook(lintBtn.dataset.repo);
            return;
        }
        const lessonsBtn = e.target.closest('.jacked-toggle-lessons');
        if (lessonsBtn) {
            _toggleLessonsPanel(lessonsBtn.dataset.repo);
            return;
        }
        const saveBtn = e.target.closest('.lessons-save');
        if (saveBtn) {
            const panel = saveBtn.closest('[id^="lessons-panel-"]');
            if (panel) _saveLessons(panel);
            return;
        }
        const deleteBtn = e.target.closest('.lesson-delete');
        if (deleteBtn) {
            const row = deleteBtn.closest('.lesson-row');
            if (row) _deleteLessonRow(row);
            return;
        }
    });
}

/**
 * Bind installation events — triggers async data load.
 */
function bindInstallationEvents() {
    const root = document.getElementById('installations-root');
    if (root) _bindBadgeClicks(root);
    loadInstallationsData();
}

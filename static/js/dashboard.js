/* TodoNess Dashboard — vanilla JS */

// ── State ──────────────────────────────────────────────────────────────
var tasks = [];
var selectedTaskId = null;
var ws = null;
var reconnectTimer = null;
var openDropdownId = null;
var searchQuery = '';
var _quickFilterActive = false;
var _resolvedFilterActive = false;  // suggestion section: show only "assessed done"
var _personFilter = '';  // empty = no filter, else person name
var _collapsedBeforeFilter = [];  // sections that were collapsed before person filter was applied
var lastSyncTime = null;
var _skillPollTimer = null;
var _runningSkills = {};
var _loadedSections = {};
var _detailSplitResizeObserver = null;
var DETAIL_EVIDENCE_STORAGE_KEY = 'todoness-evidence-width';
var DETAIL_EVIDENCE_MIN = 25;
var DETAIL_EVIDENCE_MAX = 65;
var DETAIL_EVIDENCE_MIN_PX = 260;
var DETAIL_WORKSPACE_MIN_PX = 320;
var TERMINAL_SECTIONS = ['completed', 'dismissed', 'deleted'];

// ── Valid Transitions (mirrors src/models.py VALID_TRANSITIONS) ────────
var VALID_TRANSITIONS = {
    suggested: ['active', 'waiting', 'snoozed', 'dismissed', 'deleted'],
    active: ['in_progress', 'waiting', 'snoozed', 'completed', 'dismissed', 'deleted'],
    in_progress: ['active', 'waiting', 'snoozed', 'completed', 'deleted'],
    waiting: ['active', 'in_progress', 'snoozed', 'completed', 'deleted'],
    snoozed: ['active', 'completed', 'dismissed', 'deleted'],
    completed: ['active', 'deleted'],
    dismissed: ['active', 'suggested', 'deleted'],
    deleted: ['active']
};

// ── Theme ─────────────────────────────────────────────────────────────
(function() {
    // Apply theme immediately (before DOMContentLoaded) to prevent flash
    var saved = localStorage.getItem('todoness-theme');
    if (saved) {
        document.documentElement.setAttribute('data-theme', saved);
    } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
        document.documentElement.setAttribute('data-theme', 'dark');
    }
})();

function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme');
    var next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('todoness-theme', next);
    updateThemeIcon(next);
}

function updateThemeIcon(theme) {
    var icon = document.getElementById('theme-icon');
    if (icon) {
        // Moon for light mode (click to go dark), Sun for dark mode (click to go light)
        icon.innerHTML = theme === 'dark' ? '&#9788;' : '&#9790;';
    }
}

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);

function init() {
    fetchTasks();
    connectWebSocket();
    setupInputBar();
    setupDropZones();
    startParsePoller();
    fetchSyncStatus();
    startSyncWatcher();
    setupKeyboardShortcuts();

    // Sync theme icon with current state
    var theme = document.documentElement.getAttribute('data-theme') || 'light';
    updateThemeIcon(theme);

    // Close people dropdown when clicking outside
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.person-pill-wrapper')) {
            closeAllDropdowns();
        }
        if (!e.target.closest('#person-filter')) {
            var dd = document.getElementById('person-filter-dropdown');
            if (dd) dd.classList.remove('open');
        }
    });
}

// ── WebSocket ──────────────────────────────────────────────────────────
function connectWebSocket() {
    var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(protocol + '//' + window.location.host + '/ws');
    setConnectionStatus('connecting');

    ws.onopen = function() {
        setConnectionStatus('connected');
    };

    ws.onmessage = function(event) {
        var msg = JSON.parse(event.data);
        handleWsMessage(msg);
    };

    ws.onclose = function() {
        ws = null;
        setConnectionStatus('disconnected');
        if (reconnectTimer) clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = function() {
        if (ws) ws.close();
    };
}

function setConnectionStatus(state) {
    var el = document.getElementById('connection-status');
    if (!el) return;
    el.className = 'connection-indicator ' + state;
    var label = el.querySelector('.connection-label');
    if (label) {
        var labels = { connected: 'Live', connecting: 'Connecting', disconnected: 'Offline' };
        label.textContent = labels[state] || state;
    }
}

function handleWsMessage(msg) {
    if (msg.type === 'task_created') {
        var existing = tasks.find(function(t) { return t.id === msg.task.id; });
        if (!existing) {
            tasks.push(msg.task);
        } else {
            Object.assign(existing, msg.task);
        }
        renderTaskList();
    } else if (msg.type === 'task_updated') {
        var task = tasks.find(function(t) { return t.id === msg.task.id; });
        if (task) {
            Object.assign(task, msg.task);
        } else {
            tasks.push(msg.task);
        }
        renderTaskList();
        if (selectedTaskId === msg.task.id) {
            renderDetailPane(msg.task);
        }
    } else if (msg.type === 'task_deleted') {
        tasks = tasks.filter(function(t) { return t.id !== msg.task_id; });
        renderTaskList();
        if (selectedTaskId === msg.task_id) {
            selectedTaskId = null;
            clearDetailPane();
        }
    } else if (msg.type === 'parse_error') {
        var errTask = tasks.find(function(t) { return t.id === msg.task_id; });
        if (errTask) {
            errTask.parse_status = 'error';
            errTask.error_message = msg.error_message;
            renderTaskList();
            if (selectedTaskId === msg.task_id) {
                renderDetailPane(errTask);
            }
        }
    } else if (msg.type === 'skill_running') {
        var skillKey = msg.task_id + ':' + msg.skill;
        _runningSkills[skillKey] = true;
        startSkillPoller();
        if (selectedTaskId === msg.task_id) {
            var runTask = tasks.find(function(t) { return t.id === msg.task_id; });
            if (runTask) renderDetailPane(runTask);
        }
    }
}

// ── Fetch Tasks ────────────────────────────────────────────────────────
function fetchTasks() {
    fetch('/api/tasks?exclude_status=dismissed,completed,deleted')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            // Merge: keep any previously-loaded terminal tasks, replace active-lifecycle
            var terminalTasks = tasks.filter(function(t) {
                return TERMINAL_SECTIONS.indexOf(t.status) !== -1;
            });
            var freshTasks = data.tasks || [];
            // Build map of fresh task IDs for dedup
            var freshIds = {};
            freshTasks.forEach(function(t) { freshIds[t.id] = true; });
            // Keep terminal tasks that aren't in the fresh set (avoid duplicates from status changes)
            terminalTasks = terminalTasks.filter(function(t) { return !freshIds[t.id]; });
            tasks = freshTasks.concat(terminalTasks);
            renderTaskList();
            if (selectedTaskId) {
                var t = tasks.find(function(t) { return t.id === selectedTaskId; });
                if (t) renderDetailPane(t);
                else clearDetailPane();
            }
        })
        .catch(function(err) { console.error('Failed to fetch tasks:', err); });
}

function fetchSectionTasks(sectionId) {
    return fetch('/api/tasks?status=' + sectionId)
        .then(function(res) { return res.json(); })
        .then(function(data) {
            var newTasks = data.tasks || [];
            // Merge into global tasks array, replacing any stale entries
            var newIds = {};
            newTasks.forEach(function(t) { newIds[t.id] = true; });
            tasks = tasks.filter(function(t) { return !newIds[t.id]; }).concat(newTasks);
            _loadedSections[sectionId] = true;
            renderTaskList();
        });
}


// ── Parse Status Poller ────────────────────────────────────────────────
// Polls for tasks in transitional parse states (queued/parsing/unparsed)
// since Claude writes directly to the DB, bypassing WebSocket.
var parsePollerInterval = null;

function startParsePoller() {
    parsePollerInterval = setInterval(pollParseStatus, 3000);
}

function pollParseStatus() {
    // Only poll if there are tasks in transitional states
    var pending = tasks.filter(function(t) {
        return t.parse_status === 'unparsed' || t.parse_status === 'queued' || t.parse_status === 'parsing';
    });
    if (!pending.length) return;

    // Re-fetch all tasks and update any that changed
    fetch('/api/tasks')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            var updated = false;
            (data.tasks || []).forEach(function(fresh) {
                var existing = tasks.find(function(t) { return t.id === fresh.id; });
                if (existing) {
                    // Check if parse_status changed or other fields updated
                    if (existing.parse_status !== fresh.parse_status ||
                        existing.updated_at !== fresh.updated_at) {
                        Object.assign(existing, fresh);
                        updated = true;
                    }
                }
            });
            if (updated) {
                renderTaskList();
                if (selectedTaskId) {
                    var sel = tasks.find(function(t) { return t.id === selectedTaskId; });
                    if (sel) renderDetailPane(sel);
                }
            }
        })
        .catch(function() {}); // Silent fail on poll
}

// ── Input Bar ──────────────────────────────────────────────────────────
function setupInputBar() {
    var form = document.getElementById('add-task-form');
    var input = document.getElementById('task-input');

    form.addEventListener('submit', function(e) {
        e.preventDefault();
        submitTask();
    });

    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            submitTask();
        }
    });
}

function submitTask() {
    var input = document.getElementById('task-input');
    var text = input.value.trim();
    if (!text) return;

    fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ raw_input: text })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        input.value = '';
        if (data.task) {
            var existing = tasks.find(function(t) { return t.id === data.task.id; });
            if (!existing) {
                tasks.push(data.task);
                renderTaskList();
            }
        }
    })
    .catch(function(err) { console.error('Failed to create task:', err); });
}

// ── Search Filter ─────────────────────────────────────────────────────
function applySearchFilter() {
    var input = document.getElementById('search-input');
    searchQuery = (input ? input.value : '').trim().toLowerCase();
    renderTaskList();
}

// ── Quick-Hit Filter ──────────────────────────────────────────────────
function toggleQuickFilter() {
    _quickFilterActive = !_quickFilterActive;
    var pill = document.getElementById('quick-filter-active');
    if (pill) pill.classList.toggle('active', _quickFilterActive);
    renderTaskList();
}

// ── Resolved Suggestion Filter ───────────────────────────────────────
function toggleResolvedFilter() {
    _resolvedFilterActive = !_resolvedFilterActive;
    var pill = document.getElementById('resolved-filter-suggested');
    if (pill) pill.classList.toggle('active', _resolvedFilterActive);
    renderTaskList();
}

// ── Person Filter ────────────────────────────────────────────────────
function collectAllPeople() {
    var nameSet = {};
    var activeSections = ['active', 'in_progress', 'waiting', 'snoozed', 'suggested'];
    tasks.forEach(function(t) {
        if (activeSections.indexOf(t.status) === -1) return;
        parsePeopleNames(t.key_people).forEach(function(name) {
            if (name) nameSet[name] = true;
        });
    });
    return Object.keys(nameSet).sort();
}

function updatePersonFilter() {
    var container = document.getElementById('person-filter');
    if (!container) return;
    var people = collectAllPeople();
    if (!people.length) {
        container.innerHTML = '';
        return;
    }
    var label = _personFilter
        ? '<span class="person-pill-avatar">' + getInitials(_personFilter) + '</span> '
            + escapeHtml(_personFilter)
            + ' <span class="person-filter-clear" onclick="event.stopPropagation(); clearPersonFilter()">✕</span>'
        : '👤 People';
    var activeClass = _personFilter ? ' person-filter-trigger-active' : '';
    var html = '<div class="person-filter-trigger' + activeClass + '" onclick="event.stopPropagation(); togglePersonDropdown()">'
        + label + ' ▾</div>';
    html += '<div class="person-filter-dropdown" id="person-filter-dropdown">';
    if (_personFilter) {
        html += '<div class="person-filter-pill person-filter-pill-clear" onclick="event.stopPropagation(); togglePersonFilter(\'\')">'
            + '✕ Clear filter</div>';
    }
    people.forEach(function(name) {
        var active = name === _personFilter ? ' person-filter-pill-active' : '';
        var initials = getInitials(name);
        html += '<div class="person-filter-pill' + active + '" onclick="event.stopPropagation(); togglePersonFilter(\'' + escapeHtml(name).replace(/'/g, "\\'") + '\')">'
            + '<span class="person-pill-avatar">' + initials + '</span>'
            + '<span>' + escapeHtml(name) + '</span>'
            + '</div>';
    });
    html += '</div>';
    container.innerHTML = html;
}

function togglePersonDropdown() {
    var dd = document.getElementById('person-filter-dropdown');
    if (dd) dd.classList.toggle('open');
}

function togglePersonFilter(name) {
    var wasFiltered = !!_personFilter;
    _personFilter = (_personFilter === name || name === '') ? '' : name;
    var dd = document.getElementById('person-filter-dropdown');
    if (dd) dd.classList.remove('open');

    if (_personFilter && !wasFiltered) {
        // Save current collapse state before expanding
        _collapsedBeforeFilter = [];
        ['active', 'suggested', 'waiting', 'snoozed', 'completed', 'dismissed', 'deleted'].forEach(function(s) {
            var body = document.getElementById('body-' + s);
            if (body && body.classList.contains('collapsed')) _collapsedBeforeFilter.push(s);
        });
        // Load terminal sections if needed, then expand sections with matches
        var toLoad = TERMINAL_SECTIONS.filter(function(s) { return !_loadedSections[s]; });
        var loads = toLoad.map(function(s) { return fetchSectionTasks(s); });
        Promise.all(loads).then(function() {
            renderTaskList();
            expandSectionsWithMatches();
        });
    } else if (!_personFilter) {
        // Restore collapse state
        renderTaskList();
        _collapsedBeforeFilter.forEach(function(s) {
            var body = document.getElementById('body-' + s);
            var toggle = document.getElementById('toggle-' + s);
            if (body && !body.classList.contains('collapsed')) {
                body.classList.add('collapsed');
                if (toggle) toggle.innerHTML = '&#9656;';
            }
        });
        _collapsedBeforeFilter = [];
    } else {
        // Switching between people
        renderTaskList();
        expandSectionsWithMatches();
    }
}

function clearPersonFilter() {
    togglePersonFilter('');
}

function expandSectionsWithMatches() {
    var sections = ['active', 'suggested', 'waiting', 'snoozed', 'completed', 'dismissed', 'deleted'];
    sections.forEach(function(sectionId) {
        var body = document.getElementById('body-' + sectionId);
        var toggle = document.getElementById('toggle-' + sectionId);
        if (!body) return;
        var hasMatches = body.children.length > 0;
        if (hasMatches && body.classList.contains('collapsed')) {
            body.classList.remove('collapsed');
            if (toggle) toggle.innerHTML = '&#9662;';
        }
    });
}

function applyPersonFilter() {
    renderTaskList();
}

function taskMatchesPerson(task) {
    if (!_personFilter) return true;
    var names = parsePeopleNames(task.key_people);
    return names.indexOf(_personFilter) !== -1;
}

function taskMatchesSearch(task) {
    if (!searchQuery) return true;
    var fields = [
        task.title,
        task.description,
        task.coaching_text,
        task.key_people,
        task.related_meeting,
        task.raw_input,
        task.user_notes,
        task.action_type,
        task.skill_output,
        task.waiting_activity
    ];
    for (var i = 0; i < fields.length; i++) {
        if (fields[i] && fields[i].toLowerCase().indexOf(searchQuery) !== -1) {
            return true;
        }
    }
    return false;
}

// ── Render Task List ───────────────────────────────────────────────────
function renderTaskList() {
    updatePersonFilter();
    var active = [];
    var waiting = [];
    var snoozed = [];
    var suggested = [];
    var completed = [];
    var dismissed = [];
    var deleted = [];

    tasks.forEach(function(t) {
        if (!taskMatchesSearch(t)) return;
        if (!taskMatchesPerson(t)) return;
        // Treat in_progress as active (section removed)
        if (t.status === 'active' || t.status === 'in_progress') {
            active.push(t);
        } else if (t.status === 'waiting') {
            waiting.push(t);
        } else if (t.status === 'snoozed') {
            snoozed.push(t);
        } else if (t.status === 'suggested') {
            suggested.push(t);
        } else if (t.status === 'completed') {
            completed.push(t);
        } else if (t.status === 'dismissed') {
            dismissed.push(t);
        } else if (t.status === 'deleted') {
            deleted.push(t);
        }
    });

    renderSection('active', active);
    renderSection('suggested', suggested);

    // Show/hide batch dismiss button based on resolved suggestion count
    var resolvedCount = suggested.filter(function(t) {
        var a = parseWaitingActivity(t);
        return a && a.status === 'likely_resolved';
    }).length;
    var batchBtn = document.getElementById('batch-dismiss-btn');
    if (batchBtn) {
        if (resolvedCount > 0) {
            batchBtn.style.display = '';
            batchBtn.textContent = 'Dismiss Resolved (' + resolvedCount + ')';
        } else {
            batchBtn.style.display = 'none';
        }
    }

    // Update suggestion-check button tooltip with checked/total count
    var scBtn = document.getElementById('suggestion-check-btn');
    if (scBtn && !scBtn.classList.contains('syncing')) {
        scBtn.title = _suggestionCheckTooltip();
    }

    renderSection('waiting', waiting);
    renderSection('snoozed', snoozed);
    renderSection('completed', completed);
    renderSection('dismissed', dismissed);
    renderSection('deleted', deleted);
}

function renderSection(sectionId, sectionTasks) {
    var body = document.getElementById('body-' + sectionId);
    var count = document.getElementById('count-' + sectionId);

    // Quick-hit filter for active section
    if (sectionId === 'active' && _quickFilterActive) {
        var totalCount = sectionTasks.length;
        sectionTasks = sectionTasks.filter(function(t) { return t.is_quick_hit; });
        count.textContent = sectionTasks.length + '/' + totalCount;
    // Resolved filter for suggested section
    } else if (sectionId === 'suggested' && _resolvedFilterActive) {
        var totalCount = sectionTasks.length;
        sectionTasks = sectionTasks.filter(function(t) {
            var a = parseWaitingActivity(t);
            return a && a.status === 'likely_resolved';
        });
        count.textContent = sectionTasks.length + '/' + totalCount;
    } else {
        count.textContent = sectionTasks.length;
    }

    // Sort: priority ASC, then created_at DESC (matches API ORDER BY)
    sectionTasks.sort(function(a, b) {
        var pa = a.priority || 3, pb = b.priority || 3;
        if (pa !== pb) return pa - pb;
        // Descending by created_at (newer first)
        var ca = a.created_at || '', cb = b.created_at || '';
        return ca < cb ? 1 : ca > cb ? -1 : 0;
    });

    var html = '';
    sectionTasks.forEach(function(task) {
        var selected = task.id === selectedTaskId ? ' selected' : '';
        var dueHtml = '';
        if (task.due_date) {
            var isOverdueDate = new Date(task.due_date + 'T23:59:59') < new Date() && ['active','in_progress','waiting','snoozed'].indexOf(task.status) !== -1;
            var overdue = isOverdueDate ? ' overdue' : '';
            dueHtml = '<span class="task-due' + overdue + '">' + formatDate(task.due_date) + '</span>';
            if (isOverdueDate) {
                dueHtml += '<span class="overdue-badge">Overdue</span>';
            }
        }
        var refreshingContext = task.parse_status === 'queued'
            || task.parse_status === 'parsing';
        var parseHtml = refreshingContext ? '' : parseStatusIcon(task.parse_status);
        var enrichedHtml = '';
        if (task.cw_state === 'previewing') {
            enrichedHtml = '<span class="cw-status-indicator cw-status-running" title="Cowork is working"><img class="cw-spark" src="/static/img/coworker.svg" alt="" aria-hidden="true"></span>';
        } else if (refreshingContext) {
            enrichedHtml = '<span class="cw-status-indicator cw-status-running" title="Cowork is refreshing task context"><img class="cw-spark" src="/static/img/coworker.svg" alt="" aria-hidden="true"></span>';
        } else if (task.cw_state === 'ready' && !task.cw_seen_at) {
            enrichedHtml = '<span class="cw-status-indicator cw-status-unread" title="New Cowork update"><img class="cw-spark" src="/static/img/coworker.svg" alt="" aria-hidden="true"></span>';
        } else if (task.skill_output) {
            enrichedHtml = '<span class="cw-status-indicator cw-status-complete" title="Cowork enhanced"><img class="cw-spark" src="/static/img/coworker.svg" alt="" aria-hidden="true"></span>';
        }
        var waitingIconHtml = waitingActivityIcon(task);
        var suggestionBadgeHtml = suggestionCheckBadge(task);

        // Build preview line: description, coaching, or key people
        var preview = task.description || task.coaching_text || '';
        if (!preview && task.key_people) {
            var names = parsePeopleNames(task.key_people);
            if (names.length) preview = names.join(', ');
        }
        // Action badge for non-general types
        var actionBadgeHtml = '';
        if (task.action_type && task.action_type !== 'general') {
            actionBadgeHtml = '<span class="action-badge">' + actionTypeIcon(task.action_type) + ' ' + escapeHtml(actionTypeLabel(task.action_type)) + '</span>';
        }

        // Snooze info line
        var snoozeHtml = '';
        if (task.status === 'snoozed' && task.snoozed_until) {
            var snoozeActivity = parseWaitingActivity(task);
            if (snoozeActivity && snoozeActivity.status === 'out_of_office') {
                var oofName = getOofPersonFirstName(task);
                var oofDateStr = snoozeActivity.return_date ? ' (OOO until ' + formatOofDate(snoozeActivity.return_date) + ')' : ' (OOO)';
                snoozeHtml = '<span class="snooze-info snooze-info-oof">Waiting for ' + escapeHtml(oofName) + oofDateStr + '</span>';
            } else {
                snoozeHtml = '<span class="snooze-info">Snoozed until ' + formatSnoozeTime(task.snoozed_until) + '</span>';
            }
        }

        var previewHtml = preview
            ? '<div class="task-row-preview">' + (snoozeHtml || '') + escapeHtml(truncate(preview, 80)) + actionBadgeHtml + '</div>'
            : (snoozeHtml ? '<div class="task-row-preview">' + snoozeHtml + '</div>'
                : (actionBadgeHtml ? '<div class="task-row-preview">' + actionBadgeHtml + '</div>' : ''));

        // Overdue check for active statuses
        var overdueClass = '';
        if (task.due_date && ['active','in_progress','waiting','snoozed'].indexOf(task.status) !== -1) {
            var dueD = new Date(task.due_date + 'T23:59:59');
            if (dueD < new Date()) overdueClass = ' overdue';
        }

        html += '<div class="task-row' + selected + overdueClass + '" data-id="' + task.id + '" data-status="' + escapeHtml(task.status) + '" draggable="true" onclick="selectTask(' + task.id + ')">'
            + priorityDot(task.priority, task.id)
            + '<div class="task-row-content">'
            + '<div class="task-row-top">'
            + (task.is_quick_hit ? '<span class="quick-hit-icon" title="Quick hit">&#9201;</span>' : '')
            // Carries the task id. It used to live on the priority glyph, and
            // went away with it; the id is how Phil refers to tasks when
            // debugging, so it needs a home. Paired with the source type,
            // which is what this icon already means.
            + '<span class="task-source-icon" title="Task #' + task.id + ' \u00b7 '
            + escapeHtml(task.source_type || 'manual') + '">'
            + sourceTypeIcon(task.source_type) + '</span>'
            + '<span class="task-title">' + escapeHtml(task.title) + '</span>'
            + waitingIconHtml
            + suggestionBadgeHtml
            + dueHtml
            + '</div>'
            + previewHtml
            + '</div>'
            + enrichedHtml
            + parseHtml
            + '<button class="task-row-delete" onclick="event.stopPropagation(); deleteTask(' + task.id + ')" title="Delete">&#215;</button>'
            + '</div>';
    });

    body.innerHTML = html;
}

// ── Select Task ────────────────────────────────────────────────────────
function selectTask(taskId) {
    if (selectedTaskId && selectedTaskId !== taskId) {
        stopCoworkHandoffPoller(selectedTaskId);
    }
    selectedTaskId = taskId;
    cwLoad(taskId, true);

    var rows = document.querySelectorAll('.task-row');
    rows.forEach(function(row) {
        if (parseInt(row.getAttribute('data-id')) === taskId) {
            row.classList.add('selected');
        } else {
            row.classList.remove('selected');
        }
    });

    fetch('/api/tasks/' + taskId)
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.task) {
                data.task._contexts = data.contexts || [];
                // Reconcile the list with what the server actually has. Parsing
                // happens in an external process writing straight to SQLite, so
                // no task_updated broadcast fires and pollParseStatus only runs
                // while the LOCAL array still thinks something is pending. Once
                // it drifts it never self-corrects, and this fetch used to feed
                // the detail pane alone -- so a parsed task showed its new title
                // in the pane beside its pre-parse title in the list.
                var existing = tasks.find(function(t) { return t.id === taskId; });
                if (existing && _listFieldsDiffer(existing, data.task)) {
                    Object.assign(existing, data.task);
                    renderTaskList();   // re-applies .selected from selectedTaskId
                }
                renderDetailPane(data.task);
            }
        })
        .catch(function(err) { console.error('Failed to fetch task detail:', err); });
}

// Only the fields a row actually shows. Re-rendering the whole list on every
// click would churn the DOM and lose scroll position for no visible gain.
var _LIST_FIELDS = ['title', 'description', 'status', 'priority', 'due_date',
                    'parse_status', 'action_type', 'skill_output', 'snooze_until',
                    'error_message'];

function _listFieldsDiffer(a, b) {
    for (var i = 0; i < _LIST_FIELDS.length; i++) {
        var k = _LIST_FIELDS[i];
        if ((a[k] || '') !== (b[k] || '')) return true;
    }
    return false;
}

// Any field the user could be mid-edit in. Deliberately behavioural rather than
// a list of ids and classes: the previous version checked for a `coaching-edit`
// class that no markup actually carried, so the guard silently stopped covering
// the intent textarea and background polling threw the user out of edit mode
// every few seconds. A shape test cannot rot the same way.
function _isTextEntry(el) {
    if (!el) return false;
    var tag = el.tagName;
    if (tag === 'TEXTAREA') return true;
    if (tag !== 'INPUT') return false;
    var type = (el.getAttribute('type') || 'text').toLowerCase();
    return ['text', 'search', 'url', 'email', 'tel', 'number', 'password',
            'date', 'datetime-local', 'time'].indexOf(type) !== -1;
}

// ── Render Detail Pane ─────────────────────────────────────────────────
function renderDetailPane(task) {
    var pane = document.getElementById('detail-pane');

    // Skip re-render while the user is typing in this pane, then catch up on
    // blur so the deferral never silently drops an update.
    var activeEl = document.activeElement;
    var restoreSplitFocus = activeEl
        && activeEl.classList
        && activeEl.classList.contains('detail-split-handle');
    if (activeEl && pane && pane.contains(activeEl) && _isTextEntry(activeEl)) {
        // Stash the task for a deferred re-render after blur
        pane._pendingTask = task;
        if (!pane._deferredRender) {
            pane._deferredRender = true;
            activeEl.addEventListener('blur', function onBlur() {
                activeEl.removeEventListener('blur', onBlur);
                pane._deferredRender = false;
                if (pane._pendingTask) {
                    renderDetailPane(pane._pendingTask);
                    pane._pendingTask = null;
                }
            });
        }
        return;
    }

    var statusClass = (task.status || '').replace(/\s/g, '_');
    var people = parsePeople(task.key_people);
    var sourcePerson = people.length ? people[0].name : '';
    var sourceLabel = sourcePerson || (task.source_type || 'Manual source');
    var normalizedSource = (task.source_snippet || '')
        .replace(/\s+/g, ' ').trim().toLowerCase();
    var normalizedDescription = (task.description || '')
        .replace(/\s+/g, ' ').trim().toLowerCase();
    var showTaskBrief = (task.source_type || '').toLowerCase() === 'manual'
        || !normalizedSource
        || normalizedDescription !== normalizedSource;

    // Task identity stays full-width above the evidence/action workspace.
    var headerHtml = '<div class="detail-card detail-task-header">'
        + '<div class="detail-header-row">'
        + '<h2 id="title-display-' + task.id + '">' + escapeHtml(task.title) + '</h2>'
        + '<button class="btn-edit-inline" onclick="toggleTitleEdit(' + task.id + ')" title="Edit title">&#9998;</button>'
        + getHeaderActions(task)
        + '</div>'
        + '<input type="text" id="title-edit-' + task.id + '" class="title-edit-input" style="display:none" '
        + 'value="' + escapeHtml(task.title) + '" '
        + 'onblur="saveTitle(' + task.id + ')" '
        + 'onkeydown="if(event.key===\'Enter\'){this.blur();}">'
        + '<div class="detail-meta">'
        + '<span class="meta-item"><span class="status-badge ' + statusClass + '">' + escapeHtml(task.status) + '</span></span>'
        + '<span class="meta-item">' + prioritySelector(task) + '</span>'
        + '<span class="meta-item">' + dueDateField(task) + '</span>'
        + '<span class="meta-item">' + actionTypeSelector(task) + '</span>'
        + (function() {
            var sa = parseWaitingActivity(task);
            if (sa && sa.status === 'out_of_office') {
                var oofName = getOofPersonFirstName(task);
                var dateStr = sa.return_date ? formatOofDate(sa.return_date) : 'unknown';
                return '<span class="meta-item"><span class="snooze-detail-badge snooze-oof-badge">Waiting for ' + escapeHtml(oofName) + ' (OOO until ' + escapeHtml(dateStr) + ')</span></span>';
            }
            if (task.status === 'snoozed' && task.snoozed_until) {
                return '<span class="meta-item"><span class="snooze-detail-badge">Snoozed until ' + formatSnoozeTime(task.snoozed_until) + '</span></span>';
            }
            return '';
        })()
        + '<span class="meta-item" style="margin-left:auto">' + quickHitToggle(task) + '</span>'
        + '<span id="source-meta-' + task.id + '" class="meta-item source-meta-editable" style="margin-left:auto; cursor:pointer" '
        + 'onclick="openSourceModal(' + task.id + ')" title="Click to edit source">' + sourceMetaLink(task) + '</span>'
        + '</div>'
        + '</div>';

    var lifecycleHtml = '<div class="detail-actions-bar detail-lifecycle-strip">'
        + getActionButtons(task)
        + '<span class="actions-spacer"></span>'
        + '<button class="btn btn-refresh" onclick="refreshTask(' + task.id + ')" title="Re-parse with Claude + WorkIQ">&#8635; Refresh</button>'
        + '</div>';

    var evidenceHtml = '<aside class="detail-evidence" aria-label="Source and context">'
        + '<div class="detail-card detail-source-card">'
        + '<div class="detail-label">Source and context</div>'
        + '<div class="detail-source-head">'
        + '<img class="detail-source-avatar" src="/static/img/profile-placeholder.svg" alt="">'
        + '<div><strong>' + escapeHtml(sourceLabel) + '</strong>'
        + '<span>' + sourceTypeIcon(task.source_type) + ' ' + escapeHtml(task.source_type || 'manual') + '</span></div>'
        + '</div>';

    if (task.source_snippet) {
        evidenceHtml += '<div class="detail-source-quote">'
            + escapeHtml(task.source_snippet)
            + '</div>';
    }
    evidenceHtml += '<div class="detail-source-link">' + sourceMetaLink(task, false) + '</div>';

    // Preserve the editable task brief only when it adds to the source. Manual
    // tasks always retain their editable summary, even if source metadata matches.
    if (showTaskBrief && task.description) {
        evidenceHtml += '<div class="detail-task-brief"><div class="detail-label">Task brief'
            + '<button class="btn-edit-inline" onclick="toggleDescriptionEdit(' + task.id + ')" title="Edit description">&#9998;</button>'
            + '</div>'
            + '<div id="desc-display-' + task.id + '" class="detail-description">' + renderRichText(task.description, task.key_people) + '</div>'
            + '<textarea id="desc-edit-' + task.id + '" class="description-edit-textarea" style="display:none" '
            + 'onblur="saveDescription(' + task.id + ')">' + escapeHtml(task.description) + '</textarea>'
            + '</div>';
    } else if (showTaskBrief) {
        evidenceHtml += '<div class="detail-task-brief"><div class="detail-label">Task brief'
            + '<button class="btn-edit-inline" onclick="toggleDescriptionEdit(' + task.id + ')" title="Add description">&#9998;</button>'
            + '</div>'
            + '<div id="desc-display-' + task.id + '" class="detail-description" style="color:#9e9e9e">No description</div>'
            + '<textarea id="desc-edit-' + task.id + '" class="description-edit-textarea" style="display:none" '
            + 'onblur="saveDescription(' + task.id + ')" placeholder="Add a description..."></textarea>'
            + '</div>';
    } else {
        evidenceHtml += '<details class="detail-task-brief-collapsed">'
            + '<summary>Edit stored summary</summary>'
            + '<div id="desc-display-' + task.id + '" class="detail-description">'
            + renderRichText(task.description, task.key_people)
            + '<button class="btn-edit-inline" onclick="toggleDescriptionEdit(' + task.id + ')" title="Edit task brief">&#9998;</button>'
            + '</div>'
            + '<textarea id="desc-edit-' + task.id + '" class="description-edit-textarea" style="display:none" '
            + 'onblur="saveDescription(' + task.id + ')">' + escapeHtml(task.description) + '</textarea>'
            + '</details>';
    }
    evidenceHtml += '</div>';

    // Key People (pills + add)
    evidenceHtml += '<div class="detail-card">'
        + '<div class="detail-label">Key People</div>'
        + renderPeoplePills(task.key_people, task.id)
        + '<div class="add-person-row" id="add-person-row-' + task.id + '">'
        + '<button class="btn btn-sm add-person-btn" onclick="event.stopPropagation(); showAddPersonInput(' + task.id + ')">+ Add</button>'
        + '<div class="add-person-input-wrapper" id="add-person-input-' + task.id + '" style="display:none">'
        + '<input type="text" class="add-person-name" id="add-person-name-' + task.id + '" placeholder="Name" '
        + 'onkeydown="if(event.key===\'Enter\'){event.preventDefault();saveNewPerson(' + task.id + ')}'
        + 'else if(event.key===\'Escape\'){hideAddPersonInput(' + task.id + ')}">'
        + '<button class="btn btn-sm" onclick="event.stopPropagation(); saveNewPerson(' + task.id + ')">&#10003;</button>'
        + '</div>'
        + '</div>'
        + '</div>';

    // Waiting Activity Check (between Key People and Notes)
    if (task.status === 'waiting' || (task.status === 'snoozed' && parseWaitingActivity(task) && parseWaitingActivity(task).status === 'out_of_office')) {
        evidenceHtml += renderWaitingActivityCard(task);
    }

    // Suggestion Check (for suggested tasks, between Key People and Notes)
    if (task.status === 'suggested') {
        evidenceHtml += renderSuggestionCheckCard(task);
    }

    // User Notes
    evidenceHtml += '<div class="detail-card">'
        + '<div class="detail-label">Notes</div>'
        + '<div class="notes-add-row">'
        + '<input type="text" class="notes-add-input" id="notes-add-input" placeholder="Quick note\u2026 (context for Cowork)" '
        + 'onkeydown="if(event.key===\'Enter\'){event.preventDefault();addTimestampedNote(' + task.id + ')}">'
        + '<button class="btn btn-sm notes-add-btn" onclick="addTimestampedNote(' + task.id + ')">+</button>'
        + '</div>'
        + '<textarea class="notes-textarea" id="notes-textarea" '
        + 'onblur="saveNotes(' + task.id + ')" placeholder="Add your notes...">'
        + escapeHtml(task.user_notes || '')
        + '</textarea>'
        + '</div>';

    // Error message box
    if (task.error_message && task.parse_status === 'error') {
        evidenceHtml += '<div class="parse-error-box">'
            + '<div class="parse-error-header">'
            + '<span class="parse-error-icon">&#9888;</span>'
            + '<span class="parse-error-title">Parse Error</span>'
            + '</div>'
            + '<div class="parse-error-message">' + escapeHtml(task.error_message) + '</div>'
            + '<button class="parse-error-retry" onclick="refreshTask(' + task.id + ')">&#8635; Retry</button>'
            + '</div>';
    }

    evidenceHtml += '</aside>';

    var workspaceAction = _cwActions[task.id];
    if (workspaceAction === undefined) cwLoad(task.id);
    var hasLiveWorkspaceAction = workspaceAction
        && ['previewing', 'executing', 'executed', 'execute_unconfirmed']
            .indexOf(workspaceAction.state) >= 0;
    var workspaceHtml = (['parsed', 'queued', 'parsing'].indexOf(task.parse_status) >= 0
            || hasLiveWorkspaceAction)
        ? '<section class="detail-workspace" aria-label="Cowork workspace">'
            + renderCoworkCard(task)
            + '</section>'
        : '';

    var splitHtml = '<div class="detail-split">'
        + evidenceHtml
        + '<div class="detail-split-handle" role="separator" tabindex="0" '
        + 'aria-label="Resize source and evidence pane" aria-orientation="vertical" '
        + 'aria-valuemin="' + DETAIL_EVIDENCE_MIN + '" '
        + 'aria-valuemax="' + DETAIL_EVIDENCE_MAX + '"></div>'
        + workspaceHtml
        + '</div>';

    pane.innerHTML = headerHtml + lifecycleHtml + splitHtml;
    initDetailSplit();
    if (restoreSplitFocus) {
        var replacementHandle = pane.querySelector('.detail-split-handle');
        if (replacementHandle) replacementHandle.focus();
    }
    cwInitFindingToggle(task.id);
}

function clearDetailPane() {
    var previousTaskId = selectedTaskId;
    selectedTaskId = null;
    if (previousTaskId) stopCoworkHandoffPoller(previousTaskId);
    var pane = document.getElementById('detail-pane');
    pane.innerHTML = '<div class="empty-state">'
        + '<div class="empty-state-icon">&#128203;</div>'
        + '<div>Select a task to view details</div>'
        + '</div>';
}

function getDetailEvidenceBounds(split, handle) {
    var total = split.clientWidth;
    if (!total || window.matchMedia('(max-width: 1050px)').matches) {
        return {
            min: DETAIL_EVIDENCE_MIN,
            max: DETAIL_EVIDENCE_MAX,
            stacked: true
        };
    }
    var min = Math.max(
        DETAIL_EVIDENCE_MIN,
        Math.ceil((DETAIL_EVIDENCE_MIN_PX / total) * 100)
    );
    var max = Math.min(
        DETAIL_EVIDENCE_MAX,
        Math.floor(
            ((total - handle.offsetWidth - DETAIL_WORKSPACE_MIN_PX) / total) * 100
        )
    );
    return max > min
        ? { min: min, max: max, stacked: false }
        : {
            min: DETAIL_EVIDENCE_MIN,
            max: DETAIL_EVIDENCE_MAX,
            stacked: true
        };
}

function setDetailEvidenceWidth(split, handle, value, persist) {
    var bounds = getDetailEvidenceBounds(split, handle);
    split.classList.toggle('is-stacked', bounds.stacked);
    var width = Math.min(bounds.max, Math.max(bounds.min, value));
    split.style.setProperty('--detail-evidence-width', width + '%');
    handle.setAttribute('aria-valuemin', String(bounds.min));
    handle.setAttribute('aria-valuemax', String(bounds.max));
    handle.setAttribute('aria-valuenow', String(Math.round(width)));
    if (persist) localStorage.setItem(DETAIL_EVIDENCE_STORAGE_KEY, String(width));
}

function initDetailSplit() {
    var split = document.querySelector('.detail-split');
    var handle = split && split.querySelector('.detail-split-handle');
    if (!split || !handle) return;

    var saved = Number(localStorage.getItem(DETAIL_EVIDENCE_STORAGE_KEY));
    setDetailEvidenceWidth(
        split,
        handle,
        Number.isFinite(saved) && saved ? saved : 40,
        Number.isFinite(saved) && Boolean(saved)
    );

    if (_detailSplitResizeObserver) _detailSplitResizeObserver.disconnect();
    if (window.ResizeObserver) {
        _detailSplitResizeObserver = new ResizeObserver(function() {
            setDetailEvidenceWidth(
                split,
                handle,
                Number(handle.getAttribute('aria-valuenow')) || 40,
                false
            );
        });
        _detailSplitResizeObserver.observe(split);
    }

    handle.addEventListener('pointerdown', function(e) {
        if (window.matchMedia('(max-width: 1050px)').matches) return;
        e.preventDefault();
        handle.setPointerCapture(e.pointerId);
        split.classList.add('is-resizing');
    });
    handle.addEventListener('pointermove', function(e) {
        if (!handle.hasPointerCapture(e.pointerId)) return;
        var rect = split.getBoundingClientRect();
        setDetailEvidenceWidth(
            split,
            handle,
            ((e.clientX - rect.left) / rect.width) * 100,
            true
        );
    });
    handle.addEventListener('pointerup', function(e) {
        if (handle.hasPointerCapture(e.pointerId)) handle.releasePointerCapture(e.pointerId);
        split.classList.remove('is-resizing');
    });
    handle.addEventListener('keydown', function(e) {
        var current = Number(handle.getAttribute('aria-valuenow')) || 40;
        var min = Number(handle.getAttribute('aria-valuemin')) || DETAIL_EVIDENCE_MIN;
        var max = Number(handle.getAttribute('aria-valuemax')) || DETAIL_EVIDENCE_MAX;
        var next = current;
        if (e.key === 'ArrowLeft') next = current - 2;
        else if (e.key === 'ArrowRight') next = current + 2;
        else if (e.key === 'Home') next = min;
        else if (e.key === 'End') next = max;
        else return;
        e.preventDefault();
        e.stopPropagation();
        setDetailEvidenceWidth(split, handle, next, true);
    });
}

// ── People Pills ───────────────────────────────────────────────────────
function parsePeople(keyPeople) {
    if (!keyPeople) return [];
    // Try JSON format first
    try {
        var parsed = JSON.parse(keyPeople);
        if (Array.isArray(parsed)) return parsed;
    } catch (e) {}
    // Fallback: comma-separated plain text
    return keyPeople.split(',').map(function(name) {
        return { name: name.trim(), alternatives: [] };
    }).filter(function(p) { return p.name; });
}

function parsePeopleNames(keyPeople) {
    return parsePeople(keyPeople).map(function(p) { return p.name; });
}

function getInitials(name) {
    if (!name) return '?';
    var parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
        return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return parts[0][0].toUpperCase();
}

function renderPeoplePills(keyPeople, taskId) {
    var people = parsePeople(keyPeople);
    if (!people.length) return '';

    var html = '<div class="people-list">';
    people.forEach(function(person, idx) {
        var hasAlts = person.alternatives && person.alternatives.length > 0;
        var unresolved = person.unresolved === true
            || !String(person.email || '').trim();
        var pillId = 'pill-' + taskId + '-' + idx;

        html += '<div class="person-pill-wrapper" id="wrapper-' + pillId + '">';

        // Pill — always clickable (dropdown always available for remove)
        html += '<div class="person-pill has-alternatives'
            + (unresolved ? ' is-unresolved' : '') + '" '
            + (unresolved ? 'title="Choose a resolved identity before scheduling" ' : '')
            + 'onclick="event.stopPropagation(); togglePeopleDropdown(\'' + pillId + '\')">'
            + '<span class="person-pill-avatar">' + getInitials(person.name) + '</span>'
            + '<span>' + escapeHtml(person.name) + '</span>';
        if (person.role) {
            html += ' <span class="person-role">' + escapeHtml(person.role) + '</span>';
        }
        html += '</div>';

        // Dropdown — always present
        html += '<div class="alternatives-dropdown" id="dropdown-' + pillId + '">';
        if (hasAlts || (unresolved && person.email)) {
            html += '<div class="alternatives-header">'
                + (unresolved ? 'Choose the right person' : 'Did you mean?') + '</div>';

            // Current selection (highlighted)
            html += '<div class="alternative-item selected" '
                + 'onclick="event.stopPropagation(); selectPerson(' + taskId + ', ' + idx + ', -1)">'
                + '<div class="alt-avatar">' + getInitials(person.name) + '</div>'
                + '<div class="alt-info">'
                + '<div class="alt-name">' + escapeHtml(person.name) + '</div>'
                + '<div class="alt-detail">' + escapeHtml([person.email, person.role].filter(Boolean).join(' \u00b7 ')) + '</div>'
                + '</div></div>';

            person.alternatives.forEach(function(alt, altIdx) {
                html += '<div class="alternative-item" '
                    + 'onclick="event.stopPropagation(); selectPerson(' + taskId + ', ' + idx + ', ' + altIdx + ')">'
                    + '<div class="alt-avatar">' + getInitials(alt.name) + '</div>'
                    + '<div class="alt-info">'
                    + '<div class="alt-name">' + escapeHtml(alt.name) + '</div>'
                    + '<div class="alt-detail">' + escapeHtml([alt.email, alt.role].filter(Boolean).join(' \u00b7 ')) + '</div>'
                    + '</div></div>';
            });
        } else if (unresolved) {
            html += '<div class="alternatives-header">Resolving identity...</div>'
                + '<div class="person-resolving">Riveter is finding matches. '
                + 'Choose the right person here when they appear.</div>';
        }

        // Remove option
        html += '<div class="alternative-item remove-person" '
            + 'onclick="event.stopPropagation(); removePerson(' + taskId + ', ' + idx + ')">'
            + '<div class="alt-avatar remove-avatar">\u00d7</div>'
            + '<div class="alt-info"><div class="alt-name remove-label">Remove person</div></div>'
            + '</div>';

        html += '</div>';

        html += '</div>';
    });
    html += '</div>';
    return html;
}

function togglePeopleDropdown(pillId) {
    var dropdown = document.getElementById('dropdown-' + pillId);
    if (!dropdown) return;

    var isOpen = dropdown.classList.contains('open');
    closeAllDropdowns();
    if (!isOpen) {
        dropdown.classList.add('open');
        openDropdownId = pillId;
    }
}

function closeAllDropdowns() {
    var dropdowns = document.querySelectorAll('.alternatives-dropdown.open');
    dropdowns.forEach(function(d) { d.classList.remove('open'); });
    openDropdownId = null;
}

function selectPerson(taskId, personIdx, altIdx) {
    // Swap the selected alternative into the primary position
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (!task || !task.key_people) return;

    var people = parsePeople(task.key_people);
    var person = people[personIdx];
    if (!person) {
        closeAllDropdowns();
        return;
    }

    var oldName = person.name;
    var newName = person.name;
    if (altIdx < 0) {
        if (person.unresolved !== true || !person.email) {
            closeAllDropdowns();
            return;
        }
        delete person.unresolved;
        people[personIdx] = person;
    } else {
        var alt = person.alternatives[altIdx];
        if (!alt) return;
        newName = alt.name;

        // Swap: move current to alternatives, promote the selected alt
        var oldPrimary = { name: person.name, email: person.email, role: person.role };
        var newAlternatives = person.alternatives.filter(function(_, i) {
            return i !== altIdx;
        });
        newAlternatives.unshift(oldPrimary);

        people[personIdx] = {
            name: alt.name,
            email: alt.email,
            role: alt.role,
            alternatives: newAlternatives
        };
    }

    var newKeyPeople = JSON.stringify(people);

    // Replace old name with new name in text fields
    var updates = { key_people: newKeyPeople };
    if (task.title) {
        updates.title = replacePersonName(task.title, oldName, newName);
    }
    if (task.description) {
        updates.description = replacePersonName(task.description, oldName, newName);
    }
    if (task.coaching_text) {
        updates.coaching_text = replacePersonName(task.coaching_text, oldName, newName);
    }
    if (task.related_meeting) {
        updates.related_meeting = replacePersonName(task.related_meeting, oldName, newName);
    }

    // Save all changes to server
    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderDetailPane(data.task);
            renderTaskList();
            // Auto-queue refresh so Claude re-enriches with the correct person's context
            refreshTask(data.task.id);
        }
    })
    .catch(function(err) { console.error('Failed to update person:', err); });

    closeAllDropdowns();
}

function removePerson(taskId, personIdx) {
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (!task || !task.key_people) return;

    var people = parsePeople(task.key_people);
    if (personIdx < 0 || personIdx >= people.length) return;

    people.splice(personIdx, 1);
    var newKeyPeople = people.length ? JSON.stringify(people) : '';

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key_people: newKeyPeople })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderDetailPane(data.task);
            renderTaskList();
        }
    })
    .catch(function(err) { console.error('Failed to remove person:', err); });

    closeAllDropdowns();
}

function showAddPersonInput(taskId) {
    var wrapper = document.getElementById('add-person-input-' + taskId);
    var btn = wrapper ? wrapper.previousElementSibling : null;
    if (wrapper) { wrapper.style.display = 'flex'; }
    if (btn) { btn.style.display = 'none'; }
    var input = document.getElementById('add-person-name-' + taskId);
    if (input) { input.value = ''; input.focus(); }
}

function hideAddPersonInput(taskId) {
    var wrapper = document.getElementById('add-person-input-' + taskId);
    var btn = wrapper ? wrapper.previousElementSibling : null;
    if (wrapper) { wrapper.style.display = 'none'; }
    if (btn) { btn.style.display = ''; }
}

function saveNewPerson(taskId) {
    var input = document.getElementById('add-person-name-' + taskId);
    var name = input ? input.value.trim() : '';
    if (!name) return;

    var task = tasks.find(function(t) { return t.id === taskId; });
    if (!task) return;

    var people = parsePeople(task.key_people);
    // Don't add duplicates
    var exists = people.some(function(p) {
        return p.name.toLowerCase() === name.toLowerCase();
    });
    if (exists) { hideAddPersonInput(taskId); return; }

    people.push({ name: name, alternatives: [], unresolved: true });
    var newKeyPeople = JSON.stringify(people);

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key_people: newKeyPeople })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderDetailPane(data.task);
            renderTaskList();
            refreshTask(data.task.id);
        }
    })
    .catch(function(err) { console.error('Failed to add person:', err); });
}

function replacePersonName(text, oldName, newName) {
    if (!text || !oldName || !newName) return text;
    // Replace full name
    var result = text.split(oldName).join(newName);
    // Also replace first name only if it appears as a standalone word
    var oldFirst = oldName.split(' ')[0];
    var newFirst = newName.split(' ')[0];
    if (oldFirst !== oldName && oldFirst.length > 2) {
        // Use word boundary: replace "Jane" but not "Jane" inside "JaneDoe"
        var re = new RegExp('\\b' + escapeRegex(oldFirst) + '\\b', 'g');
        result = result.replace(re, newFirst);
    }
    return result;
}

function escapeRegex(str) {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function isCoachingStale(task) {
    // While a refresh is in progress, don't show stale — the parse status
    // indicator already tells the user a refresh is happening
    if (task.parse_status !== 'parsed') return false;
    if (!task.suggestion_refreshed_at) return false;
    // Stale only when content was manually changed after last coaching refresh
    if (task.updated_at && task.updated_at > task.suggestion_refreshed_at) return true;
    return false;
}

// ── Header Actions (top-right of detail card) ─────────────────────────
function getHeaderActions(task) {
    if (task.status === 'deleted') {
        return '<div class="detail-header-actions">'
            + '<button class="btn-icon btn-icon-danger" onclick="permanentDeleteTask(' + task.id + ')" title="Permanently delete">&#128465;</button>'
            + '</div>';
    }
    return '<div class="detail-header-actions">'
        + '<button class="btn-icon" onclick="deleteTask(' + task.id + ')" title="Delete task">&#128465;</button>'
        + '</div>';
}

// ── Priority Selector ──────────────────────────────────────────────────
function prioritySelector(task) {
    var balls = { 1: '\u25CF', 2: '\u25D5', 3: '\u25D1', 4: '\u25D4', 5: '\u25CB' };
    var labels = { 1: 'P1 Urgent', 2: 'P2 High', 3: 'P3 Normal', 4: 'P4 Low', 5: 'P5 Information' };
    var html = '<span class="priority-field">'
        + '<select class="priority-select" onchange="updatePriority(' + task.id + ', this.value)">';
    for (var i = 1; i <= 5; i++) {
        var sel = i === task.priority ? ' selected' : '';
        html += '<option value="' + i + '"' + sel + '>' + balls[i] + ' ' + labels[i] + '</option>';
    }
    html += '</select></span>';
    return html;
}

function updatePriority(taskId, value) {
    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ priority: parseInt(value) })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Failed to update priority:', err); });
}

// ── Due Date Field ─────────────────────────────────────────────────────
function dueDateField(task) {
    if (task.due_date) {
        var overdue = new Date(task.due_date) < new Date() ? ' overdue' : '';
        return '<span class="due-date-field">'
            + 'Due: <input type="date" class="due-date-input' + overdue + '" '
            + 'value="' + escapeHtml(task.due_date) + '" '
            + 'onchange="updateDueDate(' + task.id + ', this.value)">'
            + '<button class="btn-clear-date" onclick="updateDueDate(' + task.id + ', \'\')" title="Remove date">&times;</button>'
            + '</span>';
    }
    return '<button class="btn-add-date" onclick="showDatePicker(' + task.id + ', this)">+ Add due date</button>';
}

function updateDueDate(taskId, value) {
    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ due_date: value || null })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Failed to update due date:', err); });
}

function showDatePicker(taskId, btn) {
    // Replace button with a date input
    var input = document.createElement('input');
    input.type = 'date';
    input.className = 'due-date-input';
    input.onchange = function() { updateDueDate(taskId, input.value); };
    input.onblur = function() {
        if (!input.value) {
            // Revert to button if no date picked
            var task = tasks.find(function(t) { return t.id === taskId; });
            if (task) renderDetailPane(task);
        }
    };
    btn.replaceWith(input);
    input.focus();
    input.showPicker();
}

// ── Action Type Selector ──────────────────────────────────────────────
function actionTypeLabel(actionType) {
    var labels = {
        'schedule-meeting': 'Schedule Meeting',
        'respond-email': 'Respond to Email',
        'review-document': 'Review Document',
        'follow-up': 'Follow Up',
        'awaiting-response': 'Awaiting Response',
        'prepare': 'Prepare',
        'general': 'General'
    };
    return labels[actionType] || 'General';
}

function actionTypeIcon(actionType) {
    var icons = {
        'schedule-meeting': '\uD83D\uDCC5',
        'respond-email': '\u2709',
        'review-document': '\uD83D\uDCC4',
        'follow-up': '\uD83D\uDD04',
        'awaiting-response': '\u231B',
        'prepare': '\uD83D\uDCCB',
        'general': '\u2699'
    };
    return icons[actionType] || '\u2699';
}

function actionTypeSelector(task) {
    var types = [
        { value: 'general', label: 'General', icon: '\u2699' },
        { value: 'schedule-meeting', label: 'Schedule Meeting', icon: '\uD83D\uDCC5' },
        { value: 'respond-email', label: 'Respond to Email', icon: '\u2709' },
        { value: 'review-document', label: 'Review Document', icon: '\uD83D\uDCC4' },
        { value: 'follow-up', label: 'Follow Up', icon: '\uD83D\uDD04' },
        { value: 'awaiting-response', label: 'Awaiting Response', icon: '\u231B' },
        { value: 'prepare', label: 'Prepare', icon: '\uD83D\uDCCB' }
    ];
    var current = task.action_type || 'general';

    var html = '<span class="action-type-field">'
        + '<select class="action-type-select" onchange="updateActionType(' + task.id + ', this.value)">';
    types.forEach(function(t) {
        var sel = t.value === current ? ' selected' : '';
        html += '<option value="' + t.value + '"' + sel + '>' + t.icon + ' ' + t.label + '</option>';
    });
    html += '</select></span>';
    return html;
}

function updateActionType(taskId, value) {
    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_type: value })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            renderDetailPane(data.task);
            // Queue coaching re-parse since action type changed
            refreshTask(data.task.id);
        }
    })
    .catch(function(err) { console.error('Failed to update action type:', err); });
}

// ── Editable Title ────────────────────────────────────────────────────
function toggleTitleEdit(taskId) {
    var display = document.getElementById('title-display-' + taskId);
    var edit = document.getElementById('title-edit-' + taskId);
    if (!display || !edit) return;

    if (edit.style.display === 'none') {
        display.style.display = 'none';
        edit.style.display = 'block';
        edit.focus();
        edit.select();
    } else {
        edit.style.display = 'none';
        display.style.display = '';
    }
}

function saveTitle(taskId) {
    var edit = document.getElementById('title-edit-' + taskId);
    if (!edit) return;

    var task = tasks.find(function(t) { return t.id === taskId; });
    var newTitle = edit.value.trim();
    var oldTitle = task ? (task.title || '') : '';

    var display = document.getElementById('title-display-' + taskId);
    if (display) display.style.display = '';
    edit.style.display = 'none';

    if (!newTitle || newTitle === oldTitle) return;

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            title: newTitle,
            raw_input: newTitle,
            description: null,
            key_people: null,
            coaching_text: null,
            skill_output: null,
            cowork_prompt: null
        })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            renderDetailPane(data.task);
            // Title changed — trigger re-parse so description, key_people, coaching update
            refreshTask(taskId);
        }
    })
    .catch(function(err) { console.error('Failed to save title:', err); });
}

// ── Editable Description ──────────────────────────────────────────────
function toggleDescriptionEdit(taskId) {
    var display = document.getElementById('desc-display-' + taskId);
    var edit = document.getElementById('desc-edit-' + taskId);
    if (!display || !edit) return;

    if (edit.style.display === 'none') {
        display.style.display = 'none';
        edit.style.display = 'block';
        edit.focus();
    } else {
        edit.style.display = 'none';
        display.style.display = 'block';
    }
}

function saveDescription(taskId) {
    var edit = document.getElementById('desc-edit-' + taskId);
    if (!edit) return;

    var task = tasks.find(function(t) { return t.id === taskId; });
    var newDesc = edit.value;
    var oldDesc = task ? (task.description || '') : '';

    // Hide edit, show display
    var display = document.getElementById('desc-display-' + taskId);
    if (display) display.style.display = 'block';
    edit.style.display = 'none';

    // Only save if changed
    if (newDesc === oldDesc) return;

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ description: newDesc })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Failed to save description:', err); });
}

// ── Editable Coaching ─────────────────────────────────────────────────
function toggleCoachingEdit(taskId) {
    var display = document.getElementById('coaching-display-' + taskId);
    var edit = document.getElementById('coaching-edit-' + taskId);
    if (!display || !edit) return;

    if (edit.style.display === 'none') {
        display.style.display = 'none';
        edit.style.display = 'block';
        edit.focus();
    } else {
        edit.style.display = 'none';
        display.style.display = 'block';
    }
}

function saveCoaching(taskId) {
    var edit = document.getElementById('coaching-edit-' + taskId);
    if (!edit) return;

    var task = tasks.find(function(t) { return t.id === taskId; });
    var newText = edit.value;
    var oldText = task ? (task.coaching_text || '') : '';

    var display = document.getElementById('coaching-display-' + taskId);
    if (display) display.style.display = 'block';
    edit.style.display = 'none';

    if (newText === oldText) return;

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ coaching_text: newText })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Failed to save coaching:', err); });
}

// ── Action Buttons (bottom bar — clear primary + secondary actions) ───
function getActionButtons(task) {
    var html = '';

    if (task.status === 'suggested') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'promote\')">Accept Task</button>';
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'waiting\')">Waiting</button>';
        html += '<button class="btn btn-subtle" onclick="doAction(' + task.id + ',\'dismiss\')">Dismiss</button>';
    } else if (task.status === 'active' || task.status === 'in_progress') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'complete\')">Mark Complete</button>';
        html += renderSnoozeButton(task);
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'waiting\')">Waiting</button>';
        html += '<button class="btn btn-subtle" onclick="doAction(' + task.id + ',\'dismiss\')">Dismiss</button>';
    } else if (task.status === 'waiting') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'transition\',\'active\')">Move to Active</button>';
        html += renderSnoozeButton(task);
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'complete\')">Mark Complete</button>';
    } else if (task.status === 'snoozed') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'transition\',\'active\')">Wake Up</button>';
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'complete\')">Mark Complete</button>';
        html += '<button class="btn btn-subtle" onclick="doAction(' + task.id + ',\'dismiss\')">Dismiss</button>';
    } else if (task.status === 'completed') {
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'active\')">Reopen</button>';
    } else if (task.status === 'dismissed') {
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'active\')">Restore</button>';
    }

    return html;
}

// ── Task Actions ───────────────────────────────────────────────────────
function doAction(taskId, action, status) {
    var body = { action: action };
    if (status) body.status = status;

    // Returned so callers can sequence follow-up work, such as advancing the
    // keyboard selection once the list has re-rendered.
    return fetch('/api/tasks/' + taskId + '/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(res) {
        if (!res.ok) return res.json().then(function(d) { throw new Error(d.error || 'Action failed'); });
        return res.json();
    })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            if (selectedTaskId === data.task.id) renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Action failed:', err.message); });
}

function deleteTask(taskId) {
    // For suggested tasks, dismiss instead of delete
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (task && task.status === 'suggested') {
        doAction(taskId, 'dismiss');
    } else {
        // Soft delete — moves to 'deleted' status, recoverable
        doAction(taskId, 'transition', 'deleted');
    }
}

function refreshTask(taskId) {
    // Reset to unparsed — the Stop hook will prompt Claude to re-enrich it
    fetch('/api/tasks/' + taskId + '/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Refresh failed:', err); });
}

function permanentDeleteTask(taskId) {
    fetch('/api/tasks/' + taskId, { method: 'DELETE' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            tasks = tasks.filter(function(t) { return t.id !== taskId; });
            renderTaskList();
            if (selectedTaskId === taskId) clearDetailPane();
        })
        .catch(function(err) { console.error('Delete failed:', err); });
}

// ── Timestamped Notes ──────────────────────────────────────────────────
function addTimestampedNote(taskId) {
    var input = document.getElementById('notes-add-input');
    if (!input || !input.value.trim()) return;
    var now = new Date();
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var h = now.getHours(), m = now.getMinutes();
    var ampm = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    var stamp = '[' + months[now.getMonth()] + ' ' + now.getDate() + ', '
        + h + ':' + (m < 10 ? '0' : '') + m + ' ' + ampm + '] ';
    var entry = stamp + input.value.trim();
    var textarea = document.getElementById('notes-textarea');
    var existing = textarea ? textarea.value.trim() : '';
    var newNotes = existing ? entry + '\n' + existing : entry;
    if (textarea) textarea.value = newNotes;
    input.value = '';
    // Save immediately
    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_notes: newNotes })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
        }
    })
    .catch(function(err) { console.error('Failed to save timestamped note:', err); });
}

// ── Save Notes ─────────────────────────────────────────────────────────
function saveNotes(taskId) {
    var textarea = document.getElementById('notes-textarea');
    if (!textarea) return;

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_notes: textarea.value })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
        }
    })
    .catch(function(err) { console.error('Failed to save notes:', err); });
}

// ── Toggle Sections ────────────────────────────────────────────────────
function toggleSection(sectionId) {
    var body = document.getElementById('body-' + sectionId);
    var toggle = document.getElementById('toggle-' + sectionId);

    if (body.classList.contains('collapsed')) {
        // Lazy-load terminal sections on first expand
        if (TERMINAL_SECTIONS.indexOf(sectionId) !== -1 && !_loadedSections[sectionId]) {
            fetchSectionTasks(sectionId).then(function() {
                body.classList.remove('collapsed');
                toggle.innerHTML = '&#9662;'; // ▾
            });
            return;
        }
        body.classList.remove('collapsed');
        toggle.innerHTML = '&#9662;'; // ▾
    } else {
        body.classList.add('collapsed');
        toggle.innerHTML = '&#9656;'; // ▸
    }
}

// ── Drag and Drop ─────────────────────────────────────────────────────
var ALL_SECTIONS = ['active', 'suggested', 'waiting', 'snoozed', 'completed', 'dismissed', 'deleted'];

function setupDropZones() {
    ALL_SECTIONS.forEach(function(sectionId) {
        var body = document.getElementById('body-' + sectionId);
        if (!body) return;

        body.addEventListener('dragover', function(e) {
            var sourceStatus = e.dataTransfer.types.indexOf('text/x-status') !== -1
                ? _dragSourceStatus : null;
            if (sourceStatus && isValidDrop(sourceStatus, sectionId)) {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
            }
        });

        body.addEventListener('dragenter', function(e) {
            e.preventDefault();
            var sourceStatus = _dragSourceStatus;
            if (sourceStatus && isValidDrop(sourceStatus, sectionId)) {
                body.classList.add('drop-target');
            }
        });

        body.addEventListener('dragleave', function(e) {
            // Only remove if leaving the body element itself (not entering a child)
            if (!body.contains(e.relatedTarget)) {
                body.classList.remove('drop-target');
            }
        });

        body.addEventListener('drop', function(e) {
            e.preventDefault();
            body.classList.remove('drop-target');
            var taskId = parseInt(e.dataTransfer.getData('text/x-task-id'));
            var sourceStatus = e.dataTransfer.getData('text/x-status');
            if (!taskId || !sourceStatus) return;
            executeDrop(taskId, sourceStatus, sectionId);
        });
    });

    // Attach dragstart/dragend at the task-list level (delegated)
    var taskList = document.querySelector('.task-list');
    if (taskList) {
        taskList.addEventListener('dragstart', function(e) {
            var row = e.target.closest('.task-row');
            if (!row) return;
            var taskId = row.getAttribute('data-id');
            var status = row.getAttribute('data-status');
            e.dataTransfer.setData('text/x-task-id', taskId);
            e.dataTransfer.setData('text/x-status', status);
            e.dataTransfer.effectAllowed = 'move';
            _dragSourceStatus = status;
            row.classList.add('dragging');
            // Highlight eligible drop zones
            requestAnimationFrame(function() {
                highlightEligibleZones(status);
            });
        });

        taskList.addEventListener('dragend', function(e) {
            var row = e.target.closest('.task-row');
            if (row) row.classList.remove('dragging');
            _dragSourceStatus = null;
            clearDropHighlights();
        });
    }
}

var _dragSourceStatus = null;

function isValidDrop(sourceStatus, targetSectionId) {
    if (sourceStatus === targetSectionId) return false;
    var allowed = VALID_TRANSITIONS[sourceStatus];
    if (!allowed) return false;
    return allowed.indexOf(targetSectionId) !== -1;
}

function highlightEligibleZones(sourceStatus) {
    ALL_SECTIONS.forEach(function(sectionId) {
        var body = document.getElementById('body-' + sectionId);
        if (!body) return;
        if (isValidDrop(sourceStatus, sectionId)) {
            body.classList.add('drop-eligible');
        }
    });
}

function clearDropHighlights() {
    ALL_SECTIONS.forEach(function(sectionId) {
        var body = document.getElementById('body-' + sectionId);
        if (!body) return;
        body.classList.remove('drop-eligible');
        body.classList.remove('drop-target');
    });
}

function executeDrop(taskId, sourceStatus, targetStatus) {
    // Use named actions for special transitions that trigger server-side behavior
    if (sourceStatus === 'suggested' && targetStatus === 'active') {
        doAction(taskId, 'promote');
    } else if (targetStatus === 'snoozed') {
        doSnooze(taskId, { duration_minutes: 60 });
    } else if (targetStatus === 'in_progress') {
        doAction(taskId, 'start');
    } else if (targetStatus === 'completed') {
        doAction(taskId, 'complete');
    } else if (targetStatus === 'dismissed') {
        doAction(taskId, 'dismiss');
    } else {
        doAction(taskId, 'transition', targetStatus);
    }
}

// ── Snooze ─────────────────────────────────────────────────────────────
function renderSnoozeButton(task) {
    var taskId = task.id;
    var oofOption = '';
    var activity = parseWaitingActivity(task);
    if (activity && activity.status === 'out_of_office' && activity.return_date) {
        var returnDate = new Date(activity.return_date + 'T09:00:00');
        if (returnDate > new Date()) {
            var firstName = getOofPersonFirstName(task);
            var dateLabel = formatOofDate(activity.return_date);
            oofOption = '<div class="snooze-option snooze-option-oof" onclick="event.stopPropagation(); snoozeUntilReturn(' + taskId + ', \'' + escapeHtml(activity.return_date) + '\')">'
                + 'Until ' + escapeHtml(firstName) + ' returns (' + escapeHtml(dateLabel) + ')'
                + '</div>';
        }
    }
    // Pre-fill date picker with day-before-due if task has a future due date
    var defaultDate = '';
    var dateHint = '';
    if (task.due_date) {
        var dueParts = task.due_date.split('-');
        var dueDate = new Date(parseInt(dueParts[0]), parseInt(dueParts[1]) - 1, parseInt(dueParts[2]));
        var dayBefore = new Date(dueDate);
        dayBefore.setDate(dayBefore.getDate() - 1);
        dayBefore.setHours(9, 0, 0, 0);
        if (dayBefore > new Date()) {
            var yy = dayBefore.getFullYear();
            var mm = ('0' + (dayBefore.getMonth() + 1)).slice(-2);
            var dd = ('0' + dayBefore.getDate()).slice(-2);
            defaultDate = yy + '-' + mm + '-' + dd;
            dateHint = '<div class="snooze-date-hint">Day before due (' + escapeHtml(formatDate(task.due_date)) + ')</div>';
        }
    }

    return '<div class="snooze-btn-wrapper" style="display:inline-block;position:relative">'
        + '<button class="btn btn-snooze" onclick="event.stopPropagation(); toggleSnoozeDropdown(' + taskId + ')">Snooze</button>'
        + '<div class="snooze-dropdown" id="snooze-dropdown-' + taskId + '">'
        + oofOption
        + '<div class="snooze-option" onclick="event.stopPropagation(); doSnooze(' + taskId + ',{duration_minutes:60})">1 hour</div>'
        + '<div class="snooze-option" onclick="event.stopPropagation(); doSnooze(' + taskId + ',{duration_minutes:240})">4 hours</div>'
        + renderWeekdaySnoozeRow(taskId)
        + '<div class="snooze-option snooze-custom">'
        + '<label class="snooze-date-label">Pick date &amp; time:</label>'
        + dateHint
        + '<div class="snooze-custom-row">'
        + '<input type="date" class="snooze-date-input" id="snooze-date-' + taskId + '"'
        + (defaultDate ? ' value="' + defaultDate + '"' : '')
        + ' onclick="event.stopPropagation(); openNativePicker(this)">'
        + '<input type="time" class="snooze-time-input" id="snooze-time-' + taskId + '" value="09:00" onclick="event.stopPropagation(); openNativePicker(this)">'
        + '<button class="snooze-go-btn" onclick="event.stopPropagation(); doSnoozeCustom(' + taskId + ')">Go</button>'
        + '</div>'
        + '</div>'
        + '</div>'
        + '</div>';
}

function toggleSnoozeDropdown(taskId) {
    // Close any other open snooze dropdowns
    document.querySelectorAll('.snooze-dropdown.open').forEach(function(d) {
        d.classList.remove('open');
    });
    var dd = document.getElementById('snooze-dropdown-' + taskId);
    if (dd) dd.classList.toggle('open');
}

function doSnooze(taskId, opts) {
    var body = { action: 'snooze' };
    if (opts.duration_minutes) body.duration_minutes = opts.duration_minutes;
    if (opts.snoozed_until) body.snoozed_until = opts.snoozed_until;

    fetch('/api/tasks/' + taskId + '/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(res) {
        if (!res.ok) return res.json().then(function(d) { throw new Error(d.error || 'Snooze failed'); });
        return res.json();
    })
    .then(function(data) {
        if (data.task) {
            var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
            if (idx >= 0) tasks[idx] = data.task;
            renderTaskList();
            if (selectedTaskId === data.task.id) renderDetailPane(data.task);
        }
    })
    .catch(function(err) { console.error('Snooze failed:', err.message); });

    // Close dropdown
    document.querySelectorAll('.snooze-dropdown.open').forEach(function(d) {
        d.classList.remove('open');
    });
}

function doSnoozeCustom(taskId) {
    var dateInput = document.getElementById('snooze-date-' + taskId);
    var timeInput = document.getElementById('snooze-time-' + taskId);
    if (!dateInput || !dateInput.value) return;
    var dateParts = dateInput.value.split('-');
    var timeParts = (timeInput && timeInput.value ? timeInput.value : '09:00').split(':');
    var d = new Date(
        parseInt(dateParts[0]), parseInt(dateParts[1]) - 1, parseInt(dateParts[2]),
        parseInt(timeParts[0]), parseInt(timeParts[1]), 0
    );
    doSnooze(taskId, { snoozed_until: d.toISOString() });
}

/**
 * The next `count` weekdays after `now`, as calendar-day offsets.
 *
 * Weekends are skipped: the row is labelled "9 AM:" and a Saturday morning
 * reminder is not what it is for. Offsets are calendar days so that
 * `snoozeToDay`'s `setDate()` arithmetic stays DST-safe.
 */
function nextWeekdaySnoozeDays(now, count) {
    var days = [];
    var cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var offset = 0;
    while (days.length < count && offset < 14) {
        offset++;
        cursor.setDate(cursor.getDate() + 1);
        var dow = cursor.getDay();
        if (dow === 0 || dow === 6) continue;
        days.push({ offset: offset, date: new Date(cursor.getTime()) });
    }
    return days;
}

function renderWeekdaySnoozeRow(taskId, now) {
    now = now || new Date();
    var dayNames = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var days = nextWeekdaySnoozeDays(now, 4);
    var buttons = '';

    for (var i = 0; i < days.length; i++) {
        var day = days[i];
        // The soonest option stays emphasised, whichever weekday it lands on.
        var leadClass = i === 0 ? ' snooze-weekday-tomorrow' : '';
        var title = day.date.toLocaleDateString('en-US', {
            weekday: 'long', month: 'short', day: 'numeric'
        });
        buttons += '<button class="snooze-weekday-btn' + leadClass + '"'
            + ' title="' + escapeHtml(title) + '"'
            + ' onclick="event.stopPropagation(); snoozeToDay(' + taskId + ',' + day.offset + ')">'
            + dayNames[day.date.getDay()] + '</button>';
    }

    return '<div class="snooze-weekday-row">'
        + '<span class="snooze-weekday-label">9 AM:</span>'
        + buttons
        + '</div>';
}

/**
 * Open a native date/time picker from a click anywhere on the field.
 *
 * Without this, `<input type="date">` only drops its calendar when the small
 * icon is hit; clicking the text puts a caret in `mm/dd/yyyy` and the date has
 * to be typed. `showPicker` needs user activation and is absent on older
 * browsers, so both failure modes fall back to plain typing.
 */
function openNativePicker(el) {
    try {
        if (el && typeof el.showPicker === 'function') el.showPicker();
    } catch (err) {
        /* no user activation, or unsupported - typing still works */
    }
}

function snoozeToDay(taskId, daysOffset) {
    var d = new Date();
    d.setDate(d.getDate() + daysOffset);
    d.setHours(9, 0, 0, 0);
    doSnooze(taskId, { snoozed_until: d.toISOString() });
}

function getOofPersonFirstName(task) {
    var people = parsePeople(task.key_people);
    if (people.length > 0 && people[0].name) {
        return people[0].name.split(' ')[0];
    }
    return 'them';
}

function snoozeUntilReturn(taskId, returnDate) {
    var parts = returnDate.split('-');
    var d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]), 9, 0, 0, 0);
    doSnooze(taskId, { snoozed_until: d.toISOString() });
}


function formatOofDate(dateStr) {
    if (!dateStr) return '';
    var parts = dateStr.split('-');
    var d = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate();
}

function formatSnoozeTime(isoString) {
    if (!isoString) return '';
    var d = new Date(isoString);
    var now = new Date();
    var diffMs = d - now;

    // If less than 24 hours away, show time only
    if (diffMs > 0 && diffMs < 24 * 60 * 60 * 1000) {
        return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    }
    // Otherwise show day + time
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return days[d.getDay()] + ' ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

// Close snooze dropdowns when clicking outside
document.addEventListener('click', function(e) {
    if (!e.target.closest('.snooze-btn-wrapper')) {
        document.querySelectorAll('.snooze-dropdown.open').forEach(function(d) {
            d.classList.remove('open');
        });
    }
});

// ── Waiting Activity ───────────────────────────────────────────────────
function parseWaitingActivity(task) {
    if (!task.waiting_activity) return null;
    try { return JSON.parse(task.waiting_activity); } catch (e) { return null; }
}

function waitingActivityIcon(task) {
    if (task.status !== 'waiting' && task.status !== 'snoozed') return '';
    var activity = parseWaitingActivity(task);
    if (!activity) return '';

    // OOO badge — shown for both waiting and snoozed tasks
    if (activity.status === 'out_of_office') {
        var returnInfo = activity.return_date ? 'OOO until ' + formatOofDate(activity.return_date) : 'Out of office';
        return '<span class="ooo-badge" title="' + escapeHtml(returnInfo) + '">OOO</span>';
    }

    // For snoozed tasks, only show OOO badge (not other waiting icons)
    if (task.status === 'snoozed') return '';

    var icons = {
        no_activity: '\uD83D\uDCA4',       // sleeping face
        activity_detected: '\uD83D\uDCAC',  // speech bubble
        may_be_resolved: '\u2705'           // checkmark
    };
    var tooltips = {
        no_activity: 'No response \u2014 checked ' + timeAgo(activity.checked_at),
        activity_detected: 'Activity detected \u2014 ' + truncate(activity.summary, 60),
        may_be_resolved: 'May be resolved \u2014 ' + truncate(activity.summary, 60)
    };
    var icon = icons[activity.status] || '';
    var tooltip = tooltips[activity.status] || '';
    if (!icon) return '';
    return '<span class="waiting-activity-icon activity-status-' + activity.status + '" title="' + escapeHtml(tooltip) + '">' + icon + '</span>';
}

function renderWaitingActivityCard(task) {
    var activity = parseWaitingActivity(task);
    if (!activity) {
        if (task.status === 'waiting') {
            return '<div class="waiting-activity-card">'
                + '<div class="detail-label">Activity Check</div>'
                + '<div class="waiting-activity-body">'
                + '<span class="waiting-activity-status">Not checked yet</span>'
                + '<button class="btn btn-sm" id="check-now-btn" onclick="requestWaitingCheckSingle(' + task.id + ')" style="margin-left:auto">Check Now</button>'
                + '</div>'
                + '</div>';
        }
        return '';
    }
    var icons = { no_activity: '\uD83D\uDCA4', activity_detected: '\uD83D\uDCAC', may_be_resolved: '\u2705', out_of_office: '' };
    var labels = { no_activity: 'No activity', activity_detected: 'Activity detected', may_be_resolved: 'May be resolved', out_of_office: 'Out of office' };
    var icon = icons[activity.status] || '';
    var label = labels[activity.status] || activity.status;
    if (activity.status === 'out_of_office') {
        icon = '<span class="ooo-badge">OOO</span>';
        if (activity.return_date) {
            label += ' until ' + formatOofDate(activity.return_date);
        }
    }

    return '<div class="waiting-activity-card">'
        + '<div class="detail-label">Activity Check</div>'
        + '<div class="waiting-activity-body">'
        + '<span class="waiting-activity-status activity-status-' + activity.status + '">'
        + icon + ' ' + escapeHtml(label)
        + '</span>'
        + '<button class="btn btn-sm" id="check-now-btn" onclick="requestWaitingCheckSingle(' + task.id + ')" style="margin-left:auto">Check Now</button>'
        + '</div>'
        + '<div class="waiting-activity-summary">' + escapeHtml(activity.summary) + '</div>'
        + '<div class="waiting-activity-checked">Checked ' + timeAgo(activity.checked_at) + '</div>'
        + '</div>';
}

var _waitingCheckPollTimer = null;

function requestWaitingCheck() {
    var btn = document.getElementById('waiting-check-btn');
    if (btn && btn.classList.contains('syncing')) return;
    if (btn) {
        btn.classList.add('syncing');
        btn.title = 'Checking activity...';
    }

    fetch('/api/sync-status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ waiting_check: true })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.ok) {
            _startWaitingCheckPoll();
        } else {
            if (btn) {
                btn.classList.remove('syncing');
                btn.title = 'Check for activity from key people';
            }
        }
    })
    .catch(function(err) {
        if (btn) {
            btn.classList.remove('syncing');
            btn.title = 'Check for activity from key people';
        }
        console.error('Waiting check request failed:', err);
    });
}

function requestWaitingCheckSingle(taskId) {
    var checkBtn = document.getElementById('check-now-btn');
    if (checkBtn) {
        checkBtn.disabled = true;
        checkBtn.textContent = 'Checking\u2026';
    }
    requestWaitingCheck();
}

function refreshAllWaiting() {
    var btn = document.getElementById('waiting-refresh-btn');
    if (btn && btn.classList.contains('syncing')) return;
    if (btn) {
        btn.classList.add('syncing');
        btn.title = 'Refreshing waiting tasks...';
    }

    var waitingTasks = tasks.filter(function(t) { return t.status === 'waiting'; });
    if (!waitingTasks.length) {
        if (btn) {
            btn.classList.remove('syncing');
            btn.title = 'Refresh all waiting tasks with AI';
        }
        return;
    }

    // Trigger refresh on each waiting task
    var promises = waitingTasks.map(function(t) {
        return fetch('/api/tasks/' + t.id + '/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }).then(function(res) { return res.json(); });
    });

    Promise.all(promises).then(function(results) {
        results.forEach(function(data) {
            if (data.task) {
                var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
                if (idx >= 0) tasks[idx] = data.task;
            }
        });
        renderTaskList();
        if (selectedTaskId) {
            var sel = tasks.find(function(t) { return t.id === selectedTaskId; });
            if (sel) renderDetailPane(sel);
        }
        if (btn) {
            btn.classList.remove('syncing');
            btn.title = 'Refresh all waiting tasks with AI';
        }
    }).catch(function(err) {
        if (btn) {
            btn.classList.remove('syncing');
            btn.title = 'Refresh all waiting tasks with AI';
        }
        console.error('Refresh all waiting failed:', err);
    });
}

function _startWaitingCheckPoll() {
    if (_waitingCheckPollTimer) return;
    _waitingCheckPollTimer = setInterval(function() {
        fetch('/api/runner-status')
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (!data['waiting-check']) {
                    // Finished
                    _stopWaitingCheckPoll();
                    var btn = document.getElementById('waiting-check-btn');
                    if (btn) {
                        btn.classList.remove('syncing');
                        btn.title = 'Check for activity from key people';
                    }
                    // Re-fetch tasks and refresh detail pane
                    fetchTasks();
                }
            })
            .catch(function() {});
    }, 5000);
}

function _stopWaitingCheckPoll() {
    if (_waitingCheckPollTimer) {
        clearInterval(_waitingCheckPollTimer);
        _waitingCheckPollTimer = null;
    }
}

// ── Suggestion Check ──────────────────────────────────────────────────
function _suggestionCheckTooltip() {
    var suggested = tasks.filter(function(t) { return t.status === 'suggested'; });
    var checked = suggested.filter(function(t) { return parseWaitingActivity(t); }).length;
    return 'Check if suggestions are already resolved (' + checked + '/' + suggested.length + ' checked)';
}

function suggestionCheckBadge(task) {
    if (task.status !== 'suggested') return '';
    var activity = parseWaitingActivity(task);
    if (!activity) return '';

    var cfg = {
        likely_resolved: { icon: '\u2713', label: 'Done?', cls: 'resolved' },
        still_pending:   { icon: '\u23F3', label: 'Pending', cls: 'pending' },
        unclear:         { icon: '?', label: 'Unclear', cls: 'unclear' }
    };
    var c = cfg[activity.status];
    if (!c) return '';

    var tooltip = escapeHtml((activity.summary || '') + ' \u2014 checked ' + timeAgo(activity.checked_at));
    return '<span class="suggestion-check-badge sc-' + c.cls + '" title="' + tooltip + '">'
        + c.icon + ' ' + c.label + '</span>';
}

function renderSuggestionCheckCard(task) {
    if (task.status !== 'suggested') return '';
    var activity = parseWaitingActivity(task);
    if (!activity) {
        return '<div class="waiting-activity-card">'
            + '<div class="detail-label">Suggestion Check</div>'
            + '<div class="waiting-activity-body">'
            + '<span class="waiting-activity-status">Not checked yet</span>'
            + '<button class="btn btn-sm" onclick="requestSuggestionCheck()" style="margin-left:auto">Check Now</button>'
            + '</div>'
            + '</div>';
    }

    var cfg = {
        likely_resolved: { icon: '\u2713', label: 'Likely done' },
        still_pending:   { icon: '\u23F3', label: 'Still pending' },
        unclear:         { icon: '?', label: 'Unclear' }
    };
    var c = cfg[activity.status] || { icon: '', label: activity.status };

    var dismissBtn = '';
    if (activity.status === 'likely_resolved') {
        dismissBtn = '<button class="btn btn-sm btn-primary" onclick="doAction(' + task.id + ',\'dismiss\')" style="margin-left:auto">Dismiss \u2014 Already Done</button>';
    } else {
        dismissBtn = '<button class="btn btn-sm" onclick="requestSuggestionCheck()" style="margin-left:auto">Re-check</button>';
    }

    return '<div class="waiting-activity-card">'
        + '<div class="detail-label">Suggestion Check</div>'
        + '<div class="waiting-activity-body">'
        + '<span class="waiting-activity-status sc-' + (activity.status === 'likely_resolved' ? 'resolved' : activity.status === 'still_pending' ? 'pending' : 'unclear') + '">'
        + c.icon + ' ' + escapeHtml(c.label)
        + '</span>'
        + dismissBtn
        + '</div>'
        + '<div class="waiting-activity-summary">' + escapeHtml(activity.summary || '') + '</div>'
        + '<div class="waiting-activity-checked">Checked ' + timeAgo(activity.checked_at) + '</div>'
        + '</div>';
}

var _suggestionCheckPollTimer = null;

function requestSuggestionCheck() {
    var btn = document.getElementById('suggestion-check-btn');
    if (btn && btn.classList.contains('syncing')) return;
    if (btn) {
        btn.classList.add('syncing');
        btn.title = 'Checking suggestions...';
    }

    fetch('/api/sync-status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ suggestion_check: true })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.ok || (data.message && data.message.toLowerCase().indexOf('already running') !== -1)) {
            _startSuggestionCheckPoll();
        } else {
            if (btn) {
                btn.classList.remove('syncing');
                btn.title = _suggestionCheckTooltip();
            }
        }
    })
    .catch(function(err) {
        if (btn) {
            btn.classList.remove('syncing');
            btn.title = _suggestionCheckTooltip();
        }
        console.error('Suggestion check request failed:', err);
    });
}

function _startSuggestionCheckPoll() {
    if (_suggestionCheckPollTimer) return;
    _suggestionCheckPollTimer = setInterval(function() {
        fetch('/api/runner-status')
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (!data['suggestion-check']) {
                    _stopSuggestionCheckPoll();
                    var btn = document.getElementById('suggestion-check-btn');
                    if (btn) {
                        btn.classList.remove('syncing');
                        btn.title = _suggestionCheckTooltip();
                    }
                    fetchTasks();
                }
            })
            .catch(function() {});
    }, 5000);
}

function _stopSuggestionCheckPoll() {
    if (_suggestionCheckPollTimer) {
        clearInterval(_suggestionCheckPollTimer);
        _suggestionCheckPollTimer = null;
    }
}

function batchDismissResolved() {
    var resolved = tasks.filter(function(t) {
        if (t.status !== 'suggested') return false;
        var a = parseWaitingActivity(t);
        return a && a.status === 'likely_resolved';
    });
    if (!resolved.length) return;
    if (!confirm('Dismiss ' + resolved.length + ' resolved suggestion' + (resolved.length > 1 ? 's' : '') + '?')) return;

    var promises = resolved.map(function(t) {
        return fetch('/api/tasks/' + t.id + '/action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'dismiss' })
        }).then(function(res) { return res.json(); });
    });

    Promise.all(promises).then(function(results) {
        results.forEach(function(data) {
            if (data.task) {
                var idx = tasks.findIndex(function(t) { return t.id === data.task.id; });
                if (idx >= 0) tasks[idx] = data.task;
            }
        });
        renderTaskList();
        if (selectedTaskId) {
            var sel = tasks.find(function(t) { return t.id === selectedTaskId; });
            if (sel) renderDetailPane(sel);
            else clearDetailPane();
        }
    }).catch(function(err) {
        console.error('Batch dismiss failed:', err);
    });
}

// ── Utilities ──────────────────────────────────────────────────────────
function timeAgo(isoString) {
    if (!isoString) return 'never';
    var now = new Date();
    var date = new Date(isoString);
    var seconds = Math.floor((now - date) / 1000);

    if (seconds < 0) return 'just now';
    if (seconds < 60) return seconds + 's ago';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + ' min ago';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' hr ago';
    var days = Math.floor(hours / 24);
    if (days < 30) return days + 'd ago';
    var months = Math.floor(days / 30);
    return months + 'mo ago';
}

function formatDate(dateStr) {
    if (!dateStr) return '';
    var d = new Date(dateStr);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    return days[d.getDay()] + ', ' + months[d.getMonth()] + ' ' + d.getDate();
}

function priorityDot(priority, taskId) {
    // Harvey balls removed at Phil's request. Priority is still carried by the
    // P1-P5 pill in the detail pane, so nothing is lost; the list just stops
    // encoding it in a glyph most people have to decode.
    return '';
}

function parseStatusIcon(parseStatus) {
    var status = parseStatus || 'parsed';
    // Only states that mean "something is still owed" are worth a row-level
    // indicator. A green tick on every parsed task is the majority case, so it
    // carries no information and just adds noise to scan past. An error still
    // shows, because that one IS actionable.
    if (status === 'parsed') return '';
    return '<span class="parse-icon"><span class="parse-indicator ' + status + '"><span class="parse-ring"></span></span></span>';
}

function parseStatusBadge(parseStatus, taskId) {
    var status = parseStatus || 'parsed';
    var labels = {
        unparsed: 'Awaiting parse',
        queued: 'Queued',
        parsing: 'Parsing\u2026',
        parsed: 'Parsed',
        error: 'Error'
    };
    var label = labels[status] || status;
    // Make unparsed/queued/parsed/error clickable to trigger refresh
    if (taskId && (status === 'unparsed' || status === 'queued' || status === 'parsed' || status === 'error')) {
        return '<span class="parse-status-badge ' + status + ' clickable" '
            + 'onclick="event.stopPropagation(); refreshTask(' + taskId + ')" '
            + 'title="Click to ' + (status === 'error' ? 'retry' : 'refresh with AI') + '">'
            + '<span class="parse-indicator ' + status + '"><span class="parse-ring"></span></span>'
            + escapeHtml(label)
            + '</span>';
    }
    return '<span class="parse-status-badge ' + status + '">'
        + '<span class="parse-indicator ' + status + '"><span class="parse-ring"></span></span>'
        + escapeHtml(label)
        + '</span>';
}

function quickHitToggle(task) {
    var active = task.is_quick_hit ? ' active' : '';
    return '<button class="quick-hit-toggle' + active + '" '
        + 'onclick="toggleQuickHit(' + task.id + ')" '
        + 'title="' + (task.is_quick_hit ? 'Remove quick hit' : 'Mark as quick hit') + '">'
        + '&#9201;</button>';
}

function toggleQuickHit(taskId) {
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (!task) return;
    var newVal = task.is_quick_hit ? 0 : 1;
    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_quick_hit: newVal })
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.task) {
            Object.assign(task, data.task);
            renderTaskList();
            renderDetailPane(task);
        }
    });
}

function sourceMetaLink(task, includeSnippet) {
    // Extract the original subject from source_id (format: type::email::subject)
    var subject = '';
    if (task.source_id) {
        var parts = task.source_id.split('::');
        if (parts.length >= 3) subject = parts.slice(2).join('::');
    }
    // Detail cards already render the snippet as a quote; callers can omit it
    // here so source metadata does not repeat the same content.
    var snippetLabel = includeSnippet === false ? '' : task.source_snippet;
    var label = subject || (snippetLabel ? truncate(snippetLabel, 50) : (task.source_type || 'manual'));
    if (task.source_url) {
        var icon = sourceTypeIcon(task.source_type);
        return icon + ' <a href="' + escapeHtml(task.source_url) + '" target="_blank" '
            + 'onclick="event.stopPropagation()" '
            + 'class="source-meta-link" title="Open in Outlook/Teams">'
            + escapeHtml(label) + ' &#8599;</a>'
            + ' <span class="source-edit-icon" title="Edit source">&#9998;</span>';
    }
    // No URL yet — single pencil serves as both icon and edit affordance
    return '<span class="source-edit-icon" title="Click to set source">&#9998; ' + escapeHtml(label) + '</span>';
}

function sourceTypeIcon(sourceType) {
    var icons = {
        email: '&#9993;',
        meeting: '&#128197;',
        chat: '&#128172;',
        manual: '&#9998;'
    };
    return '<span class="source-icon">' + (icons[sourceType] || icons.manual) + '</span>';
}

function detectSourceType(url) {
    if (!url) return null;
    if (/teams\.microsoft\.com|teams\.live\.com/i.test(url)) return 'chat';
    if (/outlook\.(office|live|com)|mail\./i.test(url)) return 'email';
    if (/calendar\.|event/i.test(url)) return 'meeting';
    return null;
}

function openSourceModal(taskId) {
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (!task) return;
    var currentUrl = task.source_url || '';
    var currentType = task.source_type || 'manual';

    // Remove existing modal if any
    var old = document.getElementById('source-modal');
    if (old) old.remove();

    var overlay = document.createElement('div');
    overlay.id = 'source-modal';
    overlay.className = 'source-modal-overlay';
    overlay.innerHTML = '<div class="source-modal">'
        + '<div class="source-modal-header">Edit Source</div>'
        + '<label class="source-modal-label">URL</label>'
        + '<input type="text" id="source-modal-url" class="source-modal-input" '
        + 'placeholder="Paste Teams or Outlook URL..." value="' + escapeHtml(currentUrl) + '">'
        + '<label class="source-modal-label">Type: <span id="source-modal-type-display">' + escapeHtml(currentType) + '</span></label>'
        + '<div class="source-modal-buttons">'
        + '<button class="btn-source-modal btn-source-cancel" onclick="closeSourceModal()">Cancel</button>'
        + '<button class="btn-source-modal btn-source-save" onclick="saveSourceModal(' + taskId + ')">Save</button>'
        + '</div></div>';
    document.body.appendChild(overlay);

    var urlInput = document.getElementById('source-modal-url');
    urlInput.focus();
    urlInput.select();

    // Live-detect type as user types/pastes
    urlInput.addEventListener('input', function() {
        var detected = detectSourceType(urlInput.value) || currentType;
        document.getElementById('source-modal-type-display').textContent = detected;
    });

    // Enter to save, Escape to cancel
    urlInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); saveSourceModal(taskId); }
        if (e.key === 'Escape') closeSourceModal();
    });

    // Click overlay to cancel
    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) closeSourceModal();
    });
}

function closeSourceModal() {
    var modal = document.getElementById('source-modal');
    if (modal) modal.remove();
}

function saveSourceModal(taskId) {
    var urlInput = document.getElementById('source-modal-url');
    if (!urlInput) return;
    var url = urlInput.value.trim();
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (!task) return;

    // If cleared, remove source
    var updates = {};
    if (!url) {
        updates = { source_url: null, source_type: 'manual', source_id: null };
    } else {
        var newType = detectSourceType(url) || task.source_type || 'manual';
        var personEmail = '';
        try {
            var people = typeof task.key_people === 'string' ? JSON.parse(task.key_people) : task.key_people;
            if (Array.isArray(people) && people.length > 0) personEmail = (people[0].email || '').toLowerCase();
        } catch(e) {}
        var subject = (task.title || '').toLowerCase().substring(0, 50);
        var newSourceId = newType + '::' + personEmail + '::' + subject;
        updates = { source_url: url, source_type: newType, source_id: newSourceId };
    }

    fetch('/api/tasks/' + taskId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updates)
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.task) closeSourceModal();
    });
}

function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    return escapeHtml(String(str || ''))
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function truncate(str, maxLen) {
    if (!str) return '';
    return str.length > maxLen ? str.substring(0, maxLen) + '...' : str;
}

// ── Rich Text with Inline People Pills ─────────────────────────────────
function renderRichText(text, keyPeople) {
    if (!text) return '';
    var people = parsePeople(keyPeople);
    if (!people.length) return escapeHtml(text);

    // Build a list of names to match (full names first, then first names)
    var replacements = [];
    people.forEach(function(p) {
        if (p.name) {
            replacements.push({ match: p.name, person: p });
        }
    });
    // Sort longest first so "Jane Doe" matches before "Jane"
    replacements.sort(function(a, b) { return b.match.length - a.match.length; });

    // Split text by matched names, replacing with inline pills
    var result = text;
    var tokens = [];
    var remaining = result;

    // Escape HTML first, then insert pill markup
    // Strategy: find all name positions, split into segments
    var segments = [];
    var lower = remaining.toLowerCase();

    // Find all match positions
    var matches = [];
    replacements.forEach(function(r) {
        var searchLower = r.match.toLowerCase();
        var startIdx = 0;
        while (true) {
            var pos = lower.indexOf(searchLower, startIdx);
            if (pos === -1) break;
            // Check it's not inside another match
            var overlaps = matches.some(function(m) {
                return pos < m.end && (pos + r.match.length) > m.start;
            });
            if (!overlaps) {
                matches.push({ start: pos, end: pos + r.match.length, person: r.person });
            }
            startIdx = pos + 1;
        }
        // Also try first name only
        var firstName = r.match.split(' ')[0];
        if (firstName.length > 2 && firstName !== r.match) {
            var fnLower = firstName.toLowerCase();
            startIdx = 0;
            while (true) {
                var pos = lower.indexOf(fnLower, startIdx);
                if (pos === -1) break;
                // Word boundary check
                var before = pos > 0 ? remaining[pos - 1] : ' ';
                var after = pos + firstName.length < remaining.length ? remaining[pos + firstName.length] : ' ';
                var isWord = /\W/.test(before) && /\W/.test(after);
                var overlaps = matches.some(function(m) {
                    return pos < m.end && (pos + firstName.length) > m.start;
                });
                if (isWord && !overlaps) {
                    matches.push({ start: pos, end: pos + firstName.length, person: r.person });
                }
                startIdx = pos + 1;
            }
        }
    });

    // Sort matches by position
    matches.sort(function(a, b) { return a.start - b.start; });

    // Build HTML from segments
    var html = '';
    var cursor = 0;
    matches.forEach(function(m) {
        if (m.start > cursor) {
            html += escapeHtml(remaining.substring(cursor, m.start));
        }
        var matchedText = remaining.substring(m.start, m.end);
        html += '<span class="inline-person-pill">'
            + '<span class="inline-pill-avatar">' + getInitials(m.person.name) + '</span>'
            + escapeHtml(matchedText)
            + '</span>';
        cursor = m.end;
    });
    if (cursor < remaining.length) {
        html += escapeHtml(remaining.substring(cursor));
    }

    return html;
}

// ── Sync Status ────────────────────────────────────────────────────────
// Server runs `claude -p /todo-refresh` every 30 min via PeriodicCallback.
// Dashboard button also triggers it on demand.
var _syncPollTimer = null;

function fetchSyncStatus() {
    fetch('/api/sync-status')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            updateSyncUI(data);
        })
        .catch(function() {});
}

function updateSyncUI(data) {
    var btn = document.getElementById('sync-btn');
    var statusText = document.getElementById('sync-status-text');

    if (data.sync_running) {
        btn.classList.add('syncing');
        btn.title = 'Sync running...';
        _startFastPoll();
    } else {
        var wasSyncing = btn.classList.contains('syncing');
        btn.classList.remove('syncing');
        btn.title = 'Sync with M365';
        if (wasSyncing) {
            fetchTasks();
            _stopFastPoll();
        }
    }

    if (data.last_sync && data.last_sync.synced_at) {
        var newSyncTime = data.last_sync.synced_at;
        if (lastSyncTime && newSyncTime !== lastSyncTime) {
            fetchTasks();
        }
        lastSyncTime = newSyncTime;
        statusText.textContent = timeAgo(newSyncTime);
    } else {
        statusText.textContent = '';
    }

    // Suggestion check button state
    var scBtn = document.getElementById('suggestion-check-btn');
    if (scBtn) {
        if (data.suggestion_check_running) {
            if (!scBtn.classList.contains('syncing')) {
                scBtn.classList.add('syncing');
                scBtn.title = 'Checking suggestions...';
                _startSuggestionCheckPoll();
            }
        } else {
            var wasChecking = scBtn.classList.contains('syncing');
            scBtn.classList.remove('syncing');
            scBtn.title = _suggestionCheckTooltip();
            if (wasChecking) {
                _stopSuggestionCheckPoll();
                fetchTasks();
            }
        }
    }
}

function _startFastPoll() {
    if (_syncPollTimer) return;
    _syncPollTimer = setInterval(function() {
        fetchSyncStatus();
        fetchTasks();
    }, 5000);
}

function _stopFastPoll() {
    if (_syncPollTimer) {
        clearInterval(_syncPollTimer);
        _syncPollTimer = null;
    }
}

// ── Background Sync Watcher ────────────────────────────────────────────
// Polls sync status every 30s to detect periodic syncs completing in the
// background (the fast-poll only runs after a manual sync click).
var _syncWatcherTimer = null;

function startSyncWatcher() {
    _syncWatcherTimer = setInterval(function() {
        // Skip if fast-poll is already running (manual sync in progress)
        if (_syncPollTimer) return;
        fetch('/api/sync-status')
            .then(function(res) { return res.json(); })
            .then(function(data) {
                updateSyncUI(data);
                // Detect sync running → start fast poll to track it
                if (data.sync_running) {
                    _startFastPoll();
                }
            })
            .catch(function() {});
    }, 30000);
}

function requestSync() {
    var btn = document.getElementById('sync-btn');
    if (btn.classList.contains('syncing')) return;

    btn.classList.add('syncing');
    btn.title = 'Sync running...';

    fetch('/api/sync-status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.ok) {
            _startFastPoll();
        } else {
            btn.classList.remove('syncing');
            btn.title = data.message || 'Sync failed';
        }
    })
    .catch(function(err) {
        btn.classList.remove('syncing');
        btn.title = 'Sync with M365';
        console.error('Sync request failed:', err);
    });
}

// ── Cowork action card ─────────────────────────────────────────────────
//
// PHASE 1 IS PREVIEW ONLY. There is no execute route on the server, so this
// card must never render a control that implies something was or will be sent.
// The user copies the draft and sends it themselves.

var CW_LABELS = {
    'respond-email':     'Reply',
    'schedule-meeting':  'Scheduling',
    'follow-up':         'Follow-up',
    'awaiting-response': 'Follow-up',
    'teams-message':     'Message',
    'prepare':           'Prep',
    'review-document':   'Review',
    'general':           'Action'
};

var CW_DEST = {
    'one_to_one': { risky: false, label: '1:1 Teams chat',
                    note: 'Linear conversation, so a reply lands in the same thread.' },
    'group':      { risky: true,  label: 'Group Teams chat',
                    note: 'Everyone in the chat would see this.' },
    'meeting':    { risky: true,  label: 'Meeting chat',
                    note: 'Everyone invited to the meeting would see this.' },
    'channel':    { risky: true,  label: 'Team channel',
                    note: 'This would be a public post to the whole team.' },
    'none':       { risky: false, label: 'No delivery destination selected',
                    note: 'Choose Teams or email before any future send action.' },
    'unknown':    { risky: true,  label: 'Unrecognised source',
                    note: 'The audience could not be determined from the source link.' }
};

// Transport, not audience shape. Kept deliberately orthogonal to CW_DEST.
var CW_CHANNEL = {
    'teams': { label: 'Teams', note: 'Direct Teams message to this recipient.' },
    'email': { label: 'Email', note: 'Email to this recipient.' }
};

// Kinds derived from a Teams source already imply the Teams transport, so their
// own note stays accurate. Anything else needs the transport spelled out.
var CW_KIND_CHANNEL = {
    'one_to_one': 'teams', 'group': 'teams', 'meeting': 'teams', 'channel': 'teams'
};

// A scheduling task ends in a calendar invite, so the reply-mechanics note
// ("a reply lands in the same thread") reads wrong beside it. The AUDIENCE
// binding still stands — it is the safety guarantee, and the invite goes to the
// same people the message would. Only the mechanics sentence changes, and only
// where there is no broadcast warning to state instead.
var CW_ACTION_NOTE = {
    'schedule-meeting': 'Times are proposed here; the invite would go to the '
        + 'same person.'
};

// taskId -> action row, or null once we know there is no preview yet.
var _cwActions = {};
var _cwLoading = {};
var _cwEditing = {};
var _cwRedo = {};
// Selection for the next run. Once a run starts, the persisted action row is
// authoritative; this map only lets the user choose the mode before that row exists.
var _cwMode = {};
// Refine = one more turn on the SAME Cowork conversation, keeping the research
// it already did. Distinct from Redo, which starts a fresh conversation.
var _cwRefine = {};
// Live, unsaved textarea contents, keyed by task id.
//
// A WebSocket `task_updated` calls renderDetailPane, which rebuilds the card
// from scratch — silently discarding whatever the user had typed into the draft
// or instruction box. That really happened: an in-place draft edit vanished and
// only the instruction reached Cowork. Buffering on every keystroke means a
// re-render restores the edit instead of destroying it.
var _cwDraftBuf = {};
var _cwInstrBuf = {};
var _cwAnswerBuf = {};
var _cwAnswerSending = {};
var _cwExecuteSending = {};
var _cwExecuteApprovals = {};

function cwBufferDraft(taskId, value) { _cwDraftBuf[taskId] = value; }
function cwBufferInstr(taskId, value) { _cwInstrBuf[taskId] = value; }
function cwBufferAnswer(taskId, questionId, value) {
    if (!_cwAnswerBuf[taskId]) _cwAnswerBuf[taskId] = {};
    _cwAnswerBuf[taskId][questionId] = value;
}

function cwClearBuffers(taskId) {
    delete _cwDraftBuf[taskId];
    delete _cwInstrBuf[taskId];
    delete _cwAnswerBuf[taskId];
}
var _cwPollers = {};
var _cwHandoffPollers = {};
var _cwStartedAt = {};

var CW_POLL_MS = 3000;
var CW_POLL_MAX = 235;   // ~700s, just past the runner's 660s timeout
var CW_HANDOFF_POLL_MS = 10000;

function cwCurrentDraft(a) {
    if (!a) return '';
    var draft = (a.draft_edited != null && a.draft_edited !== '')
        ? a.draft_edited : (a.draft || '');
    if (!draft && a.action_type === 'schedule-meeting' && a.finding) {
        return a.finding.trim();
    }
    return draft;
}

function cwElapsed(taskId, action) {
    var started = _cwStartedAt[taskId];
    if (!started && action && action.created_at) {
        var t = Date.parse(action.created_at);
        if (!isNaN(t)) started = t;
    }
    if (!started) return '';
    var secs = Math.max(0, Math.round((Date.now() - started) / 1000));
    return Math.floor(secs / 60) + ':' + ('0' + (secs % 60)).slice(-2) + ' elapsed';
}

function cwFinishedAgo(value) {
    if (!value) return '';
    var at = Date.parse(value);
    if (isNaN(at)) return '';
    var mins = Math.max(0, Math.floor((Date.now() - at) / 60000));
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.floor(hours / 24) + 'd ago';
}

function cwHeadStatus(a) {
    if (!a || !a.conversation_id) return '';
    return '<span class="cw-head-status">' + cwOpenLink(a, 'Open in Cowork') + '</span>';
}

function cwShell(cls, badge, task, body, foot, action) {
    var label = CW_LABELS[task.action_type] || 'Action';
    return '<div class="cw-card ' + cls + '">'
        + '<div class="cw-head">'
        // Referenced rather than inlined: the asset is gradient-based, and its
        // <defs> ids would collide with every other copy on the page.
        + '<img class="cw-spark" src="/static/img/coworker.svg" alt="" aria-hidden="true">'
        + '<span class="cw-type">' + label + ' &middot; Cowork</span>'
        + cwHeadStatus(action)
        + (badge ? '<span class="cw-badge">' + escapeHtml(badge) + '</span>' : '')
        + '</div>'
        + '<div class="cw-body">' + body + '</div>'
        + (foot ? '<div class="cw-foot">' + foot + '</div>' : '')
        + '</div>';
}

function cwSelectedMode(taskId, action) {
    return 'interaction';
}

function cwSetMode(taskId, mode) {
    if (mode !== 'interaction' && mode !== 'no_interaction') return;
    _cwMode[taskId] = mode;
    cwRerender(taskId);
}

function cwModeSwitch(taskId, action, locked) {
    return '';
}

function cwToolTrace(action) {
    var trace = action && action.tool_trace;
    if (!trace) return [];
    if (typeof trace === 'string') {
        try { trace = JSON.parse(trace); } catch (err) { return []; }
    }
    return Array.isArray(trace) ? trace : [];
}

function cwToolLabel(item) {
    var raw = String((item && (item.name || item.tool_name || item.tool)) || 'Tool call');
    if (raw.toLowerCase() === 'bash') {
        var input = String((item && item.input) || '').toLowerCase();
        if (/calendar|date|time|timezone|meeting/.test(input)) {
            return 'Checking date and time details';
        }
        if (/python|node|jq|powershell|script/.test(input)) {
            return 'Processing retrieved context';
        }
        if (/curl|https?:|request/.test(input)) return 'Checking a connected service';
        if (/file|dir|ls |find |path/.test(input)) return 'Reviewing local files';
        return 'Running a local helper';
    }
    var name = raw.split('-').pop().replace(/([a-z])([A-Z])/g, '$1 $2')
        .replace(/_/g, ' ').trim();
    return name || 'Tool call';
}

function cwToolIcon(item) {
    var raw = String((item && (item.name || item.tool_name || item.tool)) || '').toLowerCase();
    var type = 'generic';
    var icon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 1.5 9.7 6.3l4.8 1.7-4.8 1.7L8 14.5 6.3 9.7 1.5 8l4.8-1.7L8 1.5Z"/></svg>';
    if (raw === 'bash') {
        type = 'terminal';
        icon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" d="m3 4 3 3-3 3m5 1h5"/></svg>';
    } else if (raw.indexOf('teams') >= 0) {
        type = 'teams';
        icon = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="12.7" cy="3.4" r="1.7" fill="#d8d9ff"/><path fill="#fff" d="M2 3h8v9.5a1.5 1.5 0 0 1-1.5 1.5h-5A1.5 1.5 0 0 1 2 12.5V3Zm1.5 2v1.4h1.7v5.1h1.6V6.4h1.7V5h-5Z"/><path fill="#d8d9ff" d="M11 6h3v5.2a1.8 1.8 0 0 1-1.8 1.8H11V6Z"/></svg>';
    }
    else if (raw.indexOf('calendar') >= 0 || raw.indexOf('meeting') >= 0) {
        type = 'calendar';
        icon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="#fff" d="M2 3.5A1.5 1.5 0 0 1 3.5 2H5v2h1V2h4v2h1V2h1.5A1.5 1.5 0 0 1 14 3.5V6H2V3.5Zm0 3.7h12v5.3a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12.5V7.2Z"/><path fill="#0f6cbd" d="M5 9h2v1.2H6.2V13H5V9Zm3 0h3v1l-1.7 3H7.9l1.7-2.8H8V9Z"/></svg>';
    } else if (raw.indexOf('search') >= 0) {
        type = 'search';
        icon = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m10 10 3.5 3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    }
    else if (raw.indexOf('outlook') >= 0 || raw.indexOf('email') >= 0) {
        type = 'mail';
        icon = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="#fff" d="M1.5 3.5h13v9h-13v-9Zm1.4 1.2L8 8.4l5.1-3.7H2.9Zm10.2 6.6V6.4L8 10.1 2.9 6.4v4.9h10.2Z"/></svg>';
    }
    return '<span class="cw-tool-icon is-' + type + '" data-testid="tool-icon" '
        + 'data-tool-icon="' + type + '" aria-hidden="true">' + icon + '</span>';
}

function cwTimeline(action, liveText, liveIconName) {
    var trace = cwToolTrace(action);
    if (!trace.length && !liveText) return '';
    var events = trace.map(function(item) {
        var duration = Number(item.duration_seconds);
        var time = isFinite(duration) && duration >= 0 ? Math.round(duration) + 's' : '';
        var state = item.ok === false ? ' failed' : ' completed';
        return '<div class="cw-timeline-event' + state + '">'
            + '<span class="cw-timeline-dot" aria-hidden="true"></span>'
            + cwToolIcon(item)
            + '<div><small>' + escapeHtml(time) + '</small>'
            + '<span>' + escapeHtml(cwToolLabel(item)) + '</span></div></div>';
    });
    if (liveText) {
        var liveIcon = liveIconName
            ? cwToolIcon({name: liveIconName})
            : '<span class="cw-tool-icon is-cowork" data-testid="tool-icon" '
                + 'data-tool-icon="cowork" aria-hidden="true">'
                + '<img src="/static/img/coworker.svg" alt=""></span>';
        events.push('<div class="cw-timeline-event is-active">'
            + '<span class="cw-timeline-dot" aria-hidden="true"></span>'
            + liveIcon
            + '<div><small>now</small><span aria-live="polite">'
            + escapeHtml(liveText) + '</span></div></div>');
    }
    return '<div class="cw-timeline" data-testid="session-timeline">'
        + events.join('') + '</div>';
}

function cwExecutionLabel(task, action) {
        if (task.action_type === 'schedule-meeting') return 'Create meeting';
        if (task.action_type === 'respond-email'
                || (action && action.delivery_channel === 'email')) return 'Send email';
        return 'Send Teams message';
}

function cwExecutionProgress(task, action) {
        var destination = String(
            (action && (action.destination_display || action.destination_ref)) || ''
        ).replace(/\s*\(direct message\)\s*$/i, '').trim();
        var channel = action && action.delivery_channel;
        var text;
        if (task.action_type === 'schedule-meeting' || channel === 'calendar') {
            text = 'Creating meeting';
            return destination ? text + ' with ' + destination : text;
        }
        if (task.action_type === 'respond-email' || channel === 'email') {
            text = 'Sending email';
        } else {
            text = 'Sending Teams message';
        }
        return destination ? text + ' to ' + destination : text;
}

function cwExecutionIconName(task, action) {
        if (task.action_type === 'schedule-meeting'
                || (action && action.delivery_channel === 'calendar')) return 'calendar';
        if (task.action_type === 'respond-email'
                || (action && action.delivery_channel === 'email')) return 'outlook';
        return 'teams';
}

function cwMeetingPeople(task) {
        if (!task) return [];
        var people = parsePeople(task.key_people);
        var seen = {};
        return people.map(function(person) {
            return {
                name: String(person.name || '').trim(),
                email: String(person.email || '').trim().toLowerCase(),
                unresolved: person.unresolved === true
            };
        }).filter(function(person) {
            if (person.unresolved || !person.name || !person.email || seen[person.email]) {
                return false;
            }
            seen[person.email] = true;
            return true;
        });
}

function cwUnresolvedMeetingPeople(task) {
        if (!task) return [];
        return parsePeople(task.key_people).filter(function(person) {
            return String(person.name || '').trim()
                && (person.unresolved === true || !String(person.email || '').trim());
        });
}

function cwMeetingAttendeePills(task) {
        return cwMeetingPeople(task).map(function(person) {
            return '<span class="person-pill cw-meeting-attendee-pill" '
                + 'data-testid="meeting-attendee-pill" title="' + escapeAttr(person.email) + '">'
                + '<span class="person-pill-avatar">' + escapeHtml(getInitials(person.name))
                + '</span><span>' + escapeHtml(person.name) + '</span></span>';
        }).join('');
}

function cwMeetingDestination(people) {
        if (!people.length) return {ref: '', display: ''};
        var emails = people.map(function(person) { return person.email; });
        return {
            ref: emails.length === 1 ? emails[0] : JSON.stringify(emails),
            display: people.map(function(person) { return person.name; }).join(', ')
        };
}

function cwOpenExecuteConfirm(taskId) {
        var action = _cwActions[taskId];
        var task = tasks.find(function(item) { return item.id === taskId; });
        if (!action || !task || action.state !== 'ready') return;
        var isMeeting = task.action_type === 'schedule-meeting';
        if (isMeeting) {
            var unresolved = cwUnresolvedMeetingPeople(task);
            if (unresolved.length) {
                window.alert('Resolve ' + unresolved.map(function(person) {
                    return person.name;
                }).join(', ') + ' in Key People before scheduling. '
                    + 'Riveter is refreshing identity matches now.');
                refreshTask(taskId);
                return;
            }
            var meetingPeople = cwMeetingPeople(task);
            var currentDestination = cwMeetingDestination(meetingPeople);
            if (!action.destination_ref
                    || action.destination_ref !== currentDestination.ref
                    || action.destination_display !== currentDestination.display) {
                window.alert('The attendee list changed after this preview. '
                    + 'Start over so Cowork can check availability for the exact '
                    + 'people shown in Key People.');
                return;
            }
        }
        if (!action.destination_confirmed_at && isMeeting
                && action.destination_ref && action.destination_display) {
            cwConfirmDest(taskId, true);
            return;
        }
        if (!action.destination_confirmed_at) {
            cwOpenDestPicker(taskId);
            return;
        }
        var old = document.getElementById('execute-modal');
        if (old) old.remove();
        var label = cwExecutionLabel(task, action);
        var destination = action.destination_display || action.destination_ref || '';
        var attendeePills = isMeeting ? cwMeetingAttendeePills(task) : '';
        var destinationHtml = attendeePills
            ? '<div class="cw-meeting-attendees">' + attendeePills + '</div>'
            : '<b>' + escapeHtml(destination) + '</b>';
        if (action.delivery_channel === 'teams' && task.source_url
                && action.destination_source === 'auto_source_url') {
            destinationHtml = '<a class="cw-execute-destination-link" href="'
                + escapeHtml(task.source_url)
                + '" target="_blank" rel="noopener noreferrer">Open '
                + escapeHtml(destination) + ' conversation</a>';
        }
        var approvalDraft = cwCurrentDraft(action).trim();
        if (isMeeting && !approvalDraft && action.finding) {
            approvalDraft = action.finding.trim();
        }
        _cwExecuteApprovals[taskId] = {
            parent_action_id: action.id,
            draft: approvalDraft,
            destination_ref: action.destination_ref || '',
            destination_display: action.destination_display || '',
            delivery_channel: action.delivery_channel || '',
            destination_confirmed_at: action.destination_confirmed_at || ''
        };
        var overlay = document.createElement('div');
        overlay.id = 'execute-modal';
        overlay.className = 'source-modal-overlay cw-execute-overlay';
        overlay.innerHTML = '<div class="source-modal cw-execute-modal" role="dialog" '
            + 'aria-modal="true" aria-labelledby="execute-modal-title" '
            + 'data-testid="execute-confirmation">'
            + '<div class="cw-execute-kicker">'
            + (isMeeting ? 'Calendar action' : 'Approved action') + '</div>'
            + '<div class="source-modal-header" id="execute-modal-title">'
            + escapeHtml(label) + '?</div>'
            + '<div class="cw-execute-destination"><span>'
            + (isMeeting ? 'Attendees' : 'Destination') + '</span>'
            + destinationHtml + '</div>'
            + '<label class="source-modal-label">'
            + (isMeeting ? 'Meeting details' : 'Final draft') + '</label>'
            + '<div class="cw-execute-draft">' + renderCoworkMarkdown(approvalDraft) + '</div>'
            + '<div class="cw-execute-warning">'
            + (isMeeting
                ? 'Review every attendee and the meeting details. Cowork creates the calendar event only after you confirm.'
                : 'This performs the action through Cowork. The destination and draft cannot be changed after confirmation.')
            + '</div>'
            + '<div class="cw-execute-error" id="execute-modal-error" role="alert"></div>'
            + '<div class="source-modal-buttons">'
            + '<button class="btn-source-modal btn-source-cancel" '
            + 'onclick="cwCloseExecuteConfirm()">Cancel</button>'
            + '<button class="cw-btn cw-btn-go cw-execute-confirm" '
            + 'data-testid="execute-confirm-btn" onclick="cwConfirmExecute(' + taskId + ')">'
            + escapeHtml(label) + '</button></div></div>';
        document.body.appendChild(overlay);
        overlay.addEventListener('click', function(event) {
            if (event.target === overlay) cwCloseExecuteConfirm();
        });
}

function cwCloseExecuteConfirm() {
        if (Object.keys(_cwExecuteSending).some(function(key) {
            return _cwExecuteSending[key];
        })) return;
        var modal = document.getElementById('execute-modal');
        if (modal) modal.remove();
        Object.keys(_cwExecuteApprovals).forEach(function(key) {
            if (!_cwExecuteSending[key]) delete _cwExecuteApprovals[key];
        });
}

function cwConfirmExecute(taskId) {
        if (_cwExecuteSending[taskId]) return;
        _cwExecuteSending[taskId] = true;
        var button = document.querySelector('[data-testid="execute-confirm-btn"]');
        if (button) {
            button.disabled = true;
            button.textContent = 'Starting\u2026';
        }
        fetch('/api/tasks/' + taskId + '/cowork/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Riveter-Action': 'confirm'
            },
            body: JSON.stringify({
                approved_snapshot: _cwExecuteApprovals[taskId]
            })
        })
        .then(function(r) {
            return r.json().then(function(data) { return { ok: r.ok, data: data }; });
        })
        .then(function(result) {
            delete _cwExecuteSending[taskId];
            if (!result.ok) {
                var error = document.getElementById('execute-modal-error');
                if (error) error.textContent = result.data.error || 'Could not start the action.';
                if (button) {
                    button.disabled = false;
                    button.textContent = cwExecutionLabel(
                        tasks.find(function(item) { return item.id === taskId; }),
                        _cwActions[taskId]
                    );
                }
                return;
            }
            var modal = document.getElementById('execute-modal');
            if (modal) modal.remove();
            delete _cwExecuteApprovals[taskId];
            _cwActions[taskId] = result.data.action;
            _cwStartedAt[taskId] = Date.now();
            cwRerender(taskId);
            startCoworkPoller(taskId);
        })
        .catch(function() {
            delete _cwExecuteSending[taskId];
            var error = document.getElementById('execute-modal-error');
            if (error) error.textContent = 'The request failed before Riveter could confirm delivery.';
            if (button) button.disabled = false;
        });
}

function cwNoInteractionComplete(action) {
    return '';
}

// The intent line is WorkIQ's suggested next action. It remains editable here
// because retiring the AI Coaching card would otherwise remove the only way to
// correct generic boilerplate BEFORE Cowork runs. It writes coaching_text
// through the existing PUT /api/tasks/<id>, reusing saveCoaching().
function cwIntentBlock(task, editable) {
    var intent = task.coaching_text || '';
    if (!intent) return '';
    return '<div class="cw-intent">'
        + '<span class="i-label">Asking Cowork to:</span> '
        + '<span id="coaching-display-' + task.id + '">' + escapeHtml(intent) + '</span>'
        + (editable
            ? '<span class="i-edit" onclick="toggleCoachingEdit(' + task.id + ')">Change</span>'
            : '')
        + '<textarea id="coaching-edit-' + task.id + '" class="cw-intent-box" rows="3" '
        + 'style="display:none" onblur="saveCoaching(' + task.id + ')">'
        + escapeHtml(intent) + '</textarea>'
        + '</div>';
}

// The audience a draft is written for. This is deliberately separate from the
// Cowork conversation id: one is who would receive the message, the other is
// which research session produced it.
function cwOpenLink(a, label, title) {
    // One place that knows the deep-link shape, so the running card and the
    // finished card cannot drift apart.
    if (!a || !a.conversation_id) return '';
    return '<a class="cw-btn cw-btn-sec cw-btn-link" href="'
        + escapeHtml('https://m365.cloud.microsoft/agents/cowork#/task/'
          + encodeURIComponent(a.conversation_id))
        + '" target="_blank" rel="noopener noreferrer" '
        + 'data-testid="cw-open-cowork" '
        // The read-only instruction is scoped to the drafting turn, and the
        // barrier travels in OUR request rather than with the conversation.
        // Verified: a follow-up "send that now" made Cowork call PostMessage,
        // blocked only by our own config. So the handoff really is live, and
        // the button should say so.
        + 'title="' + escapeAttr(title
            || 'Continue this conversation in Cowork. The draft is already '
            + 'there, and asking it to send will send for real.')
        + '">' + escapeHtml(label) + '</a>';
}

function cwDestBlock(action, task) {
    var d = CW_DEST[action.destination_kind] || CW_DEST['unknown'];
    var conv = action.conversation_id || '';
    var risky = d.risky || !!action.is_broadcast;
    var confirmed = !!action.destination_confirmed_at;
    var display = action.destination_display || action.destination_ref || d.label;
    var chan = CW_CHANNEL[action.delivery_channel] || null;
    // A channel with no recipient is a voice preference, not a destination. Its
    // note says "...to this recipient", so applying it where none is bound both
    // contradicts the line above it and displaces the standing instruction to
    // pick one. The instruction is a safety surface; it outranks the preference.
    var hasRecipient = !!(action.destination_ref || action.destination_display);
    // A broadcast warning outranks any transport note — never trade it away.
    var note = (!risky && chan && hasRecipient
        && action.delivery_channel !== CW_KIND_CHANNEL[action.destination_kind])
        ? chan.note : d.note;
    // ...and it outranks the action-specific note too. Only a non-broadcast
    // destination swaps in the scheduling wording; a group chat still has to
    // say everyone would see it.
    var actionNote = task && CW_ACTION_NOTE[task.action_type];
    if (!risky && actionNote) note = actionNote;
    // Link straight to the conversation this is drafted for, so checking who
    // is actually in it is one click rather than a hunt through Teams.
    var srcUrl = (task && task.source_url) || '';
    var openLink = srcUrl
        ? '<a class="cw-dest-open" href="' + cwEscapeAttr(srcUrl) + '" '
          + 'target="_blank" rel="noopener noreferrer" '
          + 'data-testid="dest-open">Open chat</a>'
        : '';
    return '<div class="cw-dest' + (risky ? ' is-risky' : '') + '">'
        + '<span class="d-icon">' + (risky ? '&#9888;' : '&#8627;') + '</span>'
        + '<span><b>Drafted for:</b> '
        + '<span data-testid="' + (risky ? 'dest-risky' : 'dest-safe') + '">'
        + '<span data-testid="dest-status">' + escapeHtml(display) + '</span></span>'
        + (chan ? '<span class="cw-dest-chan" data-testid="dest-channel-chip">'
            + escapeHtml(chan.label) + '</span>' : '')
        + openLink
        + '<span class="d-note" data-testid="dest-note">' + escapeHtml(note) + '</span>'
        + '<span class="cw-dest-actions">'
        + (confirmed
            ? '<span class="cw-dest-badge" data-testid="dest-confirmed">&#10003; audience confirmed</span>'
            : '')
        + '<button class="cw-dest-btn" type="button" data-testid="dest-change-btn" '
        + 'onclick="cwOpenDestPicker(' + action.task_id + ')">'
        + (confirmed ? 'Change' : 'Set destination') + '</button>'
        + '</span>'
        + (conv ? '<button class="cw-debug-id" type="button" title="'
          + cwEscapeAttr(conv) + '" aria-label="Cowork troubleshooting ID">&#9432;</button>' : '')
        + '</span></div>';
}

function cwOpenDestPicker(taskId) {
    var a = _cwActions[taskId];
    var task = tasks.find(function(item) { return item.id === taskId; });
    if (!a || !task) return;
    cwCloseDestPicker();

    var isMeeting = task.action_type === 'schedule-meeting';
    var channel = a.delivery_channel || 'teams';
    var ref = a.destination_ref || '';
    var overlay = document.createElement('div');
    overlay.id = 'dest-modal';
    overlay.className = 'source-modal-overlay';
    overlay.innerHTML = '<div class="source-modal" data-testid="dest-picker">'
        + '<div class="source-modal-header">'
        + (isMeeting ? 'Meeting attendee' : 'Confirm destination') + '</div>'
        + (isMeeting ? ''
            : '<label class="source-modal-label">Channel</label>'
              + '<select id="dest-modal-channel" class="source-modal-input" data-testid="dest-channel">'
              + '<option value="teams"' + (channel === 'teams' ? ' selected' : '') + '>Teams</option>'
              + '<option value="email"' + (channel === 'email' ? ' selected' : '') + '>Email</option>'
              + '</select>')
        + '<label class="source-modal-label">'
        + (isMeeting ? 'Attendee email' : 'Recipient or conversation') + '</label>'
        + '<input type="text" id="dest-modal-ref" class="source-modal-input" '
        + 'data-testid="dest-ref" value="' + cwEscapeAttr(ref) + '">'
        + (ref.indexOf('19:') === 0
            ? '<div class="cw-dest-hint">Linked Teams conversation \u2014 leave as-is to reply in the original thread.</div>'
            : '')
        + '<label class="source-modal-label">'
        + (isMeeting ? 'Attendee name' : 'Shown as') + '</label>'
        + '<input type="text" id="dest-modal-display" class="source-modal-input" '
        + 'data-testid="dest-display" value="' + cwEscapeAttr(a.destination_display || '') + '">'
        + '<div class="source-modal-buttons">'
        + '<button class="btn-source-modal btn-source-cancel" onclick="cwCloseDestPicker()">Cancel</button>'
        + '<button class="btn-source-modal btn-source-save" data-testid="dest-confirm-btn" '
        + 'onclick="cwConfirmDest(' + taskId + ', '
        + (isMeeting ? 'true' : 'false') + ')">'
        + (isMeeting ? 'Review meeting' : 'Confirm') + '</button>'
        + '</div></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', function(event) {
        if (event.target === overlay) cwCloseDestPicker();
    });
}

function cwCloseDestPicker() {
    var existing = document.getElementById('dest-modal');
    if (existing) existing.remove();
}

function cwConfirmDest(taskId, continueToExecute) {
    var action = _cwActions[taskId];
    var task = tasks.find(function(item) { return item.id === taskId; });
    if (!action || !task) return;
    var isMeeting = task.action_type === 'schedule-meeting';
    var channel = document.getElementById('dest-modal-channel');
    var ref = document.getElementById('dest-modal-ref');
    var display = document.getElementById('dest-modal-display');
    var refValue = ref ? ref.value.trim() : (action.destination_ref || '').trim();
    var displayValue = display
        ? display.value.trim() : (action.destination_display || '').trim();
    if ((!isMeeting && !channel) || !refValue || !displayValue) return;

    var body = {
        destination_ref: refValue,
        destination_display: displayValue
    };
    if (!isMeeting) body.delivery_channel = channel.value;

    fetch('/api/tasks/' + taskId + '/cowork/destination', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.action) {
            _cwActions[taskId] = data.action;
            cwSyncTaskState(taskId, data.action);
        }
        cwCloseDestPicker();
        cwRerender(taskId);
        if (continueToExecute && data.action && data.action.destination_confirmed_at) {
            cwOpenExecuteConfirm(taskId);
        }
    })
    .catch(function(err) { console.error('Failed to confirm destination:', err); });
}

var _cwFindingExpanded = {};

function cwEscapeAttr(value) {
    return String(value || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function cwCostBadge(a) {
    // Mirrors format_cost() in cowork_runner.py. Nothing to show is the common
    // case: the endpoint has a kill switch, and overlapping previews are not
    // attributable because the credit counter is per user rather than per run.
    var c = a && a.cost_credits;
    if (c === null || c === undefined || c === '') return '';
    var n = Number(c);
    if (!isFinite(n) || n < 0) return '';
    var text = n === 0 ? 'no credits'
        : n >= 1000 ? Math.round(n).toLocaleString() + ' credits'
        : n.toFixed(1) + ' credits';
    return '<span class="cw-foot-note" title="Credits consumed by this preview,'
        + ' measured from your month-to-date usage">' + escapeHtml(text) + '</span>';
}

function cwCumulativeCostBadge(a) {
    var c = a && a.credits_cumulative;
    if (c === null || c === undefined || c === '') return '';
    var n = Number(c);
    if (!isFinite(n) || n < 0) return '';
    var text = n >= 1000 ? Math.round(n).toLocaleString()
        : Number.isInteger(n) ? n.toLocaleString()
        : n.toFixed(1);
    return '<span class="cw-foot-note" title="Credits consumed by completed Cowork'
        + ' runs for this task">' + escapeHtml(text + ' credits total') + '</span>';
}

function cwHandoffBadge(a) {
    // What happened AFTER "Open in Cowork". Absent for a preview that was never
    // handed over, and absent whenever the lookup failed - this is additive, so
    // no badge is the normal, correct resting state.
    var h = a && a.handoff;
    if (!h || !h.state) return '';

    var label, cls, title;
    if (h.waiting_on_user) {
        // The one worth interrupting for: Cowork is blocked on Phil, which is
        // how an approval prompt surfaces from the outside.
        label = 'Cowork needs you';
        cls = 'cw-handoff cw-handoff-waiting';
        title = 'Cowork is waiting for your input in the web app';
    } else if (h.state === 'running') {
        label = 'Cowork working';
        cls = 'cw-handoff cw-handoff-running';
        title = 'Cowork is still working on this conversation';
    } else if (h.state === 'completed') {
        label = 'Cowork finished' + cwHandoffAgo(h.last_activity);
        cls = 'cw-handoff cw-handoff-done';
        title = 'Cowork finished this conversation';
    } else {
        return '';
    }
    return '<span class="' + cls + '" title="' + cwEscapeAttr(title) + '">'
        + escapeHtml(label) + '</span>';
}

function cwHandoffAgo(ms) {
    // lastActivity is epoch milliseconds. Only ever a rough hint, so anything
    // implausible renders as nothing rather than as a wrong number.
    var n = Number(ms);
    if (!isFinite(n) || n <= 0) return '';
    var mins = Math.floor((Date.now() - n) / 60000);
    if (mins < 0 || mins > 60 * 24 * 30) return '';
    if (mins < 1) return ' just now';
    if (mins < 60) return ' ' + mins + 'm ago';
    var hours = Math.floor(mins / 60);
    if (hours < 24) return ' ' + hours + 'h ago';
    return ' ' + Math.floor(hours / 24) + 'd ago';
}

function cwFindingBlock(finding, taskId) {
    if (!finding) return '';
    var expanded = Boolean(_cwFindingExpanded[taskId]);
    return '<div class="cw-finding"><div class="cw-finding-label">What Cowork found</div>'
        + '<div class="cw-finding-body' + (expanded ? '' : ' cw-finding-clamped')
        + '" id="cw-finding-' + taskId + '">'
        + renderCoworkMarkdown(_stripContextRefs(finding)) + '</div>'
        + '<button class="cw-finding-toggle" id="cw-finding-toggle-' + taskId
        + '" onclick="cwToggleFinding(' + taskId + ')" style="display:none">'
        + (expanded ? 'Show less' : 'Show more') + '</button></div>';
}

function cwToggleFinding(taskId) {
    var body = document.getElementById('cw-finding-' + taskId);
    var button = document.getElementById('cw-finding-toggle-' + taskId);
    if (!body || !button) return;
    _cwFindingExpanded[taskId] = !_cwFindingExpanded[taskId];
    body.classList.toggle('cw-finding-clamped', !_cwFindingExpanded[taskId]);
    button.textContent = _cwFindingExpanded[taskId] ? 'Show less' : 'Show more';
}

function cwInitFindingToggle(taskId) {
    var body = document.getElementById('cw-finding-' + taskId);
    var button = document.getElementById('cw-finding-toggle-' + taskId);
    if (!body || !button) return;
    if (_cwFindingExpanded[taskId]) {
        button.style.display = '';
        button.textContent = 'Show less';
        return;
    }
    button.style.display = body.scrollHeight > body.clientHeight + 1 ? '' : 'none';
}

function cwRedoBlock(taskId) {
    // Only for the FAILED card. On a ready card Refine does the same job in
    // ~30s while keeping the research, so offering both there was three
    // overlapping controls (Refine / Start fresh / Start over). A failed run
    // has no conversation worth continuing, so re-running with a correction is
    // still the right move.
    if (!_cwRedo[taskId]) {
        return '<button class="cw-btn cw-btn-sec" '
            + 'title="Run again, telling Cowork what to change." '
            + 'onclick="cwToggleRedo(' + taskId + ',true)">'
            + '&#8635; Redo</button>';
    }
    return '';
}

function cwRedoRow(taskId) {
    if (!_cwRedo[taskId]) return '';
    return '<div class="cw-intent">'
        + '<input type="text" class="cw-intent-box" id="cw-redo-' + taskId + '" '
        + 'placeholder="Tell Cowork what to change\u2026 (e.g. look for times next week)" '
        + 'onkeydown="if(event.key===\'Enter\'){event.preventDefault();cwStart(' + taskId + ',true)}">'
        + '<div class="cw-intent-actions">'
        + '<span class="i-edit" onclick="cwStart(' + taskId + ',true)">Run again</span>'
        + '<span class="i-edit i-muted" onclick="cwToggleRedo(' + taskId + ',false)">Cancel</span>'
        + '</div></div>';
}

// Every preview run spawns a brand-new Cowork conversation (nothing passes
// --resume), so "start over" needs no new machinery - only a control that says
// so. Redo is framed as "tell Cowork what to change", which reads as steering
// the existing conversation; a user asking for a clean slate had no way to
// express it except by typing the request into the correction box.
function cwStartOver(taskId) {
    var ok = window.confirm(
        'Start a new Cowork conversation for this task?\n\n'
        + 'The current draft is abandoned and research begins again from '
        + 'scratch, with no correction carried over. The previous run stays in '
        + 'history, and the audience you confirmed is kept.');
    if (!ok) return;
    delete _cwRedo[taskId];
    delete _cwEditing[taskId];
    cwStart(taskId);
}

function cwStopPreview(taskId) {
    // Stops work in flight. proc.kill() only killed OUR process while the
    // server-side run carried on spending credits; this actually halts it.
    if (!window.confirm('Stop this Cowork run?\n\nWhatever it has produced so '
        + 'far is kept. Nothing was sent.')) return;
    var el = document.getElementById('cw-live-' + taskId);
    if (el) el.textContent = 'Stopping…';
    fetch('/api/tasks/' + taskId + '/cowork', { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            stopCoworkPoller(taskId);
            if (data && data.action) _cwActions[taskId] = data.action;
            cwRerender(taskId);
        })
        .catch(function () {
            stopCoworkPoller(taskId);
            cwRerender(taskId);
        });
}

function cwRefineBlock(a, taskId) {
    // Only possible on the API transport: a subprocess-produced row carries no
    // conversation id, so there is nothing to continue. That absence is the
    // honest gate — no flag lookup needed in the UI.
    if (!a || !a.conversation_id) return '';
    if (!_cwRefine[taskId]) {
        return '<button class="cw-btn cw-btn-sec" data-testid="cw-refine-btn" '
            + 'title="Ask Cowork to revise this, keeping everything it already '
            + 'researched. Much faster than starting over." '
            + 'onclick="cwToggleRefine(' + taskId + ',true)">Refine</button>';
    }
    return '';
}

function cwRefineRow(a, taskId) {
    if (!_cwRefine[taskId]) return '';
    // Instruction only. The DRAFT is edited in place, in the card's own draft
    // area, rather than duplicated into a second box below it — showing the
    // same text twice made it ambiguous which copy was the real one.
    var instr = _cwInstrBuf[taskId] || '';
    return '<div class="cw-refine" data-testid="cw-refine-row">'
        + '<div class="cw-refine-label">Change the draft above, or say what to '
        + 'change here. Either is enough.</div>'
        + '<textarea class="cw-refine-box" id="cw-refine-' + taskId + '" rows="2" '
        + 'oninput="cwBufferInstr(' + taskId + ',this.value)" '
        + 'placeholder="e.g. make it shorter and aim it just at Greg">'
        + escapeHtml(instr) + '</textarea>'
        + '<div class="cw-refine-actions">'
        + '<button class="cw-btn cw-btn-go" data-testid="cw-refine-send" '
        + 'onclick="cwSendRefine(' + taskId + ')">Send to Cowork</button>'
        + '<button class="cw-btn cw-btn-ghost" '
        + 'onclick="cwToggleRefine(' + taskId + ',false)">Cancel</button>'
        + '</div></div>';
}

function cwToggleRefine(taskId, on) {
    if (on) {
        _cwRefine[taskId] = true;
        // Refine edits the draft in place, so leave any separate Edit mode.
        delete _cwEditing[taskId];
    } else {
        delete _cwRefine[taskId];
        cwClearBuffers(taskId);
    }
    cwRerender(taskId);
    if (on) {
        var box = document.getElementById('cw-refine-' + taskId);
        if (box) box.focus();
    }
}

function cwSendRefine(taskId) {
    var box = document.getElementById('cw-refine-' + taskId);
    var draftBox = document.getElementById('cw-draft-' + taskId);
    // Read the live DOM first, but fall back to the buffer: a re-render between
    // typing and clicking would otherwise lose the edit silently.
    var instruction = ((box ? box.value : _cwInstrBuf[taskId]) || '').trim();
    var a = _cwActions[taskId] || {};
    var edited = ((draftBox ? draftBox.value : _cwDraftBuf[taskId]) || '').trim();
    var original = cwCurrentDraft(a).trim();

    // Either an instruction or an in-place edit is enough to act on. Sending
    // the edited draft verbatim is what makes "I rewrote it, now match this"
    // work without the user having to describe the change in prose.
    var changed = edited && edited !== original;
    if (!instruction && !changed) return;

    var payload = instruction;
    if (changed) {
        payload = (instruction ? instruction + "\n\n" : "")
            + "Here is my edited version. Use it as the basis for the revised "
            + "draft:\n\n" + edited;
    }

    delete _cwRefine[taskId];
    delete _cwEditing[taskId];
    cwClearBuffers(taskId);
    fetch('/api/tasks/' + taskId + '/cowork/refine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction: payload })
    })
    .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
    .then(function (res) {
        if (!res.ok) {
            var a = _cwActions[taskId];
            if (a) a.error = (res.d && res.d.error) || 'Could not continue.';
            return cwRerender(taskId);
        }
        _cwActions[taskId] = res.d.action;
        cwRerender(taskId);
        startCoworkPoller(taskId);
    })
    .catch(function () { cwRerender(taskId); });
}

function renderCoworkCard(task) {
    var a = _cwActions[task.id];
    var liveAction = a
        && ['previewing', 'executing', 'executed', 'execute_unconfirmed']
            .indexOf(a.state) >= 0;
    if (a === undefined) {
        cwLoad(task.id);
        return cwShell('', '', task,
            '<div class="cw-idle">Checking for a previous Cowork preview\u2026</div>', '', a);
    }
    var unresolvedPeople = task.action_type === 'schedule-meeting'
        ? cwUnresolvedMeetingPeople(task)
        : [];
    if (!liveAction && unresolvedPeople.length) {
        var unresolvedNames = unresolvedPeople.map(function(person) {
            return '<b>' + escapeHtml(String(person.name || '').trim()) + '</b>';
        }).join(', ');
        var matchesReady = unresolvedPeople.every(function(person) {
            return Boolean(String(person.email || '').trim());
        });
        return cwShell('', 'needs you', task,
            '<div class="cw-blocked" data-testid="cw-identity-pending">'
            + '<b>' + (matchesReady
                ? 'Confirm the attendee ' + (unresolvedPeople.length === 1 ? 'identity.' : 'identities.')
                : 'Riveter is resolving attendee ' + (unresolvedPeople.length === 1 ? 'identity.' : 'identities.'))
            + '</b><div class="cw-blocked-sub">'
            + (matchesReady
                ? 'Choose the correct match for ' + unresolvedNames
                    + ' from the primary or alternate-name dropdown in Key People. '
                    + 'Cowork will be ready after every attendee is confirmed.'
                : 'Waiting for directory matches for ' + unresolvedNames
                    + '. The choices will appear in Key People when they are ready.')
            + '</div></div>',
            '',
            null);
    }
    if (!liveAction
            && (task.parse_status === 'queued' || task.parse_status === 'parsing')) {
        return cwShell('is-running', 'refreshing', task,
            '<div class="cw-progress"><span class="cw-spinner"></span>'
            + '<span class="cw-progress-text">Refreshing task context'
            + '<span class="cw-progress-sub">Cowork will return here automatically when the refresh finishes.</span>'
            + '</span></div>',
            '',
            null);
    }
    if (!liveAction && task.parse_status !== 'parsed') return '';

    if (a && ['previewing', 'executing', 'executed', 'execute_unconfirmed'].indexOf(a.state) >= 0) {
        if (['previewing', 'executing'].indexOf(a.state) >= 0
                && !_cwPollers[task.id]) {
            startCoworkPoller(task.id);
        }
        var prog = (a.progress && a.progress.length)
            ? a.progress[a.progress.length - 1]
            : 'Cowork is reading M365';
        // Cowork can stop mid-run to ask a question in the web app, and until
        // it is answered nothing else happens. A spinner here reads as "still
        // working", which told Phil to keep waiting through 13 minutes of a run
        // that was blocked on him the whole time.
        if (a.waiting_on_user && a.state === 'previewing') {
            var interaction = a.interaction_request;
            var question = interaction
                ? cwInteractionFields(task.id, interaction)
                : '<div class="cw-blocked-sub">Loading Cowork\u2019s question\u2026</div>';
            var answerButton = interaction
                ? '<button class="cw-btn cw-btn-go" data-testid="cw-answer-submit" '
                    + 'onclick="cwSendAnswer(' + task.id + ')">Answer and continue</button>'
                : '';
            return cwShell('is-running', 'needs you', task,
                cwModeSwitch(task.id, a, true)
                + cwIntentBlock(task, false)
                + '<div class="cw-blocked" data-testid="cw-blocked" '
                + 'data-cw-invocation="'
                + escapeAttr(interaction ? interaction.invocation_id : '') + '">'
                + '<b>Cowork is waiting for your answer.</b>'
                + (a.error ? '<div class="cw-error">' + escapeHtml(a.error) + '</div>' : '')
                + question
                + '</div>',
                answerButton
                + '<button class="cw-btn cw-btn-ghost" data-testid="cw-stop" '
                + 'onclick="cwStopPreview(' + task.id + ')">Stop</button>'
                + cwCumulativeCostBadge(a),
                a);
        }

        if (a && a.state === 'executing') {
            if (!_cwPollers[task.id]) startCoworkPoller(task.id);
            var sendProgress = cwExecutionProgress(task, a);
            if (a.waiting_on_user) {
                var executeInteraction = a.interaction_request;
                return cwShell('is-running is-executing', 'needs approval', task,
                    '<div class="cw-delivery-target"><span>Acting in</span><b>'
                    + escapeHtml(a.destination_display || a.destination_ref || '') + '</b></div>'
                    + '<div class="cw-blocked" data-testid="cw-blocked" data-cw-invocation="'
                    + escapeAttr(executeInteraction ? executeInteraction.invocation_id : '') + '">'
                    + '<b>Cowork needs your approval to finish this action.</b>'
                    + (executeInteraction
                        ? cwInteractionFields(task.id, executeInteraction)
                        : '<div class="cw-blocked-sub">Loading Cowork\u2019s question\u2026</div>')
                    + '</div>',
                    (executeInteraction
                        ? '<button class="cw-btn cw-btn-go" data-testid="cw-answer-submit" '
                          + 'onclick="cwSendAnswer(' + task.id + ')">Answer and continue</button>'
                        : '')
                    + '<span class="cw-foot-note">the action is paused until you answer</span>',
                    a);
            }
            return cwShell('is-running is-executing', 'sending', task,
                '<div class="cw-delivery-target"><span>Acting in</span><b>'
                + escapeHtml(a.destination_display || a.destination_ref || '') + '</b></div>'
                + cwTimeline(a, sendProgress, cwExecutionIconName(task, a))
                + '<div class="cw-progress"><span class="cw-spinner"></span>'
                + '<span class="cw-progress-text"><span id="cw-live-' + task.id + '">'
                + escapeHtml(sendProgress) + '</span><span class="cw-progress-sub" id="cw-hb-'
                + task.id + '">' + escapeHtml(cwElapsed(task.id, a)) + '</span></span></div>',
                '<span class="cw-foot-note">approved action in progress</span>',
                a);
        }

        if (a && a.state === 'executed') {
            return cwShell('is-delivered', 'delivered', task,
                '<section class="cw-delivery-result is-confirmed" data-testid="delivery-confirmed">'
                + '<span class="cw-delivery-mark" aria-hidden="true">\u2713</span><div>'
                + '<b>Delivered to ' + escapeHtml(a.destination_display || a.destination_ref || 'the destination')
                + '</b><span>Cowork returned positive delivery evidence.</span></div></section>'
                + cwTimeline(a, '')
                + '<div class="cw-draft cw-markdown">' + renderCoworkMarkdown(cwCurrentDraft(a)) + '</div>',
                '',
                a);
        }

        if (a && a.state === 'execute_unconfirmed') {
            return cwShell('is-unconfirmed', 'check delivery', task,
                '<section class="cw-delivery-result is-unconfirmed" data-testid="delivery-unconfirmed">'
                + '<span class="cw-delivery-mark" aria-hidden="true">!</span><div>'
                + '<b>Delivery could not be confirmed</b>'
                + '<span>Check the destination before retrying. ' + escapeHtml(a.error || '') + '</span>'
                + '</div></section>'
                + cwTimeline(a, '')
                + '<div class="cw-draft cw-markdown">' + renderCoworkMarkdown(cwCurrentDraft(a)) + '</div>',
                '<button class="cw-btn cw-btn-sec" onclick="cwStart(' + task.id + ')">Start a new draft</button>',
                a);
        }
        return cwShell('is-running', '', task,
            cwModeSwitch(task.id, a, true)
            + cwIntentBlock(task, false)
            + cwTimeline(a, prog)
            + '<div class="cw-progress"><span class="cw-spinner"></span>'
            + '<span class="cw-progress-text">'
            + '<span id="cw-live-' + task.id + '">' + escapeHtml(prog) + '</span>'
            + '<span class="cw-progress-sub" id="cw-hb-' + task.id + '">'
            + escapeHtml(cwElapsed(task.id, a)) + '</span>'
            + '</span></div>',
            '<button class="cw-btn cw-btn-ghost" data-testid="cw-stop" '
            + 'onclick="cwStopPreview(' + task.id + ')">Stop</button>'
            + cwCumulativeCostBadge(a),
            a);
    }

    if (a && a.state === 'failed') {
        return cwShell('is-failed', 'failed', task,
            cwModeSwitch(task.id, a, false)
            + '<div class="cw-fail"><b>Cowork could not complete this.</b>'
            + '<div class="cw-fail-sub">' + escapeHtml(a.error || a.terminal_status || 'Unknown error')
            + '<br>Nothing was sent.</div></div>'
            + cwRedoRow(task.id),
            '<button class="cw-btn cw-btn-go" onclick="cwStart(' + task.id + ')">Retry</button>'
            + cwRedoBlock(task.id),
            a);
    }

    if (a && a.state === 'ready') {
        if (a.handoff && a.handoff.state === 'running') {
            startCoworkHandoffPoller(task.id, a.id);
        } else {
            stopCoworkHandoffPoller(task.id);
        }
        var refining = !!_cwRefine[task.id];
        // Refine edits the draft IN PLACE: the same textarea, so there is only
        // ever one copy of the text on screen and no ambiguity about which one
        // gets sent.
        var editing = !!_cwEditing[task.id] || refining;
        var draft = cwCurrentDraft(a);
        // A pending edit outranks the stored draft: a re-render must not throw
        // away what the user is in the middle of typing.
        if (editing && _cwDraftBuf[task.id] !== undefined) {
            draft = _cwDraftBuf[task.id];
        }
        var findingHtml = cwFindingBlock(a.finding, task.id);
        var draftHtml = editing
            ? '<textarea class="cw-draft is-editing" id="cw-draft-' + task.id + '" rows="8" '
              + 'oninput="cwBufferDraft(' + task.id + ',this.value)">'
              + escapeHtml(draft) + '</textarea>'
            : '<div class="cw-draft cw-markdown" role="button" tabindex="0" '
              + 'data-testid="cowork-draft-click-edit" title="Click to edit draft" '
              + 'onclick="cwOpenDraftEdit(event,' + task.id + ')" '
              + 'onkeydown="cwOpenDraftEdit(event,' + task.id + ')">'
              + renderCoworkMarkdown(draft) + '</div>';
        var editedBadge = (a.draft_edited != null && a.draft_edited !== '')
            ? '<span class="cw-foot-note">edited by you</span>' : '';
        // What this preview actually cost, measured as the change in the user's
        // month-to-date credit counter across the run. Absent when it could not
        // be attributed (two previews overlapping) or the endpoint was off.
        var costBadge = cwCostBadge(a);
        var cumulativeCostBadge = cwCumulativeCostBadge(a);
        var correction = a.redirect_text
            ? '<div class="cw-intent"><span class="i-label">Correction:</span> '
              + escapeHtml(a.redirect_text) + '</div>'
            : '';

        var foot = refining
            // In refine mode the actions live in the refine row, so the footer
            // only offers the way out.
            ? '<button class="cw-btn cw-btn-ghost" onclick="cwToggleRefine(' + task.id + ',false)">Cancel</button>'
            : editing
            ? '<button class="cw-btn cw-btn-go" onclick="cwSaveDraft(' + task.id + ')">Save edit</button>'
              + '<button class="cw-btn cw-btn-ghost" onclick="cwToggleEdit(' + task.id + ',false)">Cancel</button>'
            : '<button class="cw-btn cw-btn-go" data-testid="cw-execute-action" '
              + 'onclick="cwOpenExecuteConfirm(' + task.id + ')">'
              + escapeHtml(cwExecutionLabel(task, a)) + '</button>'
              + '<button class="cw-btn cw-btn-sec" onclick="cwCopyDraft(' + task.id + ')">Copy draft</button>'
              + cwRefineBlock(a, task.id)
              + (a.conversation_id ? '' : cwRedoBlock(task.id))
              + '<button class="cw-btn cw-btn-sec" data-testid="cw-start-over" '
              + 'title="Abandon this conversation and research again from scratch" '
              + 'onclick="cwStartOver(' + task.id + ')">Start over</button>'
              + editedBadge + costBadge + cumulativeCostBadge + cwHandoffBadge(a);

        return cwShell('', '', task,
            cwModeSwitch(task.id, a, false)
            + cwIntentBlock(task, !editing) + correction
            + cwTimeline(a, '')
            + cwNoInteractionComplete(a)
            + findingHtml + draftHtml
            + cwDestBlock(a, task) + cwRefineRow(a, task.id) + cwRedoRow(task.id),
            foot,
            a);
    }

    // No preview yet.
    return cwShell('', 'not run', task,
        cwModeSwitch(task.id, a, false)
        + cwIntentBlock(task, true)
        + '<div class="cw-idle">Cowork can check the latest state of this in M365, then draft the action.'
        + '<span class="cw-idle-sub">Nothing happens without your explicit review and confirmation.</span></div>',
        '<button class="cw-btn cw-btn-go" onclick="cwStart(' + task.id + ')">Preview with Cowork</button>'
        + '<span class="cw-foot-note">~45s &middot; read-only</span>',
        a);
}

// ── Cowork state transitions ───────────────────────────────────────────

function cwRerender(taskId) {
    var task = tasks.find(function(t) { return t.id === taskId; });
    if (task && selectedTaskId === taskId) renderDetailPane(task);
}

function cwSyncTaskState(taskId, action) {
    var task = tasks.find(function(item) { return item.id === taskId; });
    if (!task) return;
    task.cw_state = action ? action.state : null;
    task.cw_seen_at = action ? action.seen_at : null;
}

function cwLoad(taskId, markSeen) {
    if (_cwLoading[taskId]) return;
    _cwLoading[taskId] = true;
    fetch('/api/tasks/' + taskId + '/cowork' + (markSeen ? '?mark_seen=1' : ''))
        .then(function(res) {
            if (res.status === 404) return { action: null };
            return res.json();
        })
        .then(function(data) {
            delete _cwLoading[taskId];
            _cwActions[taskId] = data.action || null;
            cwSyncTaskState(taskId, data.action || null);
            if (data.action && data.action.state === 'previewing') startCoworkPoller(taskId);
            renderTaskList();
            cwRerender(taskId);
        })
        .catch(function() {
            delete _cwLoading[taskId];
            _cwActions[taskId] = null;
            cwRerender(taskId);
        });
}

function cwStart(taskId, isRedo) {
    var task = tasks.find(function(item) { return item.id === taskId; });
    var unresolved = task && task.action_type === 'schedule-meeting'
        ? cwUnresolvedMeetingPeople(task) : [];
    if (unresolved.length) {
        window.alert('Resolve ' + unresolved.map(function(person) {
            return person.name;
        }).join(', ') + ' in Key People before scheduling. '
            + 'Riveter is refreshing identity matches now.');
        refreshTask(taskId);
        return;
    }
    var body = {
        interaction_mode: cwSelectedMode(taskId, _cwActions[taskId])
    };
    if (isRedo) {
        var input = document.getElementById('cw-redo-' + taskId);
        var text = input ? input.value.trim() : '';
        if (text) body.redirect_text = text;
    }
    delete _cwRedo[taskId];
    delete _cwEditing[taskId];
    _cwStartedAt[taskId] = Date.now();

    fetch('/api/tasks/' + taskId + '/cowork', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
    .then(function(res) {
        return res.json().then(function(data) { return {ok: res.ok, data: data}; });
    })
    .then(function(result) {
        var data = result.data;
        if (!result.ok) {
            window.alert(data.error || 'Could not start Cowork.');
            return;
        }
        if (data.action) {
            _cwActions[taskId] = data.action;
            delete _cwMode[taskId];
            cwSyncTaskState(taskId, data.action);
            renderTaskList();
            startCoworkPoller(taskId);
        }
        cwRerender(taskId);
    })
    .catch(function(err) { console.error('Failed to start Cowork preview:', err); });
}

function cwToggleRedo(taskId, on) {
    if (on) { _cwRedo[taskId] = true; } else { delete _cwRedo[taskId]; }
    cwRerender(taskId);
}

function cwToggleEdit(taskId, on) {
    if (on) {
        _cwEditing[taskId] = true;
    } else {
        delete _cwEditing[taskId];
        cwClearBuffers(taskId);  // Cancel really discards.
    }
    cwRerender(taskId);
    if (on) {
        var editor = document.getElementById('cw-draft-' + taskId);
        if (editor) editor.focus();
    }
}

function cwOpenDraftEdit(event, taskId) {
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') return;
    if (event.target && event.target.closest && event.target.closest('a')) return;
    event.preventDefault();
    cwToggleEdit(taskId, true);
}

function cwSaveDraft(taskId) {
    var box = document.getElementById('cw-draft-' + taskId);
    // Fall back to the buffer, so a re-render between typing and clicking Save
    // does not silently discard the edit.
    var text = box ? box.value : _cwDraftBuf[taskId];
    if (text === undefined || text === null) return;
    delete _cwEditing[taskId];
    cwClearBuffers(taskId);

    fetch('/api/tasks/' + taskId + '/cowork', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ draft_edited: text })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        if (data.action) _cwActions[taskId] = data.action;
        cwRerender(taskId);
    })
    .catch(function(err) { console.error('Failed to save draft:', err); });
}

function cwInteractionFields(taskId, interaction) {
    var buffered = _cwAnswerBuf[taskId] || {};
    return (interaction.questions || []).map(function(question) {
        var id = String(question.id || '');
        var heading = question.header || question.question || 'Cowork question';
        var prompt = question.header && question.question
            ? '<div class="cw-blocked-sub">' + escapeHtml(question.question) + '</div>'
            : '';
        var value = buffered[id] || '';
        var input;
        if (question.options && question.options.length) {
            var availability = cwAvailabilityMatrix(taskId, question, value);
            var selectedValues = value.split('\n').filter(Boolean);
            var usesKnownOptions = selectedValues.length > 0
                && selectedValues.every(function(selectedValue) {
                    return question.options.some(function(option) {
                        return option.value === selectedValue;
                    });
                });
            input = (availability || ('<div class="cw-choice-grid" data-testid="cw-answer" '
                + 'data-cw-answer="' + escapeAttr(id) + '" '
                + 'data-cw-multi="' + (question.multi_select ? '1' : '0') + '">'
                + question.options.map(function(option) {
                    var selected = value.split('\n').indexOf(option.value) !== -1
                        ? 'true' : 'false';
                    return '<button type="button" class="cw-choice" '
                        + 'data-testid="cw-choice" data-cw-option="'
                        + escapeAttr(option.value) + '" aria-pressed="' + selected + '" '
                        + 'onclick="cwChooseOption(' + taskId + ', this)">'
                        + cwChoiceVisual(option) + '<span class="cw-choice-copy"><b>'
                        + escapeHtml(option.label) + '</b>'
                        + (option.description
                            ? '<small class="sr-only">' + escapeHtml(option.description) + '</small>'
                            : '')
                        + '</span></button>';
                }).join('') + '</div>'))
                + '<div class="cw-choice-redirect">'
                + '<label for="cw-redirect-' + taskId + '-' + escapeAttr(id) + '">'
                + 'Need a different option?</label>'
                + '<input id="cw-redirect-' + taskId + '-' + escapeAttr(id) + '" '
                + 'class="cw-choice-redirect-input" data-testid="cw-answer-redirect" '
                + 'data-cw-redirect="' + escapeAttr(id) + '" value="'
                + escapeAttr(usesKnownOptions ? '' : value)
                + '" placeholder="e.g. find something later in the day" '
                + 'oninput="cwRedirectAnswer(' + taskId + ', this)">'
                + '</div>';
        } else {
            input = '<textarea class="cw-refine-box cw-answer-box" '
                + 'data-testid="cw-answer" data-cw-answer="' + escapeAttr(id) + '" '
                + 'placeholder="Type your answer..." '
                + 'oninput="cwBufferAnswer(' + taskId
                + ', this.getAttribute(\'data-cw-answer\'), this.value)">'
                + escapeHtml(value) + '</textarea>';
        }

        function cwSelectedAttendees(taskId) {
            var task = tasks.find(function(item) { return item.id === taskId; });
            if (!task) return [];
            var people = task.key_people || [];
            if (typeof people === 'string') {
                try { people = JSON.parse(people); } catch (_err) { return []; }
            }
            if (!Array.isArray(people)) return [];
            var attendees = [];
            var emails = {};
            for (var i = 0; i < people.length; i += 1) {
                var person = people[i];
                if (!person || typeof person !== 'object') return [];
                var name = String(person.name || '').trim();
                var email = String(person.email || '').trim().toLowerCase();
                if (person.unresolved === true || !name || !email || emails[email]) return [];
                emails[email] = true;
                attendees.push({name: name, email: email});
            }
            return attendees;
        }

        function cwParseAvailability(description) {
            var match = String(description || '').match(/\[avail:(\{[^\]]+\})\]\s*$/);
            if (!match) return null;
            var parsed;
            try { parsed = JSON.parse(match[1]); } catch (_err) { return null; }
            if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') return null;
            var statuses = {};
            var allowed = ['free', 'tentative', 'busy', 'unknown'];
            for (var key in parsed) {
                if (!Object.prototype.hasOwnProperty.call(parsed, key)) continue;
                var email = String(key).trim().toLowerCase();
                var status = String(parsed[key]).trim().toLowerCase();
                if (!email || allowed.indexOf(status) === -1) return null;
                statuses[email] = status;
            }
            return statuses;
        }

        function cwPersonInitials(name) {
            var parts = String(name || '').trim().split(/\s+/).filter(Boolean);
            if (!parts.length) return '?';
            return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : ''))
                .toUpperCase();
        }

        function cwAvailabilityMatrix(taskId, question, value) {
            var attendees = cwSelectedAttendees(taskId);
            var options = question.options || [];
            if (attendees.length < 2 || options.length < 2) return '';
            var expected = attendees.map(function(person) { return person.email; }).sort();
            var rows = [];
            for (var i = 0; i < options.length; i += 1) {
                var statuses = cwParseAvailability(options[i].description);
                if (!statuses) return '';
                var actual = Object.keys(statuses).sort();
                if (actual.length !== expected.length
                        || actual.some(function(email, index) { return email !== expected[index]; })) {
                    return '';
                }
                var label = String(options[i].label || '').toLowerCase();
                var values = attendees.map(function(person) { return statuses[person.email]; });
                if ((/all free|everyone free|both free/.test(label)
                        && values.some(function(status) { return status !== 'free'; }))
                        || (/conflict|unavailable/.test(label)
                        && values.every(function(status) { return status === 'free'; }))) {
                    return '';
                }
                rows.push({option: options[i], statuses: values});
            }
            var selected = value.split('\n');
            var header = '<div class="cw-avail-corner" aria-hidden="true"></div>'
                + attendees.map(function(person) {
                    return '<div class="cw-avail-head" role="columnheader" '
                        + 'data-testid="cw-avail-col-header"><span class="person-pill '
                        + 'cw-avail-head-pill" data-testid="cw-avail-head-pill" title="'
                        + escapeAttr(person.name) + '" aria-label="' + escapeAttr(person.name)
                        + '"><span class="person-pill-avatar cw-avail-head-avatar" '
                        + 'data-testid="cw-avail-head-avatar" title="' + escapeAttr(person.name)
                        + '">' + escapeHtml(cwPersonInitials(person.name))
                        + '</span><span class="cw-avail-head-name">'
                        + escapeHtml(person.name) + '</span></span></div>';
                }).join('');
            var body = rows.map(function(row) {
                var pressed = selected.indexOf(row.option.value) !== -1 ? 'true' : 'false';
                return '<button type="button" class="cw-avail-row" role="row" '
                    + 'data-testid="cw-avail-row" data-cw-option="'
                    + escapeAttr(row.option.value) + '" aria-pressed="' + pressed + '" '
                    + 'onclick="cwChooseOption(' + taskId + ', this)">'
                    + '<span class="cw-avail-time" role="rowheader" data-testid="cw-avail-row-label">'
                    + escapeHtml(row.option.label) + '</span>'
                    + row.statuses.map(function(status, index) {
                        var label = status === 'tentative' ? 'Tentative'
                            : status[0].toUpperCase() + status.slice(1);
                        return '<span class="cw-avail-cell" role="cell" '
                            + 'data-testid="cw-avail-cell" data-status="' + status + '" '
                            + 'aria-label="' + escapeAttr(attendees[index].name + ': ' + status)
                            + '"><span aria-hidden="true">' + escapeHtml(label) + '</span></span>';
                    }).join('') + '</button>';
            }).join('');
            return '<div class="cw-avail-wrap"><div class="cw-avail-matrix" role="table" '
                + 'data-testid="cw-avail-matrix" data-cw-answer="'
                + escapeAttr(String(question.id || ''))
                + '" data-cw-multi="' + (question.multi_select ? '1' : '0')
                + '" data-attendees="' + attendees.length + '" '
                + 'style="--cw-attendees:' + attendees.length + '">'
                + '<div class="cw-avail-header" role="row">' + header + '</div>'
                + body + '</div></div>';
        }
        var questionImage = cwSafeImageUrl(question.image_url);
        var questionVisual = questionImage
            ? '<img class="cw-question-image" src="' + escapeAttr(questionImage)
                + '" alt="">'
            : '';
        return '<div class="cw-blocked-question">' + questionVisual
            + '<b>' + escapeHtml(heading)
            + '</b>' + prompt + input + '</div>';
    }).join('');
}

function cwChoiceEmoji(option) {
    var text = ((option.label || '') + ' ' + (option.description || '')).toLowerCase();
    if (/account|tenant|company|work/.test(text)) return '\u{1F3E2}';
    if (/scope|access|permission|secure/.test(text)) return '\u{1F510}';
    if (/calendar|meeting|time|schedule/.test(text)) return '\u{1F4C5}';
    if (/email|message|reply/.test(text)) return '\u2709\uFE0F';
    if (/file|document|report/.test(text)) return '\u{1F4C4}';
    return '\u2728';
}

function cwChoiceVisual(option) {
    var emoji = '<span class="cw-choice-emoji" aria-hidden="true">'
        + cwChoiceEmoji(option) + '</span>';
    var imageUrl = cwSafeImageUrl(option.image_url);
    if (!imageUrl) return emoji;
    return '<img class="cw-choice-image" src="' + escapeAttr(imageUrl)
        + '" alt="" onerror="this.hidden=true;this.nextElementSibling.hidden=false">'
        + '<span class="cw-choice-emoji" aria-hidden="true" hidden>'
        + cwChoiceEmoji(option) + '</span>';
}

function cwSafeImageUrl(value) {
    if (!value) return '';
    try {
        var url = new URL(value);
        return url.protocol === 'https:' ? url.href : '';
    } catch (_err) {
        return '';
    }
}

function cwChooseOption(taskId, button) {
    var field = button.closest('[data-cw-answer]');
    if (!field) return;
    var question = field.closest('.cw-blocked-question');
    var multi = field.getAttribute('data-cw-multi') === '1';
    if (!multi) {
        field.querySelectorAll('[data-cw-option]').forEach(function(choice) {
            choice.setAttribute('aria-pressed', 'false');
        });
    }
    var wasSelected = button.getAttribute('aria-pressed') === 'true';
    button.setAttribute('aria-pressed', String(multi ? !wasSelected : true));
    var redirect = question && question.querySelector('[data-cw-redirect="'
        + field.getAttribute('data-cw-answer') + '"]');
    if (redirect) redirect.value = '';
    cwBufferAnswer(
        taskId,
        field.getAttribute('data-cw-answer'),
        cwAnswerValue(field)
    );
}

function cwRedirectAnswer(taskId, input) {
    var question = input.closest('.cw-blocked-question');
    if (!question) return;
    question.querySelectorAll('[data-cw-option]').forEach(function(choice) {
        choice.setAttribute('aria-pressed', 'false');
    });
    cwBufferAnswer(taskId, input.getAttribute('data-cw-redirect'), input.value);
}

function cwAnswerValue(field) {
    if (field.matches('.cw-choice-grid, .cw-avail-matrix')) {
        return Array.from(field.querySelectorAll('[aria-pressed="true"]')).map(
            function(option) { return option.getAttribute('data-cw-option'); }
        ).join('\n');
    }
    return field.value || '';
}

function cwSendAnswer(taskId) {
    if (_cwAnswerSending[taskId]) return;
    var answers = {};
    var fields = document.querySelectorAll('[data-cw-answer]');
    var blocked = document.querySelector('[data-testid="cw-blocked"]');
    fields.forEach(function(field) {
        var question = field.closest('.cw-blocked-question');
        var redirect = question && question.querySelector('[data-cw-redirect="'
            + field.getAttribute('data-cw-answer') + '"]');
        var value = redirect && redirect.value.trim()
            ? redirect.value.trim() : cwAnswerValue(field).trim();
        if (value) answers[field.getAttribute('data-cw-answer')] = value;
    });
    if (!fields.length || Object.keys(answers).length !== fields.length) return;
    _cwAnswerSending[taskId] = true;
    var button = document.querySelector('[data-testid="cw-answer-submit"]');
    if (button) button.disabled = true;
    fetch('/api/tasks/' + taskId + '/cowork/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            invocation_id: blocked ? blocked.getAttribute('data-cw-invocation') : '',
            answers: answers
        })
    })
    .then(function(r) {
        return r.json().then(function(d) { return { ok: r.ok, d: d }; });
    })
    .then(function(res) {
        if (!res.ok) {
            delete _cwAnswerSending[taskId];
            var a = _cwActions[taskId];
            if (a) a.error = (res.d && res.d.error) || 'Could not answer Cowork.';
            return cwRerender(taskId);
        }
        delete _cwAnswerBuf[taskId];
        delete _cwAnswerSending[taskId];
        _cwActions[taskId] = res.d.action;
        cwRerender(taskId);
        if (res.d.action.state === 'previewing') startCoworkPoller(taskId);
    })
    .catch(function(err) {
        delete _cwAnswerSending[taskId];
        console.error('Failed to answer Cowork:', err);
        var a = _cwActions[taskId];
        if (a) a.error = 'Could not answer Cowork. Check your connection and try again.';
        cwRerender(taskId);
    });
}

function cwCopyDraft(taskId) {
    var text = cwCurrentDraft(_cwActions[taskId]);
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(function() {});
    }
}

// ── Cowork preview poller ──────────────────────────────────────────────
//
// Written fresh rather than adapted from pollSkillStatus, which polls
// /api/runner-status and terminates on skill_output — neither applies here.

function startCoworkPoller(taskId) {
    stopCoworkPoller(taskId);
    _cwPollers[taskId] = {
        count: 0,
        timer: setInterval(function() { pollCoworkStatus(taskId); }, CW_POLL_MS)
    };
}

function stopCoworkPoller(taskId) {
    var poller = _cwPollers[taskId];
    if (!poller) return;
    clearInterval(poller.timer);
    delete _cwPollers[taskId];
}

function startCoworkHandoffPoller(taskId, actionId) {
    if (_cwHandoffPollers[taskId]) return;
    _cwHandoffPollers[taskId] = {
        actionId: actionId,
        timer: setInterval(function() {
            pollCoworkHandoffStatus(taskId);
        }, CW_HANDOFF_POLL_MS)
    };
}

function stopCoworkHandoffPoller(taskId) {
    var poller = _cwHandoffPollers[taskId];
    if (!poller) return;
    clearInterval(poller.timer);
    delete _cwHandoffPollers[taskId];
}

function pollCoworkHandoffStatus(taskId) {
    var poller = _cwHandoffPollers[taskId];
    var current = _cwActions[taskId];
    if (!poller || selectedTaskId !== taskId
            || !current || current.id !== poller.actionId
            || current.state !== 'ready'
            || !current.handoff || current.handoff.state !== 'running') {
        stopCoworkHandoffPoller(taskId);
        return;
    }

    fetch('/api/tasks/' + taskId + '/cowork')
        .then(function(res) {
            if (res.status === 404) return { action: null };
            return res.json();
        })
        .then(function(data) {
            var latestPoller = _cwHandoffPollers[taskId];
            var action = data.action || null;
            if (!latestPoller || selectedTaskId !== taskId || !action
                    || action.id !== latestPoller.actionId) {
                stopCoworkHandoffPoller(taskId);
                return;
            }
            _cwActions[taskId] = action;
            cwSyncTaskState(taskId, action);
            renderTaskList();
            if (!action.handoff || action.handoff.state !== 'running') {
                stopCoworkHandoffPoller(taskId);
            }
            cwRerender(taskId);
        })
        .catch(function() {});
}

function pollCoworkStatus(taskId) {
    var poller = _cwPollers[taskId];
    if (!poller) return;
    if (++poller.count > CW_POLL_MAX) {
        stopCoworkPoller(taskId);
        return;
    }

    // Cheap liveness feedback without a full re-render on every tick.
    var hb = document.getElementById('cw-hb-' + taskId);
    if (hb) hb.textContent = cwElapsed(taskId, _cwActions[taskId]);

    fetch('/api/tasks/' + taskId + '/cowork')
        .then(function(res) {
            if (res.status === 404) return { action: null };
            return res.json();
        })
        .then(function(data) {
            var action = data.action || null;
            if (!action) return;
            var wasWaiting = !!(_cwActions[taskId] && _cwActions[taskId].waiting_on_user);
            var previousQuestion = _cwActions[taskId]
                ? _cwActions[taskId].blocked_question : null;
            _cwActions[taskId] = action;
            cwSyncTaskState(taskId, action);
            renderTaskList();
            // Update the live line in place. A full re-render would fight the
            // intent editor, which is the bug fixed in 68e4119.
            if (action.progress && action.progress.length) {
                var el = document.getElementById('cw-live-' + taskId);
                if (el) el.textContent = action.progress[action.progress.length - 1];
            }
            if (wasWaiting !== !!action.waiting_on_user
                    || previousQuestion !== action.blocked_question) {
                cwRerender(taskId);
            }
            if (['previewing', 'executing'].indexOf(action.state) < 0) {
                stopCoworkPoller(taskId);
                cwRerender(taskId);
            }
        })
        .catch(function() {}); // Silent fail on poll
}

// ── Skill Runner Status Poller ─────────────────────────────────────────
function startSkillPoller() {
    if (_skillPollTimer) return;
    _skillPollTimer = setInterval(pollSkillStatus, 5000);
}

function stopSkillPoller() {
    if (_skillPollTimer) {
        clearInterval(_skillPollTimer);
        _skillPollTimer = null;
    }
}

function pollSkillStatus() {
    fetch('/api/runner-status')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            // data is {label: true, ...} — build a set of running skill labels
            var activeSet = {};
            Object.keys(data).forEach(function(label) {
                if (label.indexOf('skill:') === 0) {
                    activeSet[label] = true;
                }
            });

            // Check each running skill to see if it finished
            // _runningSkills key: "taskId:skill", runner label: "skill:skill:taskId"
            var keys = Object.keys(_runningSkills);
            var changed = false;
            keys.forEach(function(key) {
                var parts = key.split(':');
                var taskId = parts[0];
                var skillName = parts[1];
                var runnerLabel = 'skill:' + skillName + ':' + taskId;
                if (!activeSet[runnerLabel]) {
                    // Skill finished — remove from tracker and re-fetch task
                    delete _runningSkills[key];
                    changed = true;
                    var taskId = parseInt(key.split(':')[0]);
                    // Re-fetch the task to get updated skill_output
                    fetch('/api/tasks/' + taskId)
                        .then(function(res) { return res.json(); })
                        .then(function(taskData) {
                            if (taskData.task) {
                                var idx = tasks.findIndex(function(t) { return t.id === taskData.task.id; });
                                if (idx >= 0) tasks[idx] = taskData.task;
                                renderTaskList();
                                if (selectedTaskId === taskData.task.id) {
                                    renderDetailPane(taskData.task);
                                }
                            }
                        })
                        .catch(function() {});
                }
            });

            // Stop polling if nothing is running
            if (Object.keys(_runningSkills).length === 0) {
                stopSkillPoller();
            }
        })
        .catch(function() {}); // Silent fail on poll
}

// ── Keyboard Shortcuts ────────────────────────────────────────────────
var _kbSelectedIdx = -1;
var _kbSectionIdx = 0;
var _VISIBLE_SECTIONS = ['active', 'suggested', 'waiting', 'snoozed', 'completed', 'dismissed', 'deleted'];

function setupKeyboardShortcuts() {
    document.addEventListener('keydown', handleKeyboardShortcut);
}

function handleKeyboardShortcut(e) {
    // Skip when typing in input/textarea/select
    var tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        if (e.key === 'Escape') {
            document.activeElement.blur();
            e.preventDefault();
        }
        return;
    }

    // Skip if modifier keys are held (allow browser shortcuts)
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    var key = e.key;
    // Shift+D arrives as 'D'. Action shortcuts are matched case-insensitively so
    // a held Shift does not silently swallow them. Named keys (Escape, Tab,
    // ArrowUp) are longer than one character and pass through untouched.
    var actionKey = key.length === 1 ? key.toLowerCase() : key;

    // Shortcuts overlay
    if (key === '?') {
        e.preventDefault();
        openShortcuts();
        return;
    }

    // Close shortcuts overlay or detail pane
    if (key === 'Escape') {
        var overlay = document.getElementById('shortcuts-overlay');
        if (overlay && overlay.classList.contains('open')) {
            closeShortcuts();
            e.preventDefault();
            return;
        }
        if (selectedTaskId) {
            clearDetailPane();
            _clearKeyboardSelection();
            e.preventDefault();
            return;
        }
    }

    // Focus quick-add
    if (actionKey === '/' || actionKey === 'n') {
        e.preventDefault();
        var input = document.getElementById('task-input');
        if (input) input.focus();
        return;
    }

    // Navigation: j/k or arrows
    if (actionKey === 'j' || key === 'ArrowDown') {
        e.preventDefault();
        _kbNavigate(1);
        return;
    }
    if (actionKey === 'k' || key === 'ArrowUp') {
        e.preventDefault();
        _kbNavigate(-1);
        return;
    }

    // Tab to cycle sections
    if (key === 'Tab') {
        // Once focus is inside a control, preserve native traversal. This keeps
        // the evidence/workspace separator and the controls after it reachable.
        var focused = document.activeElement;
        if (focused && focused !== document.body && focused !== document.documentElement) {
            return;
        }
        e.preventDefault();
        _kbCycleSection(e.shiftKey ? -1 : 1);
        return;
    }

    // Enter to select/open task
    if (key === 'Enter') {
        e.preventDefault();
        var rows = _getVisibleRows();
        if (_kbSelectedIdx >= 0 && _kbSelectedIdx < rows.length) {
            var taskId = parseInt(rows[_kbSelectedIdx].getAttribute('data-id'));
            if (taskId) selectTask(taskId);
        }
        return;
    }

    // Action shortcuts on selected task
    if (!selectedTaskId) return;
    var task = tasks.find(function(t) { return t.id === selectedTaskId; });
    if (!task) return;

    if (actionKey === 'c') {
        var allowedC = VALID_TRANSITIONS[task.status];
        if (allowedC && allowedC.indexOf('completed') !== -1) {
            _kbActThenAdvance(task.id, 'complete');
        }
    } else if (actionKey === 'd') {
        var allowedD = VALID_TRANSITIONS[task.status];
        if (allowedD && allowedD.indexOf('dismissed') !== -1) {
            _kbActThenAdvance(task.id, 'dismiss');
        }
    } else if (actionKey === 's') {
        if (task.status === 'suggested') {
            doAction(task.id, 'promote');
        } else {
            var allowedS = VALID_TRANSITIONS[task.status];
            if (allowedS && allowedS.indexOf('in_progress') !== -1) {
                doAction(task.id, 'start');
            }
        }
    } else if (actionKey === 'p') {
        if (task.status === 'suggested') {
            doAction(task.id, 'promote');
        }
    } else if (actionKey === 'r') {
        refreshTask(task.id);
    }
}

function _getVisibleRows() {
    return Array.prototype.slice.call(document.querySelectorAll('.task-row'));
}

function _rowTaskId(row) {
    return parseInt(row.getAttribute('data-id'));
}

function _kbActThenAdvance(taskId, action) {
    // Complete and dismiss move the task out of the section being triaged. The
    // selection used to stay on it, and since neither is a legal transition out
    // of the new status, every following keypress became a silent no-op -- the
    // shortcut looked broken. Work out the successor BEFORE acting, because the
    // list re-renders and the row moves to another section.
    var rows = _getVisibleRows();
    var successorId = null;
    for (var i = 0; i < rows.length; i++) {
        if (_rowTaskId(rows[i]) !== taskId) continue;
        for (var j = i + 1; j < rows.length; j++) {
            var candidate = _rowTaskId(rows[j]);
            if (candidate && candidate !== taskId) {
                successorId = candidate;
                break;
            }
        }
        break;
    }

    var pending = doAction(taskId, action);
    if (!pending || typeof pending.then !== 'function') return;
    pending.then(function() {
        _kbSelectAfterRemoval(successorId);
    });
}

function _kbSelectAfterRemoval(successorId) {
    var rows = _getVisibleRows();
    var idx = -1;
    for (var i = 0; i < rows.length; i++) {
        if (_rowTaskId(rows[i]) === successorId) { idx = i; break; }
    }
    if (idx === -1) {
        clearDetailPane();
        _clearKeyboardSelection();
        return;
    }
    _kbSelectedIdx = idx;
    _applyKeyboardSelection(rows);
    selectTask(successorId);
}

function _kbNavigate(direction) {
    var rows = _getVisibleRows();
    if (!rows.length) return;

    _kbSelectedIdx += direction;
    if (_kbSelectedIdx < 0) _kbSelectedIdx = 0;
    if (_kbSelectedIdx >= rows.length) _kbSelectedIdx = rows.length - 1;

    _applyKeyboardSelection(rows);
}

function _kbCycleSection(direction) {
    var sections = _VISIBLE_SECTIONS.filter(function(s) {
        var body = document.getElementById('body-' + s);
        return body && body.children.length > 0 && !body.classList.contains('collapsed');
    });
    if (!sections.length) return;

    _kbSectionIdx += direction;
    if (_kbSectionIdx < 0) _kbSectionIdx = sections.length - 1;
    if (_kbSectionIdx >= sections.length) _kbSectionIdx = 0;

    var targetSection = sections[_kbSectionIdx];
    var body = document.getElementById('body-' + targetSection);
    if (!body || !body.children.length) return;

    var firstRow = body.querySelector('.task-row');
    if (!firstRow) return;

    var rows = _getVisibleRows();
    for (var i = 0; i < rows.length; i++) {
        if (rows[i] === firstRow) {
            _kbSelectedIdx = i;
            _applyKeyboardSelection(rows);
            return;
        }
    }
}

function _applyKeyboardSelection(rows) {
    rows.forEach(function(r) { r.classList.remove('keyboard-selected'); });
    if (_kbSelectedIdx >= 0 && _kbSelectedIdx < rows.length) {
        rows[_kbSelectedIdx].classList.add('keyboard-selected');
        rows[_kbSelectedIdx].scrollIntoView({ block: 'nearest' });
    }
}

function _clearKeyboardSelection() {
    _kbSelectedIdx = -1;
    var rows = document.querySelectorAll('.task-row.keyboard-selected');
    rows.forEach(function(r) { r.classList.remove('keyboard-selected'); });
}

function openShortcuts() {
    var overlay = document.getElementById('shortcuts-overlay');
    if (overlay) overlay.classList.add('open');
}

function closeShortcuts() {
    var overlay = document.getElementById('shortcuts-overlay');
    if (overlay) overlay.classList.remove('open');
}

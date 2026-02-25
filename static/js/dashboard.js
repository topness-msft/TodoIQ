/* TodoNess Dashboard — vanilla JS */

// ── State ──────────────────────────────────────────────────────────────
var tasks = [];
var selectedTaskId = null;
var ws = null;
var reconnectTimer = null;
var openDropdownId = null;
var searchQuery = '';
var lastSyncTime = null;
var _skillPollTimer = null;
var _runningSkills = {};

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

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', init);

function init() {
    fetchTasks();
    connectWebSocket();
    setupInputBar();
    setupDropZones();
    startParsePoller();
    fetchSyncStatus();

    // Close people dropdown when clicking outside
    document.addEventListener('click', function(e) {
        if (!e.target.closest('.person-pill-wrapper')) {
            closeAllDropdowns();
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
    fetch('/api/tasks')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            tasks = data.tasks || [];
            renderTaskList();
            if (selectedTaskId) {
                var t = tasks.find(function(t) { return t.id === selectedTaskId; });
                if (t) renderDetailPane(t);
                else clearDetailPane();
            }
        })
        .catch(function(err) { console.error('Failed to fetch tasks:', err); });
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
        task.skill_output
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
    var inProgress = [];
    var active = [];
    var waiting = [];
    var snoozed = [];
    var suggested = [];
    var completed = [];
    var dismissed = [];
    var deleted = [];

    tasks.forEach(function(t) {
        if (!taskMatchesSearch(t)) return;
        if (t.status === 'in_progress') {
            inProgress.push(t);
        } else if (t.status === 'active') {
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

    renderSection('in_progress', inProgress);
    renderSection('active', active);
    renderSection('waiting', waiting);
    renderSection('snoozed', snoozed);
    renderSection('suggested', suggested);
    renderSection('completed', completed);
    renderSection('dismissed', dismissed);
    renderSection('deleted', deleted);
}

function renderSection(sectionId, sectionTasks) {
    var body = document.getElementById('body-' + sectionId);
    var count = document.getElementById('count-' + sectionId);
    count.textContent = sectionTasks.length;

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
        var parseHtml = parseStatusIcon(task.parse_status);
        var enrichedHtml = task.skill_output ? '<span class="enriched-icon" title="Skill enriched">\u26A1</span>' : '';

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
            snoozeHtml = '<span class="snooze-info">Snoozed until ' + formatSnoozeTime(task.snoozed_until) + '</span>';
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
            + priorityDot(task.priority)
            + '<div class="task-row-content">'
            + '<div class="task-row-top">'
            + '<span class="task-id-badge">#' + task.id + '</span>'
            + '<span class="task-source-icon">' + sourceTypeIcon(task.source_type) + '</span>'
            + '<span class="task-title">' + escapeHtml(task.title) + '</span>'
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
    selectedTaskId = taskId;

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
                renderDetailPane(data.task);
            }
        })
        .catch(function(err) { console.error('Failed to fetch task detail:', err); });
}

// ── Render Detail Pane ─────────────────────────────────────────────────
function renderDetailPane(task) {
    var pane = document.getElementById('detail-pane');

    var sourceIcon = sourceTypeIcon(task.source_type);
    var statusClass = (task.status || '').replace(/\s/g, '_');

    // Header card with inline actions
    var html = '<div class="detail-card">'
        + '<div class="detail-header-row">'
        + '<h2>' + escapeHtml(task.title) + '</h2>'
        + getHeaderActions(task)
        + '</div>'
        + '<div class="detail-meta">'
        + '<span class="meta-item"><span class="status-badge ' + statusClass + '">' + escapeHtml(task.status) + '</span></span>'
        + '<span class="meta-item">' + prioritySelector(task) + '</span>'
        + '<span class="meta-item">' + dueDateField(task) + '</span>'
        + '<span class="meta-item">' + actionTypeSelector(task) + '</span>'
        + '<span class="meta-item">' + parseStatusBadge(task.parse_status, task.id) + '</span>'
        + (task.status === 'snoozed' && task.snoozed_until
            ? '<span class="meta-item"><span class="snooze-detail-badge">Snoozed until ' + formatSnoozeTime(task.snoozed_until) + '</span></span>'
            : '')
        + '<span class="meta-item" style="margin-left:auto">' + sourceMetaLink(task) + '</span>'
        + '</div>';

    // Description (editable)
    if (task.description) {
        html += '<div style="margin-top:10px"><div class="detail-label">Description'
            + '<button class="btn-edit-inline" onclick="toggleDescriptionEdit(' + task.id + ')" title="Edit description">&#9998;</button>'
            + '</div>'
            + '<div id="desc-display-' + task.id + '" class="detail-description">' + renderRichText(task.description, task.key_people) + '</div>'
            + '<textarea id="desc-edit-' + task.id + '" class="description-edit-textarea" style="display:none" '
            + 'onblur="saveDescription(' + task.id + ')">' + escapeHtml(task.description) + '</textarea>'
            + '</div>';
    } else {
        html += '<div style="margin-top:10px"><div class="detail-label">Description'
            + '<button class="btn-edit-inline" onclick="toggleDescriptionEdit(' + task.id + ')" title="Add description">&#9998;</button>'
            + '</div>'
            + '<div id="desc-display-' + task.id + '" class="detail-description" style="color:#9e9e9e">No description</div>'
            + '<textarea id="desc-edit-' + task.id + '" class="description-edit-textarea" style="display:none" '
            + 'onblur="saveDescription(' + task.id + ')" placeholder="Add a description..."></textarea>'
            + '</div>';
    }

    html += '</div>';

    // Key People (pills)
    if (task.key_people) {
        html += '<div class="detail-card">'
            + '<div class="detail-label">Key People</div>'
            + renderPeoplePills(task.key_people, task.id)
            + '</div>';
    }

    // User Notes
    html += '<div class="detail-card">'
        + '<div class="detail-label">Notes</div>'
        + '<textarea class="notes-textarea" id="notes-textarea" '
        + 'onblur="saveNotes(' + task.id + ')" placeholder="Add your notes...">'
        + escapeHtml(task.user_notes || '')
        + '</textarea>'
        + '</div>';

    html += renderSkillButtons(task);

    // Skill Output (between coaching and source)
    if (task.skill_output) {
        html += '<div class="skill-output-card">'
            + '<div class="skill-output-header">'
            + '<div class="skill-output-title">\u26A1 Skill Output</div>'
            + '</div>'
            + '<div class="skill-output-text">' + renderRichText(task.skill_output, task.key_people) + '</div>';
        if (task.suggestion_refreshed_at) {
            html += '<div class="skill-output-updated">Updated ' + timeAgo(task.suggestion_refreshed_at) + '</div>';
        }
        html += '</div>';
    }

    // AI Coaching
    if (task.coaching_text) {
        var isStale = isCoachingStale(task);
        html += '<div class="coaching-card' + (isStale ? ' coaching-stale' : '') + '">'
            + '<div class="coaching-header">'
            + '<div class="coaching-title">AI Coaching'
            + '<button class="btn-edit-inline" onclick="toggleCoachingEdit(' + task.id + ')" title="Edit coaching">&#9998;</button>'
            + '</div>';
        if (isStale) {
            html += '<span class="coaching-stale-badge" title="Task has changed since last AI refresh">'
                + '&#9888; May be outdated'
                + '</span>';
        }
        html += '</div>'
            + '<div id="coaching-display-' + task.id + '" class="coaching-text">' + renderRichText(task.coaching_text, task.key_people) + '</div>'
            + '<textarea id="coaching-edit-' + task.id + '" class="notes-textarea" style="display:none" '
            + 'onblur="saveCoaching(' + task.id + ')">' + escapeHtml(task.coaching_text) + '</textarea>';
        if (task.suggestion_refreshed_at) {
            html += '<div class="coaching-updated">Updated ' + timeAgo(task.suggestion_refreshed_at) + '</div>';
        }
        html += '</div>';
    } else if (task.parse_status === 'parsed') {
        // No coaching yet — suggest refreshing
        html += '<div class="coaching-card coaching-empty">'
            + '<div class="coaching-title">AI Coaching</div>'
            + '<div class="coaching-text" style="color:#9e9e9e">No coaching yet. Click Refresh to get AI suggestions.</div>'
            + '</div>';
    }

    // Source snippet
    if (task.source_snippet) {
        html += '<div class="detail-card">'
            + '<div class="detail-label">Source</div>'
            + '<div class="detail-description">' + escapeHtml(task.source_snippet) + '</div>';
        if (task.source_url) {
            html += '<div style="margin-top:6px"><a href="' + escapeHtml(task.source_url) + '" target="_blank" style="color:#0f6cbd;font-size:12px">Open source</a></div>';
        }
        html += '</div>';
    }

    // Footer actions — contextual, prominent primary action
    html += '<div class="detail-actions-bar">'
        + getActionButtons(task)
        + '<span class="actions-spacer"></span>'
        + '<button class="btn btn-refresh" onclick="refreshTask(' + task.id + ')" title="Re-parse with Claude + WorkIQ">&#8635; Refresh</button>'
        + '</div>';

    pane.innerHTML = html;
}

function clearDetailPane() {
    selectedTaskId = null;
    var pane = document.getElementById('detail-pane');
    pane.innerHTML = '<div class="empty-state">'
        + '<div class="empty-state-icon">&#128203;</div>'
        + '<div>Select a task to view details</div>'
        + '</div>';
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
    if (!people.length) return '<div class="detail-value">' + escapeHtml(keyPeople) + '</div>';

    var html = '<div class="people-list">';
    people.forEach(function(person, idx) {
        var hasAlts = person.alternatives && person.alternatives.length > 0;
        var pillId = 'pill-' + taskId + '-' + idx;

        html += '<div class="person-pill-wrapper" id="wrapper-' + pillId + '">';

        // Pill
        html += '<div class="person-pill' + (hasAlts ? ' has-alternatives' : '') + '" '
            + 'onclick="event.stopPropagation(); togglePeopleDropdown(\'' + pillId + '\')">'
            + '<span class="person-pill-avatar">' + getInitials(person.name) + '</span>'
            + '<span>' + escapeHtml(person.name) + '</span>';
        if (person.role) {
            html += ' <span class="person-role">' + escapeHtml(person.role) + '</span>';
        }
        html += '</div>';

        // Alternatives dropdown
        if (hasAlts) {
            html += '<div class="alternatives-dropdown" id="dropdown-' + pillId + '">';
            html += '<div class="alternatives-header">Did you mean?</div>';

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

            html += '</div>';
        }

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
    if (!person || altIdx < 0) {
        closeAllDropdowns();
        return; // Already selected
    }

    var alt = person.alternatives[altIdx];
    if (!alt) return;

    var oldName = person.name;
    var newName = alt.name;

    // Swap: move current to alternatives, promote the selected alt
    var oldPrimary = { name: person.name, email: person.email, role: person.role };
    var newAlternatives = person.alternatives.filter(function(_, i) { return i !== altIdx; });
    newAlternatives.unshift(oldPrimary);

    people[personIdx] = {
        name: alt.name,
        email: alt.email,
        role: alt.role,
        alternatives: newAlternatives
    };

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

function replacePersonName(text, oldName, newName) {
    if (!text || !oldName || !newName) return text;
    // Replace full name
    var result = text.split(oldName).join(newName);
    // Also replace first name only if it appears as a standalone word
    var oldFirst = oldName.split(' ')[0];
    var newFirst = newName.split(' ')[0];
    if (oldFirst !== oldName && oldFirst.length > 2) {
        // Use word boundary: replace "Pratap" but not "Pratap" inside "PratapLadhani"
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
    var labels = { 1: 'P1 Urgent', 2: 'P2 High', 3: 'P3 Normal', 4: 'P4 Low', 5: 'P5 Information' };
    var html = '<span class="priority-field">'
        + '<span class="priority-dot-indicator p' + task.priority + '"></span>'
        + '<select class="priority-select" onchange="updatePriority(' + task.id + ', this.value)">';
    for (var i = 1; i <= 5; i++) {
        var sel = i === task.priority ? ' selected' : '';
        html += '<option value="' + i + '"' + sel + '>' + labels[i] + '</option>';
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
    } else if (task.status === 'active') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'start\')">Start Working</button>';
        html += renderSnoozeButton(task.id);
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'waiting\')">Waiting</button>';
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'complete\')">Mark Complete</button>';
        html += '<button class="btn btn-subtle" onclick="doAction(' + task.id + ',\'dismiss\')">Dismiss</button>';
    } else if (task.status === 'in_progress') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'complete\')">Mark Complete</button>';
        html += renderSnoozeButton(task.id);
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'waiting\')">Waiting</button>';
        html += '<button class="btn" onclick="doAction(' + task.id + ',\'transition\',\'active\')">Pause</button>';
    } else if (task.status === 'waiting') {
        html += '<button class="btn btn-primary" onclick="doAction(' + task.id + ',\'transition\',\'active\')">Move to Active</button>';
        html += renderSnoozeButton(task.id);
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

    fetch('/api/tasks/' + taskId + '/action', {
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
        body.classList.remove('collapsed');
        toggle.innerHTML = '&#9662;'; // ▾
    } else {
        body.classList.add('collapsed');
        toggle.innerHTML = '&#9656;'; // ▸
    }
}

// ── Drag and Drop ─────────────────────────────────────────────────────
var ALL_SECTIONS = ['in_progress', 'active', 'waiting', 'snoozed', 'suggested', 'completed', 'dismissed', 'deleted'];

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
function renderSnoozeButton(taskId) {
    return '<div class="snooze-btn-wrapper" style="display:inline-block;position:relative">'
        + '<button class="btn btn-snooze" onclick="event.stopPropagation(); toggleSnoozeDropdown(' + taskId + ')">Snooze</button>'
        + '<div class="snooze-dropdown" id="snooze-dropdown-' + taskId + '">'
        + '<div class="snooze-option" onclick="event.stopPropagation(); doSnooze(' + taskId + ',{duration_minutes:60})">1 hour</div>'
        + '<div class="snooze-option" onclick="event.stopPropagation(); doSnooze(' + taskId + ',{duration_minutes:240})">4 hours</div>'
        + '<div class="snooze-option" onclick="event.stopPropagation(); snoozeTomorrow(' + taskId + ')">Tomorrow 9 AM</div>'
        + '<div class="snooze-option" onclick="event.stopPropagation(); snoozeNextMonday(' + taskId + ')">Next Monday 9 AM</div>'
        + '<div class="snooze-option snooze-custom" onclick="event.stopPropagation(); showCustomSnooze(' + taskId + ')">'
        + 'Pick date...'
        + '<input type="datetime-local" class="snooze-datetime-input" id="snooze-custom-' + taskId + '" '
        + 'onchange="event.stopPropagation(); doSnoozeCustom(' + taskId + ', this.value)" style="display:none">'
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

function snoozeTomorrow(taskId) {
    var d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    doSnooze(taskId, { snoozed_until: d.toISOString() });
}

function snoozeNextMonday(taskId) {
    var d = new Date();
    var day = d.getDay(); // 0=Sun, 1=Mon, ...
    var daysUntilMon = day === 0 ? 1 : (8 - day);
    d.setDate(d.getDate() + daysUntilMon);
    d.setHours(9, 0, 0, 0);
    doSnooze(taskId, { snoozed_until: d.toISOString() });
}

function showCustomSnooze(taskId) {
    var input = document.getElementById('snooze-custom-' + taskId);
    if (input) {
        input.style.display = 'block';
        input.focus();
        try { input.showPicker(); } catch(e) {}
    }
}

function doSnoozeCustom(taskId, value) {
    if (!value) return;
    var d = new Date(value);
    doSnooze(taskId, { snoozed_until: d.toISOString() });
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

function priorityDot(priority) {
    var p = priority || 3;
    return '<span class="priority-dot p' + p + '"></span>';
}

function parseStatusIcon(parseStatus) {
    var status = parseStatus || 'parsed';
    if (status === 'parsed') {
        // Show briefly then fade — or always show as subtle indicator
    }
    return '<span class="parse-icon"><span class="parse-indicator ' + status + '"><span class="parse-ring"></span></span></span>';
}

function parseStatusBadge(parseStatus, taskId) {
    var status = parseStatus || 'parsed';
    var labels = {
        unparsed: 'Awaiting parse',
        queued: 'Queued',
        parsing: 'Parsing\u2026',
        parsed: 'Parsed'
    };
    var label = labels[status] || status;
    // Make unparsed/queued clickable to trigger refresh
    if (taskId && (status === 'unparsed' || status === 'queued' || status === 'parsed')) {
        return '<span class="parse-status-badge ' + status + ' clickable" '
            + 'onclick="event.stopPropagation(); refreshTask(' + taskId + ')" '
            + 'title="Click to refresh with AI">'
            + '<span class="parse-indicator ' + status + '"><span class="parse-ring"></span></span>'
            + escapeHtml(label)
            + '</span>';
    }
    return '<span class="parse-status-badge ' + status + '">'
        + '<span class="parse-indicator ' + status + '"><span class="parse-ring"></span></span>'
        + escapeHtml(label)
        + '</span>';
}

function sourceMetaLink(task) {
    var icon = sourceTypeIcon(task.source_type);
    // Extract the original subject from source_id (format: type::email::subject)
    var subject = '';
    if (task.source_id) {
        var parts = task.source_id.split('::');
        if (parts.length >= 3) subject = parts.slice(2).join('::');
    }
    // Use subject as link text, fall back to source_snippet, then source_type
    var label = subject || (task.source_snippet ? truncate(task.source_snippet, 50) : (task.source_type || 'manual'));
    if (task.source_url) {
        return icon + ' <a href="' + escapeHtml(task.source_url) + '" target="_blank" '
            + 'class="source-meta-link" title="Open in Outlook/Teams">'
            + escapeHtml(label) + ' \u2197</a>';
    }
    return icon + ' ' + escapeHtml(label);
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

function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
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
    // Sort longest first so "Pratap Ladhani" matches before "Pratap"
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
    var autoSyncCheckbox = document.getElementById('auto-sync-checkbox');

    // Update auto-sync toggle state
    if (autoSyncCheckbox && data.auto_sync_enabled !== undefined) {
        autoSyncCheckbox.checked = data.auto_sync_enabled;
    }

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

function toggleAutoSync(enabled) {
    fetch('/api/sync-status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_sync: enabled })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        var checkbox = document.getElementById('auto-sync-checkbox');
        if (checkbox && data.auto_sync_enabled !== undefined) {
            checkbox.checked = data.auto_sync_enabled;
        }
    })
    .catch(function(err) { console.error('Failed to toggle auto-sync:', err); });
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

// ── Skill Buttons ──────────────────────────────────────────────────────
function renderSkillButtons(task) {
    var actionType = task.action_type || 'general';
    if (actionType === 'general') return '';

    var skillMap = {
        'respond-email': { label: 'Draft Reply', skill: 'respond-email', icon: '\u2709' },
        'schedule-meeting': { label: 'Find Times', skill: 'schedule-meeting', icon: '\uD83D\uDCC5' },
        'follow-up': { label: 'Draft Follow-up', skill: 'follow-up', icon: '\uD83D\uDD04' },
        'awaiting-response': { label: 'Draft Follow-up', skill: 'follow-up', icon: '\u231B' },
        'prepare': { label: 'Prep Notes', skill: 'prepare', icon: '\uD83D\uDCCB' },
        'teams-message': { label: 'Draft Message', skill: 'teams-message', icon: '\uD83D\uDCAC' }
    };

    var buttons = [];
    var primary = skillMap[actionType];

    // Add primary button if mapped (teams-message only for chat source)
    if (primary) {
        if (actionType === 'teams-message' && task.source_type !== 'chat') {
            // Skip teams-message if not from chat source
        } else {
            buttons.push(primary);
        }
    }

    // Add "Draft Follow-up" as secondary if not already primary
    // (awaiting-response already uses follow-up as primary, so skip secondary)
    if (actionType !== 'follow-up' && actionType !== 'awaiting-response') {
        buttons.push({ label: 'Draft Follow-up', skill: 'follow-up', icon: '\uD83D\uDD04' });
    }

    if (!buttons.length) return '';

    var html = '<div class="skill-buttons-card"><div class="detail-label">Actions</div><div class="skill-buttons-row">';
    buttons.forEach(function(btn) {
        var skillKey = task.id + ':' + btn.skill;
        var isRunning = _runningSkills[skillKey];
        var runningClass = isRunning ? ' running' : '';
        var iconHtml = isRunning
            ? '<span class="skill-spinner"></span>'
            : '<span class="skill-btn-icon">' + btn.icon + '</span>';
        html += '<button class="btn-skill' + runningClass + '" data-skill="' + btn.skill + '" data-task-id="' + task.id + '" onclick="runSkill(' + task.id + ', \'' + btn.skill + '\')">'
            + iconHtml + ' ' + escapeHtml(btn.label)
            + '</button>';
    });
    html += '</div></div>';
    return html;
}

function runSkill(taskId, skillName) {
    var skillKey = taskId + ':' + skillName;
    if (_runningSkills[skillKey]) return;

    fetch('/api/tasks/' + taskId + '/skill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ skill: skillName })
    })
    .then(function(res) { return res.json(); })
    .then(function(data) {
        _runningSkills[skillKey] = true;
        startSkillPoller();
        if (selectedTaskId === taskId) {
            var task = tasks.find(function(t) { return t.id === taskId; });
            if (task) renderDetailPane(task);
        }
    })
    .catch(function(err) { console.error('Failed to run skill:', err); });
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

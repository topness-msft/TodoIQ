/**
 * TodoIQ API Adapter
 * Overrides mock in-memory functions with real API calls.
 * Loaded after the mock script on /todo route.
 */

// ── Data Layer ────────────────────────────────────────────
function normalizeTask(t) {
  if (typeof t.key_people === 'string') {
    try { t.key_people = JSON.parse(t.key_people); } catch(e) { t.key_people = []; }
  }
  if (!t.key_people) t.key_people = [];
  if (typeof t.priority === 'number') t.priority = 'P' + t.priority;
  if (!t.priority) t.priority = 'P3';
  t.is_quick_hit = !!t.is_quick_hit;
  t.ai_output = t.skill_output || null;
  t.ai_enriched = !!(t.skill_output || t.coaching_text);
  if (!t.notes && t.user_notes) t.notes = t.user_notes;
  // Parse waiting_activity JSON → extract summary
  if (typeof t.waiting_activity === 'string' && t.waiting_activity.startsWith('{')) {
    try {
      const wa = JSON.parse(t.waiting_activity);
      t._wa_status = wa.status; // activity_detected, no_activity, out_of_office
      t._wa_summary = wa.summary || '';
      t._wa_checked = wa.checked_at;
      t._wa_return = wa.return_date;
    } catch(e) { t._wa_summary = t.waiting_activity; }
  } else if (t.waiting_activity) {
    t._wa_summary = t.waiting_activity;
  }
  return t;
}

async function fetchTasks() {
  try {
    const res = await fetch('/api/tasks?exclude_status=deleted&limit=2000');
    const data = await res.json();
    tasks = data.tasks.map(normalizeTask);
    const today = new Date().toISOString().slice(0, 10);
    quickStreak = tasks.filter(t =>
      t.status === 'completed' && t.is_quick_hit &&
      t.updated_at && t.updated_at.startsWith(today)
    ).length;
    refresh();
  } catch (e) {
    console.error('Failed to fetch tasks:', e);
    toast('Failed to load tasks');
  }
}

function updateLocalTask(apiTask) {
  const t = normalizeTask(apiTask);
  const idx = tasks.findIndex(x => x.id === t.id);
  if (idx >= 0) tasks[idx] = t;
  else tasks.unshift(t);
  refresh();
  if (selectedId === t.id) selectTask(t.id);
}

// ── WebSocket ─────────────────────────────────────────────
let _ws = null;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  _ws = new WebSocket(proto + '//' + location.host + '/ws');
  _ws.onopen = () => {
    const d = document.querySelector('.sync-dot');
    if (d) d.classList.add('connected');
  };
  _ws.onclose = () => {
    const d = document.querySelector('.sync-dot');
    if (d) d.classList.remove('connected');
    setTimeout(connectWS, 3000);
  };
  _ws.onerror = () => { _ws.close(); };
  _ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      switch (msg.type) {
        case 'task_created':
          if (msg.task && !tasks.find(t => t.id === msg.task.id)) {
            tasks.unshift(normalizeTask(msg.task));
            refresh();
          }
          break;
        case 'task_updated':
          if (msg.task) updateLocalTask(msg.task);
          break;
        case 'task_deleted':
          tasks = tasks.filter(t => t.id !== msg.task_id);
          if (selectedId === msg.task_id) closeDetail();
          refresh();
          break;
        case 'parse_error':
          const pt = tasks.find(t => t.id === msg.task_id);
          if (pt) {
            pt.parse_status = 'error';
            pt.error_message = msg.error_message;
            refresh();
            if (selectedId === msg.task_id) selectTask(msg.task_id);
          }
          break;
        case 'skill_running':
          toast('Running ' + (msg.skill || 'skill') + '...');
          break;
      }
    } catch (err) { console.error('WS error:', err); }
  };
}

// ── API helpers ───────────────────────────────────────────
async function transitionTask(id, status) {
  try {
    const res = await fetch(`/api/tasks/${id}/action`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'transition', status })
    });
    const data = await res.json();
    if (data.task) updateLocalTask(data.task);
  } catch (e) { toast('Failed'); }
}

async function apiAction(id, body) {
  const res = await fetch(`/api/tasks/${id}/action`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (data.task) updateLocalTask(data.task);
  return data;
}

async function apiUpdate(id, fields) {
  await fetch(`/api/tasks/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields)
  });
}

// ── Override mock functions with real API calls ────────────

// Override: toggleComplete
toggleComplete = async function(id) {
  const t = tasks.find(t => t.id === id);
  if (!t) return;
  const wasActive = t.status !== 'completed';
  try {
    const body = wasActive ? { action: 'complete' } : { action: 'transition', status: 'active' };
    await apiAction(id, body);
    if (wasActive) {
      toast('Task completed');
      if (t.is_quick_hit) quickStreak++;
      showSmartComplete(id);
    } else {
      toast('Task restored');
    }
  } catch (e) { toast('Action failed'); }
};

// Override: promoteTask
promoteTask = async function(id) {
  try {
    await apiAction(id, { action: 'promote' });
    toast('Added to tasks');
  } catch (e) { toast('Failed to promote'); }
};

// Override: dismissTask
dismissTask = async function(id) {
  const t = tasks.find(t => t.id === id);
  if (!t) return;
  const prevStatus = t.status;
  try {
    await apiAction(id, { action: 'dismiss' });
    if (selectedId === id) selectTask(id); // re-render detail with dismissed state
    // Undo toast
    const c = document.getElementById('toast-container');
    const d = document.createElement('div');
    d.className = 'undo-toast';
    d.innerHTML = `Dismissed — <button class="undo-btn" onclick="undoDismiss(${id},'${prevStatus}');this.closest('.undo-toast').remove()">Undo</button>`;
    c.appendChild(d);
    setTimeout(() => { if (d.parentNode) { d.style.opacity = '0'; setTimeout(() => d.remove(), 300); } }, 5000);
  } catch (e) { toast('Failed to dismiss'); }
};

// Override: undoDismiss
undoDismiss = async function(id, prevStatus) {
  try {
    await apiAction(id, { action: 'transition', status: prevStatus || 'suggested' });
    toast('Restored');
  } catch (e) { toast('Failed to restore'); }
};

// Override: startTask
startTask = async function(id) {
  try {
    await apiAction(id, { action: 'start' });
    toast('Started');
  } catch (e) { toast('Failed to start'); }
};

// Override: wakeTask
wakeTask = async function(id) {
  try {
    await apiAction(id, { action: 'transition', status: 'active' });
    toast('Woke up');
  } catch (e) { toast('Failed'); }
};

// Override: deleteTask
deleteTask = async function(id) {
  try {
    await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
    tasks = tasks.filter(t => t.id !== id);
    if (selectedId === id) closeDetail();
    toast('Deleted');
    refresh();
  } catch (e) { toast('Failed to delete'); }
};

// Override: addTask
addTask = async function() {
  const inp = document.getElementById('add-input');
  const title = inp.value.trim();
  if (!title) return;
  try {
    const res = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ raw_input: title })
    });
    const data = await res.json();
    if (data.task) {
      tasks.unshift(normalizeTask(data.task));
      refresh();
    }
    inp.value = '';
    toast('Task created — AI will enrich it');
  } catch (e) { toast('Failed to create task'); }
};

// Override: updateNotes (with debounced save)
let _notesTimer = null;
updateNotes = function(id, v) {
  const t = tasks.find(t => t.id === id);
  if (t) { t.notes = v; t.user_notes = v; }
  clearTimeout(_notesTimer);
  _notesTimer = setTimeout(async () => {
    try { await apiUpdate(id, { user_notes: v }); }
    catch (e) { console.error('Failed to save notes'); }
  }, 1000);
};

// Override: saveTitle
saveTitle = async function(id, text) {
  const t = tasks.find(t => t.id === id);
  if (!t) return;
  const trimmed = text.trim();
  if (trimmed && trimmed !== t.title) {
    t.title = trimmed;
    refresh();
    try { await apiUpdate(id, { title: trimmed }); }
    catch (e) { toast('Failed to save'); }
  }
};

// Override: saveDescription
saveDescription = async function(id, text) {
  const t = tasks.find(t => t.id === id);
  if (!t) return;
  t.description = text.trim();
  try { await apiUpdate(id, { description: t.description }); }
  catch (e) { toast('Failed to save'); }
};

// Override: setPriority
setPriority = async function(id, pri) {
  const t = tasks.find(t => t.id === id);
  if (!t) return;
  t.priority = pri;
  const numPri = parseInt(pri.replace('P', ''));
  try { await apiUpdate(id, { priority: numPri }); }
  catch (e) { toast('Failed to save'); }
  refresh();
  selectTask(id);
};

// Override: setDueDate
if (typeof setDueDate === 'function') {
  setDueDate = async function(id, val) {
    const t = tasks.find(t => t.id === id);
    if (!t) return;
    t.due_date = val || null;
    const existing = document.getElementById('due-menu');
    if (existing) existing.remove();
    try { await apiUpdate(id, { due_date: val || null }); }
    catch (e) { toast('Failed to save'); }
    toast('Due date set');
    refresh();
    selectTask(id);
  };
}

// Override: removeDueDate
if (typeof removeDueDate === 'function') {
  removeDueDate = async function(id) {
    const t = tasks.find(t => t.id === id);
    if (!t) return;
    t.due_date = null;
    try { await apiUpdate(id, { due_date: null }); }
    catch (e) { toast('Failed to save'); }
    toast('Due date removed');
    refresh();
    selectTask(id);
  };
}

// Override: doSync
doSync = async function() {
  const b = document.getElementById('sync-btn');
  b.classList.add('syncing');
  toast('Syncing with WorkIQ...');
  try {
    await fetch('/api/sync-status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    setTimeout(async () => {
      await fetchTasks();
      b.classList.remove('syncing');
      toast('Sync complete');
    }, 5000);
  } catch (e) {
    b.classList.remove('syncing');
    toast('Sync failed');
  }
};

// Override: retryParse
retryParse = async function(id) {
  try {
    await fetch(`/api/tasks/${id}/refresh`, { method: 'POST' });
    toast('Retrying parse...');
  } catch (e) { toast('Retry failed'); }
};

// Redo skill — call POST /api/tasks/{id}/skill
async function redoSkill(id, actionType) {
  const skillMap = {
    'respond-email': 'respond-email',
    'follow-up': 'follow-up',
    'schedule-meeting': 'schedule-meeting',
    'prepare': 'prepare',
    'awaiting-response': 'follow-up',
    'general': 'follow-up'
  };
  const skill = skillMap[actionType] || 'follow-up';
  _showGeneratingCard();
  try {
    const res = await fetch(`/api/tasks/${id}/skill`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skill })
    });
    const data = await res.json();
    if (data.ok === false && data.message?.includes('already running')) {
      toast('Already generating — please wait');
    } else {
      toast('Generating — this runs in the background...');
    }
    pollForSkillResult(id);
  } catch (e) {
    toast('Failed to regenerate');
    if (selectedId === id) selectTask(id);
  }
}

function _showGeneratingCard() {
  const card = document.querySelector('.ai-action-card');
  if (card) {
    card.style.borderStyle = 'dashed';
    card.querySelector('.ai-action-body').innerHTML = `<div style="text-align:center;padding:16px;color:var(--text-muted)">
      <div style="font-size:14px;margin-bottom:4px">Generating...</div>
      <div style="font-size:12px">This runs in the background — you can navigate away.</div>
    </div>`;
    const footer = card.querySelector('.ai-action-footer');
    if (footer) footer.innerHTML = '';
  }
}

async function isSkillRunning(id) {
  try {
    const res = await fetch('/api/runner-status');
    const data = await res.json();
    for (const key of Object.keys(data)) {
      if (key.includes(':' + id) && data[key] === true) return true;
    }
  } catch(e) {}
  return false;
}

function pollForSkillResult(id) {
  let attempts = 0;
  const poll = setInterval(async () => {
    attempts++;
    if (attempts > 90) {
      clearInterval(poll);
      toast('Generation timed out — check back later');
      if (selectedId === id) selectTask(id);
      return;
    }
    try {
      const res = await fetch(`/api/tasks/${id}`);
      const data = await res.json();
      if (data.task?.skill_output) {
        clearInterval(poll);
        updateLocalTask(data.task);
        toast('AI draft generated');
      }
    } catch(e) {}
  }, 2000);
}

// Override: doSnoozeHours
if (typeof doSnoozeHours === 'function') {
  doSnoozeHours = async function(id, hours) {
    try {
      await apiAction(id, { action: 'snooze', duration_minutes: hours * 60 });
      const picker = document.getElementById('snooze-picker');
      if (picker) picker.remove();
      toast(`Snoozed for ${hours} hour${hours > 1 ? 's' : ''}`);
      if (selectedId === id) closeDetail();
    } catch (e) { toast('Snooze failed'); }
  };
}

// Override: doSnooze (custom date)
if (typeof doSnooze === 'function') {
  doSnooze = async function(id, dateStr, timeStr) {
    if (!dateStr) return;
    const snoozed_until = `${dateStr}T${timeStr || '09:00'}:00`;
    try {
      await apiAction(id, { action: 'snooze', snoozed_until });
      const picker = document.getElementById('snooze-picker');
      if (picker) picker.remove();
      toast('Snoozed');
      if (selectedId === id) closeDetail();
    } catch (e) { toast('Snooze failed'); }
  };
}

// ── Initialize: replace mock data with real API data ──────
(async function initTodoIQ() {
  // Clear mock data
  tasks = [];
  refresh();

  // Fetch real tasks
  await fetchTasks();

  // Connect WebSocket
  connectWS();

  // Wrap selectTask to check runner-status for active skill runs
  const _origSelectTask = selectTask;
  selectTask = function(id) {
    _origSelectTask(id);
    // Check server-side if a skill is running for this task
    isSkillRunning(id).then(running => {
      if (running && selectedId === id) {
        _showGeneratingCard();
        pollForSkillResult(id);
      }
    });
  };

  console.log('TodoIQ API adapter loaded —', tasks.length, 'tasks from backend');
})();

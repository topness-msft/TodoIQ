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

// ── Cowork preview: real API wiring ───────────────────────
//
// Overrides the in-memory state machine in mock-todo.html with the preview
// API. The render functions are shared; only the transitions change. The
// server owns destination_kind (parse_source_url), so the local fallback in
// cwLocalKind is never used on this page.
//
// PHASE 1 IS PREVIEW ONLY — there is no execute route to call.

const CW_POLL_MS = 3000;
const CW_POLL_MAX = 235;   // ~700s, just past the runner's 660s timeout
const _cwPollers = {};
let _cwStartedAt = {};

function cwApply(t, action) {
  t.cw_loaded = true;
  if (!action) {
    t.cw_state = 'idle';
    return t;
  }
  t.cw_action_id = action.id;
  t.cw_state = action.state;
  t.cw_finding = action.finding || '';
  t.cw_draft = action.draft || '';
  t.cw_draft_edited = action.draft_edited || '';
  t.cw_redirect_text = action.redirect_text || '';
  t.cw_dest_kind = action.destination_kind || '';
  t.cw_dest_ref = action.destination_ref || '';
  t.cw_conversation_id = action.conversation_id || '';
  t.cw_seen_at = action.seen_at || null;
  t.cw_error = action.error || '';
  t.cw_terminal_status = action.terminal_status || '';
  if (action.created_at && !_cwStartedAt[t.id]) {
    const ms = Date.parse(action.created_at);
    if (!isNaN(ms)) _cwStartedAt[t.id] = ms;
  }
  return t;
}

function cwFormatElapsed(id) {
  const started = _cwStartedAt[id];
  if (!started) return '';
  const secs = Math.max(0, Math.round((Date.now() - started) / 1000));
  return Math.floor(secs / 60) + ':' + String(secs % 60).padStart(2, '0') + ' elapsed';
}

async function cwLoad(id, markSeen) {
  const t = cwTask(id);
  if (!t || (t.cw_loaded && !markSeen)) return;
  const firstLoad = !t.cw_loaded;
  t.cw_loaded = true;          // guard before the await, so the re-render
  if (firstLoad) t.cw_state = 'loading';
  try {
    const res = await fetch(`/api/tasks/${id}/cowork${markSeen ? '?mark_seen=1' : ''}`);
    const data = res.status === 404 ? { action: null } : await res.json();
    cwApply(t, data.action);
    if (t.cw_state === 'previewing') cwStartPoller(id);
  } catch (e) {
    t.cw_state = 'idle';
  }
  cwRerender(id);
}

async function cwStart(id, isRedo) {
  const t = cwTask(id);
  if (!t) return;
  const body = {};
  if (isRedo) {
    const box = document.getElementById(`cw-redo-${id}`);
    const text = box ? box.value.trim() : '';
    if (text) body.redirect_text = text;
  }
  t.cw_redo_open = false;
  t.cw_editing = false;
  t.cw_state = 'previewing';
  _cwStartedAt[id] = Date.now();
  cwRerender(id);

  try {
    const res = await fetch(`/api/tasks/${id}/cowork`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (res.status === 409) {
      toast('A preview is already running');
    } else if (data.action) {
      cwApply(t, data.action);
    } else {
      t.cw_state = 'failed';
      t.cw_error = data.error || 'Could not start Cowork';
    }
  } catch (e) {
    t.cw_state = 'failed';
    t.cw_error = 'Could not reach the server';
  }
  cwRerender(id);
  if (t.cw_state === 'previewing') cwStartPoller(id);
}

async function cwSaveDraft(id) {
  const t = cwTask(id);
  if (!t) return;
  const box = document.getElementById(`cw-edit-${id}`);
  if (!box) return;
  const text = box.value;
  t.cw_editing = false;
  try {
    const res = await fetch(`/api/tasks/${id}/cowork`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ draft_edited: text })
    });
    const data = await res.json();
    if (data.action) cwApply(t, data.action);
  } catch (e) { toast('Failed to save draft'); }
  cwRerender(id);
}

// The intent is coaching_text on the task, not a field on the action row.
async function cwSaveIntent(id) {
  const t = cwTask(id);
  if (!t) return;
  const box = document.getElementById(`cw-intent-${id}`);
  const text = box ? box.value : null;
  t.cw_intent_editing = false;
  if (text === null || text === t.coaching_text) return cwRerender(id);
  t.coaching_text = text;
  cwRerender(id);
  try {
    await apiUpdate(id, { coaching_text: text });
  } catch (e) { toast('Failed to save'); }
}

// Client side only — there is no delete route, so the task_actions audit
// chain stays intact. Reloads on the next visit.
function cwDiscard(id) {
  const t = cwTask(id);
  if (!t) return;
  t.cw_state = 'idle';
  t.cw_editing = false;
  t.cw_redo_open = false;
  t.cw_loaded = false;
  cwRerender(id);
}

function cwStartPoller(id) {
  cwStopPoller(id);
  _cwPollers[id] = {
    count: 0,
    timer: setInterval(() => cwPoll(id), CW_POLL_MS)
  };
}

function cwStopPoller(id) {
  const poller = _cwPollers[id];
  if (!poller) return;
  clearInterval(poller.timer);
  delete _cwPollers[id];
}

async function cwPoll(id) {
  const poller = _cwPollers[id];
  if (!poller) return;
  if (++poller.count > CW_POLL_MAX) return cwStopPoller(id);

  const hb = document.getElementById(`cw-hb-${id}`);
  if (hb) hb.textContent = cwFormatElapsed(id);

  try {
    const res = await fetch(`/api/tasks/${id}/cowork`);
    if (res.status === 404) return;
    const data = await res.json();
    if (!data.action) return;
    const t = cwTask(id);
    if (!t) return cwStopPoller(id);
    cwApply(t, data.action);
    if (t.cw_state !== 'previewing') {
      cwStopPoller(id);
      cwRerender(id);
    }
  } catch (e) { /* silent */ }
}

// Load the preview lazily when a task is opened.
(function wrapSelectTaskForCowork() {
  const _prev = selectTask;
  selectTask = function(id) {
    _prev(id);
    const t = cwTask(id);
    if (t && t.action_type && t.status !== 'suggested' &&
        (!t.cw_loaded || (t.cw_state === 'ready' && !t.cw_seen_at))) cwLoad(id, true);
    if (t && t.cw_state === 'previewing' && !_cwPollers[id]) cwStartPoller(id);
  };
})();


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

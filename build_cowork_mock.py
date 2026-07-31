"""One-shot patch: derive static/mock-todo-cowork.html from static/mock-todo.html.

Swaps the static "AI Action Card" (draft + Copy) for the Cowork action card
(preview -> confirm -> receipt), keeping the real nav, list and detail chrome.
"""
import pathlib, sys

SRC = pathlib.Path("static/mock-todo.html")
DST = pathlib.Path("static/mock-todo-cowork.html")
html = SRC.read_text(encoding="utf-8")


def sub(anchor: str, new: str, label: str) -> None:
    global html
    if anchor not in html:
        sys.exit(f"ANCHOR MISS [{label}]: {anchor[:70]!r}")
    if html.count(anchor) != 1:
        sys.exit(f"ANCHOR AMBIGUOUS [{label}]: {html.count(anchor)} matches")
    html = html.replace(anchor, new)


# ── 1. title ──────────────────────────────────────────────────
sub("<title>TodoIQ</title>", "<title>TodoIQ · Cowork actions (mock)</title>", "title")

# ── 2. Cowork card CSS ────────────────────────────────────────
CSS = """
/* ── Cowork Action Card ───────────────────────────────── */
.cw-card { margin: 8px 20px; border-radius: 8px; border: 1px solid var(--ai-border); background: var(--ai-light); overflow: hidden; }
.cw-card.is-executed { border-color: var(--ready-green); background: var(--ready-light); }
.cw-card.is-failed { border-color: var(--danger); }
.cw-card.is-running { border-style: dashed; }
.cw-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px 8px; font-size: 11px; font-weight: 600; color: var(--ai); }
.cw-card.is-executed .cw-head { color: var(--ready-green); }
.cw-card.is-failed .cw-head { color: var(--danger); }
.cw-head .cw-type { flex: 1; }
.cw-badge { font-size: 11px; font-weight: 700; letter-spacing: .4px; text-transform: uppercase; padding: 1px 7px; border-radius: 10px; background: var(--bg); border: 1px solid currentColor; }
.cw-body { padding: 0 14px 12px; font-size: 14px; line-height: 1.55; }

.cw-progress { display: flex; align-items: center; gap: 10px; padding: 12px 2px; color: var(--text-secondary); }
.cw-spinner { width: 14px; height: 14px; border: 2px solid var(--ai-border); border-top-color: var(--ai); border-radius: 50%; animation: cwspin .7s linear infinite; flex-shrink: 0; }
@keyframes cwspin { to { transform: rotate(360deg); } }
.cw-progress-sub { font-size: 11px; color: var(--text-muted); font-family: ui-monospace, Consolas, monospace; }

.cw-finding { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin: 4px 0 10px; }
.cw-finding .f-label { display: block; font-size: 11px; font-weight: 700; letter-spacing: .4px; text-transform: uppercase; color: var(--text-muted); margin-bottom: 5px; }

.cw-draft { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; margin: 4px 0 6px; white-space: pre-wrap; }
.cw-card.is-executed .cw-draft { background: transparent; }
.cw-draft[contenteditable="true"] { outline: 2px solid var(--ai); outline-offset: 1px; }

/* destination line (F9: no thread ref exists, so show the resolved target) */
.cw-dest { display: flex; align-items: flex-start; gap: 7px; font-size: 11px; padding: 8px 10px; margin: 6px 0 2px; border-radius: 6px; background: var(--bg); border: 1px solid var(--border); color: var(--text-secondary); }
.cw-dest b { color: var(--text); font-weight: 600; }
.cw-dest.is-risky { border-color: var(--warning); background: color-mix(in srgb, var(--warning) 9%, transparent); }
.cw-dest .d-conv { font-family: ui-monospace, Consolas, monospace; font-size: 11px; color: var(--text-muted); word-break: break-all; }
.cw-intent { font-size: 11px; line-height: 1.5; color: var(--text-secondary); padding: 0 0 8px; margin: 0 0 8px; border-bottom: 1px solid var(--border); }
.cw-intent .i-label { color: var(--text-muted); }
.cw-intent .i-edit { color: var(--ai); cursor: pointer; margin-left: 5px; white-space: nowrap; }
.cw-intent .i-edit:hover { text-decoration: underline; }
.cw-intent-box { width: 100%; box-sizing: border-box; font: inherit; font-size: 11px; line-height: 1.5; color: var(--text); background: var(--bg); border: 1px solid var(--ai); border-radius: 6px; padding: 7px 9px; resize: vertical; }

.cw-foot { display: flex; gap: 6px; align-items: center; padding: 0 14px 12px; flex-wrap: wrap; }
.cw-btn { padding: 6px 13px; border-radius: 5px; font-size: 11px; font-weight: 600; font-family: inherit; cursor: pointer; border: none; transition: all .12s; }
.cw-btn-primary { background: var(--ai); color: #fff; }
.cw-btn-go { background: var(--ready-green); color: #fff; }
.cw-btn-primary:hover, .cw-btn-go:hover { opacity: .85; }
.cw-btn-sec { background: transparent; color: var(--ai); border: 1px solid var(--ai-border); }
.cw-btn-sec:hover { background: var(--ai-light); }
.cw-btn-ghost { background: transparent; color: var(--text-muted); border: 1px solid var(--border); }
.cw-btn-ghost:hover { color: var(--text); }
.cw-foot-note { font-size: 11px; color: var(--text-muted); margin-left: auto; }

.cw-receipt { border-top: 1px solid var(--border); margin-top: 8px; padding-top: 8px; }
.cw-receipt-head { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .4px; color: var(--text-muted); margin-bottom: 5px; }
.cw-receipt-line { display: flex; align-items: center; gap: 8px; font-size: 11px; padding: 2px 0; color: var(--text-secondary); }
.cw-receipt-line .r-ok { color: var(--ready-green); font-weight: 700; }
.cw-receipt-line .r-tool { font-family: ui-monospace, Consolas, monospace; }
.cw-receipt-line .r-dur { margin-left: auto; color: var(--text-muted); }

/* row badge */
.task-hint.cw-sent { color: var(--ready-green); font-weight: 600; }

/* mock-only banner */
.cw-mockbar { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 3000;
  display: flex; align-items: center; gap: 6px; padding: 8px 12px; border-radius: 999px;
  background: var(--bg); border: 1px solid var(--border); box-shadow: var(--shadow); font-size: 11px; }
.cw-mockbar span.mb-l { color: var(--text-muted); margin-right: 2px; }
.cw-mockbar button { padding: 4px 9px; border-radius: 999px; border: 1px solid var(--border);
  background: var(--bg); color: var(--text-secondary); font-family: inherit; font-size: 11px; cursor: pointer; }
.cw-mockbar button:hover { color: var(--text); }

/* ── Overflow Menu ────────────────────────────────────── */"""
sub("/* ── Overflow Menu ────────────────────────────────────── */", CSS, "css")

# ── 3. Replace the AI Action Card block in selectTask() ───────
OLD_CARD_START = "  // AI Action Card\n  let aiCard = '';"
OLD_CARD_END = "  const created = t.created_at"
i = html.find(OLD_CARD_START)
j = html.find(OLD_CARD_END, i)
if i < 0 or j < 0:
    sys.exit("ANCHOR MISS [card block]")
html = html[:i] + "  const aiCard = renderCoworkCard(t);\n\n" + html[j:]

# ── 4. Cowork renderer + state machine, injected before "// ── State ──" ──
JS = r"""
// ── Cowork action layer (mock) ─────────────────────────────
// Verbatim capture from the Phase 0 spike (task #2076):
//   cowork send --json --deny-tools --ref person:bknoer@microsoft.com
//   -> terminal_status "ok", 42.3s
// States idle/previewing/ready are spike-accurate. executing/executed/failed
// are ILLUSTRATIVE - no send was ever executed, so the send tool name below
// is a placeholder, not an observed value.
const CW_LABELS = {
  'respond-email':     'Reply · Cowork',
  'schedule-meeting':  'Scheduling · Cowork',
  'follow-up':         'Follow-up · Cowork',
  'awaiting-response': 'Follow-up · Cowork',
  'prepare':           'Prep · Cowork',
  'review-document':   'Review · Cowork',
  'general':           'Next step · Cowork',
};
const CW_VERB = {
  'respond-email':     'send this reply',
  'schedule-meeting':  'book this meeting',
  'follow-up':         'post this message',
  'awaiting-response': 'post this message',
  'prepare':           'save these notes',
  'review-document':   'save this review',
  'general':           'run this step',
};
const CW_HEARTBEAT = [
  ['0:01','Connecting to container'], ['0:04','Ready'],
  ['0:06','Connecting MCP servers'],  ['0:19','tool_search_tool'],
  ['0:26','mcp__m365_teams__ListChatMessages'],
  ['0:26','mcp__m365_search__SearchM365'],
  ['0:36','thinking'], ['0:37','Searching for replies'], ['0:42','done (ok)'],
];

// F9: no chat:/thread: ref exists, so the destination must be derived from
// source_url and shown to the user BEFORE they approve.
function cwDestination(t) {
  const u = decodeURIComponent(t.source_url || '');
  const m = u.match(/\/l\/message\/(19:[^/]+)\//);
  const names = (t.key_people || []).map(p => p.name);
  if (m) {
    const conv = m[1];
    if (conv.endsWith('@unq.gbl.spaces'))
      return { kind: '1:1', label: `1:1 Teams chat with <b>${esc(names[0] || 'this person')}</b>`,
               conv, risky: false, note: 'Linear conversation, so the reply lands in the same thread.' };
    return { kind: 'group', label: `<b>Group chat</b> · ${names.length || '?'} participants`,
             conv, risky: true,
             note: 'Cowork has no conversation ref. Unverified that a reply lands here rather than a 1:1 DM.' };
  }
  if (/\/l\/meeting\//.test(u)) return { kind: 'meeting', label: 'Meeting invite', conv: '', risky: false, note: '' };
  if (t.source_type === 'email') return { kind: 'email', label: 'Email thread', conv: '', risky: false, note: '' };
  return { kind: 'none', label: 'No linked source', conv: '', risky: true,
           note: 'Nothing to reply to. Cowork would have to start a new conversation.' };
}

function cwDestBlock(t) {
  const d = cwDestination(t);
  return `<div class="cw-dest${d.risky ? ' is-risky' : ''}">
    <span>${d.risky ? '⚠' : '↳'}</span>
    <span>Goes to: ${d.label}${d.note ? `<br><span style="color:var(--text-muted)">${d.note}</span>` : ''}
    ${d.conv ? `<br><span class="d-conv">${esc(d.conv)}</span>` : ''}</span></div>`;
}

function cwIntentBlock(t, editable) {
  const intent = t.coaching_text || '';
  if (!intent) return '';
  if (t.cw_intent_editing) {
    return `<div class="cw-intent"><textarea class="cw-intent-box" rows="3"
      id="cw-intent-${t.id}">${esc(intent)}</textarea>
      <div style="padding-top:6px">
        <span class="i-edit" onclick="cwSaveIntent(${t.id})">Save and re-run</span>
        <span class="i-edit" style="color:var(--text-muted)"
          onclick="cwEditIntent(${t.id},false)">Cancel</span></div></div>`;
  }
  return `<div class="cw-intent"><span class="i-label">Asked Cowork to:</span>
    ${esc(intent)}${editable
      ? `<span class="i-edit" onclick="cwEditIntent(${t.id},true)">Change</span>` : ''}</div>`;
}

function cwCard(cls, badge, t, body, foot) {
  const label = CW_LABELS[t.action_type] || 'Cowork';
  return `<div class="cw-card ${cls}">
    <div class="cw-head"><span>✦</span><span class="cw-type">${label}</span>
      ${badge ? `<span class="cw-badge">${badge}</span>` : ''}</div>
    <div class="cw-body">${body}</div>
    ${foot ? `<div class="cw-foot">${foot}</div>` : ''}</div>`;
}

function renderCoworkCard(t) {
  if (!t.action_type || t.status === 'suggested') return '';
  const s = t.cw_state || 'idle';
  const verb = CW_VERB[t.action_type] || 'do this';

  if (s === 'previewing') return cwCard('is-running', 'read-only', t,
    `${cwIntentBlock(t, false)}
     <div class="cw-progress"><div class="cw-spinner"></div><div style="flex:1">
       <div id="cw-hb">Connecting to container</div>
       <div class="cw-progress-sub" id="cw-hb-t">0:01 elapsed</div></div></div>`,
    `<button class="cw-btn cw-btn-ghost" onclick="cwSet(${t.id},'idle')">Cancel</button>
     <span class="cw-foot-note">--deny-tools · no writes</span>`);

  if (s === 'ready') return cwCard('', 'preview', t,
    `${cwIntentBlock(t, true)}
     ${t.cw_finding ? `<div class="cw-finding"><span class="f-label">What Cowork found</span>${t.cw_finding}</div>` : ''}
     <div class="cw-draft">${esc(t.cw_draft || '')}</div>
     ${cwDestBlock(t)}`,
    `<button class="cw-btn cw-btn-go" onclick="cwSet(${t.id},'executing')">Approve &amp; ${verb}</button>
     <button class="cw-btn cw-btn-sec" onclick="cwSet(${t.id},'editing')">Edit</button>
     <button class="cw-btn cw-btn-sec" onclick="cwSet(${t.id},'previewing')">↻ Redo</button>
     <button class="cw-btn cw-btn-ghost" onclick="cwSet(${t.id},'idle')">Discard</button>`);

  if (s === 'editing') return cwCard('', 'editing', t,
    `<div class="cw-draft" contenteditable="true" id="cw-edit-${t.id}">${esc(t.cw_draft || '')}</div>
     ${cwDestBlock(t)}
     <div style="font-size:11px;color:var(--text-muted);padding-top:6px">
       Your text is pinned verbatim into the execute prompt, so Cowork will not re-tone it.</div>`,
    `<button class="cw-btn cw-btn-go" onclick="cwSaveEdit(${t.id})">Approve &amp; ${verb}</button>
     <button class="cw-btn cw-btn-ghost" onclick="cwSet(${t.id},'ready')">Cancel edit</button>`);

  if (s === 'executing') return cwCard('is-running', 'sending', t,
    `<div class="cw-draft" style="opacity:.55">${esc(t.cw_draft || '')}</div>
     <div class="cw-progress"><div class="cw-spinner"></div>
       <div style="flex:1">Executing…<div class="cw-progress-sub">resuming cw-b83c3290</div></div></div>`, '');

  if (s === 'executed') return cwCard('is-executed', 'done', t,
    `<div class="cw-draft">${esc(t.cw_draft || '')}</div>
     <div class="cw-receipt"><div class="cw-receipt-head">Receipt · from tool_trace</div>
       <div class="cw-receipt-line"><span class="r-ok">✓</span>
         <span class="r-tool">${esc(t.cw_sent_tool || 'send_tool')}</span>
         <span class="r-dur">1.4s</span></div>
       <div class="cw-receipt-line">Completed ${t.cw_sent_at || 'just now'}</div></div>`,
    `<button class="cw-btn cw-btn-ghost" onclick="toggleComplete(${t.id})">Mark done</button>
     <span class="cw-foot-note">idempotent: cannot re-run</span>`);

  if (s === 'failed') return cwCard('is-failed', 'failed', t,
    `<div style="padding:8px 0 2px"><b>Cowork could not complete this.</b>
     <div style="font-size:11px;color:var(--text-secondary);margin-top:5px">
       terminal_status: <code>auth_expired</code>. Nothing was sent.</div></div>`,
    `<button class="cw-btn cw-btn-primary" onclick="cwSet(${t.id},'previewing')">Retry</button>
     <button class="cw-btn cw-btn-ghost" onclick="cwSet(${t.id},'idle')">Dismiss</button>`);

  // idle
  return cwCard('', 'not run', t,
    `<div style="padding:10px 0 2px;color:var(--text-secondary)">
       Cowork can check the latest state of this in M365, then draft the action.
       <div style="font-size:11px;color:var(--text-muted);margin-top:4px">Nothing is sent without your approval.</div>
     </div>`,
    `<button class="cw-btn cw-btn-primary" onclick="cwSet(${t.id},'previewing')">Preview with Cowork</button>
     <span class="cw-foot-note">~45s · read-only</span>`);
}

let _cwTimer = null;
function cwEditIntent(id, on) {
  const t = tasks.find(x => x.id === id); if (!t) return;
  t.cw_intent_editing = on;
  selectTask(id);
}

function cwSaveIntent(id) {
  const t = tasks.find(x => x.id === id); if (!t) return;
  const box = document.getElementById('cw-intent-' + id);
  if (box) t.coaching_text = box.value.trim();
  t.cw_intent_editing = false;
  t.cw_draft = ''; t.cw_finding = '';
  cwSet(id, 'previewing');
  toast('Intent updated — re-running preview');
}

function cwSet(id, state) {
  const t = tasks.find(x => x.id === id); if (!t) return;
  t.cw_state = state;
  clearInterval(_cwTimer); _cwTimer = null;
  selectTask(id);
  if (state === 'previewing') cwRunHeartbeat(id);
  if (state === 'executing') setTimeout(() => {
    const tt = tasks.find(x => x.id === id);
    if (tt && tt.cw_state === 'executing') {
      tt.cw_state = 'executed';
      tt.cw_sent_at = new Date().toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'});
      if (selectedId === id) selectTask(id); else refresh();
      toast('Cowork completed the action');
    }
  }, 2000);
}

function cwRunHeartbeat(id) {
  let i = 0;
  _cwTimer = setInterval(() => {
    const t = tasks.find(x => x.id === id);
    if (!t || t.cw_state !== 'previewing') { clearInterval(_cwTimer); return; }
    if (i >= CW_HEARTBEAT.length) {
      clearInterval(_cwTimer);
      t.cw_state = 'ready';
      if (!t.cw_draft) { t.cw_draft = CW_FALLBACK_DRAFT; t.cw_finding = CW_FALLBACK_FINDING; }
      if (selectedId === id) selectTask(id); else refresh();
      toast('Cowork preview ready');
      return;
    }
    const [tm, msg] = CW_HEARTBEAT[i++];
    const a = document.getElementById('cw-hb'), b = document.getElementById('cw-hb-t');
    if (a) a.textContent = msg;
    if (b) b.textContent = tm + ' elapsed';
  }, 420);
}

function cwSaveEdit(id) {
  const el = document.getElementById('cw-edit-' + id);
  const t = tasks.find(x => x.id === id);
  if (t && el) t.cw_draft = el.innerText.trim();
  cwSet(id, 'executing');
}

const CW_FALLBACK_DRAFT = "(Cowork would draft the message here, grounded in the live M365 artifact.)";
const CW_FALLBACK_FINDING = "Checked Teams and email for recent activity on this task.";

// mock-only: jump the selected task through the states
function cwMockBar() {
  const el = document.createElement('div');
  el.className = 'cw-mockbar';
  el.innerHTML = '<span class="mb-l">mock:</span>' +
    ['idle','previewing','ready','editing','executing','executed','failed']
      .map(s => `<button onclick="selectedId?cwSet(selectedId,'${s}'):toast('Select a task first')">${s}</button>`).join('');
  document.body.appendChild(el);
}
document.addEventListener('DOMContentLoaded', cwMockBar);

// ── State ──"""
sub("\n// ── State ──", JS, "js")

# ── 5. Row badge reflects Cowork state ────────────────────────
sub(
    """  if (t.ai_output && !['completed','suggested','snoozed'].includes(t.status)) {
    const hint = (t.action_type && ACTION_HINTS[t.action_type]) || '⚡ Ready';
    parts.push(`<span class="task-hint ai-ready">${hint}</span>`);
  }""",
    """  if (t.cw_state === 'executed') {
    parts.push('<span class="task-hint cw-sent">✓ Done via Cowork</span>');
  } else if (t.cw_state === 'ready') {
    parts.push('<span class="task-hint ai-ready">✦ Awaiting your approval</span>');
  } else if (t.cw_state === 'previewing' || t.cw_state === 'executing') {
    parts.push('<span class="task-hint ai-ready">✦ Cowork working…</span>');
  } else if (t.ai_output && !['completed','suggested','snoozed'].includes(t.status)) {
    const hint = (t.action_type && ACTION_HINTS[t.action_type]) || '⚡ Ready';
    parts.push(`<span class="task-hint ai-ready">${hint}</span>`);
  }""",
    "row badge",
)

# ── 6. Seed real spike task + Cowork state on existing tasks ──
BRANDON = r"""const TASKS = [
  {
    id: 2076, title: "Follow up with Brandon Knoertzer on PPCC executive-target list",
    status: "active", priority: "P3",
    description: "After Brandon shared thoughts on using executive registrations to shape the PPCC panel, I asked whether the team could get the FY26 1:1 meeting targets as a concrete starting point, and offered to check with Stephanie if he preferred. No reply is visible after my question.",
    source_type: "teams", action_type: "awaiting-response",
    source_url: "https://teams.microsoft.com/l/message/19%3a007b4f8b-2585-442b-91d9-581972e27761_08b7be88-37ac-4e2b-82af-f8bb67e5f2f7%40unq.gbl.spaces/1785358519108?context=%7B%22contextType%22%3A%22chat%22%7D",
    key_people: [{name: "Brandon Knoertzer", email: "bknoer@microsoft.com"}],
    notes: "",
    // coaching_text verbatim from the live DB — generic for auto-detected
    // awaiting-response tasks, which is exactly the F10 point
    coaching_text: "Check if there's been any reply since. If not, a brief nudge with a specific ask works best.",
    // verbatim from the Phase 0 spike
    cw_state: "ready",
    cw_finding: "<b>Brandon has not responded.</b> Your message went out <b>Wed, Jul 29 at 5:32 PM ET</b> and is still the most recent in the thread, with nothing back in Teams or email over 14 days.<br><br>His last message before your question (Jul 29, 4:55 PM) was the Stephanie update: registrations happen closer to the event so planning could be tricky; her suggestion was to find great customer stories and offer free tickets to any who aren't registered. He also asked you a question back: <b>any top-of-mind customers for the event?</b>",
    cw_draft: "Hey Brandon \u2014 circling back on this one. Any chance we can pull the FY26 1:1 meeting target list as a starting point for the PPCC exec panel? I'd rather hand the team something defined than a blank page \u2014 happy to ping Stephanie directly if that's easier on your end.",
    cw_sent_tool: "mcp__m365_teams__SendChatMessage",
    ai_enriched: true, is_quick_hit: false, created_at: "2026-07-29T21:32:00Z"
  },
  {
    id: 2036, title: "Assess customer candidates for the Microsoft Agent Lineage private preview",
    status: "active", priority: "P1",
    description: "Aamer Kaleem asked you and John Glynn to evaluate Eu Nice Loh's request for the Agent Lineage private preview and identify good customer candidates.",
    source_type: "teams", action_type: "follow-up",
    source_url: "https://teams.microsoft.com/l/message/19%3a629b1369561746ea99febfc3aac5f893%40thread.v2/1785271331514?context=%7B%22contextType%22%3A%22chat%22%7D",
    key_people: [{name: "Aamer Kaleem", email: "aamer.kaleem@microsoft.com"}, {name: "John Glynn", email: "jglynn@microsoft.com"}, {name: "Eu Nice Loh", email: "eunice.loh@microsoft.com"}],
    notes: "",
    coaching_text: "Reply in the group chat confirming you'll build the shortlist, and ask Eu Nice to scope what 'good candidate' means before you spend time on it.",
    cw_state: "ready",
    cw_finding: "No new messages in this group chat since Aamer's ask on Jul 27. John Glynn has not replied either.",
    cw_draft: "Thanks Aamer \u2014 I'll pull together a shortlist. Starting with accounts that have agent usage plus SharePoint/Teams governance needs. Eu Nice, are you looking for production-scale tenants or is a pilot-size tenant fine?",
    cw_sent_tool: "mcp__m365_teams__SendChatMessage",
    ai_enriched: true, is_quick_hit: false, created_at: "2026-07-27T10:00:00Z"
  },"""
sub("const TASKS = [", BRANDON, "seed tasks")

# give a couple of the stock demo tasks a Cowork state so the list reads coherently
sub("""    ai_enriched: true, is_quick_hit: false, created_at: "2026-04-09T10:00:00Z"
  },""",
    """    ai_enriched: true, is_quick_hit: false, created_at: "2026-04-09T10:00:00Z",
    cw_state: "executed", cw_sent_at: "9:12 AM",
    cw_draft: "Hi Sarah,\\n\\nThanks for sending the Q3 budget draft. A few adjustments: cloud infrastructure (line 12) can come down ~8% given the serverless migration; contractor budget (line 18) needs +15% to cover the auth refactor through July; training (line 24) looks right as-is.\\n\\nHappy to discuss Friday.",
    cw_sent_tool: "mcp__m365_mail__SendMail"
  },""",
    "task1 executed")

sub("""    ai_enriched: true, is_quick_hit: false, created_at: "2026-04-07T11:00:00Z"
  },""",
    """    ai_enriched: true, is_quick_hit: false, created_at: "2026-04-07T11:00:00Z",
    cw_state: "idle"
  },""",
    "task4 idle")

DST.write_text(html, encoding="utf-8")
print(f"wrote {DST} ({len(html):,} bytes)")

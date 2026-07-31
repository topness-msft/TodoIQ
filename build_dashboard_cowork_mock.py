"""Derive static/mock-dashboard-cowork.html from the REAL shipped dashboard.

The real dashboard is Tornado-rendered (src/templates/*.html) with a live REST +
WebSocket backend. This script inlines the template, CSS and JS into one standalone
file, stubs the network layer, seeds fixture tasks, and patches the detail pane so the
ACTIONS row + SKILL OUTPUT + COWORK PROMPT cards are replaced by a single Cowork action
card (preview -> approve -> execute).

Anchors come from the codebase-expert report:
  findings/codebase-expert-dashboard-render.md
Every sub() fails loudly if its anchor is missing or ambiguous, so this stays honest
if the real dashboard changes.

Run:  python build_dashboard_cowork_mock.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).parent
BASE = ROOT / "src" / "templates" / "base.html"
PAGE = ROOT / "src" / "templates" / "dashboard.html"
CSS = ROOT / "static" / "css" / "style.css"
JS = ROOT / "static" / "js" / "dashboard.js"
OUT = ROOT / "static" / "mock-dashboard-cowork.html"

js = JS.read_text(encoding="utf-8")


def sub(anchor: str, new: str, label: str) -> None:
    """Replace exactly one occurrence of `anchor` in the JS, or die."""
    global js
    n = js.count(anchor)
    if n != 1:
        sys.exit(f"anchor {label!r} matched {n} times (need exactly 1):\n{anchor[:200]}")
    js = js.replace(anchor, new)


# ── 1. ACTIONS row -> Cowork card ────────────────────────────────────────────
sub(
    "    html += renderSkillButtons(task);",
    "    html += renderCoworkCard(task);",
    "actions call site",
)

# ── 2. Drop SKILL OUTPUT + COWORK PROMPT cards (superseded by the card) ──────
# Zone runs from the Skill Output comment to the AI Coaching comment.
start = "    // Skill Output — prefer context entries over the summary field"
end = "    // AI Coaching\n    if (task.coaching_text) {"
i, j = js.find(start), js.find(end)
if i == -1 or j == -1 or j < i:
    sys.exit("could not locate the skill-output/cowork-prompt zone")
js = js[:i] + "    // [mock] Skill Output and Cowork Prompt cards are superseded\n" \
              "    // by the Cowork action card rendered above.\n\n" + js[j:]

# ── 3. Drop the AI Coaching card (both branches) ─────────────────────────────
# coaching_text is now surfaced as the editable intent line inside the Cowork
# card, so rendering it a second time below would just duplicate it. The empty
# state must go too, or every task shows a stray "No coaching yet" card.
sub(
    "    // AI Coaching\n    if (task.coaching_text) {",
    "    // AI Coaching — surfaced as the intent line inside the Cowork card.\n"
    "    if (false && task.coaching_text) {",
    "coaching card",
)
sub(
    "    } else if (task.parse_status === 'parsed') {\n"
    "        // No coaching yet — suggest refreshing",
    "    } else if (false) {\n"
    "        // No coaching yet — suggest refreshing",
    "coaching empty state",
)

# ── 4. Cowork card renderer + state machine, appended ────────────────────────
COWORK_JS = r"""

// ═══════════════════════════════════════════════════════════════════════════
// COWORK ACTION CARD  (mock)
// Replaces the Actions button row + Skill Output panel with a single
// preview-then-approve card. States: idle, previewing, ready, editing,
// executing, executed, failed.
// ═══════════════════════════════════════════════════════════════════════════

var CW_LABELS = {
    'respond-email':     'Reply',
    'schedule-meeting':  'Scheduling',
    'follow-up':         'Follow-up',
    'awaiting-response': 'Follow-up',
    'prepare':           'Prep',
    'review-document':   'Review',
    'general':           'Action'
};

var CW_VERB = {
    'respond-email':     'send this reply',
    'schedule-meeting':  'book this meeting',
    'follow-up':         'post this message',
    'awaiting-response': 'post this message',
    'prepare':           'save these notes',
    'review-document':   'save this summary',
    'general':           'do this'
};

// stderr heartbeat shape observed in the Phase 0 spike
var CW_HEARTBEAT = [
    ['0:03', 'Connecting to container'],
    ['0:09', 'Searching Teams for recent messages'],
    ['0:18', 'Reading conversation history'],
    ['0:27', 'Checking email for a reply on the same topic'],
    ['0:36', 'Drafting'],
    ['0:42', 'Finalising']
];

function cwEsc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── F9: derive the destination from source_url ──────────────────────────────
// Teams conversation ids encode their kind:
//   ...@unq.gbl.spaces  -> 1:1 chat   (linear, a person-addressed send lands right)
//   ...@thread.v2       -> group chat (person-addressed send would DM one person)
function cwDestination(task) {
    var people = parsePeople(task.key_people) || [];
    var names = people.map(function(p) { return p.name; });
    var url = task.source_url || '';
    var m = /19[:%]3?a?([^/]*?)(@unq\.gbl\.spaces|@thread\.v2|%40unq\.gbl\.spaces|%40thread\.v2)/i.exec(
        decodeURIComponent(url));
    var conv = m ? decodeURIComponent(m[0]) : '';

    if (/thread\.v2/i.test(conv)) {
        return {
            risky: true,
            label: 'Group chat &middot; ' + names.length + ' participants',
            note: 'Cowork has no conversation ref. Unverified that a reply lands here '
                + 'rather than as a 1:1 DM.',
            conv: conv
        };
    }
    if (/unq\.gbl\.spaces/i.test(conv)) {
        return {
            risky: false,
            label: '1:1 Teams chat with <b>' + cwEsc(names[0] || 'them') + '</b>',
            note: 'Linear conversation, so the reply lands in the same thread.',
            conv: conv
        };
    }
    if (task.source_type === 'email') {
        return {
            risky: false,
            label: 'Email reply to <b>' + cwEsc(names[0] || 'them') + '</b>',
            note: 'Threaded on the original message.',
            conv: ''
        };
    }
    return {
        risky: true,
        label: 'No linked source',
        note: 'Nothing to reply to. Cowork would have to start a new conversation.',
        conv: ''
    };
}

function cwDestBlock(task) {
    var d = cwDestination(task);
    return '<div class="cw-dest' + (d.risky ? ' is-risky' : '') + '">'
        + '<span class="d-icon">' + (d.risky ? '&#9888;' : '&#8627;') + '</span>'
        + '<span><b>Goes to:</b> ' + d.label
        + '<span class="d-note">' + cwEsc(d.note) + '</span>'
        + (d.conv ? '<span class="d-conv">' + cwEsc(d.conv) + '</span>' : '')
        + '</span></div>';
}

// ── The intent line: WorkIQ's suggested next action, seeded at detection ────
function cwIntentBlock(task, editable) {
    var intent = task.coaching_text || '';
    if (!intent) return '';
    if (task._cwIntentEditing) {
        return '<div class="cw-intent">'
            + '<textarea class="cw-intent-box" rows="3" id="cw-intent-' + task.id + '">'
            + cwEsc(intent) + '</textarea>'
            + '<div class="cw-intent-actions">'
            + '<span class="i-edit" onclick="cwSaveIntent(' + task.id + ')">Save and re-run</span>'
            + '<span class="i-edit i-muted" onclick="cwEditIntent(' + task.id + ',false)">Cancel</span>'
            + '</div></div>';
    }
    return '<div class="cw-intent">'
        + '<span class="i-label">Asked Cowork to:</span> ' + cwEsc(intent)
        + (editable ? '<span class="i-edit" onclick="cwEditIntent(' + task.id + ',true)">Change</span>' : '')
        + '</div>';
}

function cwShell(cls, badge, task, body, foot) {
    var label = CW_LABELS[task.action_type] || 'Action';
    return '<div class="cw-card ' + cls + '">'
        + '<div class="cw-head">'
        + '<span class="cw-spark">&#10022;</span>'
        + '<span class="cw-type">' + label + ' &middot; Cowork</span>'
        + (badge ? '<span class="cw-badge">' + badge + '</span>' : '')
        + '</div>'
        + '<div class="cw-body">' + body + '</div>'
        + (foot ? '<div class="cw-foot">' + foot + '</div>' : '')
        + '</div>';
}

function renderCoworkCard(task) {
    var s = task._cwState || 'idle';
    var verb = CW_VERB[task.action_type] || 'do this';
    var draft = task._cwDraft || '';

    if (s === 'previewing') {
        return cwShell('is-running', 'read-only', task,
            cwIntentBlock(task, false)
            + '<div class="cw-progress"><span class="cw-spinner"></span>'
            + '<span class="cw-progress-text">'
            + '<span id="cw-hb">Connecting to container</span>'
            + '<span class="cw-progress-sub" id="cw-hb-t">0:01 elapsed</span>'
            + '</span></div>',
            '<button class="cw-btn cw-btn-ghost" onclick="cwSet(' + task.id + ',\'idle\')">Cancel</button>'
            + '<span class="cw-foot-note">--deny-tools &middot; no writes</span>');
    }

    if (s === 'ready' || s === 'editing') {
        var editing = s === 'editing';
        var draftHtml = editing
            ? '<div class="cw-draft is-editing" contenteditable="true" id="cw-draft-' + task.id + '">'
              + cwEsc(draft) + '</div>'
            : '<div class="cw-draft">' + cwEsc(draft) + '</div>';
        var findingHtml = task._cwFinding
            ? '<div class="cw-finding"><div class="cw-finding-label">What Cowork found</div>'
              + task._cwFinding + '</div>'
            : '';
        var foot = editing
            ? '<button class="cw-btn cw-btn-go" onclick="cwSaveDraft(' + task.id + ')">Approve &amp; ' + verb + '</button>'
              + '<button class="cw-btn cw-btn-ghost" onclick="cwSet(' + task.id + ',\'ready\')">Cancel edit</button>'
            : '<button class="cw-btn cw-btn-go" onclick="cwSet(' + task.id + ',\'executing\')">Approve &amp; ' + verb + '</button>'
              + '<button class="cw-btn cw-btn-sec" onclick="cwSet(' + task.id + ',\'editing\')">Edit</button>'
              + '<button class="cw-btn cw-btn-sec" onclick="cwSet(' + task.id + ',\'previewing\')">&#8635; Redo</button>'
              + '<button class="cw-btn cw-btn-ghost" onclick="cwSet(' + task.id + ',\'idle\')">Discard</button>';
        return cwShell('', editing ? 'editing' : 'preview', task,
            cwIntentBlock(task, !editing) + findingHtml + draftHtml + cwDestBlock(task), foot);
    }

    if (s === 'executing') {
        return cwShell('is-running', 'sending', task,
            '<div class="cw-draft is-dim">' + cwEsc(draft) + '</div>'
            + '<div class="cw-progress"><span class="cw-spinner"></span>'
            + '<span class="cw-progress-text">Executing&hellip;'
            + '<span class="cw-progress-sub">resuming ' + cwEsc(task._cwConv || 'cw-b83c3290') + '</span>'
            + '</span></div>', '');
    }

    if (s === 'executed') {
        return cwShell('is-executed', 'done', task,
            '<div class="cw-draft">' + cwEsc(draft) + '</div>'
            + '<div class="cw-receipt">'
            + '<div class="cw-receipt-head">Receipt &middot; from tool_trace</div>'
            + '<div class="cw-receipt-line"><span class="r-ok">&#10003;</span>'
            + '<span class="r-tool">' + cwEsc(task._cwTool || 'send_tool') + '</span>'
            + '<span class="r-dur">1.4s</span></div>'
            + '<div class="cw-receipt-line r-muted">Completed ' + cwEsc(task._cwSentAt || 'just now') + '</div>'
            + '</div>',
            '<span class="cw-foot-note">idempotent &middot; cannot re-run</span>');
    }

    if (s === 'failed') {
        return cwShell('is-failed', 'failed', task,
            '<div class="cw-fail"><b>Cowork could not complete this.</b>'
            + '<div class="cw-fail-sub">terminal_status: <code>auth_expired</code>. Nothing was sent.</div>'
            + '</div>',
            '<button class="cw-btn cw-btn-go" onclick="cwSet(' + task.id + ',\'previewing\')">Retry</button>'
            + '<button class="cw-btn cw-btn-ghost" onclick="cwSet(' + task.id + ',\'idle\')">Dismiss</button>');
    }

    // idle
    return cwShell('', 'not run', task,
        cwIntentBlock(task, true)
        + '<div class="cw-idle">Cowork can check the latest state of this in M365, then draft the action.'
        + '<span class="cw-idle-sub">Nothing is sent without your approval.</span></div>',
        '<button class="cw-btn cw-btn-go" onclick="cwSet(' + task.id + ',\'previewing\')">Preview with Cowork</button>'
        + '<span class="cw-foot-note">~45s &middot; read-only</span>');
}

// ── State machine ───────────────────────────────────────────────────────────
var _cwTimer = null;

function cwTask(id) {
    return tasks.find(function(t) { return t.id === id; });
}

function cwRerender(id) {
    var t = cwTask(id);
    if (t && selectedTaskId === id) renderDetailPane(t);
    renderTaskList();
}

function cwSet(id, state) {
    var t = cwTask(id);
    if (!t) return;
    t._cwState = state;
    if (_cwTimer) { clearInterval(_cwTimer); _cwTimer = null; }
    cwRerender(id);

    if (state === 'previewing') cwHeartbeat(id);

    if (state === 'executing') {
        setTimeout(function() {
            var tt = cwTask(id);
            if (!tt || tt._cwState !== 'executing') return;
            tt._cwState = 'executed';
            tt._cwSentAt = new Date().toLocaleTimeString('en-US',
                { hour: 'numeric', minute: '2-digit' });
            cwRerender(id);
        }, 2200);
    }
}

function cwHeartbeat(id) {
    var i = 0;
    _cwTimer = setInterval(function() {
        var t = cwTask(id);
        if (!t || t._cwState !== 'previewing') { clearInterval(_cwTimer); _cwTimer = null; return; }
        if (i >= CW_HEARTBEAT.length) {
            clearInterval(_cwTimer); _cwTimer = null;
            t._cwState = 'ready';
            if (!t._cwDraft) {
                t._cwDraft = CW_FALLBACK_DRAFT;
                t._cwFinding = CW_FALLBACK_FINDING;
            }
            cwRerender(id);
            return;
        }
        var step = CW_HEARTBEAT[i++];
        var a = document.getElementById('cw-hb');
        var b = document.getElementById('cw-hb-t');
        if (a) a.textContent = step[1];
        if (b) b.textContent = step[0] + ' elapsed';
    }, 700);
}

function cwEditIntent(id, on) {
    var t = cwTask(id);
    if (!t) return;
    t._cwIntentEditing = on;
    cwRerender(id);
}

function cwSaveIntent(id) {
    var t = cwTask(id);
    if (!t) return;
    var box = document.getElementById('cw-intent-' + id);
    if (box) t.coaching_text = box.value.trim();
    t._cwIntentEditing = false;
    // the intent drives the draft, so a changed intent invalidates it
    t._cwDraft = '';
    t._cwFinding = '';
    cwSet(id, 'previewing');
}

function cwSaveDraft(id) {
    var t = cwTask(id);
    if (!t) return;
    var box = document.getElementById('cw-draft-' + id);
    if (box) t._cwDraft = box.innerText.trim();
    cwSet(id, 'executing');
}
"""

# ── 5. Mock harness: stub the network layer, seed fixtures ───────────────────
HARNESS = r"""

// ═══════════════════════════════════════════════════════════════════════════
// MOCK HARNESS — stubs the REST + WebSocket backend so this file runs
// standalone. Nothing below exists in the real dashboard.
// ═══════════════════════════════════════════════════════════════════════════

var CW_FALLBACK_FINDING = 'Checked Teams and email for activity on this task since it '
    + 'was created. No new replies.';
var CW_FALLBACK_DRAFT = '(Cowork would draft the message here, grounded in the live '
    + 'M365 artifact.)';

var MOCK_TASKS = [
    {
        id: 2076,
        title: 'Follow up with Brandon Knoertzer on PPCC executive-target list',
        description: 'After Brandon shared thoughts on using executive registrations to '
            + 'shape the PPCC panel, I asked whether the team could get the FY26 1:1 '
            + 'meeting targets as a concrete starting point, and offered to check with '
            + 'Stephanie if he preferred. No reply is visible after my question.',
        status: 'active', priority: 3, parse_status: 'parsed',
        source_type: 'chat', action_type: 'awaiting-response',
        source_url: 'https://teams.microsoft.com/l/message/19%3a007b4f8b-2585-442b-91d9-581972e27761_08b7be88-37ac-4e2b-82af-f8bb67e5f2f7%40unq.gbl.spaces/1785358519108',
        key_people: '[{"name":"Brandon Knoertzer","email":"bknoer@microsoft.com","role":"PPCC"}]',
        // the NEW intent-shaped coaching_text (post spec rewrite)
        coaching_text: 'Ask Brandon Knoertzer whether the team can pull the FY26 1:1 '
            + 'meeting targets as a concrete starting point for the PPCC exec panel, and '
            + 'offer to check with Stephanie instead if he would prefer.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, created_at: '2026-07-29T21:32:00Z', updated_at: '2026-07-29T21:32:00Z',
        // verbatim from the Phase 0 spike
        _cwState: 'ready',
        _cwFinding: '<b>Brandon has not responded.</b> Your message went out '
            + '<b>Wed, Jul 29 at 5:32 PM ET</b> and is still the most recent in the '
            + 'thread, with nothing back in Teams or email over 14 days.<br><br>'
            + 'His last message before your question (Jul 29, 4:55 PM) was the Stephanie '
            + 'update: registrations happen closer to the event so planning could be '
            + 'tricky; her suggestion was to find great customer stories and offer free '
            + 'tickets to any who are not registered. He also asked you a question back: '
            + '<b>any top-of-mind customers for the event?</b>',
        _cwDraft: 'Hey Brandon - circling back on this one. Any chance we can pull the '
            + 'FY26 1:1 meeting target list as a starting point for the PPCC exec panel? '
            + 'I would rather hand the team something defined than a blank page - happy '
            + 'to ping Stephanie directly if that is easier on your end.',
        _cwTool: 'send_tool', _cwConv: 'cw-b83c3290'
    },
    {
        id: 2036,
        title: 'Assess customer candidates for the Microsoft Agent Lineage private preview',
        description: 'Aamer Kaleem asked you and John Glynn to evaluate Eu Nice Loh\'s '
            + 'request for the Agent Lineage private preview and identify good customer '
            + 'candidates.',
        status: 'active', priority: 1, parse_status: 'parsed',
        source_type: 'chat', action_type: 'follow-up',
        source_url: 'https://teams.microsoft.com/l/message/19%3a629b1369561746ea99febfc3aac5f893%40thread.v2/1785271331514',
        key_people: '[{"name":"Aamer Kaleem","email":"aamer.kaleem@microsoft.com"},'
            + '{"name":"John Glynn","email":"jglynn@microsoft.com"},'
            + '{"name":"Eu Nice Loh","email":"eunice.loh@microsoft.com"}]',
        coaching_text: 'Reply in the group chat confirming you will build the candidate '
            + 'shortlist, and ask Eu Nice Loh to define what qualifies as a good Agent '
            + 'Lineage candidate before you invest time in it.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, created_at: '2026-07-27T10:00:00Z', updated_at: '2026-07-27T10:00:00Z',
        _cwState: 'ready',
        _cwFinding: 'No new messages in this group chat since Aamer\'s ask on Jul 27. '
            + 'John Glynn has not replied either.',
        _cwDraft: 'Thanks Aamer - I will pull together a shortlist. Starting with '
            + 'accounts that have agent usage plus SharePoint/Teams governance needs. '
            + 'Eu Nice, are you looking for production-scale tenants or is a pilot-size '
            + 'tenant fine?',
        _cwTool: 'send_tool'
    },
    {
        id: 2094,
        title: 'Ask Mehdi about the Copilot Kit\'s FinOps coverage',
        description: 'Follow up on the April discussion about the Copilot Kit\'s '
            + 'end-to-end deployment capabilities and whether FinOps is in scope.',
        status: 'active', priority: 2, parse_status: 'parsed',
        source_type: 'manual', action_type: 'follow-up',
        source_url: null,
        key_people: '[{"name":"Mehdi Slaoui Andaloussi","email":"mehdisa@microsoft.com"}]',
        coaching_text: 'Send Mehdi Slaoui Andaloussi a brief Teams message asking whether '
            + 'FinOps is an existing or planned focus for the Copilot Kit. Reference your '
            + 'April discussion about the kit\'s end-to-end deployment capabilities, and '
            + 'ask whether there is a relevant owner, roadmap item, or example to review.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, created_at: '2026-07-24T09:00:00Z', updated_at: '2026-07-24T09:00:00Z',
        _cwState: 'idle'
    },
    {
        id: 2098,
        title: 'Follow up on Chitra J\'s notes',
        description: 'Review the notes and materials Chitra J shared during the PPCC 1:1 '
            + '- Roundtables.xlsx, the Viva Engage conversation, and the Agent '
            + 'transformation stories resource.',
        status: 'active', priority: 2, parse_status: 'parsed',
        source_type: 'manual', action_type: 'review-document',
        source_url: null,
        key_people: '[{"name":"Chitra J","email":"chitraj@microsoft.com"}]',
        coaching_text: 'Work through the three items Chitra J shared in the PPCC 1:1, '
            + 'then send her a short Teams note naming which of the three you are taking '
            + 'forward and by when.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, created_at: '2026-07-25T14:00:00Z', updated_at: '2026-07-25T14:00:00Z',
        _cwState: 'executed', _cwSentAt: '9:12 AM', _cwTool: 'send_tool',
        _cwDraft: 'Hi Chitra - thanks for the three links. Taking the Roundtables sheet '
            + 'forward this week and will come back on the Agent transformation stories '
            + 'by Friday. Parking the Viva Engage thread for now.'
    },
    {
        id: 2050,
        title: 'Schedule Scale team retro and celebration',
        description: 'Organise a dedicated retro + celebration session for the Scale team '
            + 'before the org change lands and calendars fragment.',
        status: 'active', priority: 1, parse_status: 'parsed',
        source_type: 'manual', action_type: 'schedule-meeting',
        source_url: null,
        key_people: '[{"name":"Steve Jeffery","email":"stevejef@microsoft.com"}]',
        coaching_text: 'Ask Steve Jeffery to confirm the Scale team roster and pick a '
            + '90-minute slot in the next two weeks, before people move into their new '
            + 'roles and the window closes.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, due_date: '2026-08-06', created_at: '2026-07-20T08:00:00Z',
        updated_at: '2026-07-20T08:00:00Z',
        _cwState: 'idle'
    },
    {
        id: 2104,
        title: 'Confirm and progress State Street lighthouse',
        description: 'Morne Pretorius asked whether you plan to support State Street as a '
            + 'CAPE lighthouse account.',
        status: 'suggested', priority: 3, parse_status: 'parsed',
        source_type: 'chat', action_type: 'follow-up',
        source_url: 'https://teams.microsoft.com/l/message/19%3aabc123def456%40unq.gbl.spaces/1785200000000',
        key_people: '[{"name":"Morne Pretorius","email":"mornep@microsoft.com"}]',
        coaching_text: 'Confirm the decision criteria with Morne Pretorius, then contact '
            + 'the CAPE stakeholders with a specific viability ask for State Street.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, created_at: '2026-07-30T11:00:00Z', updated_at: '2026-07-30T11:00:00Z',
        _cwState: 'idle'
    },
    {
        id: 2029,
        title: 'Chase Irina Parsina for the durability calculations',
        description: 'You asked Irina Parsina for the work-in-progress durability '
            + 'calculations. Narrow the ask so it is easy to act on.',
        status: 'waiting', priority: 2, parse_status: 'parsed',
        source_type: 'chat', action_type: 'awaiting-response',
        source_url: 'https://teams.microsoft.com/l/message/19%3affee9911aabb%40unq.gbl.spaces/1785100000000',
        key_people: '[{"name":"Irina Parsina","email":"iparsina@microsoft.com"}]',
        coaching_text: 'Ask Irina Parsina for three specific things rather than the '
            + 'calculations in general: the durability dimensions, how each is weighted, '
            + 'and the source data behind them.',
        user_notes: '', skill_output: null, cowork_prompt: null,
        is_quick_hit: 0, created_at: '2026-07-18T16:00:00Z', updated_at: '2026-07-18T16:00:00Z',
        _cwState: 'failed'
    }
];

// ── Network stubs ───────────────────────────────────────────────────────────
(function stubNetwork() {
    function json(body) {
        return Promise.resolve({
            ok: true, status: 200,
            json: function() { return Promise.resolve(body); },
            text: function() { return Promise.resolve(JSON.stringify(body)); }
        });
    }

    window.fetch = function(url, opts) {
        url = String(url);
        var m = /\/api\/tasks\/(\d+)/.exec(url);
        if (m) {
            var t = tasks.find(function(x) { return x.id === parseInt(m[1], 10); })
                 || MOCK_TASKS.find(function(x) { return x.id === parseInt(m[1], 10); });
            return json({ task: t, contexts: [] });
        }
        if (url.indexOf('/api/tasks') === 0) {
            return json({ tasks: MOCK_TASKS.filter(function(t) {
                return ['dismissed', 'completed', 'deleted'].indexOf(t.status) === -1;
            }) });
        }
        if (url.indexOf('/api/runner-status') === 0) return json({ running: [] });
        if (url.indexOf('/api/sync-status') === 0) return json({ last_sync: null, auto_sync: true });
        if (url.indexOf('/api/stats') === 0) return json({});
        return json({ ok: true });
    };

    // Never connect; the real class would retry forever against nothing.
    window.WebSocket = function() {
        this.readyState = 1;
        this.send = function() {};
        this.close = function() {};
        var self = this;
        setTimeout(function() { if (self.onopen) self.onopen(); }, 0);
    };
})();

// ── Mock state bar ──────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    var bar = document.createElement('div');
    bar.className = 'cw-mockbar';
    bar.innerHTML = '<span class="cw-mockbar-label">Cowork card:</span>';
    ['idle', 'previewing', 'ready', 'editing', 'executing', 'executed', 'failed']
        .forEach(function(s) {
            var b = document.createElement('button');
            b.textContent = s;
            b.onclick = function() {
                if (!selectedTaskId) return;
                cwSet(selectedTaskId, s);
            };
            bar.appendChild(b);
        });
    document.body.appendChild(bar);

    // open the spike task so the card is visible immediately
    setTimeout(function() { selectTask(2076); }, 120);
});
"""

js = js + COWORK_JS + HARNESS

# ── 6. Cowork CSS, using the real theme variables ────────────────────────────
COWORK_CSS = r"""
/* ═══ Cowork action card (mock) ═══════════════════════════════════════════ */
.cw-card {
    background: var(--bg-secondary);
    border: 1px solid var(--accent);
    border-radius: 8px;
    margin-bottom: 12px;
    overflow: hidden;
    box-shadow: var(--card-shadow);
    /* .detail-pane is a flex container, so cards must not be shrunk to fit */
    flex: 0 0 auto;
}
.cw-card.is-executed { border-color: var(--success); }
.cw-card.is-failed   { border-color: var(--danger); }

.cw-head {
    display: flex; align-items: center; gap: 7px;
    padding: 9px 14px;
    background: var(--accent-bg);
    border-bottom: 1px solid var(--border-primary);
    font-size: 12px;
}
.cw-card.is-executed .cw-head { background: var(--skill-output-bg); }
.cw-card.is-failed .cw-head   { background: var(--danger-bg, var(--accent-bg)); }
.cw-spark { color: var(--accent); }
.cw-card.is-executed .cw-spark { color: var(--success); }
.cw-type { font-weight: 600; color: var(--text-primary); }
.cw-badge {
    margin-left: auto;
    font-size: 10px; letter-spacing: .04em; text-transform: uppercase;
    padding: 2px 8px; border-radius: 10px;
    border: 1px solid var(--accent); color: var(--accent);
}
.cw-card.is-executed .cw-badge { border-color: var(--success); color: var(--success); }
.cw-card.is-failed .cw-badge   { border-color: var(--danger);  color: var(--danger); }

.cw-body { padding: 12px 14px; }

/* intent line — WorkIQ's suggested next action */
.cw-intent {
    font-size: 12px; line-height: 1.5;
    color: var(--text-secondary);
    padding-bottom: 10px; margin-bottom: 10px;
    border-bottom: 1px solid var(--border-primary);
}
.cw-intent .i-label { color: var(--text-secondary); opacity: .8; }
.cw-intent .i-edit {
    color: var(--accent); cursor: pointer; margin-left: 6px; white-space: nowrap;
}
.cw-intent .i-edit:hover { text-decoration: underline; }
.cw-intent .i-muted { color: var(--text-secondary); }
.cw-intent-box {
    width: 100%; box-sizing: border-box;
    font: inherit; font-size: 12px; line-height: 1.5;
    color: var(--text-primary); background: var(--bg-primary);
    border: 1px solid var(--accent); border-radius: 6px;
    padding: 8px 10px; resize: vertical;
}
.cw-intent-actions { padding-top: 6px; }

/* findings */
.cw-finding {
    background: var(--bg-primary);
    border: 1px solid var(--border-primary);
    border-radius: 6px;
    padding: 10px 12px; margin-bottom: 10px;
    font-size: 13px; line-height: 1.55; color: var(--text-primary);
}
.cw-finding-label {
    font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--text-secondary); margin-bottom: 6px;
}

/* draft */
.cw-draft {
    background: var(--bg-primary);
    border: 1px solid var(--border-primary);
    border-radius: 6px;
    padding: 10px 12px;
    font-size: 13px; line-height: 1.55; color: var(--text-primary);
    white-space: pre-wrap;
}
.cw-draft.is-editing { outline: 2px solid var(--accent); outline-offset: 1px; }
.cw-draft.is-dim { opacity: .55; }
.cw-card.is-executed .cw-draft { background: var(--skill-output-bg); border-color: var(--success); }

/* destination — the F9 wrong-audience guard */
.cw-dest {
    display: flex; gap: 8px; align-items: flex-start;
    margin-top: 10px; padding: 9px 11px;
    border: 1px solid var(--border-primary); border-radius: 6px;
    background: var(--bg-primary);
    font-size: 12px; color: var(--text-secondary);
}
.cw-dest.is-risky { border-color: var(--warning); background: var(--warning-bg, var(--accent-bg)); }
.cw-dest b { color: var(--text-primary); }
.cw-dest .d-icon { color: var(--text-secondary); }
.cw-dest.is-risky .d-icon { color: var(--warning); }
.cw-dest .d-note { display: block; margin-top: 3px; }
.cw-dest .d-conv {
    display: block; margin-top: 5px;
    font-family: ui-monospace, Consolas, monospace; font-size: 11px;
    color: var(--text-secondary); opacity: .75; word-break: break-all;
}

/* progress */
.cw-progress { display: flex; align-items: center; gap: 10px; padding: 4px 0; }
.cw-progress-text { display: flex; flex-direction: column; font-size: 13px; color: var(--text-primary); }
.cw-progress-sub { font-size: 11px; color: var(--text-secondary); margin-top: 2px; }
.cw-spinner {
    width: 14px; height: 14px; flex: none;
    border: 2px solid var(--border-primary); border-top-color: var(--accent);
    border-radius: 50%; animation: cw-spin .8s linear infinite;
}
@keyframes cw-spin { to { transform: rotate(360deg); } }

/* receipt */
.cw-receipt {
    margin-top: 10px; padding: 9px 11px;
    border: 1px solid var(--border-primary); border-radius: 6px;
    background: var(--bg-primary); font-size: 12px;
}
.cw-receipt-head {
    font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--text-secondary); margin-bottom: 6px;
}
.cw-receipt-line { display: flex; gap: 8px; align-items: center; color: var(--text-primary); }
.cw-receipt-line.r-muted { color: var(--text-secondary); margin-top: 3px; }
.r-ok { color: var(--success); }
.r-tool { font-family: ui-monospace, Consolas, monospace; font-size: 11px; }
.r-dur { margin-left: auto; color: var(--text-secondary); }

.cw-idle { font-size: 13px; color: var(--text-secondary); line-height: 1.5; }
.cw-idle-sub { display: block; font-size: 11px; margin-top: 4px; opacity: .8; }
.cw-fail { font-size: 13px; color: var(--text-primary); }
.cw-fail-sub { font-size: 12px; color: var(--text-secondary); margin-top: 5px; }
.cw-fail-sub code { font-family: ui-monospace, Consolas, monospace; }

/* footer */
.cw-foot {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    padding: 10px 14px;
    border-top: 1px solid var(--border-primary);
    background: var(--bg-primary);
}
.cw-btn {
    font: inherit; font-size: 12px; font-weight: 500;
    padding: 6px 12px; border-radius: 5px; cursor: pointer;
    border: 1px solid var(--border-primary); background: var(--bg-secondary);
    color: var(--text-primary);
}
.cw-btn:hover { border-color: var(--accent); }
.cw-btn-go {
    background: var(--success); border-color: var(--success); color: #fff;
}
.cw-btn-go:hover { filter: brightness(1.08); border-color: var(--success); }
.cw-btn-sec { color: var(--accent); border-color: var(--accent); background: transparent; }
.cw-btn-ghost { color: var(--text-secondary); border-color: transparent; background: transparent; }
.cw-foot-note { margin-left: auto; font-size: 11px; color: var(--text-secondary); }

/* mock state bar — bottom-left over the empty task-list space, clear of
   both the detail header icons and the sticky detail action bar */
.cw-mockbar {
    position: fixed; left: 14px; bottom: 14px;
    display: flex; align-items: center; flex-wrap: wrap; gap: 5px;
    max-width: 360px;
    padding: 6px 10px; border-radius: 14px;
    background: var(--bg-secondary); border: 1px solid var(--border-primary);
    box-shadow: 0 4px 14px rgba(0,0,0,.13); z-index: 900;
}
.cw-mockbar-label { font-size: 11px; color: var(--text-secondary); margin-right: 3px; }
.cw-mockbar button {
    font: inherit; font-size: 11px; padding: 3px 9px; border-radius: 11px;
    border: 1px solid var(--border-primary); background: var(--bg-primary);
    color: var(--text-primary); cursor: pointer;
}
.cw-mockbar button:hover { border-color: var(--accent); color: var(--accent); }
"""

# ── 7. Assemble the standalone page ──────────────────────────────────────────
page = PAGE.read_text(encoding="utf-8")
page = re.sub(r"\{%\s*extends[^%]*%\}", "", page)
page = re.sub(r"\{%\s*block content\s*%\}", "", page)
page = re.sub(r"\{%\s*end\s*%\}", "", page)
if "{%" in page or "{{" in page:
    sys.exit("unresolved template tags remain in dashboard.html")

base = BASE.read_text(encoding="utf-8")
html = base.replace("{% block content %}{% end %}", page.strip())
html = html.replace(
    '<link rel="stylesheet" href="/static/css/style.css">',
    "<style>\n" + CSS.read_text(encoding="utf-8") + "\n" + COWORK_CSS + "\n</style>",
)
html = html.replace(
    '<script src="/static/js/dashboard.js"></script>',
    "<script>\n" + js + "\n</script>",
)
# the icon is a server-served asset; drop it rather than 404
html = html.replace('<link rel="icon" type="image/png" href="/static/img/icon.png">', "")
html = html.replace(
    '<img src="/static/img/icon.png" alt="TodoNess" class="app-icon">',
    '<span class="app-icon" style="font-size:18px">&#9776;</span>',
)
html = html.replace("<title>TodoNess</title>", "<title>TodoNess &middot; Cowork actions (mock)</title>")

if "/static/" in html:
    leftovers = set(re.findall(r'"[^"]*?/static/[^"]*"', html))
    sys.exit(f"unresolved static refs remain: {leftovers}")

OUT.write_text(html, encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)} ({len(html):,} bytes)")

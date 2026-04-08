"""Query templates for WorkIQ MCP integration.

These templates are used by Claude Code commands (/todo-refresh, /todo-add)
to query WorkIQ for M365 context. The Tornado server never calls these directly.
"""

# ── Query Templates ────────────────────────────────────────────────────────

_TASK_OUTPUT_FORMAT = (
    "For each item, return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action describing WHAT I NEED TO DO "
    "(e.g. 'Reply to Sarah's budget proposal', 'Schedule workshop walkthrough with Steve'). "
    "Not the message subject — describe the action. "
    "2. **Description**: 2-3 sentences of context: what was the original ask, current state, "
    "what specifically needs to happen next. "
    "3. **Source type**: email, teams, or meeting. "
    "4. **Key people**: For each person involved, give their FULL resolved name and email address "
    "(e.g. 'Jane Doe, jane.doe@contoso.com'). Resolve aliases and short names to full directory names. "
    "5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). "
    "6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). "
    "7. **Date**: When the item was sent/occurred. "
    "8. **Action type**: One of: respond-email, follow-up, awaiting-response, schedule-meeting, prepare, general. "
    "Format each item as a numbered task with clear field labels."
)

# Broad email scanning remains disabled — WorkIQ enterprise search cannot
# reliably scope unflagged emails to Inbox (returns Archive/Deleted items).
# However, flagged-inbox queries now work reliably (confirmed April 2026).
SCAN_EMAIL = None

SCAN_FLAGGED_EMAIL = (
    "Show me only flagged emails in my Inbox folder. "
    "Do not include emails from Archive, Deleted Items, Sent Items, or any other folder — only the Inbox. "
    + _TASK_OUTPUT_FORMAT
)

SCAN_TEAMS_MEETINGS = (
    "What Teams messages and meeting action items need my attention or action? "
    "Include: "
    "(1) Teams messages from the last {days} days directed at me by name or @mentioning me "
    "that I haven't responded to, "
    "(2) action items from meetings in the last {days} days assigned to me or that I committed to. "
    + _TASK_OUTPUT_FORMAT
)

# SCAN_AWAITING_RESPONSE disabled — results are mostly already-handled items.
# WorkIQ can't reliably distinguish "still waiting" from "already resolved informally".
# Re-evaluate if WorkIQ gains thread-state awareness.
SCAN_AWAITING_RESPONSE = None

# ── Context Queries (email as read-only context, not task source) ─────────

WAITING_CHECK_COMMS = (
    "What are my most recent emails, Teams messages, and chats with {person} "
    "since {start_date}? List all interactions found."
)

COACHING_CONTEXT_REFRESH = (
    "What are my recent emails, Teams messages, and meeting interactions with "
    "{person} related to {topic} since {start_date}? "
    "Include any relevant context, updates, or responses."
)

"""Query templates for WorkIQ MCP integration.

These templates are used by Claude Code commands (/todo-refresh, /todo-add)
to query WorkIQ for M365 context. The Tornado server never calls these directly.
"""

# ── Query Templates ────────────────────────────────────────────────────────

_TASK_OUTPUT_FORMAT = (
    "For each item, return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action describing WHAT I NEED TO DO "
    "(e.g. 'Reply to Sarah's budget proposal', 'Schedule workshop walkthrough with Steve'). "
    "Not the email subject — describe the action. "
    "2. **Description**: 2-3 sentences of context: what was the original ask, current state, "
    "what specifically needs to happen next. "
    "3. **Source type**: email, teams, or meeting. "
    "4. **Key people**: For each person involved, give their FULL resolved name and email address "
    "(e.g. 'Phil Topness, phil.topness@microsoft.com'). Resolve aliases and short names to full directory names. "
    "5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). "
    "6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). "
    "7. **Date**: When the item was sent/occurred. "
    "8. **Action type**: One of: respond-email, follow-up, awaiting-response, schedule-meeting, prepare, general. "
    "Format each item as a numbered task with clear field labels."
)

# SCAN_EMAIL disabled — WorkIQ enterprise search cannot reliably:
# - scope to Inbox folder (returns Archive/Deleted items)
# - detect flagged status (not exposed in search metadata)
# - filter by folder location (not a returned field)
# Re-enable when Graph MCP or improved WorkIQ email access is available.
SCAN_EMAIL = None

SCAN_TEAMS_MEETINGS = (
    "What Teams messages and meeting action items need my attention or action? "
    "Include: "
    "(1) Teams messages from the last {days} days directed at me by name or @mentioning me "
    "that I haven't responded to, "
    "(2) action items from meetings in the last {days} days assigned to me or that I committed to. "
    + _TASK_OUTPUT_FORMAT
)

SCAN_AWAITING_RESPONSE = (
    "What messages or emails have I SENT in the last {days} days that contain a question, "
    "request, or ask where the recipient hasn't responded yet? Only include items where "
    "I am clearly waiting for a response — not messages I sent that were purely informational. "
    "For each item, use action_type 'awaiting-response'. "
    + _TASK_OUTPUT_FORMAT
)

# Legacy single-call query (kept for reference, no longer used)
FULL_SCAN = SCAN_TEAMS_MEETINGS

EMAIL_THREAD = (
    "Show me the full email thread for the email with subject '{subject}' "
    "from {sender}. Include all replies and the most recent message."
)

PERSON_CONTEXT = (
    "What recent interactions have I had with {person_name}? "
    "Include recent emails, meetings, and any pending items."
)

CALENDAR_AVAILABILITY = (
    "What does my calendar look like for {date_range}? "
    "Show me free and busy slots."
)

TASK_REFRESH = (
    "Regarding '{task_title}': {task_description} "
    "What's the latest context from my emails and meetings about this? "
    "Has anything changed since {last_refresh}?"
)


def format_query(template: str, **kwargs) -> str:
    """Format a query template with the given parameters."""
    return template.format(**kwargs)

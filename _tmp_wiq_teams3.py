from src.services.claude_runner import run_copilot

prompt = (
    "What Teams messages and meeting action items need my attention or action? "
    "Include: (1) Teams messages from the last 1 days directed at me by name or "
    "@mentioning me that I have not responded to, (2) action items from meetings "
    "in the last 1 days assigned to me or that I committed to. For each item, "
    "return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action describing WHAT I NEED TO DO "
    "(e.g. 'Schedule workshop walkthrough with Alex'). Not the message topic - "
    "describe the action. "
    "2. **Description**: 2-3 sentences of context: what was the original ask, "
    "current state, what specifically needs to happen next. "
    "3. **Source type**: teams or meeting. "
    "4. **Key people**: For each person involved, give their FULL resolved name "
    "and email address (e.g. 'Jane Doe, jane.doe@contoso.com'). Resolve aliases "
    "and short names to full directory names. "
    "5. **Priority**: P1 (urgent/deadline today), P2 (time-sensitive), P3 (normal), P4 (low/FYI). "
    "6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). "
    "7. **Date**: When the item was sent/occurred. "
    "8. **Action type**: One of: respond-email, follow-up, schedule-meeting, prepare, general. "
    "Format each item as a numbered task with clear field labels."
)

result = run_copilot(prompt, label='refresh-teams')
print("=== WORKIQ TEAMS/MEETINGS RESULT ===")
print(result)

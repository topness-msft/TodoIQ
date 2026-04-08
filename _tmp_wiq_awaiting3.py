from src.services.claude_runner import run_copilot

prompt = (
    "What messages or emails have I SENT in the last 1 days that contain a question, "
    "request, or ask where the recipient has not responded yet? Only include items where "
    "I am clearly waiting for a response - not messages I sent that were purely informational. "
    "For each item, return it as a structured task suggestion with ALL of these fields: "
    "1. **Task title**: A clean imperative action (e.g. 'Follow up with Alex on budget approval'). "
    "2. **Description**: 2-3 sentences: what I asked, who I am waiting on, when I sent it. "
    "3. **Source type**: email, teams, or meeting. "
    "4. **Key people**: For each person involved, give their FULL resolved name and email address. "
    "5. **Priority**: P3 (normal) or P4 (low) - these are lower urgency since I am waiting, not being asked. "
    "6. **Original subject or topic**: The root subject (strip Re:/Fwd: prefixes). "
    "7. **Date**: When I sent the message. "
    "8. **Action type**: awaiting-response. "
    "Format each item as a numbered task with clear field labels."
)

result = run_copilot(prompt, label='refresh-awaiting')
print("=== WORKIQ AWAITING RESULT ===")
print(result)

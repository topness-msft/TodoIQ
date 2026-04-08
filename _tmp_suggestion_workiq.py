import sys, asyncio
sys.path.insert(0, '.')
from src.services.claude_runner import run_copilot

tasks = [
    {
        "id": 626,
        "person": "Maria Luisa Onorato (maria.onorato@microsoft.com)",
        "topic": "confirm attendee details",
        "since": "2026-03-25"
    },
    {
        "id": 623,
        "person": "Remy Ntshaykolo (remyn@microsoft.com)",
        "topic": "customer scenarios for agent-to-agent orchestration",
        "since": "2026-03-26"
    },
    {
        "id": 610,
        "person": "Justin Walker (justw@microsoft.com)",
        "topic": "follow-up on adoption pattern metrics",
        "since": "2026-03-26"
    },
    {
        "id": 594,
        "person": "Rodrigo De la Garza (rodela@microsoft.com)",
        "topic": "offsite shark tank workshop request",
        "since": "2026-03-26"
    }
]

async def check_task(t):
    prompt = f"""Call ask_work_iq with this query and output ONLY one line in the format: {t['id']}|STATUS|SUMMARY

Query: "What are my most recent emails, Teams messages, and chats with {t['person']} about {t['topic']} since {t['since']}? Was this topic resolved, addressed, or is it still pending? List all interactions found."

Classify as:
- likely_resolved: clear evidence topic was addressed/resolved
- still_pending: no response or unresolved
- unclear: activity found but can't confirm resolution

Output exactly one line: {t['id']}|STATUS|SUMMARY"""
    result = await run_copilot(prompt, tools=["workiq"])
    print(f"=== TASK {t['id']} ===")
    print(result)
    print(f"=== END {t['id']} ===")

async def main():
    for t in tasks:
        await check_task(t)

asyncio.run(main())

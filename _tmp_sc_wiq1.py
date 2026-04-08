import sys
sys.path.insert(0, '.')
from src.services.claude_runner import run_copilot
import asyncio, json

async def main():
    prompt = """Use ask_work_iq to answer: What are my most recent emails, Teams messages, and chats with Matt Sheard (msheard@microsoft.com) about EBC session April 21 (participant list, session times, facilitator, expense info, workshop design) since 2026-04-08? List interactions found and whether each was resolved or still pending. Be concise."""
    result = await run_copilot(prompt, tools=['workiq'])
    print(result)

asyncio.run(main())

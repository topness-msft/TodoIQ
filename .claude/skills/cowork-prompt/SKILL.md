---
name: cowork-prompt
description: Draft a Copilot Cowork prompt for scheduling a meeting
triggers:
  - user asks for a cowork prompt
  - "chief of staff" briefing via cowork
  - "cowork email" or "cowork briefing"
---

# Copilot Cowork — Chief of Staff Briefing Prompt

Generate a ready-to-paste prompt for Copilot Cowork (M365 chat agent) that produces a Chief of Staff-style briefing email. Cowork has access to M365 data (calendar, email, Teams, files) but **not** TodoNess tasks.

## The Prompt

Copy and send this to Copilot Cowork:

---

Review my calendar for the next 3 business days and my email/Teams activity from the past 48 hours. Write me a "Chief of Staff Briefing" email with these sections:

**⚠️ Needs Your Attention** — Unanswered messages or threads where someone is waiting on me, sorted by how long they've been waiting. Flag anything over 48 hours as overdue.

**📅 Preparing for Today** — For each meeting today, note: who's attending, what prep I need (unread docs, open threads with attendees), and any commitments I made in prior meetings with the same group.

**📊 This Week's Load** — How many meetings per day, which days are heaviest, and where I have focus time blocks.

**👥 People to Reconnect With** — Anyone I've had significant interaction with in the past month but no contact in the last 2 weeks.

Be direct and opinionated like a real chief of staff — tell me what to prioritize, what to delegate, and what I can safely ignore. Keep it scannable.

---

## What Cowork Covers vs TodoNess Briefing

| Capability | Cowork | TodoNess Briefing |
|---|---|---|
| Calendar/meetings | ✅ | ✅ (via WorkIQ) |
| Unanswered messages | ✅ | ✅ |
| Meeting commitments | ✅ | ✅ |
| Task tracking/initiatives | ❌ | ✅ |
| Stale follow-ups | ❌ | ✅ |
| CoS initiative narratives | ❌ | ✅ |

Cowork gives the **reactive** half (what's incoming). TodoNess adds the **proactive** half (what you're driving).

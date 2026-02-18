"""Parse WorkIQ responses into task suggestions with deduplication."""

import re

from ..models import list_tasks

_THREAD_PREFIX_RE = re.compile(r'^(re:\s*|fwd?:\s*)+', re.IGNORECASE)


def _normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/Fw: prefixes and normalize whitespace."""
    s = (subject or "").strip()
    s = _THREAD_PREFIX_RE.sub('', s)
    return s.strip().lower()


def generate_source_id(source_type: str, sender: str, subject: str, date: str = "") -> str:
    """Build a stable composite key for deduplication.

    Format: {source_type}::{sender_lower}::{normalized_subject_first_50}
    Date is intentionally excluded so that replies in the same email thread
    (same sender + same root subject) match existing tasks.
    """
    sender_part = (sender or "").strip().lower()
    subject_part = _normalize_subject(subject)[:50]
    return f"{source_type}::{sender_part}::{subject_part}"


def find_duplicate(title: str, source_id: str | None = None,
                   source_type: str | None = None,
                   sender: str | None = None,
                   subject: str | None = None) -> dict | None:
    """Check if a similar task already exists (active, suggested, or dismissed).

    Three-level matching:
    1. Primary: exact source_id match (most reliable)
    2. Secondary: source_type + sender + subject-prefix match (for emails)
    3. Tertiary: title-prefix fallback (first 40 chars)
    """
    all_tasks = list_tasks()
    title_lower = title.lower().strip()

    for task in all_tasks:
        # Primary: exact source_id match
        if source_id and task.get("source_id") and task["source_id"] == source_id:
            return task

    # Secondary: source_type + sender + normalized subject-prefix
    # Handles email alias variations (saurabh.pant@ vs spant@) by
    # matching on the short alias (part before dots/hyphens) as well
    if source_type and sender and subject:
        sender_lower = sender.strip().lower()
        sender_local = sender_lower.split("@")[0] if "@" in sender_lower else sender_lower
        subject_norm = _normalize_subject(subject)[:30]
        for task in all_tasks:
            task_sid = task.get("source_id") or ""
            if task_sid.startswith(f"{source_type}::"):
                parts = task_sid.split("::")
                if len(parts) >= 3:
                    task_sender = parts[1]
                    task_sender_local = task_sender.split("@")[0] if "@" in task_sender else task_sender
                    # Match if sender is exact OR one local part contains the other
                    sender_match = (
                        task_sender == sender_lower
                        or sender_local in task_sender_local
                        or task_sender_local in sender_local
                    )
                    if sender_match and parts[2].startswith(subject_norm):
                        return task

    # Tertiary: title-prefix fallback
    for task in all_tasks:
        if task["title"].lower().strip()[:40] == title_lower[:40]:
            return task

    return None


def should_create_suggestion(
    title: str,
    source_id: str | None = None,
    source_type: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
) -> bool:
    """Return True if this suggestion doesn't already exist."""
    existing = find_duplicate(title, source_id, source_type, sender, subject)
    if existing is None:
        return True
    # Don't re-suggest dismissed items
    if existing["status"] == "dismissed":
        return False
    # Don't duplicate existing items
    return False

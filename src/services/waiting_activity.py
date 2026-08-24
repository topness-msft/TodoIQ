"""The contract for `tasks.waiting_activity`.

One TEXT column holds JSON written by three different commands, each with its
own vocabulary:

    waiting-check     out_of_office | no_activity | activity_detected | may_be_resolved
    suggestion-check  likely_resolved | still_pending | unclear
    todo-parse        out_of_office (only, from its OOO probe)

Nothing in the stored shape said which of those you were holding, so a reader
had to guess from the status string. `producer` ends the guessing; where a
legacy row is genuinely ambiguous this module says so rather than picking the
likelier answer.

The change this module exists for is smaller and more important than the
schema: `/waiting-check` skips a task entirely when WorkIQ errors
(.claude/commands/waiting-check.md:79). Nothing is written, so the card goes on
showing the previous result and the user cannot tell "I looked and found
nothing" from "I could not look". `check_state` separates them, and
`signal_for` refuses to report any finding from a failed check.

Everything here is pure: no I/O, no WorkIQ, no database. The slash-command
writes this JSON inline from bash and cannot import anything, so this module's
real job is the READ path - `src/models.py` normalises every row through it, so
the 2,430 legacy rows in the live database become v2-shaped on the way out
without a migration.
"""

import json

SCHEMA_VERSION = 2

# Who wrote the row.
PRODUCER_WAITING_CHECK = "waiting-check"
PRODUCER_SUGGESTION_CHECK = "suggestion-check"
PRODUCER_TODO_PARSE = "todo-parse"

# Did the check actually run?
CHECK_OK = "ok"
CHECK_FAILED = "failed"

# How wide a net the check cast. A thread-scoped "no reply" is a far stronger
# claim than a person-scoped one, and the card must not present them alike.
SCOPE_THREAD = "thread"
SCOPE_PERSON = "person"

# What the dashboard is entitled to show.
SIGNAL_UNCHECKED = "unchecked"
SIGNAL_CHECK_FAILED = "check_failed"
SIGNAL_ACTIVITY = "activity"
SIGNAL_LOOKS_DONE = "looks_done"
SIGNAL_QUIET = "quiet"
SIGNAL_OUT_OF_OFFICE = "out_of_office"
SIGNAL_NONE = "none"

_WAITING_STATUSES = {"no_activity", "activity_detected", "may_be_resolved"}
_SUGGESTION_STATUSES = {"likely_resolved", "still_pending", "unclear"}

# out_of_office is deliberately absent from both sets above: waiting-check and
# todo-parse both write it, so it identifies nobody.
_ALL_KNOWN_STATUSES = _WAITING_STATUSES | _SUGGESTION_STATUSES | {"out_of_office"}

_SIGNAL_BY_STATUS = {
    "activity_detected": SIGNAL_ACTIVITY,
    "may_be_resolved": SIGNAL_LOOKS_DONE,
    "no_activity": SIGNAL_QUIET,
    "out_of_office": SIGNAL_OUT_OF_OFFICE,
}

_EVIDENCE_KEYS = ("excerpt", "when", "where", "url")


def _as_dict(raw):
    """Accept a JSON string or an already-decoded dict; reject anything else."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        # A half-written row must not take the dashboard down.
        return None
    return decoded if isinstance(decoded, dict) else None


def _clean_evidence(value):
    """Keep only entries a renderer can destructure unconditionally."""
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        excerpt = item.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            continue
        out.append({key: item.get(key) for key in _EVIDENCE_KEYS})
    return out


def _attribute(data, status):
    """Work out who wrote this row, or admit that it cannot be known."""
    declared = data.get("producer")
    if declared in (PRODUCER_WAITING_CHECK, PRODUCER_SUGGESTION_CHECK,
                    PRODUCER_TODO_PARSE):
        return declared
    if status in _SUGGESTION_STATUSES:
        return PRODUCER_SUGGESTION_CHECK
    if status in _WAITING_STATUSES:
        return PRODUCER_WAITING_CHECK
    # Includes out_of_office, which two producers write.
    return None


def normalise(raw):
    """Return a v2-shaped dict for any stored value, or None if there isn't one.

    Never raises: this runs on every task in every list response.
    """
    data = _as_dict(raw)
    if data is None:
        return None

    check_state = (
        CHECK_FAILED if data.get("check_state") == CHECK_FAILED else CHECK_OK
    )

    status = data.get("status")
    if not isinstance(status, str) or not status.strip():
        status = None
    if check_state == CHECK_FAILED:
        # A check that did not run has no finding of its own. Any status in the
        # row belongs to an earlier run and is preserved under `previous`.
        status = None

    previous = data.get("previous")
    previous = previous if isinstance(previous, dict) else None

    scope = data.get("source_scope")
    scope = SCOPE_THREAD if scope == SCOPE_THREAD else SCOPE_PERSON

    return {
        "version": SCHEMA_VERSION,
        "check_state": check_state,
        "status": status,
        "summary": data.get("summary"),
        "checked_at": data.get("checked_at"),
        "check_since": data.get("check_since"),
        "return_date": data.get("return_date"),
        "error": data.get("error"),
        "producer": _attribute(data, status if status else data.get("status")),
        "source_scope": scope,
        "conversation_id": data.get("conversation_id"),
        "evidence": _clean_evidence(data.get("evidence")),
        "previous": previous,
    }


def signal_for(task_status, activity):
    """Which signal a card may show. The honesty gate lives here.

    `task_status` is the task's own status, so a caller can key off it later
    without every renderer re-deriving the rule.
    """
    if activity is None:
        return SIGNAL_UNCHECKED

    if activity.get("check_state") == CHECK_FAILED:
        # The row may still carry a previous "looks done"; nobody checked this
        # time, so that is not what the card reports.
        return SIGNAL_CHECK_FAILED

    # /suggestion-check shares this column with a different vocabulary. Its
    # verdicts are not waiting activity and must not drive these signals.
    if activity.get("producer") == PRODUCER_SUGGESTION_CHECK:
        return SIGNAL_NONE

    return _SIGNAL_BY_STATUS.get(activity.get("status"), SIGNAL_NONE)


def next_check_since(activity, created_at):
    """The cursor the next check should read from.

    Deliberately not `updated_at`: waiting-check.md:106,109 set it on every
    write, including its own, so using it would shrink the window to "since I
    last looked" and step over anything that arrived while a check was failing.

    A failed check does not advance the cursor, so the period nobody managed to
    read stays inside the next window.
    """
    if activity is None:
        return created_at
    if activity.get("check_state") == CHECK_FAILED:
        return activity.get("check_since") or created_at
    return activity.get("checked_at") or created_at

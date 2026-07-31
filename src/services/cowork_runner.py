"""Cowork action layer — source URL parsing.

Phase 1 is PREVIEW ONLY. This module currently exposes only pure functions; the
subprocess runner lands in a later change.

``parse_source_url`` answers the single most safety-critical question in the action
layer: **how many people would see a reply sent here?** Teams encodes this in the
conversation id suffix, not in any query parameter:

===========================  ================  ============================
conversation id suffix       audience          live count (of 1132)
===========================  ================  ============================
``@unq.gbl.spaces``          1:1               629
``@thread.v2``               group chat        390
``19:meeting_…@thread.v2``   meeting chat       94
``@thread.skype``            team channel       14
``@thread.tacv2``            group (federated)   4
===========================  ================  ============================

Only 44% of chat-sourced tasks are 1:1, so the broadcast cases are the norm rather
than the exception. Every unrecognised shape therefore fails **safe**: anything we
cannot positively prove is a 1:1 is reported as a broadcast.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

__all__ = ["parse_source_url", "compose_prompt"]

_MESSAGE_RE = re.compile(r"/l/message/(?P<conv>[^/?#]+)(?:/(?P<msg>[^/?#]+))?")

_GUID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$",
    re.I,
)


def _norm(guid: str) -> str:
    """Compare object ids irrespective of hyphenation."""
    return guid.replace("-", "").lower()

_LABELS = {
    "one_to_one": "direct message",
    "group": "group chat",
    "meeting": "meeting chat",
    "channel": "team channel",
    "unknown": "unrecognised conversation",
    "none": "",
}


def _classify(conv: str, url: str) -> str:
    """Map a Teams conversation id to an audience kind."""
    local = conv.split("@", 1)[0]

    # A meeting chat is still an @thread.v2, so this must be checked first.
    if local.startswith("19:meeting_") or local.startswith("meeting_"):
        return "meeting"
    if conv.endswith("@unq.gbl.spaces"):
        return "one_to_one"
    if conv.endswith("@thread.skype") or "groupId=" in url:
        return "channel"
    if conv.endswith("@thread.tacv2") or conv.endswith("@thread.v2"):
        return "group"
    return "unknown"


def _counterparty(conv: str, me: str | None) -> str | None:
    """In a 1:1, the conversation id is ``19:{userA}_{userB}@unq.gbl.spaces``.

    Returns the participant who is *not* ``me``, exactly as it appears in the URL.
    Ids are matched with hyphens stripped: 38 of 629 real 1:1 links write the
    participant as bare 32-hex rather than a dashed GUID, and either side may use
    either form.

    Returns None unless the id has exactly two GUID participants and ``me`` is one
    of them -- guessing a recipient is worse than declining to.
    """
    if not me:
        return None
    local = conv.split("@", 1)[0]
    if local.startswith("19:"):
        local = local[3:]
    parts = local.split("_")
    if len(parts) != 2 or not all(_GUID_RE.match(p) for p in parts):
        return None
    me_n = _norm(me)
    normed = [_norm(p) for p in parts]
    if me_n not in normed:
        return None
    return parts[1] if normed[0] == me_n else parts[0]


def parse_source_url(url: str | None, me: str | None = None) -> dict:
    """Classify a task's ``source_url`` into a reply destination.

    Args:
        url: the task's source_url. May be None/blank/non-Teams.
        me: the current user's Entra object id. Only used to identify the
            counterparty of a 1:1 chat.

    Returns a dict with a stable key set -- callers destructure it
    unconditionally, so a missing key would surface as a crash during preview:

        kind            one_to_one | group | meeting | channel | unknown | none
        is_broadcast    True unless positively identified as a 1:1
        conversation_id Teams conversation id, or None
        message_id      the linked message's timestamp id, or None
        counterparty_id the other party's object id (1:1 + ``me`` only)
        audience_label  human-readable phrase for the UI
    """
    result = {
        "kind": "none",
        "is_broadcast": False,
        "conversation_id": None,
        "message_id": None,
        "counterparty_id": None,
        "audience_label": "",
    }

    if not url or not url.strip():
        return result

    match = _MESSAGE_RE.search(url)
    if not match:
        # Outlook items, SharePoint recordings and meeting-details links are valid
        # task sources but are not places a chat reply can be posted.
        return result

    conv = unquote(match.group("conv"))
    kind = _classify(conv, url)

    result["kind"] = kind
    result["is_broadcast"] = kind != "one_to_one"
    result["conversation_id"] = conv
    result["message_id"] = match.group("msg")
    result["audience_label"] = _LABELS[kind]
    if kind == "one_to_one":
        result["counterparty_id"] = _counterparty(conv, me)
    return result


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------

_WORKIQ_RE = re.compile(r"(?<![\w.])@workiq\b[ \t]*[-:\u2013\u2014]?[ \t]*", re.I)

_SAFETY = (
    "Produce findings first, then a draft message.\n"
    "DO NOT SEND, POST, REPLY, OR DELIVER ANYTHING. This is a preview only.\n"
    "Do not create, modify, or send any email, chat message, meeting or file.\n"
    "Return the draft as text for a human to review. Nothing you write is delivered."
)


def _get(task, key, default=""):
    """Read a field from a dict or sqlite3.Row without assuming which."""
    try:
        value = task[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _clean(text) -> str:
    """Normalise user-authored text for inclusion in a prompt.

    Deliberately does NOT attempt mojibake repair: a live-data survey found zero
    mojibake across 1958 tasks (the 644 em-dashes are genuine U+2014), so any
    "repair" here would corrupt correct text. The real encoding hazard is the
    cp1252 default at the subprocess boundary, which is handled by writing the
    prompt as UTF-8 -- not by mangling characters.
    """
    if not text:
        return ""
    return str(text).replace("\r\n", "\n").strip()


def _strip_workiq(text: str) -> str:
    """Remove the retired @WorkIQ token, keeping the surrounding prose (F11).

    Notes are already written as instructions ("@WorkIQ pull the subject from my
    calendar invites"); the token is the only dead part. The lookbehind keeps
    ``brandon@microsoft.com`` intact.
    """
    return _WORKIQ_RE.sub("", text)


def compose_prompt(task, destination: dict | None = None,
                   redirect_text: str | None = None) -> str:
    """Assemble the Cowork preview prompt from its layers.

    Layer order is semantic, not cosmetic. The correction is emitted after the
    standing layers so it overrides them, and the safety instruction is emitted
    last of all so that no user-authored layer -- note or correction -- can talk
    the run out of preview mode.

    Args:
        task: mapping or sqlite3.Row of the task's fields.
        destination: result of parse_source_url; derived from the task if omitted.
        redirect_text: one-shot steer supplied via Redo (F12).

    Returns:
        The full prompt. Callers must write it as UTF-8 -- 23 real tasks contain
        characters cp1252 cannot encode.
    """
    if destination is None:
        destination = parse_source_url(_get(task, "source_url") or None)

    parts: list[str] = []

    parts.append(
        "[ROLE]\n"
        "You are helping the user act on one of their tasks. You are running in "
        "PREVIEW mode: you research and draft, but you never deliver anything."
    )

    title = _clean(_get(task, "title"))
    description = _clean(_get(task, "description"))
    task_block = f"[TASK]\n{title}"
    if description:
        task_block += f"\n{description}"
    parts.append(task_block)

    intent = _clean(_get(task, "coaching_text"))
    if intent:
        parts.append("[INTENT]\nThe suggested next action for this task:\n" + intent)

    notes = _clean(_strip_workiq(_clean(_get(task, "user_notes"))))
    if notes:
        parts.append(
            "[NOTES]\nStanding context and instructions the user wrote themselves. "
            "Treat these as direction:\n" + notes
        )

    source_lines = []
    source_type = _clean(_get(task, "source_type"))
    if source_type:
        source_lines.append(f"Origin: {source_type}")
    label = destination.get("audience_label")
    if label:
        source_lines.append(f"Conversation: {label}")
        if destination.get("is_broadcast"):
            source_lines.append(
                f"CAUTION: this is a {label} -- more than one person would see a "
                "reply here. State who the audience is in your findings."
            )
    people = _clean(_get(task, "key_people"))
    if people:
        source_lines.append(f"Key people: {people}")
    snippet = _clean(_get(task, "source_snippet"))
    if snippet:
        source_lines.append(f"Original message:\n{snippet}")
    if source_lines:
        parts.append("[SOURCE]\n" + "\n".join(source_lines))

    correction = _clean(redirect_text)
    if correction:
        parts.append(
            "[CORRECTION]\nThe user reviewed a previous attempt and asked for this "
            "change. It overrides the intent and notes above:\n" + correction
        )

    parts.append("[OUTPUT]\n" + _SAFETY)

    return "\n\n".join(parts)

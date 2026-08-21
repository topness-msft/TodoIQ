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

import base64
import binascii
import html
import json
import re
import uuid
import zlib
from datetime import datetime, timezone
from urllib.parse import unquote, quote, urlparse

from .runtime_mode import (
    DEMO_DISABLED_MESSAGE,
    cowork_execute_enabled,
    cowork_session_enabled,
)
from .calendar_time import calendar_event_is_future, named_timezone_matches

__all__ = [
    "parse_source_url",
    "schedule_attendees",
    "compose_prompt",
    "parse_cowork_output",
]


def _require_cowork_session():
    if not cowork_session_enabled():
        raise RuntimeError(DEMO_DISABLED_MESSAGE)


def _require_cowork_execute():
    if not cowork_execute_enabled():
        raise RuntimeError(DEMO_DISABLED_MESSAGE)

_MESSAGE_RE = re.compile(r"/l/message/(?P<conv>[^/?#]+)(?:/(?P<msg>[^/?#]+))?")
_CHAT_RE = re.compile(r"/l/chat/(?P<conv>[^/?#]+)/conversations(?:[/?#]|$)")
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

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

    message_match = _MESSAGE_RE.search(url)
    chat_match = _CHAT_RE.search(url)
    match = message_match or chat_match
    if not match:
        # Outlook items, SharePoint recordings and meeting-details links are valid
        # task sources but are not places a chat reply can be posted.
        return result

    conv = unquote(match.group("conv"))
    kind = _classify(conv, url)

    result["kind"] = kind
    result["is_broadcast"] = kind != "one_to_one"
    result["conversation_id"] = conv
    result["message_id"] = message_match.group("msg") if message_match else None
    result["audience_label"] = _LABELS[kind]
    if kind == "one_to_one":
        result["counterparty_id"] = _counterparty(conv, me)
    return result


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------

_WORKIQ_RE = re.compile(r"(?<![\w.])@workiq\b[ \t]*[-:\u2013\u2014]?[ \t]*", re.I)

# The prompt is a SOFT control and always was. The hard control is
# `--tool-callback-config` (F13 proved `--deny-tools` is worthless; F14 proved
# the callback barrier works), asserted by
# tests/test_cowork_runner.py::TestArgv::test_callback_config_flag_always_present.
#
# This text is therefore scoped to the automated turn rather than the
# conversation. Phil opens the same conversation in the Cowork web app and
# drives it himself, where he legitimately wants to say "send it" and get
# Cowork's own approval card. A blanket "never deliver anything" would sit in
# the history and argue with him on his own turn. Relaxing the wording does not
# widen what the automated run can do, because the barrier still intercepts
# every write tool regardless of what the prompt says.
_SAFETY = (
    "Produce findings first, then a draft message.\n"
    "For THIS turn, do not send, post, reply or deliver anything, and do not "
    "create or modify any email, chat message, meeting or file. Return the "
    "draft as text for review.\n"
    "This scopes the current turn only. It is not a standing restriction: if "
    "the user later asks you directly to send or change something, treat that "
    "as a new instruction and follow your normal confirmation process."
)

_NO_INTERACTION = (
    "Complete this research-and-draft turn without pausing to ask the user "
    "questions. Make reasonable decisions from the available context, clearly "
    "state any assumptions in the findings, and produce the best safe draft you "
    "can. This does not change the preview-only restrictions below."
)

# Voice. Depth comes from the published Cowork skills (`work-email-voice`,
# `work-teams-voice`), which the runtime loads server-side at ~0 prompt cost and
# which carry the audience tables, playbooks and real exemplars.
#
# The invariants below stay INLINE on purpose. A skill lives outside this repo
# and outside version control, so it can change or fail to resolve without a code
# change, and a measured A/B on task 2029 showed a skill reference alone did not
# enforce the mechanical bans: 2 em-dashes with the skill only, 0 with these
# rules present. So: skill for depth, inline for the enforced floor.
_VOICE_SHARED = (
    "Write it as the user would write it themselves, so the draft is paste-ready.\n"
    "- Use contractions throughout: I'm, I'll, it's, we're, here's.\n"
    "- Never use em-dashes. Use a comma, a period, parentheses or a colon.\n"
    "- No corporate filler: no \"I hope this finds you well\", \"circling back\", "
    "\"per my last email\", \"leverage\", \"synergy\".\n"
    "- Lead with the context and the why, then the detail or the ask.\n"
    "- Be specific when recognising someone: name them and say what it changed.\n"
    "- Close by moving the work forward, with a next step, a question or an offer."
)

_SKILL_NOTE = (
    "It is a drafting guide only: it sets voice, it does not by itself "
    "authorise sending. Follow the rules below regardless, and if it is "
    "unavailable rely on them alone."
)

# App-wide voice settings, read from the gitignored data/settings.json:
#
#   "cowork_voice": {
#     "teams": "work-teams-voice",
#     "email": "work-email-voice",
#     "default_channel": null
#   }
#
# Skill names are configurable because they name something outside this repo;
# the mechanics below them are not, for the reason recorded on _VOICE_SHARED.
# Every read fails closed to these defaults, so a malformed settings file
# degrades to today's behaviour rather than to no voice at all.
_VOICE_SKILL_DEFAULTS = {"teams": "work-teams-voice", "email": "work-email-voice"}

# A skill name is an identifier. This value is user-controlled text that lands
# in a prompt whose last layer carries the write barrier, so anything that is
# not a bare name is refused and the default is used instead. Cheap, and it
# keeps a settings file from being able to argue with the safety line.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def reset_voice_settings_cache() -> None:
    """Test seam: force the next read to come from the settings file.

    Reads are not memoised. An earlier version cached on the identity of the
    parsed document, which could never hit because ``_read_settings`` parses
    the file fresh every call and returns a new object. Rather than keep a
    cache that only looked like one, the read is left direct: it is a small
    local JSON file, and it is consulted once per prompt composition (one run),
    not once per HTTP request. That is the distinction that mattered in the
    handoff case, where a per-request NETWORK call cost 7s.
    """
    return None


def _settings_doc() -> dict:
    """The whole settings document, empty when absent or malformed."""
    return workspace_settings._read_settings()


def _voice_settings() -> dict:
    """The ``cowork_voice`` block, or an empty one when absent or malformed."""
    block = _settings_doc().get("cowork_voice")
    return block if isinstance(block, dict) else {}


def voice_skill(channel: str):
    """Which voice skill to name for this channel, or None to name none.

    Returns the configured skill for a known channel, falling back to the
    shipped default. An explicit null or blank turns the skill reference off
    while leaving the inline mechanics in place, which is the supported way to
    run on the inline floor alone.
    """
    key = (channel or "").strip().lower()
    if key not in _VOICE_SKILL_DEFAULTS:
        return None

    if key not in _voice_settings():
        return _VOICE_SKILL_DEFAULTS[key]

    configured = _voice_settings()[key]
    if configured is None:
        return None
    if not isinstance(configured, str):
        return _VOICE_SKILL_DEFAULTS[key]
    name = configured.strip()
    if not name:
        return None
    if not _SKILL_NAME_RE.match(name):
        logger.warning(
            "ignoring cowork_voice.%s: %r is not a skill name", key, configured[:40]
        )
        return _VOICE_SKILL_DEFAULTS[key]
    return name


def default_delivery_channel():
    """Channel to assume when the task itself gives no signal, or None.

    Unset by default. Measured on live data, 24% of open tasks (all typed by
    the user rather than derived from Teams or mail) resolve to no channel, so
    neither voice skill fires. This is what lets an app-wide voice apply to
    those without pretending a destination was detected: it selects a register,
    it never binds an audience.
    """
    value = _voice_settings().get("default_channel")
    if not isinstance(value, str):
        return None
    channel = value.strip().lower()
    return channel if channel in _VOICE_SKILL_DEFAULTS else None


_MEETING_NOTE_MAX = 400


def meeting_preferences():
    """The user's standing meeting defaults, or None when not configured.

    Validated rather than trusted: a duration or offset that is not a sane
    number is dropped instead of being pushed into the prompt. When nothing
    survives validation this returns None and no layer is emitted, so a
    malformed block behaves exactly like an absent one.
    """
    block = _settings_doc().get("meeting_preferences")
    if not isinstance(block, dict):
        return None

    prefs = {}

    minutes = block.get("default_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= 600:
        prefs["default_minutes"] = minutes

    offset = block.get("start_offset_minutes")
    if isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= 60:
        prefs["start_offset_minutes"] = offset

    notes = block.get("notes")
    if isinstance(notes, str):
        # Collapse to a single line so freeform prose cannot fake a new layer
        # header. [OUTPUT] is still emitted after this, so the safety line wins
        # regardless, but there is no reason to let a settings value try.
        cleaned = " ".join(notes.split())[:_MEETING_NOTE_MAX].strip()
        if cleaned:
            prefs["notes"] = cleaned

    return prefs or None


def _meeting_layer() -> str:
    """Standing meeting defaults, phrased so they only bite when relevant.

    Deliberately NOT keyed to action_type. The [ACTION] guidance is, and of 17
    open tasks that read as scheduling only 6 carry
    action_type='schedule-meeting', so anything keyed that way fires about a
    third of the time. "Not consistently applied" was the actual complaint.
    """
    prefs = meeting_preferences()
    if not prefs:
        return ""

    lines = []
    minutes = prefs.get("default_minutes")
    offset = prefs.get("start_offset_minutes")
    if minutes:
        lines.append(f"- Default to {minutes} minutes unless the task says otherwise.")
    if offset:
        # Stated as fixed, with both worked examples. The earlier wording gave
        # a rationale ("so there is a gap after the previous meeting") that a
        # model could reasonably scale with the length of the meeting. The
        # offset does not move: 5 after is 5 after at any duration.
        lines.append(
            f"- Start at {offset} minutes past the hour or half hour, whatever "
            f"the length: a 25 minute meeting runs :05 to :30, a 55 minute "
            f"meeting runs :05 to :00. The offset never changes with duration."
        )
    if prefs.get("notes"):
        lines.append(f"- {prefs['notes']}")

    # The same classification gap applies to checking availability: it is stated
    # in the [ACTION] block, which fires for about a third of the tasks that
    # actually propose a time. Restated here in one line so it travels with the
    # rest of the meeting mechanics.
    lines.append(
        "- Check every invitee's free/busy before offering a time, not just the "
        "user's. If you cannot see a calendar, say so and frame the times as "
        "suggestions to confirm."
    )

    return (
        "If you propose or book a meeting time, use the user's standing "
        "preferences:\n" + "\n".join(lines)
    )


def _skill_sentence(channel: str) -> str:
    """"Use the skill X..." line, or nothing when no skill is configured."""
    name = voice_skill(channel)
    if not name:
        return ""
    return f"Use the skill {name} to set the voice of this draft. {_SKILL_NOTE}\n\n"


def _voice_email() -> str:
    return (
        _skill_sentence("email")
        + "This draft is an Outlook email from the user's Microsoft work account.\n"
        + _VOICE_SHARED + "\n"
        "- Give it a short, specific, plain subject line. No hype.\n"
        "- Open \"Hi {First},\" for peers, or just \"{First},\" for a leader or an "
        "active thread. Never \"Dear\", \"Hello,\" or \"Team,\".\n"
        "- Sign off with just \"Phil\". Never \"Best,\", \"Regards,\" or \"Thanks,\" "
        "as a closing line; gratitude belongs in the body. The signature block "
        "auto-appends, so do not retype it."
    )


# Teams mechanics below were corrected by a WorkIQ study of real sent messages.
# An earlier hand-written version claimed an emoji could soften a nudge; the data
# says he uses capitalisation and punctuation instead, and the name-dash opening
# is his signature move.
def _voice_teams() -> str:
    return (
        _skill_sentence("teams")
        + "This draft is a Teams chat message, not an email. Match chat register.\n"
        + _VOICE_SHARED + "\n"
        "- No subject, no greeting block, no sign-off and no signature.\n"
        "- Open with the name-dash pattern: \"Hi {First} - \", \"Hey {First} - \" or "
        "just \"{First} - \". In an active back-and-forth, drop the name entirely.\n"
        "- Keep it to 1-2 sentences unless it is a group coordination post.\n"
        "- No emoji. Enthusiasm is capitalisation and punctuation, not emoji.\n"
        "- Never chase with pressure: no \"Just following up\", \"Any update?\" or "
        "\"Did you see my last message?\". Reference the existing context instead."
    )


# No bound channel means the transport is genuinely unknown. Both skills are
# channel-specific, so there is no correct one to pull, and guessing email
# mechanics here is how a chat reply ends up with a subject line and a sign-off.
_VOICE_NEUTRAL = (
    "The delivery channel is not chosen yet, so keep the draft usable as either a "
    "short email or a Teams message: no subject line and no sign-off.\n"
    + _VOICE_SHARED
)


def _voice_for(channel: str) -> str:
    """The [VOICE] layer for a bound channel, or the neutral register."""
    key = (channel or "").strip().lower()
    if key == "email":
        return _voice_email()
    if key == "teams":
        return _voice_teams()
    return _VOICE_NEUTRAL


def _selected_people_for_prompt(value) -> str:
    text = _clean(value)
    if not text:
        return ""
    try:
        people = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if not isinstance(people, list):
        return text
    selected = []
    for person in people:
        if not isinstance(person, dict):
            return text
        selected.append({
            key: person[key]
            for key in ("name", "email", "role")
            if person.get(key)
        })
    return json.dumps(selected, ensure_ascii=False)


def schedule_attendees(task) -> list[dict]:
    """Return one exact, confirmed attendee set or an empty fail-closed result."""
    text = _clean(_get(task, "key_people")).strip()
    if not text:
        return []
    try:
        people = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(people, dict):
        people = [people]
    if not isinstance(people, list) or not people:
        return []

    selected = []
    seen = set()
    for person in people:
        if (
            not isinstance(person, dict)
            or person.get("unresolved") is True
            or person.get("attendance_uncertain") is True
        ):
            return []
        name = _clean(person.get("name")).strip()
        email = _clean(person.get("email")).strip().lower()
        if not name or not email or email in seen:
            return []
        seen.add(email)
        selected_person = {"name": name, "email": email}
        for field in ("role", "timezone"):
            value = _clean(person.get(field)).strip()
            if value:
                selected_person[field] = value
        selected.append(selected_person)
    return selected


def schedule_duration_minutes(task) -> int:
    """Return the requested meeting length, then the configured/default length."""
    number_words = {
        "five": 5,
        "ten": 10,
        "fifteen": 15,
        "twenty": 20,
        "twenty five": 25,
        "thirty": 30,
        "forty five": 45,
        "sixty": 60,
        "ninety": 90,
    }
    hour_words = {
        "a": 1,
        "an": 1,
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
    }
    minute_words = "|".join(
        re.escape(value) for value in sorted(number_words, key=len, reverse=True)
    )
    hour_word_pattern = "|".join(
        re.escape(value) for value in sorted(hour_words, key=len, reverse=True)
    )

    def token_value(token, words):
        return float(token) if token[0].isdigit() else words[token.lower()]

    for field in ("title", "user_notes", "description", "coaching_text"):
        text = _clean(_get(task, field))
        normalized = re.sub(r"\s+", " ", text.replace("-", " ")).strip().lower()
        hour_minute = re.search(
            r"\b(" + hour_word_pattern + r"|\d+(?:\.\d+)?)"
            r"\s+(?:hours?|hrs?)(?:\s+and)?\s+("
            + minute_words
            + r"|\d{1,3})\s+(?:minutes?|mins?)\b",
            normalized,
            re.I,
        )
        if hour_minute:
            value = round(
                token_value(hour_minute.group(1), hour_words) * 60
                + token_value(hour_minute.group(2), number_words)
            )
            if 5 <= value <= 480:
                return value
        compound_hour = re.search(
            r"\b(" + hour_word_pattern + r"|\d+(?:\.\d+)?)"
            r"\s+(?:(?:hours?)\s+)?and\s+a\s+half"
            r"(?:\s+hours?)?\b",
            normalized,
            re.I,
        )
        if compound_hour:
            base = token_value(compound_hour.group(1), hour_words)
            value = round((base + 0.5) * 60)
            if 5 <= value <= 480:
                return value
        if re.search(r"\bhours?\s+and\s+a\s+half\b", normalized, re.I):
            return 90
        if re.search(r"\bhalf\s+(?:an?\s+)?hours?\b", normalized, re.I):
            return 30
        match = re.search(
            r"\b(\d{1,3})\s+(?:minutes?|mins?)\b", normalized, re.I
        )
        if match:
            value = int(match.group(1))
            if 5 <= value <= 480:
                return value
        word_match = re.search(
            r"\b(" + minute_words + r")\s+minutes?\b",
            normalized,
            re.I,
        )
        if word_match:
            return number_words[word_match.group(1).lower()]
        hour_match = re.search(
            r"\b(\d+(?:\.\d+)?)\s+(?:hours?|hrs?)\b",
            normalized,
            re.I,
        )
        if hour_match:
            value = round(float(hour_match.group(1)) * 60)
            if 5 <= value <= 480:
                return value
        word_hour_match = re.search(
            r"\b(" + hour_word_pattern + r")\s+hours?\b",
            normalized,
            re.I,
        )
        if word_hour_match:
            value = hour_words[word_hour_match.group(1).lower()] * 60
            if value <= 480:
                return value
    prefs = meeting_preferences() or {}
    value = prefs.get("default_minutes")
    return value if isinstance(value, int) and 5 <= value <= 480 else 30


def _source_reference_lines(task) -> list[str]:
    primary = _clean(_get(task, "source_url")).strip()
    if primary and not _HTTP_URL_RE.fullmatch(primary):
        primary = ""

    seen = {primary} if primary else set()
    related = []
    raw_input = _clean(_get(task, "raw_input"))
    for match in _HTTP_URL_RE.finditer(raw_input):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        if candidate and candidate not in seen:
            seen.add(candidate)
            related.append(candidate)

    lines = []
    if primary:
        lines.append("Source URL: " + primary)
    if related:
        lines.append(
            "Related source URLs (original order, deduplicated against Source URL):"
        )
        lines.extend("- " + url for url in related)
    if lines:
        lines.append(
            "Reference only. Do not treat these URLs as delivery destinations or "
            "infer recipients or audiences from them."
        )
    return lines


def _compose_native_schedule_prompt(task, redirect_text: str | None = None) -> str:
    lines = [_handoff_title_line(_get(task, "title"))]
    description = _clean(_get(task, "description"))
    if description:
        lines.append(description)
    lines.extend(_source_reference_lines(task))
    people = schedule_attendees(task)
    if not people:
        raise ValueError(
            "Scheduling requires at least one confirmed attendee with an email."
        )
    lines.append(
        "Selected attendees: " + "; ".join(
            f"{person['name']} <{person['email']}>"
            + (
                f" ({person['timezone']})"
                if person.get("timezone")
                else ""
            )
            for person in people
        )
    )
    notes = _clean(_strip_workiq(_clean(_get(task, "user_notes")))).strip()
    if notes:
        lines.append("User agenda/context: " + notes)
    prefs = meeting_preferences()
    duration_minutes = schedule_duration_minutes(task)
    if prefs:
        defaults = []
        if prefs.get("default_minutes"):
            defaults.append(
                f"default to {prefs['default_minutes']} minutes unless the task "
                "specifies another duration"
            )
        if prefs.get("start_offset_minutes"):
            defaults.append(
                f"start at {prefs['start_offset_minutes']} minutes past the hour "
                "or half hour"
            )
        if prefs.get("notes"):
            defaults.append(prefs["notes"])
        lines.append("Meeting preferences: " + "; ".join(defaults) + ".")
    lines.extend([
        "Use native calendar scheduling flow with confirmed emails; do not use "
        "people profile.",
        "Call FindMeetingTimes for both calendars; check free/busy and work schedules; "
        "offer three exact available times in organizer local time.",
        "If <3 or unavailable, ask_user a text-only clarification.",
        "Do not call CreateEvent before the user selects one; create only the "
        "selected time.",
        "Include the agenda; do not guess timezone.",
        "Each option: "
        '[slot:{"start":"offset ISO","end":"offset ISO","timezone":"Windows zone"}] '
        f'[avail:{{"email":"free"}}]. Times must be {duration_minutes}m; all attendees; '
        "free/tentative only.",
    ])
    correction = _clean(redirect_text)
    if correction:
        lines.append("User correction: " + correction)
    lines.append(
        "Non-negotiable after any correction: requested duration is invariant; never "
        "shorten it; never shift a selected slot. The selected option already includes "
        "the configured start offset: use its start and end markers verbatim. A timing "
        "correction requires fresh FindMeetingTimes and a new selection. CreateEvent "
        "start and end must exactly match the selected slot."
    )
    return "\n\n".join(line for line in lines if line)



# What a given KIND of action has to get right, beyond what the task text says.
#
# Added after walking a real scheduling task through the flow: Cowork checked
# the user's own calendar for six weeks, proposed three slots and wrote the
# message, but never checked the OTHER person's availability and proposed no
# agenda. The draft therefore offered times they might not have free, and gave
# them nothing to prepare against.
#
# Deliberately narrow. Only action types where the generic prompt demonstrably
# produces a worse draft get a block, and each block says what to CHECK and
# what to INCLUDE rather than restating the task.
_ACTION_GUIDANCE = {
    "prepare": (
        "The user is preparing for something that has already been scheduled. "
        "Anchor on what is actually on the calendar: date, attendees and any "
        "material already attached or shared.\n"
        "- Surface what has changed since it was booked, and what decisions the "
        "meeting has to reach.\n"
        "- Draft the preparation itself, not advice about preparing."
    ),
    "review-document": (
        "Read the document before commenting on it. If you cannot open it, say "
        "so rather than reviewing from the file name.\n"
        "- Give specific, located feedback, not general impressions."
    ),
}



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


_HANDOFF_TITLE_MAX = 90
_HANDOFF_TITLE_PREFIX = "Riveter: "


def _handoff_title_line(title) -> str:
    """The single line the Cowork task list will show for this conversation.

    Kept on one line and short, because a task list renders a truncated prefix.
    The "Riveter:" marker is what tells a handed-off task apart from one the user
    started in Cowork themselves.
    """
    text = " ".join((_clean(title) or "").split())
    if not text:
        return _HANDOFF_TITLE_PREFIX.rstrip() + " task"

    budget = _HANDOFF_TITLE_MAX - len(_HANDOFF_TITLE_PREFIX)
    if len(text) > budget:
        text = text[: budget - 1].rstrip() + "\u2026"
    return _HANDOFF_TITLE_PREFIX + text


def compose_prompt(task, destination: dict | None = None,
                   redirect_text: str | None = None,
                   delivery_channel: str | None = None,
                   interaction_mode: str = "interaction") -> str:
    """Assemble the Cowork preview prompt from its layers.

    Layer order is semantic, not cosmetic. The correction is emitted after the
    standing layers so it overrides them, and the safety instruction is emitted
    last of all so that no user-authored layer -- note or correction -- can talk
    the run out of preview mode.

    Args:
        task: mapping or sqlite3.Row of the task's fields.
        destination: result of parse_source_url; derived from the task if omitted.
        redirect_text: one-shot steer supplied via Redo (F12).
        delivery_channel: bound transport ("teams" or "email"). Selects the voice
            register. Anything unrecognised falls back to the neutral voice, so a
            future channel cannot silently inherit email mechanics.

    Returns:
        The full prompt. Callers must write it as UTF-8 -- 23 real tasks contain
        characters cp1252 cannot encode.
    """
    if destination is None:
        destination = parse_source_url(_get(task, "source_url") or None)

    action_type = _clean(_get(task, "action_type")).strip().lower()
    if action_type == "schedule-meeting":
        return _compose_native_schedule_prompt(task, redirect_text)

    parts: list[str] = []

    # The Cowork web app derives a task title by truncating the opening text of
    # the prompt, and /v1/subscribe has no title field to override it. Without
    # this line every handed-off task is listed as "[ROLE] You are helping the
    # user act", so the whole list is unreadable. Emitted above the tagged
    # layers, which are semantic and must keep their order.
    parts.append(_handoff_title_line(_get(task, "title")))

    parts.append(
        "[ROLE]\n"
        "You are helping the user act on one of their tasks. This turn is a "
        "research-and-draft pass: gather the context and prepare the action so "
        "the user can review it. Sending, if it happens at all, happens on a "
        "later turn the user drives."
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
    source_lines.extend(_source_reference_lines(task))
    label = destination.get("audience_label")
    if label:
        source_lines.append(f"Conversation: {label}")
        if destination.get("is_broadcast"):
            source_lines.append(
                f"CAUTION: this is a {label} -- more than one person would see a "
                "reply here. State who the audience is in your findings."
            )
    people = _selected_people_for_prompt(_get(task, "key_people"))
    if people:
        source_lines.append(f"Key people: {people}")
    snippet = _clean(_get(task, "source_snippet"))
    if snippet:
        source_lines.append(f"Original message:\n{snippet}")
    if source_lines:
        parts.append("[SOURCE]\n" + "\n".join(source_lines))

    parts.append("[VOICE]\n" + _voice_for(delivery_channel))

    # What this KIND of action has to get right. After the task and source so it
    # can refer to them, before the correction so the user can still override it.
    guidance = _ACTION_GUIDANCE.get(
        action_type
    )
    if guidance:
        parts.append("[ACTION]\n" + guidance)

    # Standing meeting defaults. Not keyed to action_type on purpose: the
    # [ACTION] block above is, and only 6 of 17 open tasks that read as
    # scheduling are classified that way, which is why the preference was
    # applied inconsistently. Phrased as a condition so it costs two lines on
    # prompts that never propose a meeting. Before the correction, so a Redo
    # ("make it 60 minutes") still overrides it.
    meetings = _meeting_layer()
    if meetings:
        parts.append("[MEETINGS]\n" + meetings)

    correction = _clean(redirect_text)
    if correction:
        parts.append(
            "[CORRECTION]\nThe user reviewed a previous attempt and asked for this "
            "change. It overrides the intent and notes above:\n" + correction
        )

    if interaction_mode == "no_interaction":
        parts.append("[INTERACTION]\n" + _NO_INTERACTION)

    parts.append("[OUTPUT]\n" + _SAFETY)

    return "\n\n".join(parts)


def compose_refine_prompt(
    instruction: str,
    interaction_mode: str = "interaction",
    schedule_duration: int | None = None,
) -> str:
    """The prompt for a FOLLOW-UP turn on an existing conversation.

    Deliberately minimal. The conversation already holds [ROLE], [TASK],
    [SOURCE], [VOICE], [INTENT] and [NOTES] from the first turn, so re-sending
    them wastes tokens and risks stacking conflicting [CORRECTION] blocks on
    top of each other.

    ``_SAFETY`` is restated because it also carries the OUTPUT CONTRACT, not
    just the do-not-send rule. Without it a free-form instruction such as "what
    did Brandon originally write?" gets answered in prose, ``_extract_draft``
    finds no draft block, and the card renders blank with no error.

    The safety text is decoration, not the control: the real barrier is the
    per-request ``toolCallbackConfig``, which ``continue_preview`` re-sends on
    every turn.
    """
    text = _clean(instruction)
    if not text:
        raise ValueError("A refine instruction is required.")
    parts = ["[REFINEMENT]\n" + text]
    if schedule_duration is not None:
        parts.append(
            "[SCHEDULE CORRECTION]\n"
            "Run fresh FindMeetingTimes for organizer and every attendee, then ask "
            "the user to select a new exact slot. Do not reuse or shift the prior "
            f"slot. Preserve the requested duration of {schedule_duration} minutes; "
            "CreateEvent start and end must exactly match the new selection."
        )
    if interaction_mode == "no_interaction":
        parts.append("[INTERACTION]\n" + _NO_INTERACTION)
    parts.append("[OUTPUT]\n" + _SAFETY)
    return "\n\n".join(parts)


def compose_execution_prompt(action: dict) -> str:
    """Bind an approved draft and destination into one narrow action request."""
    draft = (action.get("draft_edited") or action.get("draft") or "").strip()
    destination = (action.get("destination_display") or "").strip()
    destination_ref = (action.get("destination_ref") or "").strip()
    action_type = action.get("action_type") or "general"
    if action_type == "schedule-meeting":
        verb = "Create the meeting"
        content_rule = (
            " Use the exact final draft as the event body so Riveter can verify "
            "the approved meeting content."
        )
    elif action_type == "respond-email" or action.get("delivery_channel") == "email":
        verb = "Send the email"
        content_rule = (
            " Treat the first `Subject:` line as the exact subject and all "
            "remaining text as the exact body. Use SendEmailWithAttachments to "
            "send a new message; do not use ReplyToMessage or ReplyAllToMessage. "
            "Do not add attachments."
        )
    else:
        verb = "Send the Teams message"
        content_rule = ""
    return (
        "[APPROVED ACTION]\n"
        f"{verb} now to {destination} ({destination_ref}).\n\n"
        "[FINAL DRAFT]\n"
        f"{draft}\n\n"
        "Perform exactly this approved action. Do not rewrite the draft, change "
        "the destination, add recipients, or take any other write action. If the "
        "service asks for approval or missing required details, ask the user."
        f"{content_rule}"
    )


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)
_EMAIL_DRAFT_HEADING_RE = re.compile(
    r"^\s*(?:\*\*draft(?: email| reply)?"
    r"(?:\s*(?:\(not sent\)|for review))?\*\*|"
    r"#{1,6}\s+draft(?: email| reply)?"
    r"(?:\s*(?:\(not sent\)|for review))?)"
    r"(?:\s+\([^)]*\))?\s*$",
    re.I,
)
_EMAIL_TO_RE = re.compile(r"^\s*(?:\*\*)?to:(?:\*\*)?\s+(.+?)\s*$", re.I)
_EMAIL_SUBJECT_RE = re.compile(
    r"^\s*(?:\*\*)?subject:(?:\*\*)?\s+(.+?)\s*$", re.I
)

# Wording that introduces a proposed message, as opposed to a quoted excerpt of
# someone else's. Cowork quotes both, and the quoted original is often the longer
# of the two -- so length alone picks the wrong one.
_DRAFT_CUE_RE = re.compile(
    r"\b(draft|nudge|reply|response|proposed|suggest\w*|"
    r"you (?:can|could) (?:send|drop|post|use)|here'?s (?:a|the) (?:short )?"
    r"(?:message|note|reply|nudge|draft))\b",
    re.I,
)

# Chat-framing questions Cowork appends after a draft. The UI's approve control
# answers these, so leaving them in makes the draft look like it needs a reply.
_OFFER_RE = re.compile(
    r"^\s*(?:want me to|shall i|should i|would you like me to|let me know if|"
    r"(?:just\s+)?say the word)\b.*$",
    re.I | re.M,
)

_AUTH_HINT = "cowork auth login"


def _blockquote_blocks(text: str) -> list[tuple[str, bool]]:
    """Return each contiguous blockquote run as ``(body, introduced_by_draft_cue)``.

    The cue is taken from the nearest preceding non-blank, non-quoted line, which
    is where Cowork announces what the quote is ("Here's a short Teams nudge you
    can drop into the chat" vs "Here is what Brandon originally wrote").
    """
    blocks: list[tuple[str, bool]] = []
    current: list[str] = []
    last_prose = ""

    def flush():
        if current:
            body = "\n".join(current).strip()
            if body:
                blocks.append((body, bool(_DRAFT_CUE_RE.search(last_prose))))
            current.clear()

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            body = stripped[1:]
            current.append(body[1:] if body.startswith(" ") else body)
        else:
            flush()
            if stripped:
                last_prose = stripped
    flush()
    return blocks


def _extract_structured_email_draft(text: str) -> tuple[str, str] | None:
    """Extract Cowork's bold To/Subject email block without guessing from prose."""
    lines = text.splitlines()
    for heading_index, line in enumerate(lines):
        if not _EMAIL_DRAFT_HEADING_RE.fullmatch(line):
            continue
        markers = [
            index
            for index in range(heading_index + 1, min(len(lines), heading_index + 7))
            if lines[index].strip()
        ]
        if not markers:
            continue
        first_index = markers[0]
        to_match = _EMAIL_TO_RE.fullmatch(lines[first_index])
        if to_match:
            if len(markers) < 2:
                continue
            subject_index = markers[1]
        else:
            subject_index = first_index
        subject_match = _EMAIL_SUBJECT_RE.fullmatch(lines[subject_index])
        if not subject_match:
            continue

        end_index = next(
            (
                index
                for index in range(subject_index + 1, len(lines))
                if lines[index].strip() == "---"
            ),
            len(lines),
        )
        body = "\n".join(lines[subject_index + 1:end_index]).strip()
        subject = subject_match.group(1).strip()
        if not subject or not body:
            continue

        start_index = heading_index
        previous = heading_index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous >= 0 and lines[previous].strip() == "---":
            start_index = previous

        findings = lines[:start_index]
        if end_index < len(lines):
            findings.extend(lines[end_index + 1:])
        if to_match:
            findings.extend(["", f"Draft recipient: {to_match.group(1).strip()}"])
        draft = f"Subject: {subject}\n\n{body}"
        return draft, "\n".join(findings)
    return None


def _extract_draft(text: str) -> tuple[str | None, str]:
    """Split Cowork's reply into (draft, findings).

    A fenced code block is preferred when present -- it is the least ambiguous
    signal. Otherwise a blockquote introduced by draft wording wins, and only if
    no such cue exists does length decide. Picking purely by length hands back the
    quoted original whenever it is longer than the proposed reply, which is common.

    Returns ``(None, text)`` when nothing draft-shaped is found. Inventing a draft
    would be worse than showing an empty editor.
    """
    fences = _FENCE_RE.findall(text)
    if fences:
        draft = max(fences, key=len).strip()
        return draft, _FENCE_RE.sub("", text)

    structured_email = _extract_structured_email_draft(text)
    if structured_email:
        return structured_email

    blocks = _blockquote_blocks(text)
    if not blocks:
        return None, text

    cued = [b for b, is_cued in blocks if is_cued]
    draft = max(cued, key=len) if cued else max((b for b, _ in blocks), key=len)

    # Remove only the chosen block from the findings, keeping quoted excerpts.
    findings: list[str] = []
    current: list[str] = []
    dropped = False

    def resolve():
        nonlocal dropped
        if not current:
            return
        body = "\n".join(
            l.lstrip()[1:].lstrip(" ") for l in current
        ).strip()
        if not dropped and body == draft:
            dropped = True
        else:
            findings.extend(current)
        current.clear()

    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            current.append(line)
            continue
        resolve()
        findings.append(line)
    resolve()

    return draft, "\n".join(findings)


def _tidy_finding(text: str) -> str:
    """Drop trailing chat framing and collapse the gaps it leaves behind."""
    text = _OFFER_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_cowork_output(stdout: str, stderr: str = "") -> dict:
    """Parse one ``cowork send --json`` result into fields for ``task_actions``.

    Args:
        stdout: raw process stdout.
        stderr: raw process stderr, used to recognise auth failure.

    Returns a dict with a stable key set regardless of outcome:

        terminal_status, duration_seconds, conversation_id,
        finding, draft, tool_trace, error, raw_text

    ``error`` is None on success and a human-readable string otherwise.

    Note: ``tool_trace`` entries are preserved verbatim but carry NO information
    about whether a write actually happened. G1b showed ``ok=True`` on a send tool
    that was intercepted and never executed, so the flag means "the call returned",
    not "the action occurred".

    Nor is ``tool_name`` the key the denylist matches on. G1d recorded an
    intercepted Teams post as ``"Post message"`` — a display label absent from all
    154 names in that probe's config — while ``build_callback_config`` denies the canonical
    ``m365_teams-PostMessage``/``PostMessage``. Never audit denylist coverage by
    diffing these names against it; compare against a fresh tool enumeration
    (G1c) instead.
    """
    result = {
        "terminal_status": None,
        "duration_seconds": None,
        "conversation_id": None,
        "finding": "",
        "draft": None,
        "tool_trace": [],
        "tools": [],
        "barrier": _barrier_verdict([], None, ""),
        "error": None,
        "raw_text": "",
        "cancelled": False,
    }

    if _AUTH_HINT in (stderr or ""):
        result["error"] = (
            "Cowork is not authenticated. Run `cowork auth login` and try again."
        )
        return result

    if not stdout or not stdout.strip():
        detail = (stderr or "").strip()
        result["error"] = (
            f"Cowork produced no output. {detail}" if detail
            else "Cowork produced no output."
        )
        return result

    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        result["error"] = f"Could not parse Cowork output: {exc}"
        return result

    if not isinstance(payload, dict):
        result["error"] = "Cowork output was not a JSON object."
        return result

    result["terminal_status"] = payload.get("terminal_status")
    result["duration_seconds"] = payload.get("duration_seconds")
    result["conversation_id"] = payload.get("conversation_id")

    trace = payload.get("tool_trace") or []
    result["tool_trace"] = [
        {
            "tool_name": t.get("tool_name"),
            "ok": t.get("ok"),
            "duration_seconds": t.get("duration_seconds"),
            "input": t.get("input") or t.get("inp"),
        }
        for t in trace
        if isinstance(t, dict)
    ]

    text = payload.get("text") or ""
    result["raw_text"] = text

    result["tools"] = _canonical_tools(
        payload.get("sse_events"), payload.get("approved_inputs")
    )

    result["barrier"] = _barrier_verdict(
        result["tool_trace"], payload.get("callback_exchanges"), text,
        tools=result["tools"],
    )
    if result["barrier"]["status"] == "BREACHED":
        # Loud on purpose. This is the one condition where a run that looks
        # entirely normal may have performed a real M365 write.
        logging.getLogger(__name__).error(
            "WRITE BARRIER: %s", result["barrier"]["reason"]
        )
        result["error"] = result["barrier"]["reason"]
    elif result["barrier"]["status"] == "held_unconfirmed":
        # Not an error — every write was on the interception list, so the run
        # is not blocked on this. But it must not be silent either: this is the
        # state that would look identical if the tenant gate silently dropped
        # our config (upstream #18550). The PROACTIVE tenant precheck is the
        # real guard; this warning is how a sustained pattern becomes visible.
        logging.getLogger(__name__).warning(
            "WRITE BARRIER: %s", result["barrier"]["reason"]
        )

    draft, findings = _extract_draft(text)
    result["draft"] = _tidy_finding(draft) if draft else None
    result["finding"] = _tidy_finding(findings)

    # A run the user stopped on purpose is not a crash. It keeps whatever text
    # Cowork produced before the stop and reports itself as cancelled, so the
    # card can say "you stopped this" instead of apologising for a failure.
    if result["terminal_status"] == "cancel":
        result["cancelled"] = True
    elif result["terminal_status"] != "ok":
        result["error"] = (
            f"Cowork finished with status "
            f"{result['terminal_status'] or 'unknown'!s}."
        )

    return result


def parse_execution_output(stdout: str, stderr: str = "") -> dict:
    """Parse an unbarriered turn and require positive write-tool evidence."""
    result = parse_cowork_output(stdout, stderr)
    barrier = result.get("barrier") or {}
    if (
        barrier.get("status") == "BREACHED"
        and result.get("error") == barrier.get("reason")
    ):
        result["error"] = None

    tools = result.get("tools") or []
    successful_writes = [
        tool.get("name")
        for tool in tools
        if tool.get("ok") is True
        and _looks_like_write(tool.get("name"))
    ]
    if not tools:
        successful_writes = [
            tool.get("tool_name")
            for tool in result.get("tool_trace") or []
            if tool.get("ok") is True
            and _looks_like_write(tool.get("tool_name"))
        ]
    result["executed_write_tools"] = [
        name for name in successful_writes if name
    ]
    write_attempts = [
        tool.get("name")
        for tool in tools
        if _looks_like_write(tool.get("name"))
    ]
    write_attempts.extend(
        tool.get("tool_name")
        for tool in result.get("tool_trace") or []
        if _looks_like_write(tool.get("tool_name"))
    )
    cancellation_text = " ".join(
        str(result.get(key) or "")
        for key in ("finding", "raw_text")
    )
    result["cancelled"] = False
    if (
        result.get("terminal_status") == "ok"
        and not result.get("error")
        and not any(write_attempts)
        and re.search(r"\b(?:cancelled|canceled)\b", cancellation_text, re.I)
        and re.search(
            r"\b(?:nothing was sent|not sent|did not send|didn't send)\b",
            cancellation_text,
            re.I,
        )
    ):
        result["cancelled"] = True
    result["delivery_confirmed"] = bool(
        result.get("terminal_status") == "ok"
        and result["executed_write_tools"]
        and not result.get("error")
    )
    return result


# ===========================================================================
# Preview subprocess runner
# ===========================================================================
#
# SAFETY MODEL — read before changing anything below.
#
#   --deny-tools ............. NOT a control. G1 sent a real email with it set.
#                              It denies tool *approval requests*; M365 write
#                              tools never raise one. Never reintroduce it.
#   --tool-callback-config ... The only empirically proven control (G1b).
#                              static_results returns a canned string *instead
#                              of* executing the tool.
#
# This is DEFENCE IN DEPTH, not a sandbox. G1c enumerates the write tools, and
# among them `graph-CallGraph` ("Direct Graph POST/PUT/PATCH") is a universal
# bypass, while `host-SetupScheduledPrompt` runs work after this process exits.
# A denylist over an open set can never be proven complete. Preview turns keep
# the barrier; approved execution turns use their own entry point and omit it.
#
# G1f proved callback interception does not apply to the built-in `bash` tool,
# even when both `bash` and its displayed `Bash` label are listed. G1h then
# established that Python urllib in the Cowork shell sandbox could reach
# neither Graph nor the public internet. Keep `bash` listed in case callback
# coverage changes, but do not
# claim that its presence currently blocks shell execution.
#
# Do NOT treat `tool_trace[].ok` as evidence of what happened. It was True in
# both G1 (email sent) and G1b (intercepted) — it records the call, not the
# execution.

import functools
import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from . import workspace_settings
from .workspace_settings import api_transport_enabled

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "logs"
WRITE_TOOLS_PATH = Path(__file__).resolve().parent / "cowork_write_tools.json"

# claude_runner's 300s default would kill a live Cowork session mid-flight.
COWORK_TIMEOUT = 660

# Progress ring size. A p90 preview (224s) emits roughly 100 lines, so this
# keeps a whole typical run while bounding a pathological one.
_PROGRESS_MAX = 200

_BLOCK_MESSAGE = (
    "BLOCKED: TodoIQ preview mode intercepted this call. "
    "Nothing was sent, saved or modified. For THIS turn, report the draft "
    "instead of retrying or reaching for another tool to achieve the same "
    "effect. This is not a standing restriction on the conversation: if the "
    "user later asks you directly to do it, treat that as a new instruction "
    "and follow your normal confirmation process."
)

# Derived, never hand-copied, so the two cannot drift apart. Only the first
# sentence is used: the G1b capture predates the current wording ("The email was
# NOT sent" rather than "Nothing was sent, saved or modified"), and the tail is
# expected to keep evolving. The opening sentence is the stable part the agent
# quotes back verbatim.
_BLOCK_MARKER = _BLOCK_MESSAGE.split(". ")[0] + "."

_CALLBACK_TIMEOUT = 30

# Public constants from cowork_cli/auth/constants.py. The API transport needs no
# CLI process, only these plus the MSAL cache the CLI already maintains — which
# is also the hidden coupling the architect flagged as the biggest risk here.
_API_CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"   # Azure PowerShell
_API_SCOPE = "6ab48b67-cd74-4ad4-81af-5932984589be/access_as_user"
_API_AUTHORITY = "https://login.microsoftonline.com/organizations"

class AlreadyRunning(RuntimeError):
    """A preview is already in flight for this task."""


# label -> {"proc":, "thread":, "result": dict|None}
_runs: dict = {}
_runs_lock = threading.Lock()
_auth_recovery_lock = threading.Lock()
_auth_login_fn = subprocess.run
_island_probe_lock = threading.Lock()
_island_probe_attempted = False
_cached_island_url = None
_ISLAND_PROBE_FN = None


def reset_registry() -> None:
    """Test hook. Drops all tracked runs without touching live processes."""
    global _island_probe_attempted, _cached_island_url
    with _runs_lock:
        _runs.clear()
    with _island_probe_lock:
        _island_probe_attempted = False
        _cached_island_url = None


def _default_island_probe():
    """Resolve CMP routing through the installed Cowork client, if available."""
    try:
        from cowork_cli.auth.manager import AuthManager
        from cowork_cli.config.settings import get_settings
        from cowork_cli.services.session import SessionManager

        settings = get_settings(None)
        return SessionManager(settings, AuthManager(settings)).base_url
    except Exception as exc:
        logger.warning("Could not resolve Cowork island: %s", exc)
        return None


def _island_from_routing_cache():
    """The island URL from the CLI's routing cache, without importing it.

    ``%APPDATA%/cowork/routing_cache.json`` is plain JSON that the CLI keeps
    up to date, and every API spike read it directly. Preferring it removes a
    ``cowork_cli`` import from the critical path, so a broken or moved package
    degrades island resolution instead of taking it out entirely.

    Returns None on every failure; the CLI probe is still the fallback.
    """
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        path = Path(appdata) / "cowork" / "routing_cache.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            return None
        endpoint = ((entries[0] or {}).get("result") or {}).get("endpoint")
        return endpoint or None
    except Exception:  # noqa: BLE001
        logger.debug("routing cache unreadable", exc_info=True)
        return None


def resolve_cowork_island():
    """Probe once and cache the resolved runtime URL, including failed probes.

    Prefers the routing cache file over the CLI probe: same answer, no Python
    import, so the API transport keeps working if ``cowork_cli`` breaks.
    """
    if not cowork_session_enabled():
        return None
    global _island_probe_attempted, _cached_island_url
    with _island_probe_lock:
        if _island_probe_attempted:
            return _cached_island_url
        # An explicitly injected probe is an override and wins outright — that
        # is what the seam is for, and it also stops a test reading the real
        # machine's routing cache.
        if _ISLAND_PROBE_FN is not None:
            try:
                resolved = _ISLAND_PROBE_FN()
            except Exception as exc:
                logger.warning("Cowork island probe failed: %s", exc)
                resolved = None
        else:
            resolved = _island_from_routing_cache()
            if not resolved:
                try:
                    resolved = _default_island_probe()
                except Exception as exc:
                    logger.warning("Cowork island probe failed: %s", exc)
                    resolved = None
        _cached_island_url = resolved or None
        _island_probe_attempted = True
        return _cached_island_url


def get_cached_cowork_island():
    """Return the current cache without waiting for an in-flight network probe."""
    return _cached_island_url


def preview_label(task_id) -> str:
    """LOAD-BEARING. Must not start with 'skill:'.

    claude_runner.py:123 guards `if not label.startswith("skill:"): return`, so
    a 'skill:'-prefixed label would persist this runner's 21KB CLI payload into
    tasks.skill_output and corrupt the Skill Output card.
    """
    return f"cowork:preview:{task_id}"


def execution_label(task_id) -> str:
    """A separate registry slot prevents preview state from masking a send."""
    return f"cowork:execute:{task_id}"


# Transcribed from the Aether server source on 2026-08-10:
#   aether_runtime/src/orchestrator/domain/eval/auth.py
#
# The write barrier is gated on this list. tool_callback.py rejects any tenant
# not in it with a 404 (deliberately not 403, "to avoid leaking eval-tenant
# membership"), after which tool_callback_config is ignored and write tools
# execute for real. Upstream issue #18550 documents exactly that happening on
# the MSA consumer path.
#
# This is a COPY of a list we do not control and cannot query. It can go stale
# the moment upstream edits it, which is why the precheck below is advisory.
EVAL_ALLOWED_TENANTS = frozenset({
    # SYNTHETIC_EVAL_TENANTS
    "258e9af2-1c09-4fbd-9b9c-a1f08bda4697",  # coworkevals
    "a68d3331-d391-490f-8d52-83ae83bc8ec7",  # DeepWorkAgent SEVAL
    "afb89a62-a289-41e3-9947-49839427385d",  # InceptionBench
    # plus tenants with real users
    "72f988bf-86f1-41af-91ab-2d7cd011db47",  # Microsoft (dogfood) — ours
    "e6a916c6-9ab1-40d0-b5e4-07208617ed9e",
    "71d086c2-9cf9-4c75-8a31-bf3d1144111e",
})


def _cowork_whoami():
    """Signed-in identity, via the CLI imported as a library.

    Reuses the CLI's own MSAL token store, so this costs nothing and needs no
    separate auth.
    """
    from cowork_cli.auth.manager import AuthManager
    from cowork_cli.config.settings import get_settings

    return AuthManager(get_settings()).whoami()


_precheck_cache = {"verdict": None, "at": 0.0}
_PRECHECK_TTL = 600  # seconds; identity changes only on re-auth


def tenant_barrier_precheck(_whoami=None, *, use_cache=False):
    """Is the write barrier's precondition still true, before we run anything?

    ``_barrier_verdict`` is reactive: by the time it reports, a write may
    already have happened. This is the proactive half — the server gates
    ``tool_callback_config`` on tenant membership, and the CLI will tell us
    which tenant we are on, so the two can be compared up front.

    Advisory, never blocking, and never raises. Our copy of the allowlist can
    go stale the moment upstream edits it, so treating a mismatch as fatal
    would strand the user over our own bookkeeping. It warns; the reactive
    verdict still backstops the run itself.

    The first call costs ~5.5s (importing the CLI plus an MSAL silent refresh)
    and later ones ~7ms, so callers on a request path pass ``use_cache=True``
    and rely on ``warm_barrier_precheck()`` having run at startup.
    """
    import time as _time

    if use_cache:
        cached = _precheck_cache["verdict"]
        if cached and (_time.time() - _precheck_cache["at"]) < _PRECHECK_TTL:
            return cached

    try:
        who = (_whoami or _cowork_whoami)()
        tenant = getattr(who, "tenant_id", "") or ""
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "unknown",
            "reason": f"Could not resolve the signed-in tenant: {exc}",
            "tenant_id": "",
        }

    if not tenant:
        verdict = {
            "status": "unknown",
            "reason": "Cowork reported no tenant, so the barrier precondition "
                      "could not be checked.",
            "tenant_id": "",
        }
    elif tenant in EVAL_ALLOWED_TENANTS:
        verdict = {
            "status": "ok",
            "reason": "Signed-in tenant is on the eval allowlist the write "
                      "barrier depends on.",
            "tenant_id": tenant,
        }
    else:
        verdict = {
            "status": "AT_RISK",
            "reason": (
                f"Signed-in tenant {tenant} is not on our copy of "
                f"EVAL_ALLOWED_TENANTS. The server may ignore "
                f"tool_callback_config entirely, in which case write tools "
                f"execute for real (see upstream issue 18550). Either the "
                f"allowlist changed upstream or this machine signed in "
                f"elsewhere."
            ),
            "tenant_id": tenant,
        }

    _precheck_cache["verdict"] = verdict
    _precheck_cache["at"] = _time.time()
    return verdict


def warm_barrier_precheck() -> None:
    """Populate the precheck cache off the request path.

    Called in a daemon thread at server startup so the first preview does not
    pay the ~5.5s cold cost. Failure is silently ignored: an unwarmed cache
    only means the next caller computes it.
    """
    if not cowork_session_enabled():
        return
    try:
        tenant_barrier_precheck(use_cache=True)
    except Exception:  # noqa: BLE001
        pass


def _canonical_tools(sse_events, approved_inputs=None):
    """Tool calls with their CANONICAL names, from the `ts`/`tx` SSE events.

    ``tool_trace`` carries display labels: G1d logged an intercepted Teams post
    as "Post message", which matches none of the 154 canonical names in that
    probe's config. The same run's ``sse_events`` carries the real name on the
    ``ts`` (tool start) event, and we were already receiving it and throwing it
    away. From the G1b capture, one call, two names:

        tool_trace  "Send email with attachments"
        ts.tn       "mcp__outlook__SendEmailWithAttachments"

    Correlated on ``tid``, so a start with no exec (a run killed mid-tool, which
    is exactly when we most want to know) still reports with ``ok=None``.
    """
    starts = {}
    order = []
    for ev in sse_events or []:
        if not isinstance(ev, dict):
            continue
        name = ev.get("tn")
        tid = ev.get("tid")
        if not name or not tid:
            continue
        if ev.get("event") == "ts":
            if tid not in starts:
                starts[tid] = {
                    "name": name,
                    "ok": None,
                    "duration_ms": None,
                    "input": ev.get("inp"),
                    "approved_input": (approved_inputs or {}).get(tid),
                }
                order.append(tid)
        elif ev.get("event") == "tx":
            entry = starts.setdefault(
                tid,
                {
                    "name": name,
                    "ok": None,
                    "duration_ms": None,
                    "input": ev.get("inp"),
                    "approved_input": (approved_inputs or {}).get(tid),
                },
            )
            if tid not in order:
                order.append(tid)
            entry["ok"] = ev.get("ok")
            entry["duration_ms"] = ev.get("dur")
    return [starts[t] for t in order]


def _barrier_verdict(tool_trace, callback_exchanges, text="", tools=None):
    """Did the write barrier actually engage on this run?

    Reading the Aether server source (2026-08-10) established that
    ``tool_callback_config`` is an eval-harness mechanism, not a product safety
    feature, and that it is tenant-gated:

        # aether_runtime/src/orchestrator/api/v1/tool_callback.py
        if tenant_id not in EVAL_ALLOWED_TENANTS:
            raise HTTPException(status_code=404, detail="Not found")

    Our barrier holds because we sign in on the Microsoft tenant, which is on
    that allowlist. Upstream issue #18550 documents the same gate silently
    dropping the config on the MSA path, after which real Graph writes ran.

    The failure mode is silent, so we check every run from output we already
    parse.

    The signal is NOT ``callback_exchanges``: that array is empty in the G1b
    capture, which is a Graph-confirmed successful interception. Nor is it
    ``ok`` or ``output`` — both are identical between G1 (really sent) and G1b
    (blocked). What differs is that ``static_results`` feeds the tool our canned
    string, which the agent quotes back. So the marker's presence is the
    evidence, and its absence beside a write tool is the breach.

    Deliberately fails loud: an unrecognised shape reads as BREACHED, because
    the dangerous direction of a wrong answer is claiming safety we lack.

    Per-run, not per-call. Two writes with one interception still reads as held;
    tightening that needs a CLI that populates ``callback_exchanges``, which is
    why that array is honoured when non-empty.
    """
    writes = [t.get("tool_name") for t in tool_trace
              if _looks_like_write(t.get("tool_name"))]
    # Canonical names from sse_events match the denylist exactly, where display
    # labels only reach the verb heuristic. Both are reported so a breach names
    # whichever form the caller will recognise.
    writes += [t.get("name") for t in (tools or [])
               if _looks_like_write(t.get("name"))]
    if not writes:
        return {
            "status": "not_exercised",
            "reason": "No write tool was attempted, so the barrier was not tested.",
            "tools": [],
        }

    lowered = (text or "").lower()
    intercepted = (
        _BLOCK_MARKER in (text or "")
        or any(cue in lowered for cue in _INTERCEPT_CUES)
        or bool([e for e in (callback_exchanges or []) if isinstance(e, dict)])
    )
    if intercepted:
        # Positive evidence outranks any structural inference: if we can SEE the
        # block, it held, whether or not our list happened to name this tool.
        return {
            "status": "held",
            "reason": "A write tool was called and interception was observed.",
            "tools": [],
        }

    # The dangerous case is a write we never ASKED to block: nothing intercepted
    # it and no approval gate stood in its place. Proven live on 2026-08-10 —
    # releasing one tool from `tool_names` removed the barrier outright, and the
    # runtime offered no `ta` approval event to replace it.
    unrequested = [n for n in dict.fromkeys(str(n) for n in writes)
                   if not _we_asked_to_block(n)]
    if unrequested:
        names = ", ".join(unrequested)
        return {
            "status": "BREACHED",
            "reason": (
                f"Write tool ran that we never asked to block: {names}. No "
                f"interception was requested for it, so this action could have "
                f"really happened. Check build_callback_config()."
            ),
            "tools": unrequested,
        }

    # Every write was on the denylist, so the config we sent asked the runtime
    # to intercept all of them; we just have no quotable confirmation. Missing
    # evidence is not evidence of failure, and calling it BREACHED is what
    # trained the alarm to be ignored. The PROACTIVE tenant precheck is the real
    # guarantee here; this is the reactive corroboration.
    return {
        "status": "held_unconfirmed",
        "reason": (
            "Every write tool called was on the interception list, but the "
            "reply did not restate the block. Interception was requested and "
            "no delivery was observed."
        ),
        "tools": [],
    }


def _norm_tool(name):
    return str(name or "").strip().lower()


# Verbs that mutate, as whole TOKENS of the action.
#
# Matched by equality against tokens rather than as substrings. Substring
# matching put "share" inside "sharepoint" and flagged three read-only
# SharePoint tools as unrequested writes, which failed a real preview during
# the Phase 4 soak. Equality also stops "settings" reading as "set".
#
# This is only the BACKSTOP for tools that are not on the denylist; anything we
# actually deny is matched by name across every spelling.
_WRITE_VERB_TOKENS = frozenset({
    "send", "post", "create", "update", "delete", "remove", "add",
    "edit", "write", "upload", "move", "reply", "forward", "schedule",
    "scheduled", "set", "setup", "modify", "insert", "draft", "share",
    "invite", "save", "accept", "decline", "cancel", "flag",
})

# Kept for reference by callers/tests that predate tokenisation.
_WRITE_VERBS = tuple(sorted(_WRITE_VERB_TOKENS))


def _action_segment(name):
    """The action part of a tool name, without the server or product.

    The runtime reports ``mcp__<server>__<Action>``. Only the last segment is
    the action: ``sharepoint_onedrive`` is a product name and must never be
    searched for verbs.
    """
    raw = str(name or "").strip()
    if raw.lower().startswith("mcp__"):
        parts = [p for p in raw[5:].split("__") if p]
        return parts[-1] if parts else raw
    if "-" in raw:
        return raw.split("-", 1)[1]
    return raw


def _tokens(text):
    """camelCase, snake_case and spaced words -> lowercase tokens."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(text or ""))
    return [t.lower() for t in re.split(r"[^A-Za-z0-9]+", spaced) if t]


def _looks_like_write(name):
    norm = _norm_tool(name)
    if not norm:
        return False
    if norm in _container_tools():
        return False
    # Name match across every spelling. The runtime says
    # `mcp__graph__CallGraph` where our denylist says `graph-CallGraph`; a
    # plain string comparison missed 20 of 84 entries, including CallGraph
    # itself — the universal Graph bypass — leaving the canary blind to them.
    if _spellings(name) & _barrier_names():
        return True
    return bool(set(_tokens(_action_segment(name))) & _WRITE_VERB_TOKENS)


# Denied for CONTAINMENT, not because they mutate M365. `Bash` is on the
# denylist so a run cannot shell out and bypass the barrier, and `Skill` is how
# our own prompt loads the voice guides — but neither touches the user's
# mailbox, so neither is evidence that a write was attempted.
#
# Derived rather than hardcoded, from a structural property of the denylist:
# every M365 service tool is namespaced (`outlook-SendEmailWithAttachments`,
# `graph-CallGraph`), while the container-local ones are bare single words
# (`bash`, `create`, `edit`, `skill`, `stop_bash`, `task`, `write_agent`).
# Hardcoding three of the seven let `Skill` slip through and report a research
# run as "a write tool was called" — the same slide 7b693b0 fixed for `Bash`.
#
# The exclusion is on the EXACT bare name, so `Create folder` and
# `mcp__outlook_calendar__CreateEvent` are still writes.
def _container_tools():
    global _container_tools_cache
    if _container_tools_cache is None:
        _container_tools_cache = frozenset(
            _norm_tool(t) for t in load_write_tools() if "-" not in t
        )
    return _container_tools_cache


_container_tools_cache = None


class _ContainerTools:
    """Lazy view of the container-local tool names.

    A plain frozenset at import time would read the denylist file on import;
    this defers it while still supporting ``in`` and iteration, so callers and
    tests can treat ``_CONTAINER_TOOLS`` as the set it has always been.
    """

    def __contains__(self, item):
        return item in _container_tools()

    def __iter__(self):
        return iter(_container_tools())

    def __len__(self):
        return len(_container_tools())

    def __eq__(self, other):
        return set(_container_tools()) == set(other)

    def __repr__(self):
        return repr(_container_tools())


_CONTAINER_TOOLS = _ContainerTools()


def _barrier_names():
    """Every spelling of a denylisted tool, matching build_callback_config().

    Cached: this is now consulted by ``_looks_like_write`` for every tool in
    every trace, and recomputing it meant re-reading the JSON and expanding 84
    entries each time. Unmemoised it took the unit suite past 8 minutes from a
    ~60s baseline. The denylist is a static file, so one build is enough.
    """
    global _barrier_names_cache
    if _barrier_names_cache is None:
        names = set()
        for tool in load_write_tools():
            for alias in _tool_aliases(tool):
                names |= _spellings(alias)
        _barrier_names_cache = names
    return _barrier_names_cache


_barrier_names_cache = None


def _spellings(name):
    """Plausible spellings of an observed tool name.

    Three forms name the same tool and none of them match as raw strings:

        runtime canonical   mcp__outlook__SendEmailWithAttachments
        our config          outlook-SendEmailWithAttachments
        display label       "Send email with attachments"

    G1d logged an intercepted Teams post as "Post message". Collapsing spaces
    and separators reduces all three to one comparable key.
    """
    raw = str(name or "").strip()
    out = {_norm_tool(raw)}
    if raw.lower().startswith("mcp__"):
        parts = [p for p in raw[5:].split("__") if p]
        if parts:
            out.add(_norm_tool(parts[-1]))
            if len(parts) >= 2:
                out.add(_norm_tool("-".join(parts[-2:])))
    if "-" in raw:
        out.add(_norm_tool(raw.split("-", 1)[1]))
    # Space/separator-insensitive key so a display label reaches the same
    # bucket as the canonical name it stands for.
    out |= {re.sub(r"[^a-z0-9]", "", s) for s in set(out)}
    return {s for s in out if s}


def _we_asked_to_block(name):
    """Did we put this tool in ``tool_names`` for this run?"""
    return bool(_spellings(name) & _barrier_names())


# Interception phrased in the model's own words. `static_results` feeds the tool
# our canned string, but the agent restates it rather than quoting it, so the
# literal marker is absent from a genuinely blocked run. A real 2026-08-10
# capture read "the send was blocked before anything went out ... Nothing was
# sent or saved" and matched none of _BLOCK_MARKER.
_INTERCEPT_CUES = (
    "was blocked", "wasn't able to send", "was not able to send",
    "nothing was sent", "not sent", "blocked before", "did not send",
    "didn't send", "preview mode", "intercepted",
)


def load_write_tools() -> list:
    """The G1c-1.21.88 denylist: 83 writes plus one retained query tool.

    Cached — read once per process. It is a file that ships with the code, and
    it is now on the hot path of every write check.
    """
    global _write_tools_cache
    if _write_tools_cache is None:
        _write_tools_cache = json.loads(
            WRITE_TOOLS_PATH.read_text(encoding="utf-8")
        )
    return _write_tools_cache


_write_tools_cache = None


def _tool_aliases(name: str) -> list:
    """Qualified name plus its bare suffix.

    G1b's working config carried both qualified ("outlook-SendEmail") and bare
    ("SendEmail") forms, so we never learned which one the CLI matches on.
    Including both costs nothing and removes the guess.
    """
    out = [name]
    if "-" in name:
        bare = name.split("-", 1)[1]
        if bare and bare not in out:
            out.append(bare)
    return out


def build_callback_config(task_id, log_dir=None) -> Path:
    """Write the per-run interception config and return its path."""
    log_dir = Path(log_dir) if log_dir else LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    names: list = []
    for tool in load_write_tools():
        for alias in _tool_aliases(tool):
            if alias not in names:
                names.append(alias)

    config = {
        "tool_names": names,
        "static_results": {n: _BLOCK_MESSAGE for n in names},
        "callback_hints": {},
        "timeout_seconds": _CALLBACK_TIMEOUT,
    }

    path = log_dir / f"cowork_deny_{task_id}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def write_prompt_file(task_id, prompt: str, log_dir=None) -> Path:
    """Prompts go via file, never argv.

    Two reasons: they contain newlines, and 23 real tasks (1.2%) contain
    characters cp1252 cannot encode. encoding='utf-8' here is not optional.
    """
    log_dir = Path(log_dir) if log_dir else LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"cowork_prompt_{task_id}.txt"
    path.write_text(prompt, encoding="utf-8")
    return path


def build_argv(prompt_path, config_path, refs=None) -> list:
    argv = [
        "cowork",
        "send",
        "--json",
        "--tool-callback-config",
        str(config_path),
        "--prompt-file",
        str(prompt_path),
    ]
    for ref in refs or []:
        argv += ["--ref", ref]
    return argv


def _spawn_default(argv, **kwargs):
    if os.name == "nt":
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
    return subprocess.Popen(argv, **kwargs)


def start_preview(task_id, prompt, refs=None, *, spawn=None, log_dir=None,
                  conversation_id=None, action_id=None,
                  schedule_people=None, schedule_duration=None) -> str:
    """Spawn a preview run and return its label. Non-blocking.

    ``conversation_id`` is optional and, when given, is the id the caller has
    ALREADY persisted so that Stop is addressable while the run is still
    starting up. This is turn 1 regardless: the id says where to address the
    run, not that a conversation is being resumed.
    """
    _require_cowork_session()
    label = preview_label(task_id)

    with _runs_lock:
        entry = _runs.get(label)
        if entry is not None and entry["result"] is None:
            raise AlreadyRunning(label)
        _runs[label] = {
            "proc": None, "thread": None, "result": None, "progress": deque(maxlen=_PROGRESS_MAX),
        }

    config_path = build_callback_config(task_id, log_dir=log_dir)
    prompt_path = write_prompt_file(task_id, prompt, log_dir=log_dir)
    argv = build_argv(prompt_path, config_path, refs)

    # Check the barrier's precondition before spending a minute on a run that
    # might write for real. Advisory: it logs and continues, because our copy of
    # the allowlist can go stale and _barrier_verdict still checks the result.
    pre = tenant_barrier_precheck(use_cache=True)
    if pre["status"] == "AT_RISK":
        logger.error("WRITE BARRIER PRECHECK: %s", pre["reason"])
    elif pre["status"] == "unknown":
        logger.warning("WRITE BARRIER PRECHECK: %s", pre["reason"])

    # Transport choice. Flagged because it REPLACES a working path; the proven
    # subprocess stays in charge unless the flag is explicitly on. Everything
    # above this line — the prompt, the barrier config, the precheck — is
    # transport-independent and runs either way.
    if api_transport_enabled():
        thread = threading.Thread(
            target=_collect_api,
            args=(label, task_id, prompt, config_path,
                  Path(log_dir) if log_dir else LOG_DIR),
            kwargs={
                "conversation_id": conversation_id,
                "is_follow_up": False,
                "action_id": action_id,
                "schedule_people": schedule_people,
                "schedule_duration": schedule_duration,
            },
            daemon=True,
            name=f"cowork-api-{task_id}",
        )
        with _runs_lock:
            _runs[label]["thread"] = thread
        thread.start()
        return label

    spawn = spawn or _spawn_default
    try:
        proc = spawn(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        with _runs_lock:
            _runs[label]["result"] = _failure(f"Failed to launch Cowork: {exc}")
        return label

    thread = threading.Thread(
        target=_collect,
        args=(
            label,
            proc,
            task_id,
            Path(log_dir) if log_dir else LOG_DIR,
            argv,
            spawn,
        ),
        daemon=True,
        name=f"cowork-{task_id}",
    )

    with _runs_lock:
        _runs[label]["proc"] = proc
        _runs[label]["thread"] = thread

    thread.start()
    return label


def continue_preview(
    task_id,
    conversation_id,
    instruction,
    *,
    interaction_mode="interaction",
    log_dir=None,
    action_id=None,
    schedule_people=None,
    schedule_duration=None,
) -> str:
    """Run a FOLLOW-UP turn on an existing Cowork conversation. Non-blocking.

    Separate entry point rather than a flag on ``start_preview``: the divergence
    is only the conversation id and the prompt, and threading a resume flag
    through that function would infect the busiest path in this module.

    Only possible on the API transport. The CLI has no ``--resume`` and its
    stdout carries no conversation id, so a subprocess-produced row has nothing
    to continue from — which is why the UI gates the affordance on
    ``conversation_id`` being present.

    Why it is worth having: a fresh run re-researches M365 from zero, measured
    at 27s to 6 minutes and 69 to 355 credits. A follow-up turn keeps the
    conversation's context and completed in about 30s in a live check.
    """
    _require_cowork_session()
    if not conversation_id:
        raise ValueError("A conversation id is required to continue a preview.")
    prompt = compose_refine_prompt(
        instruction,
        interaction_mode=interaction_mode,
        schedule_duration=schedule_duration,
    )
    label = preview_label(task_id)

    with _runs_lock:
        entry = _runs.get(label)
        if entry is not None and entry["result"] is None:
            raise AlreadyRunning(label)
        _runs[label] = {
            "proc": None, "thread": None, "result": None,
            "progress": deque(maxlen=_PROGRESS_MAX),
        }

    # SAFETY: the barrier travels in the request body PER TURN, so a follow-up
    # turn is only barriered if we build and send the config again.
    config_path = build_callback_config(task_id, log_dir=log_dir)
    write_prompt_file(f"{task_id}_refine", prompt, log_dir=log_dir)

    pre = tenant_barrier_precheck(use_cache=True)
    if pre["status"] == "AT_RISK":
        logger.error("WRITE BARRIER PRECHECK: %s", pre["reason"])
    elif pre["status"] == "unknown":
        logger.warning("WRITE BARRIER PRECHECK: %s", pre["reason"])

    thread = threading.Thread(
        target=_collect_api,
        args=(label, task_id, prompt, config_path,
              Path(log_dir) if log_dir else LOG_DIR),
        kwargs={
            "conversation_id": conversation_id,
            "is_follow_up": True,
            "action_id": action_id,
            "schedule_people": schedule_people,
            "schedule_duration": schedule_duration,
        },
        daemon=True,
        name=f"cowork-refine-{task_id}",
    )
    with _runs_lock:
        _runs[label]["thread"] = thread
    thread.start()
    return label


def start_execution(
    task_id,
    prompt,
    conversation_id,
    *,
    approval_kind=None,
    approved_snapshot=None,
    approved_calendar_event=None,
    action_id=None,
    log_dir=None,
) -> str:
    """Run one explicitly approved, unbarriered API follow-up turn."""
    _require_cowork_execute()
    if not api_transport_enabled():
        raise RuntimeError("Direct actions require the Cowork API transport.")
    if not conversation_id:
        raise ValueError("A conversation id is required to execute an action.")

    label = execution_label(task_id)
    with _runs_lock:
        entry = _runs.get(label)
        if entry is not None and entry["result"] is None:
            raise AlreadyRunning(label)
        _runs[label] = {
            "proc": None,
            "thread": None,
            "result": None,
            "progress": deque(maxlen=_PROGRESS_MAX),
        }

    try:
        write_prompt_file(f"{task_id}_execute", prompt, log_dir=log_dir)
        thread = threading.Thread(
            target=_collect_api,
            args=(
                label,
                task_id,
                prompt,
                None,
                Path(log_dir) if log_dir else LOG_DIR,
            ),
            kwargs={
                "conversation_id": conversation_id,
                "is_follow_up": True,
                "approval_kind": approval_kind,
                "approved_snapshot": dict(approved_snapshot or {}),
                "approved_calendar_event": dict(approved_calendar_event or {}),
                "action_id": action_id,
            },
            daemon=True,
            name=f"cowork-execute-{task_id}",
        )
        with _runs_lock:
            _runs[label]["thread"] = thread
        thread.start()
    except Exception:
        with _runs_lock:
            _runs.pop(label, None)
        raise
    return label


def answer_interaction(conversation_id, invocation_id, answers):
    """Answer an ``aq`` event on the existing live run.

    The preview's original subscriber remains connected while the runtime is in
    ``needs_user_input``. Posting the answer wakes that run; opening a second
    follow-up subscriber would conflict with the registry and duplicate events.

    This mirrors the Cowork web client's ``ask_user_answer`` raw event. Answer
    keys are stringified question indexes and multi-select values are joined
    with newlines.
    """
    _require_cowork_session()
    if not conversation_id:
        raise ValueError("A conversation id is required to answer Cowork.")
    if not invocation_id:
        raise ValueError("An interaction invocation id is required.")
    cleaned = {
        str(key): str(value).strip()
        for key, value in (answers or {}).items()
        if str(key).strip() and str(value).strip()
    }
    if not cleaned:
        raise ValueError("At least one answer is required.")

    token, base, _tenant, _oid = _api_auth_fn()
    body = {
        "conversationId": conversation_id,
        "role": "user",
        "content": [{
            "type": "ask_user_answer",
            "rawEvent": {
                "invocationId": invocation_id,
                "answers": cleaned,
            },
        }],
    }
    client = _api_http_client_fn()
    with client:
        response = client.post(
            f"{base}/v1/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Conversation-ID": conversation_id,
            },
            json=body,
            timeout=15,
        )
    if 400 <= response.status_code < 500:
        raise CoworkAnswerRejected(response.status_code)
    if response.status_code not in (200, 202):
        raise RuntimeError(
            f"POST /v1/messages returned HTTP {response.status_code}"
        )
    return True


class CoworkAnswerRejected(RuntimeError):
    """The runtime definitively rejected an answer without accepting it."""

    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(
            f"POST /v1/messages returned HTTP {status_code}"
        )


def _record_progress(label, raw_line) -> None:
    """Append one CLI stderr line to a run's progress ring, if it is user-facing.

    Called from the stderr reader thread, so it takes the registry lock and
    never raises: a progress hiccup must not affect the run.
    """
    text = _progress_text(raw_line)
    if not text:
        return
    _append_progress(label, text)


def _append_progress(label, text) -> None:
    """Append already-clean progress text to a run's ring.

    The API transport reports structured events rather than CLI stderr, so its
    text needs no `[cowork] streaming - ...` unwrapping. Both transports land in
    the SAME ring, which is what lets the card read progress without knowing
    which transport is running.

    Consecutive duplicates are dropped. A repeated line is not progress, and on
    a real run 22 copies of "Connecting MCP servers" pushed everything
    informative out of a ring that only keeps the tail. A line recurring AFTER
    something else is kept: returning to a phase is real information.
    """
    if not text:
        return
    try:
        with _runs_lock:
            entry = _runs.get(label)
            if entry is None:
                return
            ring = entry["progress"]
            if ring and ring[-1] == text:
                return
            ring.append(text)
    except Exception:  # noqa: BLE001
        logger.debug("could not record progress", exc_info=True)


def get_progress(label) -> list:
    """The recent progress lines for a run, oldest first.

    Bounded by ``_PROGRESS_MAX``; a long run keeps only the tail, which is what
    the card shows anyway.
    """
    with _runs_lock:
        entry = _runs.get(label)
        return list(entry["progress"]) if entry else []


# A single preview cannot plausibly consume a month of credits. A jump beyond
# this means something else moved the per-user counter, almost certainly another
# client signed in as the same person, so we decline to attribute it.
_COST_SANITY_CEILING = 10_000.0


def _cost_get(path):
    """GET a runtime path with the CLI's own session, as a library.

    Same targeted use as the tenant precheck: no transport migration, just the
    authenticated client we already have.
    """
    from cowork_cli.auth.manager import AuthManager
    from cowork_cli.config.settings import get_settings

    settings = get_settings()
    session_mod = __import__(
        "cowork_cli.services.session", fromlist=["SessionManager"]
    )
    session = session_mod.SessionManager(settings, AuthManager(settings))
    return session.sync_get(path)


def cost_snapshot(_get=None):
    """Month-to-date credits consumed by this user, or None.

    `GET /v1/cost` is a read proxy over Neptune's costing API. The value is
    monotonic within a month and does not drift when nothing is running, so the
    difference across a preview is that preview's cost.

    Returns None rather than raising on every failure path. The endpoint has a
    documented kill switch (an ECS flight can disable it, giving 404) and
    returns 503 when Neptune is throttled. Cost is decoration; it must never be
    able to fail a preview.
    """
    get = _get or _cost_get
    try:
        payload = get("/v1/cost").json()
        value = (payload.get("user") or {}).get("consumed")
        return float(value) if isinstance(value, (int, float)) else None
    except Exception:  # noqa: BLE001
        logger.debug("cost snapshot unavailable", exc_info=True)
        return None


def cost_delta(before, after):
    """Credits consumed between two snapshots, or None if not trustworthy.

    Rejects a decrease (the monthly reset at ``resetOn``, or a Neptune
    correction) and an implausible jump. Zero is a real answer, not a missing
    one: a cached turn can legitimately cost nothing.
    """
    if before is None or after is None:
        return None
    delta = after - before
    if delta < 0 or delta > _COST_SANITY_CEILING:
        return None
    return delta


# --- handoff status ---------------------------------------------------------
#
# What happened AFTER "Open in Cowork". Handing a draft over is fire-and-forget
# today: TodoIQ never learns whether Phil acted on it. `GET /v1/tasks` is keyed
# by the SAME composite conversation id our deep link already uses, so we can
# just ask.
#
# Verified read-only against production (2026-08-10): 237 tasks across 5 pages,
# and 17 of our 18 stored conversation ids matched, carrying our own task
# titles. That correlation was ASSUMED in the plan and then tested, because the
# obvious-looking data source in this project has been wrong three times
# (callback_exchanges, creditsMillicents, `tx ok`).
#
# Purely additive: it enriches a card that works without it, so it ships
# unflagged. Same call already made for the cost badge.

_HANDOFF_TTL = 30          # seconds; a dashboard poll must not mean a round trip
_HANDOFF_RETRY = 10        # seconds to wait before retrying after a failed refresh
_HANDOFF_MAX_PAGES = 6     # ~300 tasks; ours sat on page 3 in the real capture
_handoff_cache = {"at": 0.0, "tasks": None, "refreshing": False}
_handoff_lock = threading.Lock()
_handoff_idle = threading.Event()
_handoff_idle.set()

# Cowork is blocked waiting for a human. This is how an approval prompt shows
# up from the outside, and it is the whole reason this is worth reading: TodoIQ
# can say "Cowork needs you" while owning no execute route whatsoever.
_WAITING_STATES = frozenset({"needs_user_input"})


def reset_handoff_cache() -> None:
    """Drop the cached task list. Used by tests and after an explicit refresh.

    Drains any in-flight refresh first. Refreshes became asynchronous, so
    clearing the in-flight guard while a thread was still running let that
    thread land its result in the *next* test's cache. Draining here fixes
    every caller at once rather than one test at a time.
    """
    wait_for_handoff_refresh(timeout=10)
    with _handoff_lock:
        _handoff_cache["at"] = 0.0
        _handoff_cache["tasks"] = None
        _handoff_cache["refreshing"] = False
    _handoff_idle.set()


def wait_for_handoff_refresh(timeout=5) -> bool:
    """Block until no refresh is in flight. For tests and shutdown only."""
    return _handoff_idle.wait(timeout=timeout)


def _refresh_handoff_tasks(get) -> None:
    """Repopulate the cache out of band. Never raises, never blocks a caller.

    A failure must leave the previous answer in place. Wiping the cache on a
    throttled response would turn one bad request into a blank badge on every
    card until the next success.
    """
    try:
        tasks = _fetch_handoff_tasks(get)
    except Exception:  # noqa: BLE001
        logger.debug("handoff refresh failed; keeping previous data", exc_info=True)
        tasks = None

    with _handoff_lock:
        if tasks is not None:
            _handoff_cache["tasks"] = tasks
            _handoff_cache["at"] = time.monotonic()
        else:
            # Back off so a hard-down endpoint is not retried on every click.
            _handoff_cache["at"] = time.monotonic() - _HANDOFF_TTL + _HANDOFF_RETRY
        _handoff_cache["refreshing"] = False
    _handoff_idle.set()


def _start_handoff_refresh(get) -> None:
    """Kick off one refresh, or do nothing if one is already running."""
    with _handoff_lock:
        if _handoff_cache["refreshing"]:
            return
        _handoff_cache["refreshing"] = True
    _handoff_idle.clear()
    threading.Thread(
        target=_refresh_handoff_tasks, args=(get,),
        name="handoff-refresh", daemon=True,
    ).start()


def _fetch_handoff_tasks(get):
    """All visible tasks, following ``nextOffset``. Bounded; never raises."""
    tasks = {}
    offset = None
    for _ in range(_HANDOFF_MAX_PAGES):
        path = "/v1/tasks" + (f"?offset={offset}" if offset else "")
        body = get(path).json()
        if not isinstance(body, dict):
            break
        batch = body.get("tasks")
        if not isinstance(batch, list):
            break
        for entry in batch:
            if isinstance(entry, dict) and entry.get("taskId"):
                tasks[entry["taskId"]] = entry
        offset = body.get("nextOffset")
        if not batch or not offset:
            break
    return tasks


def handoff_status(conversation_id, _get=None):
    """State of a handed-over conversation, or None when it cannot be read.

    Returns ``{"state", "waiting_on_user", "last_activity", "title"}``.

    NEVER blocks on the network. ``GET /v1/tasks`` pages up to six times and
    was measured at ~7s; doing that inside the request path made the first
    task clicked after each TTL expiry stall for seconds while every other
    click answered in ~20ms. Phil reported it as "switching between tasks can
    be very slow", and the stall tracked the cache clock rather than the task.

    This is decoration on a card that is already complete without it, so the
    right trade is stale-but-instant: serve whatever is cached and refresh out
    of band. A cold cache returns None and the badge simply appears on the next
    read.

    Fails soft on every path. A throttled endpoint, an expired token or a shape
    change must degrade to today's behaviour rather than break the card.
    """
    if not cowork_session_enabled():
        return None
    if not conversation_id:
        return None

    get = _get or _cost_get
    try:
        now = time.monotonic()
        with _handoff_lock:
            tasks = _handoff_cache["tasks"]
            stale = now - _handoff_cache["at"] >= _HANDOFF_TTL

        if tasks is None or stale:
            _start_handoff_refresh(get)

        if tasks is None:
            return None

        entry = tasks.get(conversation_id)
        if not entry:
            return None

        state = entry.get("state") or ""
        return {
            "state": state,
            "waiting_on_user": state in _WAITING_STATES,
            "last_activity": entry.get("lastActivity"),
            "title": entry.get("title") or "",
        }
    except Exception:  # noqa: BLE001
        logger.debug("handoff status unavailable", exc_info=True)
        return None


def cost_is_attributable(concurrent_runs) -> bool:
    """Can this run's cost be told apart from anything else's?

    The counter is per USER, not per run, so two overlapping previews make both
    deltas meaningless. We show nothing rather than a wrong number.
    """
    return (concurrent_runs or 0) <= 1


def format_cost(delta) -> str:
    """What a user reads. Empty string when there is nothing to say."""
    if delta is None:
        return ""
    if delta == 0:
        return "no credits"
    if delta >= 1000:
        return f"{delta:,.0f} credits"
    return f"{delta:.1f} credits"


# Replaceable seam, same pattern as _auth_login_fn. `_collect` runs in tests
# hundreds of times, and a real cost snapshot is a ~1s network round trip, so
# leaving this unmocked took the unit suite from 35s to 313s.
_cost_snapshot_fn = cost_snapshot


def _failure(error: str) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": error,
        "auth_failed": False,
    }


def _progress_text(line):
    """A user-facing progress string from one CLI stderr line, or None.

    The CLI emits `[cowork] streaming - 0:44 elapsed - <what it is doing>` while
    a run is in flight (cowork_cli/services/send_progress.py). Everything else
    on stderr is noise for this purpose: the update banner, the `irm ... | iex`
    install hint, tracebacks.
    """
    text = (line or "").strip()
    if not text or not text.startswith("[cowork]"):
        return None
    text = text[len("[cowork]"):].strip()
    # "streaming - 0:44 elapsed - tool: x" -> "tool: x"
    parts = text.split(" - ", 2)
    if len(parts) == 3:
        text = parts[2].strip()
    # Raw tool names are developer-facing. The CLI emits its own human copy
    # alongside them ("Searching your Teams and calendar"), 35 lines against 12
    # in a real run, so dropping these costs nothing and keeps the card
    # readable. Per-tool detail belongs in the completed trace instead.
    if text.startswith("tool:"):
        return None
    return text or None


def _drain_process(proc, timeout, on_stderr_line=None):
    """Run a child to completion, draining both pipes concurrently.

    Returns ``(stdout, stderr, returncode)``. Raises
    ``subprocess.TimeoutExpired`` if the child outlives ``timeout``; the caller
    owns killing it.

    This replaces ``proc.communicate(timeout=...)``, and the load-bearing
    warning that used to sit here still applies: never ``wait()`` then read.
    That deadlocks once the child exceeds the OS pipe buffer, and our payload is
    already 21KB.

    The invariant is preserved because both pipes are drained on their own
    threads *for the whole life of the process*, which is exactly how
    ``communicate()`` is implemented. Waiting is safe when something is already
    consuming. What we gain over ``communicate()`` is that stderr lines are
    visible while the run is still going, instead of all at once at the end.

    ``on_stderr_line`` is called from the reader thread for each raw line. It is
    wrapped so a caller-side bug cannot break a preview.
    """
    out_chunks = []
    err_chunks = []

    def drain(pipe, sink, hook=None):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                if hook is not None:
                    try:
                        hook(line)
                    except Exception:  # noqa: BLE001
                        logger.debug("progress hook raised", exc_info=True)
        except Exception:  # noqa: BLE001
            logger.debug("pipe drain ended early", exc_info=True)

    threads = []
    for pipe, sink, hook in (
        (proc.stdout, out_chunks, None),
        (proc.stderr, err_chunks, on_stderr_line),
    ):
        if pipe is None:
            continue
        t = threading.Thread(
            target=drain, args=(pipe, sink, hook), daemon=True,
            name="cowork-pipe-drain",
        )
        t.start()
        threads.append(t)

    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Readers keep running; the caller kills the child, which closes the
        # pipes and lets them finish. Partial output is still in the sinks.
        raise

    # The child is gone, so these are bounded: the pipes are at EOF.
    for t in threads:
        t.join(timeout=15)

    return "".join(out_chunks), "".join(err_chunks), code


def _collect(label, proc, task_id, log_dir, argv, spawn_fn) -> None:
    """Drain the child to completion. Runs on a worker thread.

    communicate() — never wait() then read(). The naive pattern deadlocks once
    the child exceeds the OS pipe buffer, and the spike output was already 21KB.
    """
    error = None
    stdout = stderr = ""

    # Cost is the difference in the user's month-to-date credit counter across
    # this run. Snapshot here rather than in start_preview: the GET costs about
    # a second and start_preview is on the request path, while the child takes
    # several seconds just to reach "RUN started", so nothing has been spent yet.
    concurrent = _active_run_count()
    cost_before = _cost_snapshot_fn()

    try:
        stdout, stderr, _ = _drain_process(
            proc, COWORK_TIMEOUT, on_stderr_line=lambda ln: _record_progress(label, ln)
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr, _ = _drain_process(proc, 15)
        except Exception:
            pass
        error = f"Cowork timed out after {COWORK_TIMEOUT}s."
    except Exception as exc:
        error = f"Cowork run failed: {exc}"

    stdout = stdout or ""
    stderr = stderr or ""

    # Auth expires silently: exit 1, EMPTY stdout, hint only on stderr.
    auth_failed = _AUTH_HINT in stderr
    auth_error = "Cowork is not authenticated. Run: cowork auth login"
    if auth_failed and error is None:
        with _auth_recovery_lock:
            try:
                login_kwargs = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "timeout": 120,
                }
                if os.name == "nt":
                    login_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
                login = _auth_login_fn(["cowork", "auth", "login"], **login_kwargs)
            except Exception:
                login = None

        if login is not None and login.returncode == 0:
            try:
                retry_proc = spawn_fn(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    encoding="utf-8",
                    errors="replace",
                )
                with _runs_lock:
                    entry = _runs.get(label)
                    if entry is not None:
                        entry["proc"] = retry_proc
                stdout, stderr = retry_proc.communicate(timeout=COWORK_TIMEOUT)
                proc = retry_proc
                stdout = stdout or ""
                stderr = stderr or ""
                auth_failed = _AUTH_HINT in stderr
                error = auth_error if auth_failed else None
            except subprocess.TimeoutExpired:
                retry_proc.kill()
                error = f"Cowork timed out after {COWORK_TIMEOUT}s."
                auth_failed = False
            except Exception as exc:
                error = f"Cowork run failed: {exc}"
                auth_failed = False
        else:
            error = auth_error

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"cowork_preview_{task_id}.log").write_text(
            stderr, encoding="utf-8"
        )
    except Exception as exc:
        logger.debug("Could not write Cowork stderr log: %s", exc)

    # Close the cost measurement before publishing the result. Only claim a
    # number when this run was the only one in flight: the counter is per user,
    # so overlapping previews cannot be told apart.
    cost = None
    if cost_is_attributable(max(concurrent, _active_run_count())):
        cost = cost_delta(cost_before, _cost_snapshot_fn())

    with _runs_lock:
        entry = _runs.get(label)
        if entry is not None:
            entry["result"] = {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "error": error,
                "auth_failed": auth_failed,
                "cost_credits": cost,
            }


# --- API transport: SSE -> the CLI's own stdout document --------------------
#
# The migration is cheap only because NOTHING downstream changes. This turns a
# raw SSE stream into the same JSON the CLI writes to stdout, so
# parse_cowork_output, _barrier_verdict, _canonical_tools, _extract_draft and
# both UIs are untouched. Verified against real captures on 2026-08-10.

_TERMINAL_RUN_STATES = ("fail", "cancel")
_TURN_COMPLETE_RUN_STATES = ("ok", "fail", "cancel")
_SSE_DECOMPRESSED_LIMIT = 512 * 1024
_SSE_BASE64_LIMIT = 4 * ((_SSE_DECOMPRESSED_LIMIT + 2) // 3)


def _decompress_sse_data(data):
    """Decode one bounded gzip/base64 SSE envelope."""
    encoded = data.get("data")
    if not isinstance(encoded, str) or len(encoded) > _SSE_BASE64_LIMIT:
        return None
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(compressed, _SSE_DECOMPRESSED_LIMIT + 1)
        if (
            len(decoded) > _SSE_DECOMPRESSED_LIMIT
            or decoder.unconsumed_tail
            or not decoder.eof
        ):
            return None
        decoded += decoder.flush(_SSE_DECOMPRESSED_LIMIT + 1 - len(decoded))
        if len(decoded) > _SSE_DECOMPRESSED_LIMIT or decoder.unused_data:
            return None
        inner = json.loads(decoded.decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        zlib.error,
    ):
        return None
    return inner if isinstance(inner, dict) else None


def _iter_sse(lines):
    """Yield ``(kind, data)`` from raw SSE lines.

    The kind is on its own ``event:`` line, NOT inside the ``data:`` JSON.
    Reading ``data["event"]`` yields None for every event, which is why an early
    spike appeared to hang for 600s: the terminal-event break never fired.
    """
    kind = ""
    for raw in lines:
        line = (raw or "").rstrip("\r\n")
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            try:
                data = json.loads(line[5:].strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("compressed") is True:
                data = _decompress_sse_data(data)
                if data is None:
                    continue
            yield kind, data


def _api_payload_from_events(events, conversation_id, approved_inputs=None):
    """Build the CLI-equivalent stdout document from ``(kind, data)`` pairs."""
    text_parts = []
    sse_events = []
    starts = {}
    order = []
    terminal = ""

    for kind, data in events:
        if not isinstance(data, dict):
            continue
        if kind == "dx":
            value = data.get("t")
            if isinstance(value, str):
                text_parts.append(value)
        elif kind in ("ts", "tx"):
            # _canonical_tools reads ev["event"], so fold the kind in — the
            # runtime keeps it on a separate wire line.
            sse_events.append({**data, "event": kind})
            tid = data.get("tid")
            name = data.get("tn")
            if tid and name:
                if tid not in starts:
                    starts[tid] = {
                        "tool_name": name,
                        "ok": None,
                        "duration_seconds": None,
                        "input": data.get("inp"),
                    }
                    order.append(tid)
                if kind == "tx":
                    starts[tid]["ok"] = data.get("ok")
                    dur = data.get("dur")
                    if isinstance(dur, (int, float)):
                        starts[tid]["duration_seconds"] = round(dur / 1000.0, 3)
        elif kind == "rl":
            state = data.get("st")
            if state in _TURN_COMPLETE_RUN_STATES:
                terminal = state

    return {
        "terminal_status": terminal,
        "duration_seconds": None,
        "conversation_id": conversation_id,
        "tool_trace": [starts[t] for t in order],
        "text": "".join(text_parts),
        "sse_events": sse_events,
        "approved_inputs": dict(approved_inputs or {}),
        "callback_exchanges": [],
    }


def _parse_aq_interaction(data):
    """Canonical interaction request using the Cowork web client's answer keys."""
    if not isinstance(data, dict):
        return None
    invocation_id = str(data.get("iid") or data.get("invocationId") or "")
    raw_questions = data.get("q", data.get("question"))
    if not invocation_id or not raw_questions:
        return None
    if not isinstance(raw_questions, list):
        raw_questions = [{
            "id": data.get("questionId") or data.get("id") or "q-0",
            "question": str(raw_questions),
        }]

    questions = []
    for index, raw in enumerate(raw_questions):
        if not isinstance(raw, dict):
            raw = {"question": str(raw)}
        question_id = str(index)
        producer_id = str(raw.get("id") or "").strip()
        prompt = str(raw.get("question") or "").strip()
        header = str(raw.get("header") or "").strip()
        image_url = _safe_interaction_image_url(
            raw.get("imageUrl") or raw.get("image")
        )
        options = []
        for option in raw.get("options") or []:
            if isinstance(option, dict):
                value = str(
                    option.get("value") or option.get("label") or ""
                ).strip()
                label = str(option.get("label") or value).strip()
                description = str(option.get("description") or "").strip()
                option_image_url = _safe_interaction_image_url(
                    option.get("imageUrl") or option.get("image")
                )
            else:
                value = label = str(option).strip()
                description = ""
                option_image_url = ""
            if value:
                options.append({
                    "value": value,
                    "label": label,
                    "description": description,
                    "image_url": option_image_url,
                })
        if question_id and (prompt or header or options):
            questions.append({
                "id": question_id,
                "producer_id": producer_id,
                "header": header,
                "question": prompt,
                "options": options,
                "multi_select": bool(raw.get("multiSelect")),
                "image_url": image_url,
            })
    if not questions:
        return None
    return {"invocation_id": invocation_id, "questions": questions}


_AVAILABILITY_MARKER_RE = re.compile(r"\[avail:(\{.*?\})\]", re.I)
_SLOT_MARKER_RE = re.compile(r"\[slot:(\{.*?\})\]", re.I)
_SCHEDULE_OPTION_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?\*{0,2}Option\s+\d+\s*"
    r"(?::|[—–-])\s*(.+?)\*{0,2}\s*$"
)
_FIND_MEETING_TIMES_TOOL = "mcp__outlook_calendar__FindMeetingTimes"
_ATTENDEE_CLARIFICATION_RE = re.compile(
    r"\b(?:attendee|attendees|invitee|invitees|who should attend|which people)\b",
    re.I,
)
_UNKNOWN_TIMEZONE_RE = re.compile(
    r"(?:(?:time\s*zone|timezone).{0,32}(?:unknown|unconfirmed|not visible|"
    r"unavailable|uncertain|unclear|cannot|can't|could not|couldn't)|"
    r"(?:unknown|unconfirmed|not visible|unavailable|uncertain|unclear|cannot|"
    r"can't|could not|couldn't).{0,32}(?:time\s*zone|timezone))",
    re.I,
)


def _attendee_emails(attendees) -> list[str]:
    values = {
        str(person.get("email") or "").strip().lower()
        for person in attendees or []
        if isinstance(person, dict)
    }
    values.discard("")
    return sorted(values)


def _successful_find_meeting_call(events) -> dict | None:
    starts = {}
    successful = None
    for kind, data in events or []:
        if not isinstance(data, dict):
            continue
        tool_id = str(data.get("tid") or "")
        name = str(data.get("tn") or "")
        if kind == "ts" and tool_id and name == _FIND_MEETING_TIMES_TOOL:
            starts[tool_id] = {"name": name, "input": data.get("inp")}
        elif (
            kind == "tx"
            and tool_id in starts
            and name == starts[tool_id]["name"]
            and data.get("ok") is True
        ):
            raw_input = starts[tool_id]["input"]
            try:
                params = (
                    json.loads(raw_input)
                    if isinstance(raw_input, str)
                    else raw_input
                )
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(params, dict):
                continue
            attendees = params.get("attendees")
            if not isinstance(attendees, list):
                continue
            normalized = {
                str(value).strip().lower()
                for value in attendees
                if str(value).strip()
            }
            duration = params.get("duration_minutes")
            if not isinstance(duration, int) or duration < 1:
                continue
            successful = {
                "attendees": normalized,
                "duration_minutes": duration,
                "tool_id": tool_id,
            }
    return successful


def _parse_now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_slot_marker(description, duration_minutes, now):
    match = _SLOT_MARKER_RE.search(description)
    if not match:
        return None
    try:
        slot = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(slot, dict):
        return None
    timezone_name = str(slot.get("timezone") or "").strip()
    try:
        start = datetime.fromisoformat(
            str(slot.get("start") or "").replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(
            str(slot.get("end") or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if (
        not timezone_name
        or not named_timezone_matches(start.isoformat(), timezone_name)
        or not named_timezone_matches(end.isoformat(), timezone_name)
        or start.tzinfo is None
        or end.tzinfo is None
        or start.utcoffset() is None
        or end.utcoffset() is None
        or start <= now.astimezone(start.tzinfo)
        or end <= start
        or (end - start).total_seconds() != duration_minutes * 60
    ):
        return None
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "timezone": timezone_name,
        "instant": start.astimezone(timezone.utc).isoformat(),
    }


def _parse_availability_markers(description):
    availability = {}
    matches = list(_AVAILABILITY_MARKER_RE.finditer(description))
    if not matches:
        return None
    for match in matches:
        try:
            marker = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(marker, dict):
            return None
        for email, status in marker.items():
            normalized_email = str(email).strip().lower()
            normalized_status = str(status).strip().lower()
            if (
                not normalized_email
                or (
                    normalized_email in availability
                    and availability[normalized_email] != normalized_status
                )
            ):
                return None
            availability[normalized_email] = normalized_status
    return availability


def certify_schedule_interaction(
    interaction,
    events,
    attendees,
    *,
    duration_minutes,
    start_offset_minutes=None,
    now=None,
):
    """Certify query-backed, model-derived schedule choices."""
    expected = _attendee_emails(attendees)
    meeting_call = _successful_find_meeting_call(events)
    if (
        not expected
        or not meeting_call
        or meeting_call["attendees"] != set(expected)
        or meeting_call["duration_minutes"] != duration_minutes
    ):
        return None
    now_value = _parse_now(now)
    if now_value is None:
        return None
    if start_offset_minutes is None:
        start_offset_minutes = (
            meeting_preferences() or {}
        ).get("start_offset_minutes", 0)
    if not isinstance(interaction, dict):
        return None
    questions = interaction.get("questions")
    if not isinstance(questions, list) or len(questions) != 1:
        return None
    question = questions[0]
    options = question.get("options") if isinstance(question, dict) else None
    if (
        question.get("multi_select")
        or not isinstance(options, list)
        or not 1 <= len(options) <= 3
    ):
        return None
    visible_text = " ".join(
        str(value or "")
        for value in (
            question.get("header"),
            question.get("question"),
            *(
                value
                for option in options
                for value in (
                    option.get("label"),
                    option.get("value"),
                    option.get("description"),
                )
            ),
        )
    )
    if _UNKNOWN_TIMEZONE_RE.search(visible_text):
        return None
    values = [str(option.get("value") or "").strip() for option in options]
    if any(not value for value in values) or len(set(values)) != len(options):
        return None
    slots = []
    instants = set()
    for option in options:
        description = str(option.get("description") or "")
        normalized = _parse_availability_markers(description)
        if normalized is None:
            return None
        if set(normalized) != set(expected):
            return None
        if any(status not in {"free", "tentative"} for status in normalized.values()):
            return None
        slot = _parse_slot_marker(description, duration_minutes, now_value)
        slot_start = (
            datetime.fromisoformat(slot["start"]) if slot else None
        )
        if (
            not slot
            or slot_start.minute % 30 != start_offset_minutes % 30
            or slot["instant"] in instants
        ):
            return None
        instants.add(slot["instant"])
        slots.append({
            "value": str(option.get("value") or "").strip(),
            "start": slot["start"],
            "end": slot["end"],
            "timezone": slot["timezone"],
            "availability": normalized,
        })
    certified = json.loads(json.dumps(interaction))
    certified["schedule_evidence"] = {
        "valid": True,
        "source": "FindMeetingTimes+interaction",
        "attendees": expected,
        "query_backed": True,
        "duration_minutes": duration_minutes,
        "start_offset_minutes": start_offset_minutes,
        "slots": slots,
    }
    return certified


def schedule_interaction_from_text(
    text,
    tool_trace,
    attendees,
    *,
    duration_minutes,
    start_offset_minutes=None,
    now=None,
):
    """Recover query-backed schedule choices emitted as assistant text."""
    source = str(text or "")
    headings = list(_SCHEDULE_OPTION_RE.finditer(source))
    if not 1 <= len(headings) <= 3:
        return None
    options = []
    for index, heading in enumerate(headings):
        block_end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(source)
        )
        description = source[heading.end():block_end].strip()
        if (
            not _SLOT_MARKER_RE.search(description)
            or _parse_availability_markers(description) is None
        ):
            return None
        options.append({
            "label": heading.group(1).strip(),
            "value": f"slot-{index + 1}",
            "description": description,
        })

    events = []
    if all(
        isinstance(event, (list, tuple)) and len(event) == 2
        for event in tool_trace or []
    ):
        events = list(tool_trace)
    else:
        for index, event in enumerate(tool_trace or []):
            if (
                not isinstance(event, dict)
                or event.get("tool_name") != _FIND_MEETING_TIMES_TOOL
                or event.get("ok") is not True
            ):
                continue
            tool_id = f"text-recovery-{index}"
            events.extend([
                ("ts", {
                    "tid": tool_id,
                    "tn": _FIND_MEETING_TIMES_TOOL,
                    "inp": event.get("input"),
                }),
                ("tx", {
                    "tid": tool_id,
                    "tn": _FIND_MEETING_TIMES_TOOL,
                    "ok": True,
                }),
            ])

    stable_source = json.dumps(options, sort_keys=True, separators=(",", ":"))
    interaction = {
        "invocation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, stable_source)),
        "questions": [{
            "id": "0",
            "producer_id": "",
            "header": "Choose a meeting time",
            "question": "Which available time should I use for the meeting?",
            "multi_select": False,
            "options": options,
        }],
    }
    return certify_schedule_interaction(
        interaction,
        events,
        attendees,
        duration_minutes=duration_minutes,
        start_offset_minutes=start_offset_minutes,
        now=now,
    )


def schedule_text_only_interaction(interaction, attendees):
    fallback = json.loads(json.dumps(interaction))
    evidence = fallback.get("schedule_evidence")
    rejected_values = list(
        evidence.get("rejected_option_values") or []
    ) if isinstance(evidence, dict) else []
    for question in fallback.get("questions") or []:
        rejected_values.extend(
            str(option.get("value") or "").strip()
            for option in question.get("options") or []
            if isinstance(option, dict) and str(option.get("value") or "").strip()
        )
        question["header"] = "Availability needs another check"
        question["question"] = (
            "I could not verify suitable working-hours slots for every attendee. "
            "Tell me what to check or change."
        )
        question["options"] = []
        question["multi_select"] = False
    fallback["schedule_evidence"] = {
        "valid": False,
        "source": "FindMeetingTimes+interaction",
        "attendees": _attendee_emails(attendees),
        "query_backed": False,
        "rejected_option_values": sorted(set(rejected_values)),
    }
    return fallback


def schedule_interaction_is_attendee_clarification(interaction) -> bool:
    """Identify attendee questions so slot certification does not consume them."""
    questions = interaction.get("questions") if isinstance(
        interaction, dict
    ) else None
    if not isinstance(questions, list) or not questions:
        return False
    visible_prompt = " ".join(
        str(value or "")
        for question in questions
        if isinstance(question, dict)
        for value in (question.get("header"), question.get("question"))
    )
    has_slot_marker = any(
        _SLOT_MARKER_RE.search(str(option.get("description") or ""))
        for question in questions
        if isinstance(question, dict)
        for option in question.get("options") or []
        if isinstance(option, dict)
    )
    return bool(
        _ATTENDEE_CLARIFICATION_RE.search(visible_prompt)
        and not has_slot_marker
    )


def schedule_interaction_is_certified(
    interaction,
    attendees,
    duration_minutes=None,
) -> bool:
    evidence = interaction.get("schedule_evidence") if isinstance(
        interaction, dict
    ) else None
    if not isinstance(evidence, dict):
        return False
    slots = evidence.get("slots")
    duration = evidence.get("duration_minutes")
    if (
        evidence.get("valid") is not True
        or evidence.get("source") != "FindMeetingTimes+interaction"
        or evidence.get("query_backed") is not True
        or evidence.get("attendees") != _attendee_emails(attendees)
        or not isinstance(duration, int)
        or (
            duration_minutes is not None
            and duration != duration_minutes
        )
        or not isinstance(slots, list)
        or not 1 <= len(slots) <= 3
    ):
        return False
    now = datetime.now(timezone.utc)
    parsed_slots = [
        _parse_slot_marker(
            "[slot:" + json.dumps({
                "start": slot.get("start"),
                "end": slot.get("end"),
                "timezone": slot.get("timezone"),
            }, separators=(",", ":")) + "]",
            duration,
            now,
        )
        for slot in slots
        if isinstance(slot, dict)
    ]
    start_offset = evidence.get("start_offset_minutes")
    if start_offset is None:
        start_offset = (meeting_preferences() or {}).get("start_offset_minutes")
    if start_offset is None and parsed_slots and parsed_slots[0]:
        start_offset = datetime.fromisoformat(parsed_slots[0]["start"]).minute % 30
    return (
        isinstance(start_offset, int)
        and not isinstance(start_offset, bool)
        and len(parsed_slots) == len(slots)
        and all(parsed_slots)
        and all(
            datetime.fromisoformat(slot["start"]).minute % 30 == start_offset % 30
            for slot in parsed_slots
        )
    )


def schedule_answer_is_safe(
    interaction,
    answers,
    attendees,
    duration_minutes=None,
) -> bool:
    """Allow free-text corrections, but require current evidence for slot choices."""
    questions = interaction.get("questions") if isinstance(interaction, dict) else None
    if not isinstance(questions, list) or not isinstance(answers, dict):
        return False
    selected_option = False
    evidence = interaction.get("schedule_evidence")
    rejected_values = set(
        evidence.get("rejected_option_values") or []
    ) if isinstance(evidence, dict) else set()
    for question in questions:
        answer = str(answers.get(str(question.get("id")), "")).strip()
        if answer in rejected_values:
            return False
        option_values = {
            str(option.get("value") or "").strip()
            for option in question.get("options") or []
            if isinstance(option, dict)
        }
        if answer and answer in option_values:
            selected_option = True
    if schedule_interaction_is_attendee_clarification(interaction):
        return True
    if not selected_option:
        return True
    return schedule_interaction_is_certified(
        interaction,
        attendees,
        duration_minutes,
    )


def schedule_answers_for_recheck(interaction, answers):
    """Keep exact certified choices; turn all other text into a research request."""
    prepared = {}
    questions = {
        str(question.get("id")): question
        for question in interaction.get("questions") or []
        if isinstance(question, dict)
    }
    for question_id, answer in answers.items():
        question = questions.get(str(question_id), {})
        option_values = {
            str(option.get("value") or "").strip()
            for option in question.get("options") or []
            if isinstance(option, dict)
        }
        if answer in option_values:
            prepared[str(question_id)] = answer
        else:
            prepared[str(question_id)] = (
                "Treat this only as a scheduling preference. Re-run "
                "FindMeetingTimes for every confirmed attendee and return "
                "certified choices; do not create an event yet. User request: "
                + answer
            )
    return prepared


def _execution_approval_answer(data, approval_kind):
    """Return the one safe answer for a channel-matched send confirmation."""
    interaction = _parse_aq_interaction(data)
    if not interaction or approval_kind not in {"teams", "email", "calendar"}:
        return None
    questions = interaction["questions"]
    if len(questions) != 1 or questions[0]["multi_select"]:
        return None

    question = questions[0]
    text = " ".join(
        part for part in (question["header"], question["question"]) if part
    ).lower()
    channel_terms = {
        "teams": ("teams", "chat", "message"),
        "email": ("email",),
        "calendar": ("calendar", "event", "invite", "meeting"),
    }
    if not any(term in text for term in channel_terms[approval_kind]):
        return None
    action_terms = {
        "teams": ("send", "post"),
        "email": ("send",),
        "calendar": ("create", "schedule", "send"),
    }
    if not any(term in text for term in action_terms[approval_kind]):
        return None
    forbidden_terms = {
        "teams": ("archive", "create", "delete", "edit", "forward", "remove", "schedule", "update"),
        "email": ("archive", "create", "delete", "edit", "forward", "post", "remove", "schedule", "update"),
        "calendar": ("archive", "delete", "edit", "forward", "post", "remove", "update"),
    }
    if any(re.search(rf"\b{term}\b", text) for term in forbidden_terms[approval_kind]):
        return None

    affirmative = {"approve", "confirm", "create", "schedule", "send", "yes"}
    matches = []
    for option in question["options"]:
        value = option["value"].strip()
        label = option["label"].strip()
        if value.lower() in affirmative or label.lower() in affirmative:
            matches.append(value)
    if len(matches) != 1:
        return None
    return interaction["invocation_id"], {"0": matches[0]}


_AETHER_FOOTERS = {
    "email": (
        '<span style="font-size:11px;color:#666;">Sent by '
        '<a href="https://aka.ms/cowork?cw_source=outlook&amp;'
        'cw_tool=SendEmailWithAttachments">Copilot Cowork</a></span>'
    ),
    "teams": (
        '<span style="font-size:11px;color:#666;">Sent by '
        '<a href="https://aka.ms/cowork?cw_source=teams&amp;'
        'cw_tool=PostMessage">Copilot Cowork</a></span>'
    ),
    "calendar": (
        '<span style="font-size:11px;color:#666;">Sent by '
        '<a href="https://aka.ms/cowork?cw_source=calendar&amp;'
        'cw_tool=CreateEvent">Copilot Cowork</a></span>'
    ),
}


_EMAIL_ATOM_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _normalize_single_email(value):
    """Return one normalized bare email address, or None for unsafe input."""
    if not isinstance(value, str):
        return None
    address = value.strip()
    if (
        not address
        or len(address) > 254
        or any(char.isspace() for char in address)
        or address.count("@") != 1
    ):
        return None
    local, domain = address.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or not domain
        or "." not in domain
        or any(
            not atom or not _EMAIL_ATOM_RE.fullmatch(atom)
            for atom in local.split(".")
        )
    ):
        return None
    labels = domain.split(".")
    if any(
        not label
        or len(label) > 63
        or not _EMAIL_DOMAIN_LABEL_RE.fullmatch(label)
        for label in labels
    ):
        return None
    return address.lower()


def _approved_email_input(reviewed_draft, destination):
    """Build the exact Outlook input from the email draft reviewed in Riveter."""
    draft = str(reviewed_draft or "").strip()
    lines = draft.splitlines()
    if not lines or not lines[0].lower().startswith("subject:"):
        return None
    subject = lines[0].split(":", 1)[1].strip()
    body = "\n".join(lines[1:]).strip()
    destination = _normalize_single_email(destination)
    if not subject or not body or not destination:
        return None
    # Outlook's approval payload is HTML even when the initial proposal was text.
    rendered = html.escape(body, quote=False).replace("\r\n", "\n")
    rendered = rendered.replace("\r", "\n").replace("\n", "<br>")
    return {
        "to": [destination],
        "subject": subject,
        "content_type": "HTML",
        "body": (
            rendered
            + "<br><br><!-- aether-footer -->"
            + _AETHER_FOOTERS["email"]
        ),
    }


def _render_calendar_event_body(reviewed_draft, subject):
    """Render only the approved agenda as deterministic, safe event HTML."""
    lines = [line.strip() for line in str(reviewed_draft or "").splitlines()]
    title = f"**{subject}**"
    try:
        start = lines.index(title)
    except ValueError:
        return None

    agenda_lines = []
    in_agenda = False
    for line in lines[start + 1:]:
        if not line:
            continue
        if line in {"**Agenda**", "**Agenda:**"}:
            in_agenda = True
            continue
        if not in_agenda:
            continue
        if not line.startswith("- "):
            break
        agenda_lines.append(line[2:])
    if not agenda_lines:
        return None

    def render_line(value):
        match = re.fullmatch(r"\*\*(.+?)\*\*(.*)", value)
        if not match:
            return html.escape(value, quote=False)
        return (
            f"<strong>{html.escape(match.group(1), quote=False)}</strong>"
            f"{html.escape(match.group(2), quote=False)}"
        )

    return (
        "<p><strong>Agenda</strong></p><ul>"
        + "".join(f"<li>{render_line(line)}</li>" for line in agenda_lines)
        + "</ul>"
    )


def _calendar_event_identity_matches(actual, expected, *, require_footer=False):
    """Match every calendar field except body content, which approval replaces."""
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    actual = dict(actual)
    expected = dict(expected)
    actual_content_type = str(actual.pop("content_type", "html")).strip().lower()
    expected_content_type = str(expected.pop("content_type", "html")).strip().lower()
    if actual_content_type != expected_content_type or expected_content_type != "html":
        return False
    # Importance is deliberately normalized at approval time, not trusted from
    # the model's proposal or preview trace.
    actual.pop("importance", None)
    expected.pop("importance", None)
    if set(actual) != set(expected):
        return False

    raw_body = actual.pop("body", None)
    expected.pop("body", None)
    if not isinstance(raw_body, str):
        return False
    proposed, marker, footer = raw_body.partition("<!-- aether-footer -->")
    if marker:
        if footer != _AETHER_FOOTERS["calendar"]:
            return False
    elif require_footer:
        return False
    actual_attendees = actual.pop("attendees", None)
    expected_attendees = expected.pop("attendees", None)
    if not isinstance(actual_attendees, list) or not isinstance(
        expected_attendees, list
    ):
        return False
    normalize = lambda values: sorted(str(value).strip().lower() for value in values)
    return (
        normalize(actual_attendees) == normalize(expected_attendees)
        and actual == expected
    )


def _calendar_event_matches(
    actual, expected, *, require_footer=False, reviewed_draft=None
):
    """Verify delivery used the deterministic body built from the reviewed draft."""
    if isinstance(actual, str):
        try:
            actual = json.loads(actual)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(actual, dict):
        return False
    if reviewed_draft is not None and str(
        actual.get("importance", "normal")
    ).strip().lower() != "normal":
        return False
    if not _calendar_event_identity_matches(
        actual,
        expected,
        require_footer=require_footer or reviewed_draft is not None,
    ):
        return False
    raw_body = actual.get("body")
    proposed, _marker, _footer = raw_body.partition("<!-- aether-footer -->")
    if reviewed_draft is None:
        return proposed == str(expected.get("body") or "")
    rendered = _render_calendar_event_body(
        reviewed_draft, str(expected.get("subject") or "")
    )
    return rendered is not None and proposed == rendered + "<br><br>"


def _approved_destination_attendees(destination):
    destination = str(destination or "").strip()
    if not destination:
        return []
    if destination.startswith("["):
        try:
            values = json.loads(destination)
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(values, list):
            return []
        return sorted(str(value).strip().lower() for value in values)
    return [destination.lower()]


def _execution_tool_approval(
    data,
    approval_kind,
    approved_snapshot,
    conversation_id,
    *,
    approved_calendar_event=None,
):
    """Build one approval for the exact write action reviewed in Riveter."""
    if (
        not isinstance(data, dict)
        or approval_kind not in {"teams", "email", "calendar"}
        or not isinstance(approved_snapshot, dict)
        or not conversation_id
    ):
        return None
    approval_id = str(data.get("aid") or "").strip()
    server_name = str(data.get("sn") or "").strip()
    tool_name = str(data.get("tn") or "").strip()
    params = data.get("params")
    if approval_kind == "calendar":
        expected = approved_calendar_event
        if (
            not approval_id
            or server_name.lower() != "outlook_calendar"
            or re.sub(r"[^a-z0-9]", "", tool_name.lower()) != "createevent"
            or not _calendar_event_identity_matches(
                params,
                expected,
                require_footer=True,
            )
            or not calendar_event_is_future(
                expected,
                now=_calendar_now_fn() if _calendar_now_fn is not None else None,
            )
        ):
            return None
        destination = _approved_destination_attendees(
            approved_snapshot.get("destination_ref")
        )
        event_attendees = sorted(
            str(value).strip().lower()
            for value in expected.get("attendees", [])
        )
        if not destination or event_attendees != destination:
            return None
        reviewed_body = _render_calendar_event_body(
            approved_snapshot.get("draft"), str(expected.get("subject") or "")
        )
        if reviewed_body is None:
            return None
        edited_input = dict(expected)
        edited_input["importance"] = "normal"
        edited_input["body"] = (
            reviewed_body
            + "<br><br><!-- aether-footer -->"
            + _AETHER_FOOTERS["calendar"]
        )
        return _tool_approval_payload(
            data,
            approval_id,
            server_name,
            tool_name,
            conversation_id,
            edited_input=edited_input,
        )

    if approval_kind == "email":
        expected = _approved_email_input(
            approved_snapshot.get("draft"),
            approved_snapshot.get("destination_ref"),
        )
        actual_identity = dict(params) if isinstance(params, dict) else {}
        expected_identity = dict(expected) if isinstance(expected, dict) else {}
        actual_body = actual_identity.pop("body", None)
        expected_identity.pop("body", None)
        if (
            not approval_id
            or server_name.lower() != "outlook"
            or re.sub(r"[^a-z0-9]", "", tool_name.lower())
            != "sendemailwithattachments"
            or not isinstance(params, dict)
            or not isinstance(expected, dict)
            or not isinstance(actual_body, str)
            or set(params) != set(expected)
            or actual_identity != expected_identity
        ):
            return None
        return _tool_approval_payload(
            data,
            approval_id,
            server_name,
            tool_name,
            conversation_id,
            edited_input=expected,
        )

    if (
        not approval_id
        or server_name.lower() != "m365_teams"
        or re.sub(r"[^a-z0-9]", "", tool_name.lower()) != "postmessage"
        or not isinstance(params, dict)
    ):
        return None

    destination = str(approved_snapshot.get("destination_ref") or "").strip()
    if not destination or str(params.get("chat_id") or "").strip() != destination:
        return None

    raw_body = str(params.get("body") or "")
    proposed, footer_marker, footer = raw_body.partition("<!-- aether-footer -->")
    expected_footer = _AETHER_FOOTERS["teams"]
    if not footer_marker or footer != expected_footer:
        return None
    allowed_tags = {"<p>", "</p>", "<br>", "<br/>", "<br />"}
    if any(tag.lower() not in allowed_tags for tag in re.findall(r"<[^>]+>", proposed)):
        return None
    proposed = re.sub(r"<br\s*/?>", " ", proposed, flags=re.I)
    proposed = re.sub(r"</?p>", " ", proposed, flags=re.I)
    proposed = html.unescape(proposed)
    proposed = " ".join(proposed.replace("\xa0", " ").split())
    approved = " ".join(
        str(approved_snapshot.get("draft") or "").replace("\xa0", " ").split()
    )
    if not approved or proposed != approved:
        return None

    return _tool_approval_payload(
        data, approval_id, server_name, tool_name, conversation_id
    )


def _tool_approval_payload(
    data,
    approval_id,
    server_name,
    tool_name,
    conversation_id,
    *,
    edited_input=None,
):
    payload = {
        "always_allow": False,
        "approval_id": approval_id,
        "approved": True,
        "conversation_id": conversation_id,
        "edited_input": edited_input,
        "scope": None,
        "server_name": server_name,
        "session_id": conversation_id,
        "tool_name": tool_name,
    }
    approval_context = data.get("ac")
    if approval_context:
        payload["approval_context"] = approval_context
    return payload


def _safe_interaction_image_url(value):
    """Keep explicit HTTPS images; raw HTML and active URL schemes stay inert."""
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme == "https" and parsed.netloc else ""


def read_blocked_question(conversation_id):
    """Read the current pending structured ``aq`` snapshot for one conversation.

    A fresh subscription injects the runtime's authoritative pending-interactive
    snapshot before live events. It is answer-aware and contains at most one
    question, avoiding historical ``aq``/``aa`` reconciliation entirely.
    """
    _require_cowork_session()
    if not conversation_id:
        return None
    pending_question = None
    compatibility_boundary = False
    try:
        token, base, _tenant, _oid = _api_auth_fn()
        url = (
            f"{base}/v1/subscribe?conversationId="
            f"{quote(conversation_id, safe='')}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        }
        deadline = time.monotonic() + 5
        client = _api_http_client_fn()
        with client:
            with client.stream(
                "GET", url, headers=headers, json=None, timeout=5,
            ) as response:
                if response.status_code != 200:
                    logger.warning(
                        "blocked-question subscription failed: HTTP %s",
                        response.status_code,
                    )
                    return None
                kind = ""
                for raw in response.iter_lines():
                    if time.monotonic() >= deadline:
                        break
                    line = (raw or "").rstrip("\r\n")
                    if line.startswith("event:"):
                        kind = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip())
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(data, dict):
                        continue
                    if kind == "aq":
                        parsed = _parse_aq_interaction(data)
                        if data.get("replay") is True:
                            return parsed
                        pending_question = parsed
                    elif kind == "aa":
                        if (
                            pending_question
                            and str(data.get("iid") or "")
                            == pending_question["invocation_id"]
                        ):
                            pending_question = None
                    elif kind == "rpc":
                        compatibility_boundary = True
                        break
                    elif kind == "rl":
                        state = data.get("st")
                        if state in _TERMINAL_RUN_STATES:
                            pending_question = None
                            break
        return pending_question if compatibility_boundary else None
    except Exception:
        logger.warning("could not read blocked Cowork question", exc_info=True)
        return None


def _active_run_count() -> int:
    """How many previews are in flight right now, including this one.

    The credit counter is per user, so overlapping runs cannot be attributed.
    """
    with _runs_lock:
        return sum(1 for e in _runs.values() if e.get("result") is None)


# Injected seam, matching _spawn_default / _cost_snapshot_fn / _auth_login_fn.
# The architect ruled against a CoworkTransport protocol: this codebase already
# has an idiom for swapping implementations, and a function pair is far cheaper
# to delete if the API path disappoints.
_api_run_fn = None
_calendar_now_fn = None


class CoworkAuthExpired(Exception):
    """MSAL could not produce a token silently — the refresh token is gone.

    Distinct from a transport error: the fix is to re-authenticate, not to
    retry. The subprocess path detects the same condition by finding
    `cowork auth login` on stderr.
    """


# HTTP statuses that mean "your token is no good", as opposed to "the service
# is unwell". Re-authenticating on a 500 would burn a device-code prompt on a
# server problem and still fail.
_AUTH_HTTP_CODES = ("401", "403")


def _is_auth_failure(exc) -> bool:
    """Does this exception mean we need to re-authenticate?"""
    if isinstance(exc, CoworkAuthExpired):
        return True
    text = str(exc)
    return any(f"HTTP {code}" in text for code in _AUTH_HTTP_CODES)


def _collect_api(label, task_id, prompt, config_path, log_dir,
                 conversation_id=None, is_follow_up=None,
                 approval_kind=None, approved_snapshot=None,
                 approved_calendar_event=None, action_id=None,
                 schedule_people=None, schedule_duration=None) -> None:
    """Run one preview over the runtime HTTP API. Worker thread.

    Twin of ``_collect``. It MUST publish the same result dict shape, because
    ``parse_cowork_output`` and both UIs read it and neither knows which
    transport produced it.

    Auth recovery mirrors ``_collect`` (L1358-1404): auth expires SILENTLY, so
    one ``cowork auth login`` and exactly one retry. Without this an expired
    token strands a preview, which is the correctness gap that kept the API
    transport off by default.
    """
    concurrent = _active_run_count()
    cost_before = _cost_snapshot_fn()

    error = None
    stdout = ""
    auth_failed = False
    try:
        config = (
            json.loads(Path(config_path).read_text(encoding="utf-8"))
            if config_path is not None
            else None
        )
        runner = _api_run_fn or _api_run_default
        on_progress = lambda text: _append_progress(label, text)  # noqa: E731
        run_kwargs = {
            "conversation_id": conversation_id,
            "is_follow_up": is_follow_up,
        }
        if approval_kind:
            run_kwargs["approval_kind"] = approval_kind
        if approved_snapshot:
            run_kwargs["approved_snapshot"] = approved_snapshot
        if approved_calendar_event:
            run_kwargs["approved_calendar_event"] = approved_calendar_event
        if action_id:
            run_kwargs["action_id"] = action_id
        if schedule_people is not None:
            run_kwargs["schedule_people"] = schedule_people
        if schedule_duration is not None:
            run_kwargs["schedule_duration"] = schedule_duration
        call = functools.partial(runner, **run_kwargs)
        try:
            payload = call(prompt, config, on_progress)
        except Exception as exc:  # noqa: BLE001
            if not _is_auth_failure(exc):
                raise
            payload = _api_reauth_and_retry(call, prompt, config, on_progress)
        stdout = json.dumps(payload)
    except CoworkAuthExpired as exc:
        auth_failed = True
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("api transport run failed", exc_info=True)
        error = f"Cowork run failed: {exc}"

    cost = None
    if cost_is_attributable(max(concurrent, _active_run_count())):
        cost = cost_delta(cost_before, _cost_snapshot_fn())

    with _runs_lock:
        entry = _runs.get(label)
        if entry is not None:
            entry["result"] = {
                "exit_code": 1 if error else 0,
                "stdout": stdout,
                "stderr": "",
                "error": error,
                "auth_failed": auth_failed,
                "cost_credits": cost,
            }


def _api_reauth_and_retry(runner, prompt, config, on_progress):
    """Log in once, then retry the run exactly once.

    Not a loop: a token that is still bad after a successful login is a real
    problem and must surface rather than spin.
    """
    logger.info("API transport: token rejected, attempting re-authentication")
    with _auth_recovery_lock:
        try:
            login_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 120,
            }
            if os.name == "nt":
                login_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            login = _auth_login_fn(["cowork", "auth", "login"], **login_kwargs)
        except Exception:  # noqa: BLE001
            login = None

    if login is None or login.returncode != 0:
        raise CoworkAuthExpired(
            "Cowork is not authenticated. Run `cowork auth login` and try again."
        )

    try:
        return runner(prompt, config, on_progress)
    except Exception as exc:  # noqa: BLE001
        if _is_auth_failure(exc):
            raise CoworkAuthExpired(
                "Cowork is not authenticated. Run `cowork auth login` and try "
                "again."
            ) from exc
        raise


def _api_auth_default():
    """Token, island URL and identity for the API transport.

    Split out of ``_api_run_default`` so the protocol can be tested without a
    network or a signed-in machine. Returns ``(token, base_url, tenant, oid)``.
    """
    import msal

    cfg_dir = Path(os.environ["APPDATA"]) / "cowork"
    cache = msal.SerializableTokenCache()
    cache.deserialize((cfg_dir / "msal_cache.bin").read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        _API_CLIENT_ID, authority=_API_AUTHORITY, token_cache=cache,
    )
    account = app.get_accounts()[0]
    acquired = app.acquire_token_silent([_API_SCOPE], account=account)
    # Silent acquisition returns None (or a dict with no token) when the
    # 90-day refresh token has expired or been revoked. This is the API's
    # version of the subprocess path's "exit 1, empty stdout, hint on stderr",
    # and it must be recognisable so _collect_api can re-authenticate rather
    # than reporting a generic failure.
    if not acquired or "access_token" not in acquired:
        raise CoworkAuthExpired("MSAL could not acquire a token silently")

    base = resolve_cowork_island() or get_cached_cowork_island()
    # "<oid>.<tenant>" must be split and REVERSED into "<tenant>:<oid>:<uuid>".
    # account["realm"] is the string "organizations", not the tenant guid;
    # using it cost a 403 TENANT_MISMATCH.
    oid, tenant = account["home_account_id"].split(".", 1)
    return acquired["access_token"], base, tenant, oid


def _api_http_client_default():
    import httpx

    return httpx.Client(timeout=httpx.Timeout(COWORK_TIMEOUT, connect=20.0))


# Injected seams, same idiom as _spawn_default / _cost_snapshot_fn.
_api_auth_fn = _api_auth_default
_api_http_client_fn = _api_http_client_default


def new_conversation_id(_auth=None):
    """Mint an addressable conversation id BEFORE the run starts, or None.

    The id is minted client side and merely echoed back by the runtime, so
    there is no reason to wait for a run to finish before knowing it. Waiting
    is what made Stop unusable for the first ~30s of every run: cancellation
    targets POST /v1/conversations/{id}/pause, and until the row had an id
    there was nothing to address.

    Fails soft. If auth is unavailable the run itself is about to fail anyway,
    and returning None simply restores the previous behaviour of minting inside
    the run.
    """
    auth = _auth or _api_auth_fn
    try:
        _token, _base, tenant, oid = auth()
    except Exception:  # noqa: BLE001
        logger.debug("could not mint a conversation id up front", exc_info=True)
        return None
    return f"{tenant}:{oid}:{uuid.uuid4()}"


def _api_run_default(prompt, config, on_progress, conversation_id=None,
                     is_follow_up=None, approval_kind=None,
                     approved_snapshot=None, approved_calendar_event=None,
                     action_id=None, schedule_people=None,
                     schedule_duration=None):
    """Run one turn over the runtime HTTP API and fold the SSE stream into a
    CLI-shaped document.

    THE REQUEST SHAPE DIFFERS BY TURN. There is no published spec for this SSE
    protocol, but the `cowork` CLI is another client of the same API and its
    source documents the sequence (cowork_cli/services/live_session.py:213):

        turn 1      POST /v1/subscribe     prompt rides the subscribe body
        follow-up   GET  /v1/subscribe     re-resolves pod locality, opens SSE
                    POST /v1/messages      delivers the prompt

    Re-POSTing /v1/subscribe on an EXISTING conversation is not the sanctioned
    path. It happened to work while the actor pod was still warm and then failed
    on task 2268: HTTP 200, stream closed 1.1s later with zero events, so there
    was no terminal event and the run reported "status unknown".

    ``toolCallbackConfig`` rides on BOTH shapes, so the write barrier is sent
    per turn either way.
    """
    token, base, tenant, oid = _api_auth_fn()
    # The turn kind is now EXPLICIT rather than inferred from "did we get an
    # id". It used to be inferred, which meant an id could only ever be
    # supplied for a follow-up, which in turn meant turn 1 could not be given
    # an id up front -- and that is what left Stop with nothing to address for
    # the first ~30s of a run. Callers that do not say fall back to the old
    # inference, so nothing that predates this changes behaviour.
    if is_follow_up is None:
        is_follow_up = bool(conversation_id)
    # Full UUID rather than the CLI's `cw-<8 hex>`, matching the format the
    # Cowork web app mints for its own tasks. Both are accepted by the runtime.
    #
    # This was changed while chasing an HTTP 403 from the web app and does NOT
    # fix it: the 403 reproduces with a full UUID too. Kept only because
    # matching the web app's format removes one confounder. Do not read this as
    # a working handoff.
    conversation_id = conversation_id or f"{tenant}:{oid}:{uuid.uuid4()}"

    body = {
        "conversationId": conversation_id,
        "role": "user",
        "content": [{"type": "text", "text": prompt}],
    }
    if config is not None:
        body["toolCallbackConfig"] = config
    sse_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    post_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    events = []
    approved_inputs = {}
    client = _api_http_client_fn()
    with client:
        verb = "GET" if is_follow_up else "POST"
        stream_body = None if is_follow_up else body
        # GET /v1/subscribe takes the conversation as a QUERY PARAMETER
        # (aether subscribe.py: `conversation_id: ... Query(alias="conversationId")`).
        # Omitting it is a 400, which is what task 2268's refine hit. The POST
        # form carries it in the body instead.
        stream_url = f"{base}/v1/subscribe"
        if is_follow_up:
            stream_url += f"?conversationId={quote(conversation_id, safe='')}"
        with client.stream(
            verb, stream_url, headers=sse_headers, json=stream_body,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"{verb} /v1/subscribe failed: HTTP {response.status_code}"
                )
            if is_follow_up:
                # The stream is open; now deliver this turn's message on it.
                posted = client.post(
                    f"{base}/v1/messages", headers=post_headers, json=body,
                )
                if posted.status_code not in (200, 202):
                    raise RuntimeError(
                        f"POST /v1/messages returned HTTP {posted.status_code}"
                    )
            ask_user_answered = False
            tool_approval_answered = False
            schedule_correction_answered = False
            for kind, data in _iter_sse(response.iter_lines()):
                events.append((kind, data))
                text = _api_progress_text(kind, data)
                if text:
                    on_progress(text)
                if kind == "aq":
                    approval = None
                    if approval_kind:
                        approval = (
                            None
                            if ask_user_answered
                            else _execution_approval_answer(data, approval_kind)
                        )
                    if approval is not None:
                        invocation_id, answers = approval
                        answer_body = {
                            "conversationId": conversation_id,
                            "role": "user",
                            "content": [{
                                "type": "ask_user_answer",
                                "rawEvent": {
                                    "invocationId": invocation_id,
                                    "answers": answers,
                                },
                            }],
                        }
                        answered = client.post(
                            f"{base}/v1/messages",
                            headers={
                                **post_headers,
                                "X-Conversation-ID": conversation_id,
                            },
                            json=answer_body,
                            timeout=15,
                        )
                        if answered.status_code not in (200, 202):
                            raise RuntimeError(
                                "Cowork rejected the approved action "
                                f"confirmation: HTTP {answered.status_code}"
                            )
                        ask_user_answered = True
                        logger.info(
                            "answered %s execution approval for conversation %s",
                            approval_kind,
                            conversation_id,
                        )
                    elif action_id:
                        from ..models import set_blocked_question_if_missing

                        interaction = _parse_aq_interaction(data)
                        if not interaction:
                            continue
                        if (
                            schedule_people
                            and not schedule_interaction_is_attendee_clarification(
                                interaction
                            )
                        ):
                            schedule_start_offset = (
                                meeting_preferences() or {}
                            ).get("start_offset_minutes", 0)
                            certified = certify_schedule_interaction(
                                interaction,
                                events,
                                schedule_people,
                                duration_minutes=schedule_duration,
                                start_offset_minutes=schedule_start_offset,
                            )
                            if certified:
                                interaction = certified
                            elif not schedule_correction_answered:
                                correction = (
                                    "Recheck with FindMeetingTimes using every confirmed "
                                    "attendee email. Offer exactly three returned slots "
                                    f"that start {schedule_start_offset} minutes after "
                                    "the hour or half-hour, with offset-aware [slot] "
                                    "metadata and complete "
                                    "free/tentative [avail] evidence, or ask a text-only "
                                    "clarification. Do not say a timezone is unknown."
                                )
                                answer_body = {
                                    "conversationId": conversation_id,
                                    "role": "user",
                                    "content": [{
                                        "type": "ask_user_answer",
                                        "rawEvent": {
                                            "invocationId": interaction["invocation_id"],
                                            "answers": {
                                                question["id"]: correction
                                                for question in interaction["questions"]
                                            },
                                        },
                                    }],
                                }
                                answered = client.post(
                                    f"{base}/v1/messages",
                                    headers={
                                        **post_headers,
                                        "X-Conversation-ID": conversation_id,
                                    },
                                    json=answer_body,
                                    timeout=15,
                                )
                                if answered.status_code not in (200, 202):
                                    raise RuntimeError(
                                        "Cowork rejected the scheduling correction: "
                                        f"HTTP {answered.status_code}"
                                    )
                                schedule_correction_answered = True
                                logger.info(
                                    "requested one scheduling evidence correction for %s",
                                    conversation_id,
                                )
                                continue
                            else:
                                interaction = schedule_text_only_interaction(
                                    interaction, schedule_people
                                )
                        encoded = json.dumps(interaction, separators=(",", ":"))
                        stored = set_blocked_question_if_missing(action_id, encoded)
                        if stored:
                            logger.info(
                                "surfaced execution question for conversation %s",
                                conversation_id,
                            )
                if kind == "ta" and approval_kind and not tool_approval_answered:
                    approval = _execution_tool_approval(
                        data,
                        approval_kind,
                        approved_snapshot,
                        conversation_id,
                        approved_calendar_event=approved_calendar_event,
                    )
                    if not approval:
                        params = data.get("params")
                        logger.warning(
                            "Rejected Cowork tool action that did not match approval: %s",
                            {
                                "approval_id": data.get("aid"),
                                "server_name": data.get("sn"),
                                "tool_name": data.get("tn"),
                                "parameter_keys": (
                                    sorted(params) if isinstance(params, dict) else None
                                ),
                                "parameter_type": type(params).__name__,
                            },
                        )
                        raise RuntimeError(
                            "Cowork requested a tool action that did not exactly "
                            "match the action approved in Riveter. Nothing was "
                            "approved."
                        )
                    answered = client.post(
                        f"{base}/v1/tool-approval",
                        headers={
                            **post_headers,
                            "X-Conversation-ID": conversation_id,
                        },
                        json=approval,
                        timeout=15,
                    )
                    if answered.status_code not in (200, 202):
                        raise RuntimeError(
                            "Cowork rejected the approved tool action: "
                            f"HTTP {answered.status_code}"
                        )
                    edited_input = approval.get("edited_input")
                    if isinstance(edited_input, dict):
                        tool_event_id = data.get("tid")
                        if not tool_event_id:
                            for event_kind, event_data in reversed(events):
                                if (
                                    event_kind == "ts"
                                    and _spellings(event_data.get("tn"))
                                    & _spellings(data.get("tn"))
                                ):
                                    tool_event_id = event_data.get("tid")
                                    break
                        if tool_event_id:
                            approved_inputs[str(tool_event_id)] = edited_input
                    tool_approval_answered = True
                    logger.info(
                        "answered %s tool approval for conversation %s",
                        approval_kind,
                        conversation_id,
                    )
                if kind == "rl" and data.get("st") in _TURN_COMPLETE_RUN_STATES:
                    break

    if not events:
        # 200 then silence. Distinct from "the run failed" and from "the run
        # finished oddly", and the user can act on it: try again, or start
        # fresh. Reporting it as "status unknown" told them nothing.
        raise RuntimeError(
            "Cowork accepted the request but sent no events. The conversation "
            "may have expired; try again, or use Start over."
        )

    return _api_payload_from_events(
        events, conversation_id, approved_inputs=approved_inputs
    )


def _api_progress_text(kind, data):
    """One user-facing progress line from an SSE event, or None.

    Mirrors the CLI's own mapping (cowork_cli/services/send_progress.py:102).
    The important one is ``ps``, which carries the readable sentence such as
    "Searching your Teams and calendar". Reading only ``tk`` showed container
    plumbing on a loop: a real run reached 4 minutes with 22 of its 25 progress
    lines being the identical string "Connecting MCP servers".

    Raw tool names are deliberately NOT surfaced, matching ``_progress_text`` on
    the subprocess path: they are developer-facing and the runtime emits its own
    human copy alongside them.
    """
    if not isinstance(data, dict):
        return None
    if kind == "ps":
        msg = str(data.get("msg") or "").strip()
        return msg[:80] if msg else None
    if kind == "th":
        return "Thinking"
    if kind == "dx":
        return "Writing the reply"
    if kind == "fr":
        return "Finalizing"
    if kind == "tk":
        items = data.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                text = item.get("af") or item.get("desc")
                if text:
                    return str(text).strip()[:80]
    return None


def _api_post(path, body):
    """POST to the runtime with the CLI's own authenticated session."""
    from cowork_cli.auth.manager import AuthManager
    from cowork_cli.config.settings import get_settings

    settings = get_settings()
    session_mod = __import__(
        "cowork_cli.services.session", fromlist=["SessionManager"]
    )
    session = session_mod.SessionManager(settings, AuthManager(settings))
    return session.sync_post(path, body)


def cancel_run(conversation_id, _post=None) -> bool:
    """Stop a run that is in flight. Returns whether the runtime accepted it.

    THIS IS THE CAPABILITY THE SUBPROCESS PATH DOES NOT HAVE.

    ``proc.kill()`` only kills our local process: the server-side run carries on
    and keeps spending credits. The cowork_cli library path was worse — a spike
    proved ``close_live()`` does not halt a turn at all (still running at 50s),
    which is why that migration was closed.

    Verified live on 2026-08-10: `pause` returned 200 ``success: true``, the SSE
    stream produced ``rl st=cancel`` 0.9s later, and the run was fully stopped
    3.0s after the request.

    ``mode: hard`` per aether control.py: interrupt immediately and cancel
    in-flight LLM and tool calls. ``soft`` would wait for the current turn,
    which is not what a user pressing Stop means.

    Never raises. A cancel that cannot be delivered reports False so the caller
    can say so, rather than leaving the card claiming it stopped something.
    """
    if not cowork_session_enabled() or not conversation_id:
        return False
    post = _post or _api_post
    try:
        response = post(
            f"/v1/conversations/{conversation_id}/pause",
            {"mode": "hard", "reason": "Stopped from TodoIQ"},
        )
        if getattr(response, "status_code", None) != 200:
            logger.warning(
                "cancel refused: HTTP %s", getattr(response, "status_code", "?")
            )
            return False
        # 200 is not the same as "it stopped" — the body carries the verdict.
        return bool((response.json() or {}).get("success"))
    except Exception:  # noqa: BLE001
        logger.debug("cancel failed", exc_info=True)
        return False


def is_running(label) -> bool:
    with _runs_lock:
        entry = _runs.get(label)
        return entry is not None and entry["result"] is None


def get_result(label):
    with _runs_lock:
        entry = _runs.get(label)
        return entry["result"] if entry else None


def wait_for(label, timeout=None):
    """Block until a run finishes. For tests and startup recovery."""
    with _runs_lock:
        entry = _runs.get(label)
    if entry is None:
        return None
    thread = entry.get("thread")
    if thread is not None:
        thread.join(timeout if timeout is not None else COWORK_TIMEOUT + 30)
    return get_result(label)


def active_labels() -> list:
    with _runs_lock:
        return [k for k, v in _runs.items() if v["result"] is None]

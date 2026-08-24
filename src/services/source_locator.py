"""Where a task came from, in a form that can be re-opened.

`tasks.source_id` looks like a locator and is not one: it is a dedup key,
`{type}::{person}::{subject_first_50}` (.claude/commands/todo-refresh.md:102-110).
Two different threads about the same subject collide by design, and nothing can
be re-opened from it. The only re-usable identifier the app stores is
`source_url`, captured opportunistically.

This module gives that identifier a shape, so a check can ask "can I re-read
where this came from?" and get an answer instead of re-deriving a regex at each
call site.

What it deliberately does NOT do:

- It does not map an Outlook URL to a Graph message id. Whether that mapping is
  deterministic is an open spike, so `internet_message_id` is reserved and left
  null. A null says "not established"; a populated guess would say "this is
  where the mail is", which nobody has shown.
- It does not turn a meeting URL into a Calendar event id. A meeting link gives
  the meeting CHAT thread, not the event, so `is_thread_readable` reports False
  for a meeting even though a conversation id is present.
- It does not replace `parse_source_url` in the delivery paths
  (`cowork_runner.py`, `structured_delivery.py`). Those decide broadcast
  audience, are heavily tested, and changing them is a separate refactor with
  its own parity audit. This wraps that function; it does not displace it.
"""

import json

from .cowork_runner import parse_source_url

SCHEMA_VERSION = 1

KIND_TEAMS_CHAT = "teams_chat"
KIND_TEAMS_CHANNEL = "teams_channel"
KIND_MEETING = "meeting"
KIND_EMAIL = "email"

_KINDS = {KIND_TEAMS_CHAT, KIND_TEAMS_CHANNEL, KIND_MEETING, KIND_EMAIL}

# Who put this here. "captured" means the identifier was recorded when the task
# was created; "derived_from_url" means it was recovered afterwards from a link
# we happened to keep. Only build() and normalise() set it - a caller cannot
# hand in "captured" and be believed, or the field would just record what the
# writer wished were true.
SOURCE_CAPTURED = "captured"
SOURCE_DERIVED = "derived_from_url"

_KEYS = (
    "version", "kind", "conversation_id", "message_id", "team_id",
    "channel_id", "internet_message_id", "event_id", "source",
)

# parse_source_url speaks in delivery-audience terms (is this a broadcast?).
# Here the question is different: what can be re-read?
_KIND_FROM_PARSE = {
    "one_to_one": KIND_TEAMS_CHAT,
    "group": KIND_TEAMS_CHAT,
    "channel": KIND_TEAMS_CHANNEL,
    "meeting": KIND_MEETING,
}


def _blank(kind, source):
    return {
        "version": SCHEMA_VERSION,
        "kind": kind,
        "conversation_id": None,
        "message_id": None,
        "team_id": None,
        "channel_id": None,
        # Reserved, never populated: the mappings behind these are open spikes.
        "internet_message_id": None,
        "event_id": None,
        "source": source,
    }


def from_source_url(url):
    """Recover a locator from a stored link, or None if there isn't one."""
    parsed = parse_source_url(url)
    kind = _KIND_FROM_PARSE.get(parsed["kind"])
    if kind is None:
        return None
    located = _blank(kind, SOURCE_DERIVED)
    located["conversation_id"] = parsed["conversation_id"]
    located["message_id"] = parsed["message_id"]
    return _validated(located)


def _validated(located):
    """Refuse a record that cannot actually locate anything.

    A locator whose identifying field is missing is worse than no locator: a
    caller checking "is there a locator?" would believe the origin is
    re-openable when it is not.
    """
    kind = located["kind"]
    if kind == KIND_TEAMS_CHAT and not located["conversation_id"]:
        return None
    if kind == KIND_TEAMS_CHANNEL and not (
        located["team_id"] and located["channel_id"] and located["message_id"]
    ):
        return None
    if kind == KIND_MEETING and not (
        located["conversation_id"] or located["event_id"]
    ):
        return None
    if kind == KIND_EMAIL and not located["internet_message_id"]:
        return None
    return located


def normalise(raw):
    """Return a v1 locator for any stored value, or None. Never raises."""
    if raw is None:
        return None
    data = raw
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(data, dict):
        return None

    kind = data.get("kind")
    if kind not in _KINDS:
        return None

    source = (
        SOURCE_CAPTURED if data.get("source") == SOURCE_CAPTURED
        else SOURCE_DERIVED
    )
    located = _blank(kind, source)
    for key in ("conversation_id", "message_id", "team_id", "channel_id"):
        value = data.get(key)
        located[key] = value if isinstance(value, str) and value.strip() else None
    return _validated(located)


def is_thread_readable(located):
    """Whether the originating thread can actually be re-read.

    A meeting is excluded on purpose: the link yields the meeting chat, and the
    event-id mapping is unresolved, so promising a re-read would overstate what
    we can do.
    """
    if not located:
        return False
    if located["kind"] == KIND_TEAMS_CHAT:
        return bool(located["conversation_id"])
    if located["kind"] == KIND_TEAMS_CHANNEL:
        return bool(
            located["team_id"] and located["channel_id"] and located["message_id"]
        )
    return False


def to_json(located):
    return json.dumps(located) if located else None

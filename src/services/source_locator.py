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

- It does not replace `parse_source_url` in the delivery paths
  (`cowork_runner.py`, `structured_delivery.py`). Those decide broadcast
  audience, are heavily tested, and changing them is a separate refactor with
  its own parity audit. This wraps that function; it does not displace it.

Email and meeting identifiers were originally reserved and left null, on the
grounds that neither mapping was established. Probing live Graph through WorkIQ
on 2026-08-24 established both, so the nulls became a false limitation rather
than an honest one:

    Outlook `?ItemID=` IS a Graph message id.
      GET /me/messages/{ItemID}                       -> 200, has conversationId
      GET /me/messages?$filter=conversationId eq '..' -> 200, the whole thread
      That filter must NOT be combined with $orderby: Graph rejects the pair
      with InefficientFilter.

    Teams `/l/meeting/details?eventId=` IS a Graph event id.
      GET /me/events/{eventId}          -> 200, has onlineMeeting.joinUrl
      joinUrl embeds 19:meeting_...@thread.v2
      GET /me/chats/{that id}/messages  -> 200, real messages

87 Outlook and 300 meeting URLs in the live database carry one of these and had
been discarded. `read_plan` keeps those proven sequences in one place, because
the repeated lesson here is that a worker shown no endpoint invents none.
"""

import json
from urllib.parse import parse_qs, unquote, urlparse

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


def _query_param(url, name):
    try:
        values = parse_qs(urlparse(url).query).get(name)
    except (ValueError, AttributeError):
        return None
    if not values:
        return None
    value = unquote(values[0]).strip()
    return value or None


def from_source_url(url):
    """Recover a locator from a stored link, or None if there isn't one."""
    parsed = parse_source_url(url)
    kind = _KIND_FROM_PARSE.get(parsed["kind"])
    if kind is not None:
        located = _blank(kind, SOURCE_DERIVED)
        located["conversation_id"] = parsed["conversation_id"]
        located["message_id"] = parsed["message_id"]
        return _validated(located)

    # parse_source_url only speaks Teams chat/channel/meetup links. The two
    # shapes below carry perfectly good ids it was never asked about.
    if not url or not str(url).strip():
        return None

    item_id = _query_param(url, "ItemID")
    if item_id and "outlook" in (urlparse(url).hostname or ""):
        located = _blank(KIND_EMAIL, SOURCE_DERIVED)
        # An Outlook ItemID is the Graph message id, URL-encoded. The decode
        # matters: a trailing %3d left as-is is rejected by Graph.
        located["message_id"] = item_id
        return _validated(located)

    event_id = _query_param(url, "eventId")
    if event_id:
        located = _blank(KIND_MEETING, SOURCE_DERIVED)
        located["event_id"] = event_id
        return _validated(located)

    return None


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
    if kind == KIND_EMAIL and not (
        located["message_id"] or located["internet_message_id"]
    ):
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
    for key in ("conversation_id", "message_id", "team_id", "channel_id",
                "internet_message_id", "event_id"):
        value = data.get(key)
        located[key] = value if isinstance(value, str) and value.strip() else None
    return _validated(located)


def read_plan(located):
    """The endpoint sequence proven to reach this conversation.

    Kept here rather than restated in each prompt, because the recurring
    failure in this project is a worker that was shown no endpoint and
    therefore invented none - 3b5e16d for Teams delivery, and a live
    waiting-check run that searched for a URL as text instead of fetching the
    chat. Every sequence below returned 200 against live Graph on 2026-08-24.
    """
    if not located:
        return []

    kind = located["kind"]

    if kind == KIND_TEAMS_CHAT and located["conversation_id"]:
        return [f"/me/chats/{located['conversation_id']}/messages?$top=50"]

    if kind == KIND_TEAMS_CHANNEL and (
        located["team_id"] and located["channel_id"] and located["message_id"]
    ):
        return [
            f"/teams/{located['team_id']}/channels/{located['channel_id']}"
            f"/messages/{located['message_id']}/replies?$top=50"
        ]

    if kind == KIND_MEETING:
        # A meeting the app already knows the chat for needs no lookup.
        if located["conversation_id"]:
            return [f"/me/chats/{located['conversation_id']}/messages?$top=50"]
        if located["event_id"]:
            return [
                f"/me/events/{located['event_id']}"
                "?$select=id,subject,start,end,organizer,onlineMeeting",
                "/me/chats/{19:meeting_...@thread.v2 from onlineMeeting.joinUrl}"
                "/messages?$top=50",
            ]

    if kind == KIND_EMAIL and located["message_id"]:
        return [
            f"/me/messages/{located['message_id']}"
            "?$select=id,subject,conversationId,internetMessageId,receivedDateTime,from",
            "/me/messages?$filter=conversationId eq '{conversationId from step 1}'"
            "&$select=id,subject,receivedDateTime,from&$top=25",
        ]

    return []


def is_thread_readable(located):
    """Whether the originating conversation can actually be re-read.

    Defined as "there is a plan", so a caller cannot be told something is
    readable and then handed nothing to read. Email and meetings take two hops
    rather than one, but both are proven, so both count.
    """
    return bool(read_plan(located))


def to_json(located):
    return json.dumps(located) if located else None

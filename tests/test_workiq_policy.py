from dataclasses import FrozenInstanceError, fields

import pytest

from src.services import workiq_policy as policy

from src.services.workiq_policy import (
    CalendarAction,
    CapabilityError,
    build_calendar_operation,
    discover_read_capabilities,
)
from src.services.workiq_setup import load_runtime_manifest


def test_policy_allows_only_exact_runtime_discovered_action_tool():
    tools = [
        {"name": "ask_work_iq"},
        {"name": "accept_eula"},
        {"name": "create_message", "annotations": {"readOnlyHint": True}},
        {"name": "workiq-ask"},
        {"name": "ASK_WORK_IQ"},
    ]
    tools.append({"name": "do_action"})
    assert discover_read_capabilities(tools) == ("do_action",)


def test_policy_discovers_actual_ask_without_the_stale_alias():
    tools = [
        {"name": "retrieve"},
        {"name": "fetch"},
        {"name": "ask"},
        {"name": "accept_eula"},
    ]
    tools.append({"name": "do_action"})
    assert discover_read_capabilities(tools) == ("do_action", "fetch", "ask")


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["ask"], ("ask",)),
        (["fetch"], ("fetch",)),
        (["ask", "fetch", "do_action"], ("do_action", "fetch", "ask")),
    ],
)
def test_policy_discovers_independent_owned_reads(names, expected):
    assert discover_read_capabilities([{"name": name} for name in names]) == expected


def test_ask_policy_mints_frozen_question_only_operation():
    question = "  private-question@example.test  "
    operation = policy.build_ask_operation(question)
    assert operation.question == question
    assert {field.name for field in fields(operation)} == {"question", "_mint"}
    assert policy.require_ask_operation(operation).question == question
    with pytest.raises(FrozenInstanceError):
        operation.question = "replacement"
    with pytest.raises(CapabilityError):
        policy.require_ask_operation(policy.AskOperation(question, object()))
    with pytest.raises(CapabilityError):
        policy.require_ask_operation({"question": question})
    with pytest.raises(TypeError):
        policy.build_ask_operation(question, fileUrls=["private"])
    with pytest.raises(TypeError):
        policy.build_ask_operation()


@pytest.mark.parametrize("question", [None, "", " \n ", 7, {}, [], True])
def test_ask_policy_rejects_invalid_questions(question):
    with pytest.raises(CapabilityError):
        policy.build_ask_operation(question)


@pytest.mark.parametrize(
    "tools",
    [
        [],
        [{"name": "do_action"}, {"name": "do_action"}],
        [{"name": 7}],
        [{"name": "ask_work_iq"}],
        [{"name": "ask"}, {"name": "ask"}],
        [{"name": "ask"}, None],
        {},
    ],
)
def test_policy_fails_closed_without_one_valid_action_tool(tools):
    with pytest.raises(CapabilityError):
        discover_read_capabilities(tools)


@pytest.mark.parametrize(
    ("action", "body", "path"),
    [
        (
            CalendarAction.FIND_MEETING_TIMES,
            {
                "attendees": [{
                    "type": "required",
                    "emailAddress": {"name": "Ada", "address": "ada@example.com"},
                }],
                "timeConstraint": {
                    "activityDomain": "work",
                    "timeSlots": [{
                        "start": {"dateTime": "2026-09-08T09:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-09-08T17:00:00", "timeZone": "UTC"},
                    }],
                },
                "meetingDuration": "PT30M",
                "maxCandidates": 10,
                "returnSuggestionReasons": True,
                "minimumAttendeePercentage": 100,
            },
            "/me/findMeetingTimes",
        ),
        (
            CalendarAction.GET_SCHEDULE,
            {
                "schedules": ["ada@example.com"],
                "startTime": {
                    "dateTime": "2026-09-08T09:00:00",
                    "timeZone": "UTC",
                },
                "endTime": {
                    "dateTime": "2026-09-08T17:00:00",
                    "timeZone": "UTC",
                },
            },
            "/me/calendar/getSchedule",
        ),
    ],
)
def test_calendar_policy_emits_only_two_exact_actions(action, body, path):
    operation = build_calendar_operation(action, body)
    assert operation.path == path
    assert operation.body == body


@pytest.mark.parametrize(
    "path",
    [
        "/me/sendMail",
        "/me/events",
        "/ME/findMeetingTimes",
        "/me/findMeetingTimes?x=1",
        "/me/findMeetingTimes#x",
        "/me/../findMeetingTimes",
        "prefix/me/findMeetingTimes",
        "/me/findMeetingTimes/suffix",
        "",
        None,
        7,
    ],
)
def test_calendar_policy_rejects_every_other_path(path):
    with pytest.raises(CapabilityError):
        build_calendar_operation(path, {})


@pytest.mark.parametrize("locator", [
    {"kind": "teams_chat", "conversation_id": "19:chat_alpha@thread.v2"},
    {"kind": "teams_channel", "team_id": "team/alpha+=", "channel_id": "19:channel@thread.tacv2", "message_id": "1756000000000"},
    {"kind": "email", "message_id": "AAMk/alpha="},
    {"kind": "meeting", "event_id": "AAMk/event="},
    {"kind": "meeting", "conversation_id": "19:meeting_alpha@thread.v2"},
])
def test_source_policy_frozen_bound_primitives_and_input_mutation(locator):
    from dataclasses import replace
    from src.services.source_locator import resolve

    normalized = resolve({**locator, "source": "captured"}, None)
    operation = policy.build_source_operation(normalized)
    assert {f.name for f in fields(operation)} == {"kind", "identifiers", "locator_source", "_mint"}
    assert operation.kind == locator["kind"]
    assert operation.locator_source == "captured"
    assert dict(operation.identifiers) == {k: v for k, v in locator.items() if k != "kind"}
    normalized.update(kind="email", message_id="foreign")
    assert dict(operation.identifiers) == {k: v for k, v in locator.items() if k != "kind"}
    assert policy.require_source_operation(operation) == operation
    with pytest.raises(FrozenInstanceError):
        operation.kind = "email"
    for forged in [locator, object(), replace(operation, _mint=object()),
                   replace(operation, kind="teams_chat" if operation.kind == "email" else "email"),
                   replace(operation, identifiers=(("message_id", "foreign"),))]:
        with pytest.raises(CapabilityError):
            policy.require_source_operation(forged)
    with pytest.raises(TypeError):
        policy.build_source_operation(locator, path="/me/messages")


@pytest.mark.parametrize("value", [
    "", " ", None, 7, True, [], {}, " leading", "trailing ", "a\nb", "a\rb",
    "a\x00b", "a\tb", "a\x7fb", "a\u202eb", "https://evil.test", "//evil",
    "a?x=1", "a#fragment", "a%2Fb", "a\\b", ".", "..", "a/../b", "x" * 2049,
])
def test_source_policy_rejects_invalid_bound_identifiers(value):
    with pytest.raises(CapabilityError):
        policy.build_source_operation({"kind": "email", "message_id": value, "source": "captured"})


@pytest.mark.parametrize("locator", [
    None, {}, {"source_id": "teams::private@example.test::topic"}, "/me/messages",
    {"kind": "email", "internet_message_id": "<private@example.test>"},
    {"kind": "teams_chat", "message_id": "1"},
    {"kind": "teams_channel", "team_id": "t", "channel_id": "c"},
    {"kind": "meeting"},
    {"kind": "teams_chat", "conversation_id": "chat", "event_id": "event"},
    {"kind": "email", "message_id": "mail", "conversation_id": "model-conversation"},
    {"kind": "email", "message_id": "mail", "entityUrls": ["/me/messages"]},
    {"kind": "email", "message_id": "mail", "source": "model"},
    {"kind": "email", "message_id": "mail", "version": True},
])
def test_source_policy_rejects_missing_mixed_or_generic_authority(locator):
    with pytest.raises(CapabilityError):
        policy.build_source_operation(locator)


def test_source_policy_accepts_only_resolved_winner_without_url_mixing():
    from src.services.source_locator import resolve

    captured = {"kind": "teams_chat", "conversation_id": "19:chat@thread.v2", "source": "captured"}
    located = resolve(captured, "https://outlook.office.com/?ItemID=foreign")
    operation = policy.build_source_operation(located)
    assert dict(operation.identifiers) == {"conversation_id": "19:chat@thread.v2"}
    derived = resolve(None, "https://outlook.office.com/?ItemID=AAMk%2Falpha%3D")
    assert policy.build_source_operation(derived).locator_source == "derived_from_url"
    stale = resolve({"kind": "meeting", "event_id": "old", "source": "derived_from_url"}, None)
    with pytest.raises(CapabilityError):
        policy.build_source_operation(stale)

"""Manage a safe, isolated Riveter demo server."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_DIR = Path(
    os.environ.get("RIVETER_DEMO_DIR", PROJECT_ROOT / "data" / "demo")
).resolve()
PORT = int(os.environ.get("RIVETER_DEMO_PORT", "8776"))
DB_PATH = DEMO_DIR / "riveter-demo.db"
SETTINGS_PATH = DEMO_DIR / "settings.json"
LOG_PATH = DEMO_DIR / "riveter-demo.log"
PID_PATH = DEMO_DIR / "riveter-demo.pid"


def _configure(db_path=DB_PATH):
    os.environ["RIVETER_DEMO_MODE"] = "1"
    os.environ["RIVETER_DEMO_ALLOW_TODO_PARSE"] = "1"
    os.environ["RIVETER_DEMO_ALLOW_COWORK_SESSION"] = "1"
    os.environ["RIVETER_DEMO_ALLOW_COWORK_EXECUTE"] = "1"
    os.environ["TODONESS_DB_PATH"] = str(db_path)
    os.environ["TODONESS_SETTINGS_PATH"] = str(SETTINGS_PATH)
    os.environ["TODONESS_LOG_FILE"] = str(LOG_PATH)


def _pid_record():
    try:
        value = json.loads(PID_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _pid():
    record = _pid_record()
    try:
        return int(record["pid"]) if record else None
    except (KeyError, TypeError, ValueError):
        return None


def _pid_running(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        handle = ctypes.windll.kernel32.OpenProcess(
            0x1000, False, pid  # PROCESS_QUERY_LIMITED_INFORMATION
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _process_identity(pid):
    if not _pid_running(pid):
        return None
    if os.name == "nt":
        import ctypes
        import ctypes.wintypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            creation = ctypes.wintypes.FILETIME()
            exit_time = ctypes.wintypes.FILETIME()
            kernel = ctypes.wintypes.FILETIME()
            user = ctypes.wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            size = ctypes.wintypes.DWORD(32768)
            executable = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, executable, ctypes.byref(size)
            ):
                return None
            created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return {
                "pid": pid,
                "created": created,
                "executable": executable.value.lower(),
            }
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        executable = str(Path(f"/proc/{pid}/exe").resolve()).lower()
        return {
            "pid": pid,
            "created": int(stat[21]),
            "executable": executable,
        }
    except (OSError, IndexError, ValueError):
        return None


def _demo_process(pid):
    record = _pid_record()
    if (
        not record
        or record.get("pid") != pid
        or record.get("created") is None
        or not record.get("executable")
    ):
        return False
    current = _process_identity(pid)
    return bool(
        current
        and current["created"] == record["created"]
        and current["executable"] == str(record["executable"]).lower()
    )


def _port_open():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.3):
            return True
    except OSError:
        return False


def _write_settings():
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(
            {
                "demo_mode": True,
                "cowork_api_transport": True,
                "task_workspaces": {"enabled": False},
                "meeting_preferences": {
                    "default_minutes": 25,
                    "start_offset_minutes": 5,
                    "notes": "Demo data only",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _set_action_result(action_id, **fields):
    from src.db import get_connection

    allowed = {
        "state",
        "finding",
        "draft",
        "terminal_status",
        "tool_trace",
        "error",
        "delivery_confirmed_at",
        "completed_at",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    conn = get_connection()
    try:
        assignments = ",".join(f"{key}=?" for key in values)
        conn.execute(
            f"UPDATE task_actions SET {assignments} WHERE id=?",
            (*values.values(), action_id),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_database(path):
    _configure(path)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.db import get_connection, init_db
    from src.models import create_task, create_task_action
    from src.services.cowork_runner import certify_schedule_interaction

    conn = get_connection()
    init_db(conn)
    conn.close()

    people = {}
    people_data = {
        "rima": ("Rima Reyes", "rima.reyes", "11111111-1111-4111-8111-111111111111"),
        "bobby": ("Bobby Chang", "bobby.chang", "22222222-2222-4222-8222-222222222222"),
        "luis": ("Luis Camino", "luis.camino", "33333333-3333-4333-8333-333333333333"),
        "steve": ("Steve Jeffery", "steve.jeffery", "44444444-4444-4444-8444-444444444444"),
        "manuela": ("Manuela Pichler", "manuela.pichler", "55555555-5555-4555-8555-555555555555"),
        "adrian": ("Adrian Maclean", "adrian.maclean", "66666666-6666-4666-8666-666666666666"),
        "aamer": ("Aamer Kaleem", "aamer.kaleem", "77777777-7777-4777-8777-777777777777"),
    }
    demo_self_oid = "00000000-0000-4000-8000-000000000000"

    def teams_source_url(person_key, message_id):
        person_oid = people_data[person_key][2]
        conversation_id = f"19:{person_oid}_{demo_self_oid}@unq.gbl.spaces"
        return (
            f"https://teams.microsoft.com/l/message/{conversation_id}/{message_id}"
            "?context=%7B%22contextType%22%3A%22chat%22%7D"
        )

    for key, (name, email_name, object_id) in people_data.items():
        alternatives = []
        if key == "aamer":
            alternatives = [{
                "name": "Aamer K.",
                "email": "aamer.k@example.invalid",
                "role": "Possible directory match",
            }]
        people[key] = json.dumps([{
            "name": name,
            "email": f"{email_name}@example.invalid",
            "aad_object_id": object_id,
            "role": "Confirmed demo identity",
            "alternatives": alternatives,
        }])
    people["workshop"] = json.dumps([
        json.loads(people[key])[0] for key in ("steve", "rima", "adrian")
    ])

    create_task(
        "Find the current tester for the new Cowork API with Rima",
        (
            "Rima Reyes was asked to identify the person currently exercising the "
            "Todo workflow so a newly available Cowork API build can be validated. "
            "A later project update names a tester, but the handoff and validation "
            "window still need to be confirmed before the task can be closed."
        ),
        status="suggested", priority=4, source_type="chat",
        source_id="demo::rima::cowork-api-tester",
        source_date="2026-08-17T16:25:00Z",
        source_url=teams_source_url("rima", "1787000000001"),
        source_snippet=(
            "On August 17, a project chat asked Rima Reyes to identify the current "
            "Todo workflow tester for a newly available Cowork API build."
        ),
        coaching_text=(
            "Review the detected update, confirm the named tester with Rima, and "
            "record the validation owner and target date."
        ),
        action_type="awaiting-response", key_people=people["rima"],
        due_date="2026-08-21",
    )
    create_task(
        "Follow up with Luis on the generated customer presentations",
        (
            "Luis Camino was given refreshed customer-onboarding and Lighthouse "
            "presentation sets and asked to flag anything missing. A newer review "
            "note indicates the presentation set was checked and accepted, making "
            "this a strong candidate to dismiss rather than send another reminder."
        ),
        status="suggested", priority=4, source_type="chat",
        source_id="demo::luis::presentation-review",
        source_date="2026-08-18T14:10:00Z",
        source_url=teams_source_url("luis", "1787000000002"),
        source_snippet=(
            "On August 18, Luis Camino received refreshed customer presentations "
            "with a request to identify gaps; a later review note records approval."
        ),
        coaching_text="The follow-up appears resolved; verify the review note and dismiss it.",
        action_type="awaiting-response", key_people=people["luis"],
    )
    create_task(
        "Confirm Manuela's PPCC distribution list preference",
        (
            "Manuela Pichler was asked whether she wanted to join the PPCC "
            "distribution list. A later activity summary records her preference "
            "and indicates that the membership update was completed, so the "
            "original unanswered-message suggestion now appears resolved."
        ),
        status="suggested", priority=4, source_type="chat",
        source_id="demo::manuela::distribution-preference",
        source_date="2026-08-20T17:29:00Z",
        source_url=teams_source_url("manuela", "1787000000003"),
        source_snippet=(
            "On August 20, Manuela Pichler was asked for her PPCC distribution-list "
            "preference; subsequent activity records both her answer and the update."
        ),
        coaching_text="Verify the membership note, then dismiss this resolved suggestion.",
        action_type="awaiting-response", key_people=people["manuela"],
    )
    create_task(
        "Clarify the AIA engagement model with Bobby",
        (
            "Bobby Chang responded to a discussion about how AI Architects engage "
            "with accounts, but two operational questions remain open: whether "
            "architects are assigned to named accounts and how hands-on they should "
            "be during building and deployment. A recent update may answer one part."
        ),
        status="suggested", priority=4, source_type="chat",
        source_id="demo::bobby::aia-engagement-model",
        source_date="2026-08-18T15:40:00Z",
        source_url=teams_source_url("bobby", "1787000000004"),
        source_snippet=(
            "On August 18, Bobby Chang provided broader engagement-model context "
            "without clearly resolving account assignment and delivery expectations."
        ),
        coaching_text=(
            "Check the recent activity, then draft a concise Teams follow-up for "
            "only the remaining unanswered engagement-model question."
        ),
        action_type="follow-up", key_people=people["bobby"],
        due_date="2026-08-24",
    )
    create_task(
        "Coordinate the five-customer dashboard examples with Adrian",
        (
            "A dashboard review left a concrete follow-up to align with Adrian "
            "Maclean on five representative customer examples. The working session "
            "should settle the customer mix, required CRM and domain attributes, "
            "and which examples best demonstrate the executive review experience."
        ),
        status="suggested", priority=2, source_type="meeting",
        source_id="demo::adrian::dashboard-examples",
        source_date="2026-08-19T15:00:00Z",
        source_snippet=(
            "On August 19, a dashboard review assigned an alignment with Adrian "
            "Maclean on five customer examples and their required data."
        ),
        coaching_text=(
            "Draft a Teams follow-up that proposes five example categories and asks "
            "Adrian to confirm the customer and data mix."
        ),
        action_type="follow-up", key_people=people["adrian"],
        due_date="2026-08-24",
    )
    create_task(
        "Prepare the account-team briefing with Aamer",
        (
            "Aamer Kaleem asked for an account-team briefing that combines scenario "
            "discovery, architecture education, objection handling, and a focused "
            "demo plan. Build a review-ready agenda that distinguishes confirmed "
            "customer needs from open questions and assigns owners for each section."
        ),
        status="suggested", priority=1, source_type="meeting",
        source_id="demo::aamer::account-briefing",
        source_date="2026-08-20T13:00:00Z",
        source_snippet=(
            "On August 20, a planning meeting assigned Aamer Kaleem and the account "
            "team a structured briefing covering scenarios, architecture, and demos."
        ),
        coaching_text=(
            "Prepare a briefing outline with objectives, open questions, demo "
            "sequence, decision points, and named follow-up owners."
        ),
        action_type="prepare", key_people=people["aamer"],
        due_date="2026-08-25",
    )
    create_task(
        "Schedule the Lighthouse workshop mapping session with Steve",
        (
            "Steve Jeffery is needed in a working session to map existing workshops "
            "and activities into the FY27 Lighthouse program structure. Find a "
            "25-minute slot, include a draft inventory and decision criteria in the "
            "agenda, and make the desired ownership decisions explicit."
        ),
        status="suggested", priority=2, source_type="meeting",
        source_id="demo::steve::workshop-mapping",
        source_date="2026-08-19T16:00:00Z",
        source_snippet=(
            "On August 19, the program-direction meeting requested a workshop "
            "mapping session with Steve Jeffery to settle the FY27 structure."
        ),
        coaching_text=(
            "Find a 25-minute working-hours slot with Steve, Rima, and Adrian and "
            "draft a decision-led agenda for mapping workshops into Lighthouse."
        ),
        action_type="schedule-meeting", key_people=people["workshop"],
        due_date="2026-08-24",
    )
    create_task(
        "Schedule the Friday demo review with Bobby Chang",
        (
            "Bobby Chang should review the Riveter demo before Friday's audience "
            "session. Find a 30-minute working-hours slot and include an agenda that "
            "covers the opening task story, safe data boundaries, Cowork preview, "
            "and the closing adoption message."
        ),
        priority=2, source_type="manual",
        source_id="demo::bobby::friday-demo-review",
        coaching_text=(
            "Schedule a 30-minute review with Bobby and include the four demo "
            "decision points in the invitation."
        ),
        action_type="schedule-meeting", key_people=people["bobby"],
        due_date="2026-08-21",
    )
    create_task(
        "Reply to Adrian with the FY27 program direction",
        (
            "Adrian Maclean requested a concise statement of the FY27 program shape "
            "before the team chooses a path for a customer engagement. Respond by "
            "email with the current program boundaries, what remains undecided, and "
            "the decision the account team can safely make now."
        ),
        priority=2, source_type="email",
        source_id="demo::adrian::fy27-direction",
        source_date="2026-08-19T11:30:00Z",
        source_snippet=(
            "On August 19, Adrian Maclean requested FY27 program guidance needed "
            "before the account team can select an engagement path."
        ),
        coaching_text=(
            "Draft a direct email response with the current direction, known "
            "constraints, and one explicit next decision."
        ),
        action_type="respond-email", key_people=people["adrian"],
        due_date="2026-08-21",
    )
    create_task(
        "Send Luis the customer-assignment update in Teams",
        (
            "Luis Camino still needs a clear update on Lighthouse customer coverage "
            "after the squad kickoff surfaced an assignment gap. Send a Teams note "
            "that explains what has been checked, identifies the current assignment "
            "owner, and gives Luis a concrete date for the final answer."
        ),
        priority=2, source_type="chat",
        source_id="demo::luis::customer-assignment",
        source_date="2026-08-18T12:15:00Z",
        source_url=teams_source_url("luis", "1787000000005"),
        source_snippet=(
            "On August 18, a squad kickoff identified that Luis Camino did not yet "
            "have confirmed Lighthouse customer assignments."
        ),
        coaching_text=(
            "Draft a short Teams update for Luis with the current owner, remaining "
            "dependency, and expected decision date."
        ),
        action_type="follow-up", key_people=people["luis"],
        due_date="2026-08-21",
    )
    create_task(
        "Prepare the Lighthouse customer-list rationale with Rima",
        (
            "Rima Reyes is reviewing the Lighthouse customer list and needs a clear "
            "explanation for every inclusion and omission. Prepare a decision table "
            "that records the selection criteria, the evidence available for each "
            "customer, and the owner for resolving any missing information."
        ),
        priority=2, source_type="meeting",
        source_id="demo::rima::customer-list-rationale",
        source_date="2026-08-18T13:00:00Z",
        source_snippet=(
            "On August 18, the leadership standup assigned Rima Reyes a review of "
            "the Lighthouse customer list and its inclusion criteria."
        ),
        coaching_text=(
            "Prepare a concise customer decision table and highlight only the "
            "omissions that still need leadership input."
        ),
        action_type="prepare", key_people=people["rima"],
        due_date="2026-08-22",
    )
    create_task(
        "Review the dashboard customer examples with Aamer",
        (
            "Aamer Kaleem requested five representative dashboard examples that "
            "show useful customer, CRM, and domain signals without overwhelming the "
            "reviewer. The example set is being assembled now; compare each example "
            "against the agreed narrative and document any missing data."
        ),
        status="in_progress", priority=2, source_type="meeting",
        source_id="demo::aamer::dashboard-example-review",
        source_date="2026-08-19T15:15:00Z",
        source_snippet=(
            "On August 19, Aamer Kaleem requested five dashboard examples that "
            "demonstrate customer, CRM, and domain signals together."
        ),
        coaching_text=(
            "Review the five-example draft, flag narrative or data gaps, and record "
            "a clear accept-or-revise decision for each example."
        ),
        action_type="review-document", key_people=people["aamer"],
        due_date="2026-08-24",
    )
    create_task(
        "Wait for Steve's Lighthouse workshop invitation",
        (
            "Steve Jeffery is coordinating the workshop-to-program mapping session, "
            "and the next move depends on the calendar invitation and draft agenda. "
            "Keep the mapping inventory ready, but do not send a duplicate invite; "
            "check again after the agreed planning window."
        ),
        status="waiting", priority=3, source_type="email",
        source_id="demo::steve::workshop-invitation",
        source_date="2026-08-19T16:20:00Z",
        source_snippet=(
            "On August 19, the program thread recorded that Steve Jeffery would "
            "coordinate the mapping-session invitation and initial agenda."
        ),
        coaching_text=(
            "Wait for the invitation, then verify that the agenda includes the "
            "inventory, mapping criteria, and ownership decisions."
        ),
        action_type="awaiting-response", key_people=people["steve"],
        user_notes="Check after Monday's program planning window.",
    )
    create_task(
        "Document Manuela's customer-search requirements",
        (
            "Manuela Pichler helped define searchable customer selection for both "
            "the main dashboard and customer views. The agreed requirements now "
            "cover type-ahead behavior, large-list usability, and consistent search "
            "results across both entry points, and the review notes are complete."
        ),
        status="completed", priority=3, source_type="meeting",
        source_id="demo::manuela::customer-search-requirements",
        source_date="2026-08-20T14:00:00Z",
        source_snippet=(
            "On August 20, Manuela Pichler confirmed the customer-search behavior "
            "needed across the main dashboard and customer views."
        ),
        coaching_text=(
            "The requirements are documented and accepted; retain this completed "
            "task as context for later implementation work."
        ),
        action_type="review-document", key_people=people["manuela"],
    )
    create_task(
        "Review the Power Up asset transition with Luis",
        (
            "A flagged program thread involving Luis Camino discusses whether Power "
            "Up learning assets should transition into another enablement program, "
            "but it does not establish a direct owner or immediate decision. Keep "
            "the review snoozed until the program team clarifies ownership."
        ),
        status="snoozed", priority=4, source_type="email",
        source_id="demo::luis::asset-transition",
        source_date="2026-08-16T10:00:00Z",
        source_snippet=(
            "On August 16, a program email involving Luis Camino raised a possible "
            "learning-asset transition without assigning a concrete owner."
        ),
        coaching_text=(
            "Revisit only when program ownership is clarified; avoid creating an "
            "unowned follow-up from an informational thread."
        ),
        action_type="review-document", key_people=people["luis"],
        due_date="2026-09-01",
    )
    create_task(
        "Dismiss the superseded AMR kickoff follow-up with Bobby",
        (
            "An earlier squad-status check appeared to need Bobby Chang's response, "
            "but the consolidated kickoff summary now records every squad's status. "
            "The individual reminder would duplicate the completed rollup and has "
            "therefore been dismissed as superseded."
        ),
        status="dismissed", priority=5, source_type="chat",
        source_id="demo::bobby::superseded-kickoff-follow-up",
        source_date="2026-08-18T18:00:00Z",
        source_url=teams_source_url("bobby", "1787000000006"),
        source_snippet=(
            "On August 18, a consolidated kickoff summary replaced the earlier "
            "individual status request associated with Bobby Chang."
        ),
        coaching_text=(
            "No action is needed because the consolidated summary contains the "
            "required kickoff status."
        ),
        action_type="general", key_people=people["bobby"],
    )
    create_task(
        "Dismiss the outdated FY27 guidance request to Adrian",
        (
            "A previous request asked Adrian Maclean for an early FY27 program view, "
            "but a later planning artifact replaced that request with an approved "
            "direction and current decision log. The old request is retained only "
            "to demonstrate dismissed-task history."
        ),
        status="dismissed", priority=5, source_type="email",
        source_id="demo::adrian::outdated-guidance-request",
        source_date="2026-08-15T09:00:00Z",
        source_snippet=(
            "On August 15, an early guidance request to Adrian Maclean was "
            "superseded by the approved program direction and decision log."
        ),
        coaching_text=(
            "No response is needed; use the current program-direction task instead "
            "of reopening this superseded request."
        ),
        action_type="general", key_people=people["adrian"],
    )

    conn = get_connection()
    try:
        suggestion_activity = {
            "demo::rima::cowork-api-tester": {
                "status": "activity_detected",
                "summary": (
                    "A later project update names a likely tester, but the handoff "
                    "and validation date still need confirmation."
                ),
                "checked_at": "2026-08-20T18:00:00Z",
            },
            "demo::luis::presentation-review": {
                "status": "likely_resolved",
                "summary": (
                    "A later review note says the refreshed customer presentation "
                    "set was checked and accepted."
                ),
                "checked_at": "2026-08-20T18:00:00Z",
            },
            "demo::manuela::distribution-preference": {
                "status": "likely_resolved",
                "summary": (
                    "Subsequent activity records Manuela's preference and completion "
                    "of the distribution-list update."
                ),
                "checked_at": "2026-08-20T18:00:00Z",
            },
            "demo::bobby::aia-engagement-model": {
                "status": "may_be_resolved",
                "summary": (
                    "A recent engagement-model update may answer the account "
                    "assignment question, but delivery expectations remain unclear."
                ),
                "checked_at": "2026-08-20T18:00:00Z",
            },
        }
        narrative_details = {
            "demo::rima::cowork-api-tester": (
                "As of August 20, 2026, a likely tester is named but the handoff "
                "and validation window are not confirmed.",
                "Confirm the tester and target date with Rima Reyes.",
            ),
            "demo::luis::presentation-review": (
                "As of August 20, 2026, a later review note indicates that Luis "
                "Camino accepted the refreshed presentation set.",
                "Verify the review note and dismiss the resolved suggestion.",
            ),
            "demo::manuela::distribution-preference": (
                "As of August 20, 2026, Manuela Pichler's preference and the "
                "membership update are both recorded.",
                "Verify the update and dismiss the resolved suggestion.",
            ),
            "demo::bobby::aia-engagement-model": (
                "As of August 20, 2026, account assignment may be clarified, but "
                "Bobby Chang's delivery expectations remain open.",
                "Ask Bobby only for the remaining delivery-model decision.",
            ),
            "demo::adrian::dashboard-examples": (
                "As of August 20, 2026, the five-example customer and data mix "
                "still needs Adrian Maclean's agreement.",
                "Send Adrian the proposed categories and request confirmation.",
            ),
            "demo::aamer::account-briefing": (
                "As of August 20, 2026, Aamer Kaleem's briefing outline has not "
                "yet been assembled for account-team review.",
                "Prepare the agenda, open questions, demo sequence, and owners.",
            ),
            "demo::steve::workshop-mapping": (
                "As of August 20, 2026, Steve Jeffery, Rima Reyes, and Adrian "
                "Maclean have three verified scheduling options.",
                "Review the proposed time and agenda without creating the event.",
            ),
            "demo::bobby::friday-demo-review": (
                "As of August 20, 2026, Bobby Chang's Friday review is required "
                "but has not been placed on the calendar.",
                "Review the proposed invitation and choose whether to schedule it.",
            ),
            "demo::adrian::fy27-direction": (
                "As of August 20, 2026, Adrian Maclean still needs the approved "
                "program boundaries and the remaining account decision.",
                "Review the prepared email response before any delivery.",
            ),
            "demo::luis::customer-assignment": (
                "As of August 20, 2026, Luis Camino's account match is still being "
                "confirmed by the program lead.",
                "Review the prepared Teams update before any delivery.",
            ),
            "demo::rima::customer-list-rationale": (
                "As of August 20, 2026, Rima Reyes is still validating the "
                "evidence behind customer inclusions and omissions.",
                "Complete the decision table and escalate missing evidence.",
            ),
            "demo::aamer::dashboard-example-review": (
                "As of August 20, 2026, Aamer Kaleem's five examples are being "
                "reviewed and several data gaps remain.",
                "Record an accept-or-revise decision for each example.",
            ),
            "demo::steve::workshop-invitation": (
                "As of August 20, 2026, Steve Jeffery's invitation and draft "
                "agenda have not arrived.",
                "Wait through the planning window, then verify the invitation.",
            ),
            "demo::manuela::customer-search-requirements": (
                "As of August 20, 2026, Manuela Pichler's search requirements are "
                "documented, reviewed, and complete.",
                "Retain the completed task as implementation context.",
            ),
            "demo::luis::asset-transition": (
                "As of August 20, 2026, the thread involving Luis Camino still "
                "does not assign a transition owner.",
                "Keep the review snoozed until program ownership is clarified.",
            ),
            "demo::bobby::superseded-kickoff-follow-up": (
                "As of August 20, 2026, the consolidated kickoff summary has "
                "replaced the individual request associated with Bobby Chang.",
                "Leave the duplicate reminder dismissed.",
            ),
            "demo::adrian::outdated-guidance-request": (
                "As of August 20, 2026, the approved decision log supersedes the "
                "earlier guidance request to Adrian Maclean.",
                "Use the current FY27 response task and keep this request dismissed.",
            ),
        }
        for source_id, (current_state, next_step) in narrative_details.items():
            conn.execute(
                "UPDATE tasks SET description=description || ? || ? WHERE source_id=?",
                (
                    f" Current state: {current_state}",
                    f" Next step: {next_step}",
                    source_id,
                ),
            )
        for source_id, activity in suggestion_activity.items():
            conn.execute(
                "UPDATE tasks SET waiting_activity=? WHERE source_id=?",
                (json.dumps(activity), source_id),
            )
        conn.execute(
            "UPDATE tasks SET created_at=?,updated_at=?",
            ("2026-08-20T18:00:00Z", "2026-08-20T18:00:00Z"),
        )
        conn.commit()
        conn.close()
        conn = None

        def seed_cowork_result(
            source_id,
            *,
            finding,
            draft,
            destination_kind,
            destination_ref,
            destination_display,
            delivery_channel,
            tool_trace,
            blocked_question=None,
            answered_interaction=None,
        ):
            lookup_conn = get_connection()
            try:
                task_id = lookup_conn.execute(
                    "SELECT id FROM tasks WHERE source_id=?", (source_id,)
                ).fetchone()[0]
            finally:
                lookup_conn.close()
            action = create_task_action(
                task_id,
                intent="Review the deterministic demo result without executing it.",
                notes_snapshot="Safe demo preview; no external write was performed.",
                destination_kind=destination_kind,
                destination_ref=destination_ref,
                destination_display=destination_display,
                delivery_channel=delivery_channel,
                destination_source="auto_key_people",
                blocked_question=blocked_question,
                answered_interaction=answered_interaction,
            )
            _set_action_result(
                action["id"],
                state="ready",
                finding=finding,
                draft=draft,
                tool_trace=json.dumps(tool_trace),
            )
            update_conn = get_connection()
            try:
                update_conn.execute(
                    "UPDATE task_actions SET destination_kind=?,destination_ref=?,"
                    "destination_display=?,delivery_channel=?,destination_source=?,"
                    "had_interaction=?,created_at=?,updated_at=? WHERE id=?",
                    (
                        destination_kind,
                        destination_ref,
                        destination_display,
                        delivery_channel,
                        "auto_key_people",
                        int(blocked_question is not None),
                        "2026-08-20T18:00:00Z",
                        "2026-08-20T18:00:00Z",
                        action["id"],
                    ),
                )
                update_conn.commit()
            finally:
                update_conn.close()
            return action["id"]

        seed_cowork_result(
            "demo::luis::customer-assignment",
            finding=(
                "The squad kickoff established that Luis still needs a confirmed "
                "Lighthouse assignment. The reviewable next step is a concise Teams "
                "update naming the current owner and decision date."
            ),
            draft=(
                "Hi Luis — I checked on the Lighthouse assignment gap from the squad "
                "kickoff. The program lead is confirming the account match, and I "
                "will send you the final assignment by Monday. I will also make sure "
                "you have the account context and the right squad contact."
            ),
            destination_kind="one_to_one",
            destination_ref="demo-teams-luis-camino",
            destination_display="Luis Camino",
            delivery_channel="teams",
            tool_trace=[{
                "tool_name": "mcp__teams__GetMessages",
                "ok": True,
                "output": "Demo-safe summary of the assignment discussion.",
            }],
        )
        seed_cowork_result(
            "demo::adrian::fy27-direction",
            finding=(
                "The current program notes support a short response that separates "
                "confirmed FY27 boundaries from the one account decision still open."
            ),
            draft=(
                "Subject: FY27 program direction and next account decision\n\n"
                "Hi Adrian,\n\nThe current direction is to map existing workshops "
                "into the Lighthouse structure while keeping customer-specific "
                "delivery decisions with the account team. The remaining decision "
                "is which engagement path best fits this account. I suggest we use "
                "the mapping session to confirm that choice and its owner.\n\nThanks"
            ),
            destination_kind="one_to_one",
            destination_ref="adrian.maclean@example.invalid",
            destination_display="Adrian Maclean",
            delivery_channel="email",
            tool_trace=[{
                "tool_name": "mcp__outlook__GetMessage",
                "ok": True,
                "output": "Demo-safe summary of the program-direction thread.",
            }],
        )

        workshop_attendees = [
            json.loads(people["workshop"])[index]["email"] for index in range(3)
        ]
        availability = {email: "free" for email in workshop_attendees}
        slot_values = (
            (
                "Option 1",
                "2027-01-11T10:05:00+00:00",
                "2027-01-11T10:30:00+00:00",
                "Monday, January 11 · 10:05–10:30 UTC",
            ),
            (
                "Option 2",
                "2027-01-12T13:35:00+00:00",
                "2027-01-12T14:00:00+00:00",
                "Tuesday, January 12 · 13:35–14:00 UTC",
            ),
            (
                "Option 3",
                "2027-01-13T15:05:00+00:00",
                "2027-01-13T15:30:00+00:00",
                "Wednesday, January 13 · 15:05–15:30 UTC",
            ),
        )
        workshop_interaction = {
            "invocation_id": "demo-workshop-availability",
            "questions": [{
                "id": "0",
                "producer_id": "slot",
                "header": "Choose a verified 25-minute time",
                "question": "Which Lighthouse workshop mapping time should be used?",
                "multi_select": False,
                "image_url": "",
                "options": [{
                    "value": value,
                    "label": label,
                    "description": (
                        "[slot:"
                        + json.dumps(
                            {"start": start, "end": end, "timezone": "UTC"},
                            separators=(",", ":"),
                        )
                        + "] [avail:"
                        + json.dumps(availability, separators=(",", ":"))
                        + "]"
                    ),
                    "image_url": "",
                } for value, start, end, label in slot_values],
            }],
        }
        workshop_interaction = certify_schedule_interaction(
            workshop_interaction,
            [
                (
                    "ts",
                    {
                        "tid": "demo-find-times",
                        "tn": "mcp__outlook_calendar__FindMeetingTimes",
                        "inp": json.dumps({
                            "attendees": workshop_attendees,
                            "duration_minutes": 25,
                        }),
                    },
                ),
                (
                    "tx",
                    {
                        "tid": "demo-find-times",
                        "tn": "mcp__outlook_calendar__FindMeetingTimes",
                        "ok": True,
                    },
                ),
            ],
            json.loads(people["workshop"]),
            duration_minutes=25,
            start_offset_minutes=5,
            now="2026-08-20T18:00:00+00:00",
        )
        if workshop_interaction is None:
            raise RuntimeError("Could not certify deterministic demo availability")
        selected_slot = workshop_interaction["schedule_evidence"]["slots"][0]
        workshop_answer = {
            "kind": "interaction_answer",
            "interaction": workshop_interaction,
            "answers": {"0": selected_slot["value"]},
        }
        workshop_draft = (
            "**Lighthouse workshop mapping**\n\n"
            "- **When:** Monday, January 11, 2027, 10:05–10:30 UTC\n"
            "- **Attendees:** Steve Jeffery, Rima Reyes, Adrian Maclean\n"
            "- **Where:** Teams meeting\n"
            "- **Agenda:** Map existing workshops into the FY27 Lighthouse "
            "structure, confirm decision criteria, and assign owners."
        )
        seed_cowork_result(
            "demo::steve::workshop-mapping",
            finding=(
                "All three confirmed attendees are free for three query-backed "
                "25-minute options. The first option is selected in this reviewable "
                "preview, but no calendar event has been created."
            ),
            draft=workshop_draft,
            destination_kind="meeting",
            destination_ref=";".join(workshop_attendees),
            destination_display="Steve Jeffery, Rima Reyes, and Adrian Maclean",
            delivery_channel=None,
            blocked_question=json.dumps(
                workshop_interaction, separators=(",", ":")
            ),
            answered_interaction=json.dumps(
                workshop_answer, separators=(",", ":")
            ),
            tool_trace=[
                {
                    "tool_name": "mcp__outlook_calendar__FindMeetingTimes",
                    "ok": True,
                    "output": "Three demo-safe query-backed options.",
                },
                {
                    "tool_name": "mcp__outlook_calendar__CreateEvent",
                    "ok": False,
                    "input": {
                        "subject": "Lighthouse workshop mapping",
                        "start": selected_slot["start"],
                        "end": selected_slot["end"],
                        "time_zone": "UTC",
                        "attendees": workshop_attendees,
                        "body": workshop_draft,
                        "content_type": "html",
                        "is_online_meeting": True,
                    },
                    "output": "Not executed; deterministic review preview only.",
                },
            ],
        )
        conn = get_connection()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        if conn is not None:
            conn.close()


def reset():
    if _demo_process(_pid()) or _port_open():
        print("ERROR: Riveter Demo is running. Stop it before reset.", file=sys.stderr)
        return 1
    PID_PATH.unlink(missing_ok=True)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = DEMO_DIR / "riveter-demo.reset.db"
    for path in (
        temp_path,
        Path(str(temp_path) + "-wal"),
        Path(str(temp_path) + "-shm"),
    ):
        path.unlink(missing_ok=True)
    _write_settings()
    _seed_database(temp_path)
    for path in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        path.unlink(missing_ok=True)
    os.replace(temp_path, DB_PATH)
    print(f"Demo data reset: {DB_PATH}")
    return 0


def serve():
    _configure()
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from src.app import (
        PARSE_CHECK_INTERVAL_MS,
        _check_unparsed,
        make_app,
        setup_logging,
    )
    from src.db import get_connection, init_db
    import tornado.ioloop

    setup_logging(str(LOG_PATH))
    conn = get_connection()
    init_db(conn)
    conn.close()
    app = make_app()
    app.auto_sync_enabled = False
    app.auto_suggestion_check_enabled = False
    app.demo_mode = True
    app.listen(PORT, address="127.0.0.1")
    ioloop = tornado.ioloop.IOLoop.current()
    parse_callback = tornado.ioloop.PeriodicCallback(
        _check_unparsed, PARSE_CHECK_INTERVAL_MS
    )
    parse_callback.start()
    app.parse_callback = parse_callback

    def stop_loop(*_args):
        ioloop.add_callback(ioloop.stop)

    signal.signal(signal.SIGTERM, stop_loop)
    signal.signal(signal.SIGINT, stop_loop)
    identity = _process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("Could not establish demo process identity")
    PID_PATH.write_text(json.dumps(identity), encoding="utf-8")
    try:
        ioloop.start()
    finally:
        parse_callback.stop()
        if _pid() == os.getpid():
            PID_PATH.unlink(missing_ok=True)
    return 0


def start():
    if _demo_process(_pid()) and _port_open():
        print(f"Riveter Demo is already running at http://localhost:{PORT}")
        return 0
    if _port_open():
        print(f"ERROR: Port {PORT} is already in use.", file=sys.stderr)
        return 1
    PID_PATH.unlink(missing_ok=True)
    if not DB_PATH.exists() and reset() != 0:
        return 1
    _write_settings()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_PATH.open("a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "serve"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "RIVETER_DEMO_MODE": "1"},
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=True,
    )
    log_handle.close()
    for _ in range(40):
        if proc.poll() is not None:
            print(f"ERROR: Demo server exited with code {proc.returncode}.", file=sys.stderr)
            return 1
        if _port_open():
            print(f"Riveter Demo: http://localhost:{PORT} (PID {proc.pid})")
            return 0
        time.sleep(0.25)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    print("ERROR: Demo server did not become ready.", file=sys.stderr)
    return 1


def stop():
    pid = _pid()
    if not _pid_running(pid):
        PID_PATH.unlink(missing_ok=True)
        print("Riveter Demo is not running.")
        return 0
    if not _demo_process(pid):
        print(
            f"ERROR: PID {pid} is not the Riveter Demo process; refusing to stop it.",
            file=sys.stderr,
        )
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not _pid_running(pid):
            PID_PATH.unlink(missing_ok=True)
            print("Riveter Demo stopped.")
            return 0
        time.sleep(0.25)
    print(f"ERROR: Demo PID {pid} did not stop.", file=sys.stderr)
    return 1


def status():
    pid = _pid()
    running = _demo_process(pid) and _port_open()
    state = "running" if running else "stopped"
    print(f"Riveter Demo is {state}. URL: http://localhost:{PORT}")
    return 0 if running else 1


def main():
    commands = {
        "start": start,
        "stop": stop,
        "status": status,
        "reset": reset,
        "serve": serve,
    }
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "start"
    if command not in commands:
        print("Usage: python scripts/demo_server.py start|stop|status|reset")
        return 2
    return commands[command]()


if __name__ == "__main__":
    raise SystemExit(main())

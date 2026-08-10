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
import json

__all__ = ["parse_source_url", "compose_prompt", "parse_cowork_output"]

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

_VOICE_EMAIL = (
    "Use the skill work-email-voice to set the voice of this draft. " + _SKILL_NOTE
    + "\n\nThis draft is an Outlook email from the user's Microsoft work account.\n"
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
_VOICE_TEAMS = (
    "Use the skill work-teams-voice to set the voice of this draft. " + _SKILL_NOTE
    + "\n\nThis draft is a Teams chat message, not an email. Match chat register.\n"
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

_VOICE_BY_CHANNEL = {"email": _VOICE_EMAIL, "teams": _VOICE_TEAMS}



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
                   redirect_text: str | None = None,
                   delivery_channel: str | None = None) -> str:
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

    parts: list[str] = []

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

    parts.append("[VOICE]\n" + _VOICE_BY_CHANNEL.get(
        (delivery_channel or "").strip().lower(), _VOICE_NEUTRAL))

    correction = _clean(redirect_text)
    if correction:
        parts.append(
            "[CORRECTION]\nThe user reviewed a previous attempt and asked for this "
            "change. It overrides the intent and notes above:\n" + correction
        )

    parts.append("[OUTPUT]\n" + _SAFETY)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.S)

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
    r"^\s*(?:want me to|shall i|should i|would you like me to|let me know if)\b.*$",
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
        }
        for t in trace
        if isinstance(t, dict)
    ]

    text = payload.get("text") or ""
    result["raw_text"] = text

    result["tools"] = _canonical_tools(payload.get("sse_events"))

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

    if result["terminal_status"] != "ok":
        result["error"] = (
            f"Cowork finished with status "
            f"{result['terminal_status'] or 'unknown'!s}."
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
# A denylist over an open set can never be proven complete. The only hard
# guarantee in Phase 1 is structural: there is no execute endpoint at all.
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

import logging
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

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
    "Nothing was sent, saved or modified. Do not retry, and do not attempt "
    "another tool to achieve the same effect. Report the draft instead."
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


def resolve_cowork_island():
    """Probe once and cache the resolved runtime URL, including failed probes."""
    global _island_probe_attempted, _cached_island_url
    with _island_probe_lock:
        if _island_probe_attempted:
            return _cached_island_url
        probe = _ISLAND_PROBE_FN or _default_island_probe
        try:
            resolved = probe()
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
    try:
        tenant_barrier_precheck(use_cache=True)
    except Exception:  # noqa: BLE001
        pass


def _canonical_tools(sse_events):
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
                starts[tid] = {"name": name, "ok": None, "duration_ms": None}
                order.append(tid)
        elif ev.get("event") == "tx":
            entry = starts.setdefault(tid, {"name": name, "ok": None, "duration_ms": None})
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


# Verbs that mutate. Matched against the *display* name as well as the
# canonical one, because G1d recorded an intercepted Teams post as
# "Post message" — a label absent from all 154 names in that probe's config.
# Matching only the denylist would therefore miss exactly the calls that matter.
_WRITE_VERBS = (
    "send", "post", "create", "update", "delete", "remove", "add",
    "edit", "write", "upload", "move", "reply", "forward", "schedule",
    "set ", "set_", "modify", "insert", "draft", "share", "invite",
)


def _looks_like_write(name):
    norm = _norm_tool(name)
    if not norm:
        return False
    if norm in _CONTAINER_TOOLS:
        return False
    if norm in {_norm_tool(n) for n in load_write_tools()}:
        return True
    return any(v in norm for v in _WRITE_VERBS)


# Denied for CONTAINMENT, not because they mutate M365. `Bash` is on the
# denylist so a run cannot shell out and bypass the barrier, but it touches
# nothing in the user's mailbox, is never intercepted, and so tripped the
# "write ran with no interception" rule on every single run. That alone
# accounted for most of the 12/18 false BREACHED verdicts measured against
# live rows on 2026-08-10.
_CONTAINER_TOOLS = frozenset({"bash", "task", "write_agent", "str_replace_editor"})


def _barrier_names():
    """Every spelling of a denylisted tool, matching build_callback_config()."""
    names = set()
    for tool in load_write_tools():
        for alias in _tool_aliases(tool):
            names |= _spellings(alias)
    return names


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
    """The G1c-1.21.88 denylist: 83 writes plus one retained query tool."""
    return json.loads(WRITE_TOOLS_PATH.read_text(encoding="utf-8"))


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


def start_preview(task_id, prompt, refs=None, *, spawn=None, log_dir=None) -> str:
    """Spawn a preview run and return its label. Non-blocking."""
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
    """
    if not text:
        return
    try:
        with _runs_lock:
            entry = _runs.get(label)
            if entry is not None:
                entry["progress"].append(text)
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
_HANDOFF_MAX_PAGES = 6     # ~300 tasks; ours sat on page 3 in the real capture
_handoff_cache = {"at": 0.0, "tasks": None}
_handoff_lock = threading.Lock()

# Cowork is blocked waiting for a human. This is how an approval prompt shows
# up from the outside, and it is the whole reason this is worth reading: TodoIQ
# can say "Cowork needs you" while owning no execute route whatsoever.
_WAITING_STATES = frozenset({"needs_user_input"})


def reset_handoff_cache() -> None:
    """Drop the cached task list. Used by tests and after an explicit refresh."""
    with _handoff_lock:
        _handoff_cache["at"] = 0.0
        _handoff_cache["tasks"] = None


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

    Fails soft on every path. This is decoration on a card that is already
    complete without it, so a throttled endpoint, an expired token or a shape
    change must degrade to today's behaviour rather than break the card.
    """
    if not conversation_id:
        return None

    get = _get or _cost_get
    try:
        now = time.monotonic()
        with _handoff_lock:
            fresh = (
                _handoff_cache["tasks"] is not None
                and now - _handoff_cache["at"] < _HANDOFF_TTL
            )
            tasks = _handoff_cache["tasks"] if fresh else None

        if tasks is None:
            tasks = _fetch_handoff_tasks(get)
            with _handoff_lock:
                _handoff_cache["tasks"] = tasks
                _handoff_cache["at"] = time.monotonic()

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

_TERMINAL_RUN_STATES = ("ok", "error", "failed", "completed")


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
                yield kind, json.loads(line[5:].strip())
            except (json.JSONDecodeError, TypeError):
                continue


def _api_payload_from_events(events, conversation_id):
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
                    starts[tid] = {"tool_name": name, "ok": None,
                                   "duration_seconds": None}
                    order.append(tid)
                if kind == "tx":
                    starts[tid]["ok"] = data.get("ok")
                    dur = data.get("dur")
                    if isinstance(dur, (int, float)):
                        starts[tid]["duration_seconds"] = round(dur / 1000.0, 3)
        elif kind == "rl":
            state = data.get("st")
            if state in _TERMINAL_RUN_STATES:
                terminal = state

    return {
        "terminal_status": terminal,
        "duration_seconds": None,
        "conversation_id": conversation_id,
        "tool_trace": [starts[t] for t in order],
        "text": "".join(text_parts),
        "sse_events": sse_events,
        "callback_exchanges": [],
    }


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


def _collect_api(label, task_id, prompt, config_path, log_dir) -> None:
    """Run one preview over the runtime HTTP API. Worker thread.

    Twin of ``_collect``. It MUST publish the same result dict shape, because
    ``parse_cowork_output`` and both UIs read it and neither knows which
    transport produced it.

    Cancellation is the capability the subprocess path lacks and the reason this
    exists: the CLI-as-library spike proved ``close_live()`` cannot halt a turn,
    while the API exposes a real cancel. The route is not wired up yet — that is
    a later phase — but this is the shape it hangs off.
    """
    concurrent = _active_run_count()
    cost_before = _cost_snapshot_fn()

    error = None
    stdout = ""
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        runner = _api_run_fn or _api_run_default
        payload = runner(prompt, config, lambda text: _append_progress(label, text))
        stdout = json.dumps(payload)
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
                "auth_failed": False,
                "cost_credits": cost,
            }


def _api_run_default(prompt, config, on_progress):
    """POST /v1/subscribe and fold the SSE stream into a CLI-shaped document.

    Imports live here so the module keeps loading when the API path is off,
    which is the default. Auth currently piggybacks on the CLI's MSAL cache; an
    independent device-code flow has NOT been exercised and is a prerequisite
    before this becomes the default transport.
    """
    import uuid

    import httpx
    import msal

    cfg_dir = Path(os.environ["APPDATA"]) / "cowork"
    cache = msal.SerializableTokenCache()
    cache.deserialize((cfg_dir / "msal_cache.bin").read_text(encoding="utf-8"))
    app = msal.PublicClientApplication(
        _API_CLIENT_ID, authority=_API_AUTHORITY, token_cache=cache,
    )
    account = app.get_accounts()[0]
    token = app.acquire_token_silent([_API_SCOPE], account=account)["access_token"]
    base = resolve_cowork_island() or get_cached_cowork_island()

    # "<oid>.<tenant>" must be split and REVERSED into "<tenant>:<oid>:<uuid>".
    # acct["realm"] is the string "organizations", not the tenant guid; using it
    # cost a 403 TENANT_MISMATCH.
    oid, tenant = account["home_account_id"].split(".", 1)
    conversation_id = f"{tenant}:{oid}:cw-{uuid.uuid4().hex[:8]}"

    body = {
        "conversationId": conversation_id,
        "role": "user",
        "content": [{"type": "text", "text": prompt}],
        "toolCallbackConfig": config,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    events = []
    with httpx.Client(timeout=httpx.Timeout(COWORK_TIMEOUT, connect=20.0)) as client:
        with client.stream(
            "POST", f"{base}/v1/subscribe", headers=headers, json=body,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"POST /v1/subscribe failed: HTTP {response.status_code}"
                )
            for kind, data in _iter_sse(response.iter_lines()):
                events.append((kind, data))
                if kind == "tk":
                    _api_progress(data, on_progress)
                if kind == "rl" and data.get("st") in _TERMINAL_RUN_STATES:
                    break

    return _api_payload_from_events(events, conversation_id)


def _api_progress(data, on_progress):
    """Surface a `tk` task-card update as one progress line, best effort."""
    try:
        items = data.get("items")
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict):
                text = item.get("af") or item.get("desc")
                if text:
                    on_progress(str(text))
    except Exception:  # noqa: BLE001
        logger.debug("api progress hook raised", exc_info=True)


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

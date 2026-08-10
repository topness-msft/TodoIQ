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

    result["barrier"] = _barrier_verdict(
        result["tool_trace"], payload.get("callback_exchanges"), text
    )
    if result["barrier"]["status"] == "BREACHED":
        # Loud on purpose. This is the one condition where a run that looks
        # entirely normal may have performed a real M365 write.
        logging.getLogger(__name__).error(
            "WRITE BARRIER: %s", result["barrier"]["reason"]
        )
        result["error"] = result["barrier"]["reason"]

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
from collections import deque
from pathlib import Path

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


def _barrier_verdict(tool_trace, callback_exchanges, text=""):
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
    if not writes:
        return {
            "status": "not_exercised",
            "reason": "No write tool was attempted, so the barrier was not tested.",
            "tools": [],
        }

    intercepted = _BLOCK_MARKER in (text or "") or bool(
        [e for e in (callback_exchanges or []) if isinstance(e, dict)]
    )
    if intercepted:
        return {
            "status": "held",
            "reason": "A write tool was called and interception was observed.",
            "tools": [],
        }

    names = ", ".join(dict.fromkeys(str(n) for n in writes))
    return {
        "status": "BREACHED",
        "reason": (
            f"Write tool ran with no sign of interception: {names}. The callback "
            f"barrier may not have engaged, so this action could have really "
            f"happened. Check the EVAL_ALLOWED_TENANTS gate (upstream #18550)."
        ),
        "tools": writes,
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
    if norm in {_norm_tool(n) for n in load_write_tools()}:
        return True
    return any(v in norm for v in _WRITE_VERBS)


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

    with _runs_lock:
        entry = _runs.get(label)
        if entry is not None:
            entry["result"] = {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "error": error,
                "auth_failed": auth_failed,
            }


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

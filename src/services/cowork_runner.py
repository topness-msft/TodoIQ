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
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "logs"
WRITE_TOOLS_PATH = Path(__file__).resolve().parent / "cowork_write_tools.json"

# claude_runner's 300s default would kill a live Cowork session mid-flight.
COWORK_TIMEOUT = 660

_BLOCK_MESSAGE = (
    "BLOCKED: TodoIQ preview mode intercepted this call. "
    "Nothing was sent, saved or modified. Do not retry, and do not attempt "
    "another tool to achieve the same effect. Report the draft instead."
)

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
        _runs[label] = {"proc": None, "thread": None, "result": None}

    config_path = build_callback_config(task_id, log_dir=log_dir)
    prompt_path = write_prompt_file(task_id, prompt, log_dir=log_dir)
    argv = build_argv(prompt_path, config_path, refs)

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


def _failure(error: str) -> dict:
    return {
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error": error,
        "auth_failed": False,
    }


def _collect(label, proc, task_id, log_dir, argv, spawn_fn) -> None:
    """Drain the child to completion. Runs on a worker thread.

    communicate() — never wait() then read(). The naive pattern deadlocks once
    the child exceeds the OS pipe buffer, and the spike output was already 21KB.
    """
    error = None
    stdout = stderr = ""
    try:
        stdout, stderr = proc.communicate(timeout=COWORK_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=15)
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

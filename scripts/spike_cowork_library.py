"""Try the Cowork CLI as a library, safely. Read-only.

Run me:

    cd <worktree>
    $env:PYTHONIOENCODING='utf-8'
    python scripts/spike_cowork_library.py            # barriered preview
    python scripts/spike_cowork_library.py --cancel   # spike 1: cancellation

WHAT THIS IS
------------
TodoIQ shells out to `cowork send --json`. The same package exposes a Python
API. This runs a real Cowork turn through that API so we can see what changes,
without touching TodoIQ.

SAFETY
------
It sends the SAME `tool_callback_config` the production path builds, so every
write tool is intercepted and returns a canned refusal. That is the only
empirically proven control we have (G1b), and `send_message` accepts it, which
is what makes a library migration survivable at all.

It also runs the tenant precheck first. The barrier is gated on
EVAL_ALLOWED_TENANTS server-side; if we are not on that list the config is
silently ignored and writes execute for real. If the precheck is not "ok" this
script refuses to run.

The prompt is read-only by construction: it asks Cowork to look something up and
report back. Nothing is drafted for sending.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.cowork_runner import (  # noqa: E402
    build_callback_config,
    tenant_barrier_precheck,
)

PROMPT = (
    "Look at my Microsoft 365 mail and Teams from the last two days and tell me, "
    "in three short bullets, what looks most likely to need a reply. "
    "Do not draft anything and do not send anything. Just report what you find."
)


def _rule(title):
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def _preflight():
    _rule("PREFLIGHT")
    pre = tenant_barrier_precheck()
    print(f"  tenant   : {pre['tenant_id'] or '(unknown)'}")
    print(f"  barrier  : {pre['status']}")
    if pre["status"] != "ok":
        print(f"\n  REFUSING TO RUN: {pre['reason']}")
        print("  The write barrier is tenant-gated. Without it, a write tool")
        print("  that the agent decides to call would execute for real.")
        return None

    from cowork_cli.auth.manager import AuthManager
    from cowork_cli.config.settings import get_settings

    settings = get_settings()
    auth = AuthManager(settings)
    if not auth.is_authenticated():
        print("\n  REFUSING TO RUN: Cowork is not authenticated.")
        print("  Run: cowork auth login")
        return None
    who = auth.whoami()
    print(f"  signed in: {who.username}")
    return settings, auth


def _callback_config():
    """The production barrier, verbatim - same builder, same denylist."""
    import json
    from pathlib import Path

    path = build_callback_config("spike")
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    tools = cfg.get("static_results") or {}
    print(f"  barrier  : {len(tools)} write tools intercepted")
    return cfg


def run_preview(settings, auth, cfg):
    from cowork_cli.services.session import SessionManager

    session = SessionManager(settings, auth)
    print(f"  island   : {session.base_url[:64]}")

    _rule("LIVE EVENTS (this is what the subprocess cannot give us in flight)")
    started = time.monotonic()
    counts: dict[str, int] = {}

    def on_event(kind, data):
        counts[kind] = counts.get(kind, 0) + 1
        el = time.monotonic() - started
        if kind == "ts":
            print(f"  {el:6.1f}s  TOOL START   {data.get('tn')}")
        elif kind == "tx":
            ok = data.get("ok")
            print(f"  {el:6.1f}s  TOOL DONE    {data.get('tn')}  ok={ok} {data.get('dur')}ms")
        elif kind == "rl":
            print(f"  {el:6.1f}s  RUN          {data.get('st')}")
        elif kind == "tk":
            t = (data.get("t") or data.get("c") or "").strip()
            if t:
                print(f"  {el:6.1f}s  status       {t[:60]}")

    resp = session.send_message(
        PROMPT,
        tool_callback_config=cfg,
        on_event=on_event,
        timeout=660,
    )

    _rule("RESULT")
    print(f"  terminal_status   : {resp.terminal_status!r}")
    print(f"  duration          : {resp.duration_seconds:.1f}s")
    print(f"  conversation_id   : {resp.conversation_id}")
    print(f"  tool calls        : {len(resp.tool_trace)}")
    print(f"  callback_exchanges: {len(resp.callback_exchanges)}")
    print(f"  sse events seen   : {sum(counts.values())}  {counts}")

    print("\n  --- tools, with CANONICAL names ---")
    for t in resp.tool_trace:
        print(f"    {getattr(t, 'tool_name', '?')}  ok={getattr(t, 'ok', '?')}")

    print("\n  --- text ---")
    text = resp.text or ""
    print("  " + (text[:900].replace("\n", "\n  ") or "(empty)"))

    if "BLOCKED: TodoIQ preview mode" in text:
        print("\n  >>> The barrier fired: a write tool was intercepted.")


def run_cancel_spike(settings, auth, cfg):
    """SPIKE 1, the gate on Phase 3: can an in-flight run be stopped?

    The subprocess can always be killed. If the library cannot, a hung preview
    has no bound, which is a reliability regression rather than gap-parity.
    """
    from cowork_cli.services.session import SessionManager

    _rule("SPIKE 1 - CANCELLATION")
    print("  Starting a real turn, then abandoning the thread after 20s.")
    print("  Watching whether anything can actually stop it.\n")

    session = SessionManager(settings, auth)
    done = threading.Event()
    box = {}

    def work():
        try:
            box["resp"] = session.send_message(
                PROMPT, tool_callback_config=cfg, timeout=660
            )
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    t = threading.Thread(target=work, daemon=True, name="cowork-cancel-spike")
    start = time.monotonic()
    t.start()

    finished = done.wait(timeout=20)
    if finished:
        print(f"  Run finished on its own in {time.monotonic() - start:.1f}s.")
        print("  Inconclusive: try again with a longer prompt.")
        return

    print(f"  Still running at {time.monotonic() - start:.1f}s. Attempting to stop it.")
    stops = [n for n in ("close_live", "cancel", "abort", "stop", "close")
             if hasattr(session, n)]
    print(f"  SessionManager stop-ish methods: {stops or 'NONE'}")

    for name in stops:
        try:
            getattr(session, name)()
            print(f"    called {name}()")
        except Exception as exc:  # noqa: BLE001
            print(f"    {name}() raised {type(exc).__name__}: {exc}")

    stopped = done.wait(timeout=30)
    elapsed = time.monotonic() - start

    _rule("SPIKE 1 VERDICT")
    if stopped:
        print(f"  STOPPED after {elapsed:.1f}s.")
        print("  A cancellation path exists. Phase 3 stays on the table.")
    else:
        print(f"  NOT STOPPED after {elapsed:.1f}s.")
        print("  Nothing on SessionManager halted the turn, and a Python thread")
        print("  cannot be killed. Under the library, a hung preview has no")
        print("  bound; the subprocess has proc.kill().")
        print("\n  That is a RELIABILITY REGRESSION, not gap-parity, and per the")
        print("  transport plan it is close to disqualifying for Phase 3.")
    print("\n  (The turn may still be running server-side; it is read-only.)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cancel", action="store_true",
                    help="run spike 1 (cancellation) instead of a preview")
    args = ap.parse_args()

    pre = _preflight()
    if not pre:
        return 1
    settings, auth = pre
    cfg = _callback_config()

    if args.cancel:
        run_cancel_spike(settings, auth, cfg)
    else:
        run_preview(settings, auth, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

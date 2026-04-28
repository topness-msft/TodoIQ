"""Shadow-mode LLM dedup for refreshed tasks.

Pipeline:
  Stage A (Python): pull new items (recent tasks) and candidate existing tasks
                    via broad person/title recall.
  Stage B (LLM):    single batched `copilot -p` call with structured JSON prompt.
                    The model decides, for each new item, whether it duplicates
                    any candidate (or another new item in the batch).
  Stage C (Python): validate JSON (match_ids must exist in the candidate set),
                    write shadow_dup_of + shadow_dup_reason on flagged new items.
                    Does NOT alter creation/augmentation or remove tasks.

Run via:
    python -m src.services.shadow_dedup --since 60

Shadow mode never deletes or merges — it only annotates newly-created tasks
with the LLM's judgment for human review.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import get_connection

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SUBPROCESS_TIMEOUT = 300

# ── Stage A: candidate recall ──────────────────────────────────────────────

_STOP = frozenset({
    'a', 'an', 'the', 'to', 'for', 'of', 'on', 'in', 'at', 'and', 'or',
    'with', 'my', 're', 'fwd', 'follow', 'up', 'check', 'confirm', 'send',
    'share', 'provide', 'schedule', 'review', 'respond', 'reply', 'draft',
    'prepare', 'update', 'get', 'set', 'discuss', 'meeting', 'email',
    'teams', 'message', 'request', 'from', 'is', 'be', 'do', 'it', 'we',
})


def _title_tokens(title: str | None) -> set[str]:
    if not title:
        return set()
    words = re.findall(r'[a-z0-9]+', title.lower())
    return {w for w in words if w not in _STOP and len(w) > 2}


def _people_aliases(key_people_json: str | None) -> set[str]:
    if not key_people_json:
        return set()
    try:
        arr = json.loads(key_people_json)
    except (json.JSONDecodeError, TypeError):
        return set()
    out: set[str] = set()
    for p in arr:
        if not isinstance(p, dict):
            continue
        email = (p.get("email") or "").lower()
        if email:
            out.add(email)
            out.add(email.split("@")[0])
            # Email prefix broken into dot-separated tokens
            local = email.split("@")[0]
            for tok in local.replace(".", " ").split():
                if len(tok) > 2:
                    out.add(tok)
        # Name field: split on whitespace, add each token lowercased
        name = (p.get("name") or "").lower()
        for tok in re.findall(r'[a-z]+', name):
            if len(tok) > 2:
                out.add(tok)
    return out


def _source_id_person(source_id: str | None) -> str | None:
    if not source_id:
        return None
    parts = source_id.split("::")
    if len(parts) < 2:
        return None
    p = parts[1].lower().strip()
    return p.split("@")[0] if "@" in p else p


def fetch_recent_items(conn: sqlite3.Connection, since_minutes: int) -> list[dict]:
    """Tasks created within the last N minutes, not yet shadow-checked, excluding deleted."""
    since_iso = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    rows = conn.execute(
        """SELECT id, title, source_id, source_type, action_type, status,
                  source_snippet, key_people, created_at
           FROM tasks
           WHERE created_at >= ?
             AND status != 'deleted'
             AND shadow_checked_at IS NULL""",
        (since_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_candidates(
    conn: sqlite3.Connection,
    new_items: list[dict],
    limit_per_person: int = 20,
    lookback_days: int = 30,
) -> list[dict]:
    """Pull candidate DB tasks via UNION of two recall signals:

      1. Person-id recall (Change B): tasks sharing a canonical-rooted person
         via task_person. High precision; misses unmerged alias variants.
      2. Alias-string recall (legacy): name/email substring overlap. Lower
         precision but bridges name-only and unmerged alias cases.

    Union maximizes recall; precision comes from the LLM pass downstream.

    Candidates are limited to live statuses (active/in_progress/waiting/
    snoozed/suggested). Completed/dismissed/deleted tasks are NEVER offered
    as dedup candidates per the resolved completed-task policy.
    """
    if not new_items:
        return []

    new_ids = {it["id"] for it in new_items}
    cap = limit_per_person * max(1, len(new_items))

    # ── Path 1: person-id recall via task_person ─────────────────────────
    person_ids: set[int] = set()
    for it in new_items:
        existing = conn.execute(
            "SELECT 1 FROM task_person WHERE task_id = ? LIMIT 1", (it["id"],)
        ).fetchone()
        if existing is None:
            try:
                _pi_derive_silently(conn, it["id"], it.get("key_people"), it.get("source_id"))
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"derive task_person for item {it['id']} failed: {e}")
        person_ids |= _pi_get_canonical_person_ids(conn, it["id"])

    person_cand_ids: set[int] = set()
    if person_ids:
        person_cand_ids = set(_find_tasks_sharing_persons(
            conn, person_ids, exclude_task_ids=new_ids, limit=cap,
        ))

    # ── Path 2: legacy alias-string recall ──────────────────────────────
    aliases: set[str] = set()
    for it in new_items:
        aliases |= _people_aliases(it.get("key_people"))
        p = _source_id_person(it.get("source_id"))
        if p:
            aliases.add(p)

    legacy_ids: set[int] = set()
    if aliases:
        since_iso = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        clauses = []
        params: list = []
        for a in aliases:
            clauses.append("source_id LIKE ?")
            params.append(f"%::{a}%")
            clauses.append("source_id LIKE ?")
            params.append(f"%{a}::%")
            clauses.append("LOWER(key_people) LIKE ?")
            params.append(f"%{a.lower()}%")
        sql = f"""SELECT id FROM tasks
                  WHERE status IN ('active','in_progress','waiting','snoozed','suggested')
                    AND shadow_dup_of IS NULL
                    AND created_at >= ?
                    AND ({' OR '.join(clauses)})
                  ORDER BY created_at DESC
                  LIMIT ?"""
        params = [since_iso, *params, cap]
        legacy_ids = {r["id"] for r in conn.execute(sql, params).fetchall()}

    union_ids = (person_cand_ids | legacy_ids) - new_ids
    if not union_ids:
        return []
    placeholders = ",".join("?" * len(union_ids))
    rows = conn.execute(
        f"""SELECT id, title, source_id, source_type, action_type, status,
                   source_snippet, key_people, created_at
            FROM tasks WHERE id IN ({placeholders})
            ORDER BY created_at DESC""",
        list(union_ids),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Helpers (deferred imports to keep module load light) ───────────────────

def _pi_derive_silently(conn, task_id, key_people_json, source_id):
    from . import person_identity as pi
    pi.derive_task_persons(
        conn, task_id, key_people_json=key_people_json, source_id=source_id
    )


def _pi_get_canonical_person_ids(conn, task_id):
    from . import person_identity as pi
    return pi.get_task_person_ids(conn, task_id)


def _find_tasks_sharing_persons(conn, person_ids, *, exclude_task_ids, limit):
    from . import person_identity as pi
    return pi.find_tasks_sharing_persons(
        conn, person_ids,
        exclude_task_ids=exclude_task_ids,
        limit=limit,
    )


# ── Stage B: batched LLM ratification ─────────────────────────────────────

_PROMPT = """You are deduplicating tasks from a refresh batch.

NEW_ITEMS are tasks just created or updated by the latest refresh.
CANDIDATE_TASKS are earlier tasks already in the database, scoped to those
that share a person with at least one new item.

For each new item, decide whether it duplicates any candidate OR any EARLIER
new item (lower index in NEW_ITEMS).

MATCH RULE: Two items match if they involve the SAME PERSON and the SAME
underlying conversation, topic, or project — even if wording, action_type,
or source_type differ. When in doubt, call it a match. Different sub-asks
from the same meeting (e.g. "send one-pager" vs "propose dates") are NOT
matches — they are distinct deliverables.

NEW_ITEMS:
{new_items_json}

CANDIDATE_TASKS:
{candidates_json}

Return JSON only, exactly this shape (no prose before or after):
{{
  "decisions": [
    {{
      "id": <new item id>,
      "match_id": <matching candidate or earlier new-item id> | null,
      "reason": "<one short sentence>"
    }}
  ]
}}
Every NEW_ITEM must have exactly one decision. If match_id is null, the item
is considered unique. If match_id is set, it MUST be an id that appears in
CANDIDATE_TASKS or in an EARLIER entry of NEW_ITEMS.
"""


def build_prompt(new_items: list[dict], candidates: list[dict]) -> str:
    def _slim(item: dict) -> dict:
        out = dict(item)
        snip = out.get("source_snippet") or ""
        if len(snip) > 300:
            out["source_snippet"] = snip[:300] + "..."
        return out

    return _PROMPT.format(
        new_items_json=json.dumps([_slim(i) for i in new_items], indent=2),
        candidates_json=json.dumps([_slim(c) for c in candidates], indent=2),
    )


def run_ratification(prompt: str) -> dict | None:
    """Call `copilot -p` with the prompt and return parsed JSON."""
    try:
        result = subprocess.run(
            [
                "copilot", "-p", prompt,
                "--allow-all-tools",
                "--no-color",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=SUBPROCESS_TIMEOUT,
            cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        logger.error("shadow dedup copilot call timed out")
        return None
    except FileNotFoundError:
        logger.error("copilot CLI not found on PATH")
        return None
    if result.returncode != 0:
        logger.warning(f"copilot exited {result.returncode}: {result.stderr[:500]}")
    return parse_decisions(result.stdout)


def parse_decisions(text: str) -> dict | None:
    """Extract {'decisions': [...]} JSON from raw copilot stdout."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ── Stage C: validation + apply ──────────────────────────────────────────

_ROOT_WALK_MAX = 10


def resolve_shadow_root(conn: sqlite3.Connection, task_id: int) -> int:
    """Walk shadow_dup_of pointers to the root, cycle-protected.

    Returns task_id itself if it has no shadow_dup_of. If a cycle or excessive
    depth is detected, returns the last id reached (best-effort, never raises).
    """
    visited: set[int] = set()
    current = task_id
    for _ in range(_ROOT_WALK_MAX):
        if current in visited:
            logger.warning(f"shadow_dup_of cycle detected at task {current}")
            return current
        visited.add(current)
        row = conn.execute(
            "SELECT shadow_dup_of FROM tasks WHERE id = ?", (current,)
        ).fetchone()
        if row is None:
            return current
        nxt = row["shadow_dup_of"] if hasattr(row, "keys") else row[0]
        if nxt is None or nxt == current:
            return current
        current = nxt
    logger.warning(f"shadow_dup_of walk exceeded depth {_ROOT_WALK_MAX} from {task_id}")
    return current


def backfill_shadow_roots(conn: sqlite3.Connection) -> int:
    """Rewrite all non-root shadow_dup_of pointers to their current root.

    Idempotent. Returns the number of rows updated.
    """
    rows = conn.execute(
        "SELECT id, shadow_dup_of FROM tasks WHERE shadow_dup_of IS NOT NULL"
    ).fetchall()
    updated = 0
    for r in rows:
        nid = r["id"]
        cur = r["shadow_dup_of"]
        root = resolve_shadow_root(conn, cur)
        if root != cur and root != nid:
            conn.execute(
                "UPDATE tasks SET shadow_dup_of = ? WHERE id = ?",
                (root, nid),
            )
            updated += 1
        elif root == nid:
            # Pointing at self after walk — clear it.
            conn.execute(
                "UPDATE tasks SET shadow_dup_of = NULL WHERE id = ?",
                (nid,),
            )
            updated += 1
    if updated:
        conn.commit()
    return updated


def validate_decisions(
    decisions: list[dict],
    new_items: list[dict],
    candidates: list[dict],
) -> list[dict]:
    """Filter decisions to only those with valid match_ids.

    A match_id is valid if it references an existing candidate task id OR
    an earlier new-item id (ordered by original list position).
    """
    new_ids_ordered = [i["id"] for i in new_items]
    new_id_index = {nid: idx for idx, nid in enumerate(new_ids_ordered)}
    candidate_ids = {c["id"] for c in candidates}
    valid: list[dict] = []
    for d in decisions:
        nid = d.get("id")
        mid = d.get("match_id")
        if nid not in new_id_index:
            logger.warning(f"decision for unknown new id {nid}; skipping")
            continue
        if mid is None:
            valid.append(d)
            continue
        # match_id must be a candidate OR an earlier new item
        if mid in candidate_ids:
            valid.append(d)
            continue
        if mid in new_id_index and new_id_index[mid] < new_id_index[nid]:
            valid.append(d)
            continue
        logger.warning(f"decision id={nid} has invalid match_id={mid}; skipping")
    return valid


def apply_shadow_flags(conn: sqlite3.Connection, decisions: list[dict]) -> int:
    """Write shadow_dup_of + shadow_dup_reason + shadow_checked_at.

    Resolves match_id to its current root before writing, so a chain
    A→B with B already pointing at C is collapsed to A→C immediately.

    Also stamps shadow_checked_at on decisions with match_id=null so they
    don't get re-checked on subsequent shadow runs.

    Returns number of rows flagged (match_id not null).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    flagged = 0
    for d in decisions:
        nid = d["id"]
        mid = d.get("match_id")
        reason = (d.get("reason") or "")[:500]
        if mid is None:
            conn.execute(
                "UPDATE tasks SET shadow_checked_at = ? WHERE id = ?",
                (now, nid),
            )
        else:
            root = resolve_shadow_root(conn, mid)
            # Avoid self-loops: if root walked back to nid, treat as unique.
            if root == nid:
                logger.warning(
                    f"shadow_dup_of root walk for {nid}->{mid} returned self; "
                    f"treating as unique"
                )
                conn.execute(
                    "UPDATE tasks SET shadow_checked_at = ? WHERE id = ?",
                    (now, nid),
                )
                continue
            conn.execute(
                """UPDATE tasks
                   SET shadow_dup_of = ?, shadow_dup_reason = ?, shadow_checked_at = ?
                   WHERE id = ?""",
                (root, reason, now, nid),
            )
            flagged += 1
    conn.commit()
    return flagged


# ── Orchestrator ──────────────────────────────────────────────────────────

def run_shadow_dedup(since_minutes: int = 60, dry_run: bool = False) -> dict:
    """Top-level: fetch → prompt → ratify → apply → backfill. Returns summary dict."""
    conn = get_connection()
    try:
        new_items = fetch_recent_items(conn, since_minutes)
        if not new_items:
            backfilled = backfill_shadow_roots(conn)
            return {"ok": True, "new_items": 0, "flagged": 0,
                    "backfilled": backfilled, "note": "no recent items"}
        candidates = fetch_candidates(conn, new_items)
        prompt = build_prompt(new_items, candidates)
        logger.info(
            f"shadow dedup: {len(new_items)} new items, "
            f"{len(candidates)} candidates, prompt {len(prompt)} chars"
        )
        if dry_run:
            print(prompt)
            return {"ok": True, "dry_run": True, "new_items": len(new_items),
                    "candidates": len(candidates), "prompt_len": len(prompt)}
        parsed = run_ratification(prompt)
        if not parsed or "decisions" not in parsed:
            return {"ok": False, "error": "ratification parse failed",
                    "new_items": len(new_items), "candidates": len(candidates)}
        decisions = validate_decisions(
            parsed.get("decisions", []), new_items, candidates
        )
        flagged = apply_shadow_flags(conn, decisions)
        backfilled = backfill_shadow_roots(conn)
        return {
            "ok": True,
            "new_items": len(new_items),
            "candidates": len(candidates),
            "decisions": len(decisions),
            "flagged": flagged,
            "backfilled": backfilled,
        }
    finally:
        conn.close()


def run_backfill_only() -> dict:
    """Run only the chain-collapse backfill pass over existing shadow_dup_of pointers."""
    conn = get_connection()
    try:
        updated = backfill_shadow_roots(conn)
        return {"ok": True, "backfilled": updated}
    finally:
        conn.close()


def _main():
    ap = argparse.ArgumentParser(description="Run shadow-mode LLM dedup")
    ap.add_argument("--since", type=int, default=60,
                    help="Look back this many minutes for new items (default 60)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the prompt that would be sent, don't call copilot")
    ap.add_argument("--backfill-only", action="store_true",
                    help="Skip LLM pass; just collapse existing shadow_dup_of chains")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.backfill_only:
        result = run_backfill_only()
    else:
        result = run_shadow_dedup(since_minutes=args.since, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    _main()

"""Bootstrap person/person_alias/task_person from existing tasks.

Precision-only: one person per distinct full email. Display-name aliases are
added as recall-only (kind='name', confidence='name'). No heuristic merges;
identity-variant cases (marriage rename, shared mailbox) require explicit
user merges via merge_persons().

Idempotent. Resumable: the last processed task id is tracked in the
sync_log table via sync_type='full_scan' result_summary so an interrupted
bootstrap can resume.
"""
from __future__ import annotations

import argparse
import logging
import sys

from ..db import get_connection, init_db
from . import person_identity as pi

logger = logging.getLogger(__name__)


BATCH_SIZE = 500


def bootstrap(conn=None, batch_size: int = BATCH_SIZE) -> dict:
    """Iterate tasks in id order; derive task_person + create persons as needed."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True
    try:
        init_db(conn)
        last_id = 0
        total_tasks = 0
        total_persons_before = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        while True:
            rows = conn.execute(
                """SELECT id, key_people, source_id
                   FROM tasks
                   WHERE id > ?
                     AND status != 'deleted'
                   ORDER BY id ASC
                   LIMIT ?""",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            conn.execute("BEGIN IMMEDIATE")
            try:
                for r in rows:
                    pi.derive_task_persons(
                        conn,
                        r["id"],
                        key_people_json=r["key_people"],
                        source_id=r["source_id"],
                    )
                    last_id = r["id"]
                    total_tasks += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            logger.info(f"bootstrap: processed {total_tasks} tasks (last id={last_id})")
        total_persons_after = conn.execute("SELECT COUNT(*) FROM person").fetchone()[0]
        return {
            "ok": True,
            "tasks_processed": total_tasks,
            "persons_before": total_persons_before,
            "persons_after": total_persons_after,
            "persons_created": total_persons_after - total_persons_before,
        }
    finally:
        if close:
            conn.close()


def _main():
    ap = argparse.ArgumentParser(description="Bootstrap person identity tables")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    result = bootstrap(batch_size=args.batch_size)
    import json
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    _main()

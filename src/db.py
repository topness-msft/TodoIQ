"""SQLite database initialization and connection management for TodoNess."""

import os
import sqlite3
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DB_DIR / "claudetodo.db"


def get_connection() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection):
    """Add columns that may be missing from older databases."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "action_type" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN action_type TEXT DEFAULT 'general'")
        conn.commit()
    if "skill_output" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN skill_output TEXT")
        conn.commit()

    # Migrate sync_log to support 'full_scan' sync_type
    sync_types = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sync_log'"
        ).fetchall()
    ]
    if sync_types and "full_scan" not in (sync_types[0] or ""):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sync_log_new (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_type       TEXT NOT NULL
                                    CHECK (sync_type IN ('flagged_emails','meetings','task_refresh','manual','full_scan')),
                result_summary  TEXT,
                tasks_created   INTEGER DEFAULT 0,
                tasks_updated   INTEGER DEFAULT 0,
                synced_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );
            INSERT INTO sync_log_new SELECT * FROM sync_log;
            DROP TABLE sync_log;
            ALTER TABLE sync_log_new RENAME TO sync_log;
        """)


def init_db(conn: sqlite3.Connection | None = None):
    """Create all tables if they don't exist."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    conn.executescript(SCHEMA_SQL)
    _migrate(conn)

    if close:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('suggested','active','in_progress','completed','dismissed','deleted')),
    parse_status    TEXT NOT NULL DEFAULT 'parsed'
                        CHECK (parse_status IN ('unparsed','queued','parsing','parsed')),
    raw_input       TEXT,
    priority        INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    due_date        TEXT,
    committed_date  TEXT,
    source_type     TEXT DEFAULT 'manual'
                        CHECK (source_type IN ('email','meeting','chat','manual')),
    source_id       TEXT,
    source_url      TEXT,
    source_snippet  TEXT,
    coaching_text   TEXT,
    action_type     TEXT DEFAULT 'general'
                        CHECK (action_type IN ('schedule-meeting','respond-email','review-document','follow-up','awaiting-response','prepare','general')),
    skill_output    TEXT,
    key_people      TEXT,
    related_meeting TEXT,
    user_notes      TEXT DEFAULT '',
    suggestion_refreshed_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS task_context (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    context_type  TEXT NOT NULL
                      CHECK (context_type IN ('email_thread','meeting','calendar_event','suggestion')),
    content       TEXT NOT NULL,
    query_used    TEXT,
    fetched_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS refresh_schedule (
    task_id                INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    interval_minutes       INTEGER NOT NULL DEFAULT 30,
    next_refresh_at        TEXT,
    last_refresh_at        TEXT,
    consecutive_no_change  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type       TEXT NOT NULL
                        CHECK (sync_type IN ('flagged_emails','meetings','task_refresh','manual','full_scan')),
    result_summary  TEXT,
    tasks_created   INTEGER DEFAULT 0,
    tasks_updated   INTEGER DEFAULT 0,
    synced_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parse_status ON tasks(parse_status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_task_context_task_id ON task_context(task_id);
CREATE INDEX IF NOT EXISTS idx_refresh_next ON refresh_schedule(next_refresh_at);
"""

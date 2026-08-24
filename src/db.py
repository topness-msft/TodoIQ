"""SQLite database initialization and connection management for TodoNess."""

import os
import sqlite3
from pathlib import Path

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "claudetodo.db"
DB_PATH = Path(os.environ.get("TODONESS_DB_PATH", _DEFAULT_DB_PATH)).resolve()
DB_DIR = DB_PATH.parent

_STATUS_CHECK = (
    "CHECK (status IN ('suggested','active','in_progress','waiting','snoozed',"
    "'completed','dismissed','deleted'))"
)
_PARSE_STATUS_CHECK = (
    "CHECK (parse_status IN ('unparsed','queued','parsing','parsed','error'))"
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _value(row, key: str, index: int):
    return row[key] if isinstance(row, sqlite3.Row) else row[index]


def _task_column_definition(row) -> str:
    name = _value(row, "name", 1)
    quoted = _quote_identifier(name)
    if name == "id":
        return f"{quoted} INTEGER PRIMARY KEY AUTOINCREMENT"
    if name == "status":
        return f"{quoted} TEXT NOT NULL DEFAULT 'active' {_STATUS_CHECK}"
    if name == "parse_status":
        return f"{quoted} TEXT NOT NULL DEFAULT 'parsed' {_PARSE_STATUS_CHECK}"
    if name == "priority":
        return f"{quoted} INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5)"
    if name == "source_type":
        return (
            f"{quoted} TEXT DEFAULT 'manual' "
            "CHECK (source_type IN ('email','meeting','chat','manual'))"
        )
    if name == "action_type":
        return (
            f"{quoted} TEXT DEFAULT 'general' CHECK (action_type IN "
            "('schedule-meeting','respond-email','review-document','follow-up',"
            "'awaiting-response','prepare','teams-message','general'))"
        )

    definition = f"{quoted} {_value(row, 'type', 2) or 'TEXT'}"
    if _value(row, "notnull", 3):
        definition += " NOT NULL"
    default = _value(row, "dflt_value", 4)
    if default is not None:
        text = str(default).strip()
        # SQLite requires an expression default to be parenthesised, but
        # PRAGMA table_info reports it without the parentheses. Re-emitting one
        # bare produces "syntax error near (" and would abort the rebuild, so
        # any call-shaped default is wrapped back up.
        if "(" in text and not text.startswith("("):
            text = f"({text})"
        definition += f" DEFAULT {text}"
    if _value(row, "pk", 5):
        definition += " PRIMARY KEY"
    return definition


def _tables_referencing_tasks(conn: sqlite3.Connection) -> dict[str, int]:
    counts = {}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table in tables:
        name = _value(table, "name", 0)
        foreign_keys = conn.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(name)})"
        ).fetchall()
        if any(_value(key, "table", 2) == "tasks" for key in foreign_keys):
            counts[name] = conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(name)}"
            ).fetchone()[0]
    return counts


def _rebuild_tasks_constraints(conn: sqlite3.Connection) -> None:
    """Widen task CHECK constraints without dropping columns or child rows."""
    columns = conn.execute("PRAGMA table_info(tasks)").fetchall()
    names = [_value(row, "name", 1) for row in columns]
    task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    child_counts = _tables_referencing_tasks(conn)
    indexes = [
        _value(row, "sql", 0)
        for row in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='tasks' AND sql IS NOT NULL"
        ).fetchall()
    ]
    triggers = [
        _value(row, "sql", 0)
        for row in conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='trigger' AND tbl_name='tasks' AND sql IS NOT NULL"
        ).fetchall()
    ]

    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        definitions = ", ".join(
            _task_column_definition(row) for row in columns
        )
        conn.execute(f"CREATE TABLE tasks_new ({definitions})")
        quoted_names = ", ".join(_quote_identifier(name) for name in names)
        conn.execute(
            f"INSERT INTO tasks_new ({quoted_names}) "
            f"SELECT {quoted_names} FROM tasks"
        )
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
        for statement in indexes + triggers:
            conn.execute(statement)

        if conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] != task_count:
            raise RuntimeError("Task migration row-count mismatch")
        for table, expected in child_counts.items():
            actual = conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
            if actual != expected:
                raise RuntimeError(f"Task migration changed {table} row count")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Task migration failed foreign-key validation")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def get_connection() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def backfill_source_locators(conn: sqlite3.Connection) -> int:
    """Recover re-openable source ids from links already on disk.

    1,270 of the 2,371 live tasks carry a resolvable Teams conversation inside
    `source_url` that nothing had ever read out. This reads them once, at
    startup, rather than on every request: `list_tasks` fetches the whole
    non-deleted set (models.py), so deriving per read would put ~1,900 regexes
    on each dashboard load for a value that does not change.

    Only rows with an empty column are touched, so anything captured at task
    creation - better evidence than anything reconstructable afterwards - is
    left exactly as it was. Returns how many rows were filled.
    """
    # Imported here rather than at module scope to keep db.py free of a
    # dependency on the services layer, which is the direction the rest of the
    # package imports in.
    from .services import source_locator

    # A sufficiently old database may predate either column. There is nothing to
    # recover from a table with no links in it, and this runs on every startup,
    # so it asks rather than assumes.
    columns = {_value(row, "name", 1)
               for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if not {"source_locator", "source_url"} <= columns:
        return 0

    rows = conn.execute(
        "SELECT id, source_url FROM tasks "
        "WHERE source_locator IS NULL AND source_url IS NOT NULL "
        "AND TRIM(source_url) != ''"
    ).fetchall()

    filled = []
    for row in rows:
        located = source_locator.from_source_url(_value(row, "source_url", 1))
        if located:
            filled.append((source_locator.to_json(located), _value(row, "id", 0)))

    if filled:
        conn.executemany(
            "UPDATE tasks SET source_locator = ? WHERE id = ?", filled
        )
        conn.commit()
    return len(filled)


def _migrate(conn: sqlite3.Connection):
    """Add columns that may be missing from older databases."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if "cowork_revision" not in cols:
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN cowork_revision INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        cols.append("cowork_revision")
    if "action_type" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN action_type TEXT DEFAULT 'general'")
        conn.commit()
    if "skill_output" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN skill_output TEXT")
        conn.commit()
    if "snoozed_until" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN snoozed_until TEXT")
        conn.commit()
    if "waiting_activity" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN waiting_activity TEXT")
        conn.commit()
    for column, definition in (
        ("source_date", "TEXT"),
        ("error_message", "TEXT"),
        ("cowork_prompt", "TEXT"),
        ("is_quick_hit", "INTEGER NOT NULL DEFAULT 0"),
        # Where the task came from, in a form that can be re-opened. source_id
        # cannot do this: it is a dedup key built from type/person/subject, so
        # two different threads about one subject collide by design. No CHECK
        # constraint here, so this never triggers _rebuild_tasks_constraints.
        ("source_locator", "TEXT"),
    ):
        if column not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
            conn.commit()
            cols.append(column)

    # SQLite cannot widen CHECK constraints in place. Rebuild once, preserving
    # every existing/unknown column and all child rows.
    task_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    sql = (task_sql[0] or "") if task_sql else ""
    if (
        "'snoozed'" not in sql
        or "'error'" not in sql
        or "'teams-message'" not in sql
    ):
        _rebuild_tasks_constraints(conn)

    action_cols = [
        r[1] for r in conn.execute("PRAGMA table_info(task_actions)").fetchall()
    ]
    if "cowork_revision" not in action_cols:
        conn.execute(
            "ALTER TABLE task_actions ADD COLUMN cowork_revision INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        action_cols.append("cowork_revision")
    if "cost_credits" not in action_cols:
        # Credits consumed by one preview, measured as the difference in the
        # user's month-to-date counter (GET /v1/cost) across the run. REAL, not
        # TEXT: it is a number and gets formatted for display.
        conn.execute("ALTER TABLE task_actions ADD COLUMN cost_credits REAL")
        conn.commit()
    if "seen_at" not in action_cols:
        conn.execute("ALTER TABLE task_actions ADD COLUMN seen_at TEXT")
        conn.commit()
    if "island_url" not in action_cols:
        conn.execute("ALTER TABLE task_actions ADD COLUMN island_url TEXT")
        conn.commit()
    # Audience binding. SQLite cannot add a CHECK with ALTER TABLE, so the
    # allowed delivery_channel values are enforced by the API layer as well.
    for column in (
        "delivery_channel",
        "destination_display",
        "destination_confirmed_at",
        "destination_source",
    ):
        if column not in action_cols:
            conn.execute(f"ALTER TABLE task_actions ADD COLUMN {column} TEXT")
            conn.commit()
    if "parent_action_id" not in action_cols:
        # A refine turn continues an existing Cowork conversation rather than
        # starting a new one. It is still its OWN row so the correction chain
        # stays auditable, and this points back at the attempt it refines.
        conn.execute(
            "ALTER TABLE task_actions ADD COLUMN parent_action_id INTEGER"
        )
        conn.commit()
    if "blocked_question" not in action_cols:
        conn.execute(
            "ALTER TABLE task_actions ADD COLUMN blocked_question TEXT"
        )
        conn.commit()
    if "answered_interaction" not in action_cols:
        conn.execute(
            "ALTER TABLE task_actions ADD COLUMN answered_interaction TEXT"
        )
        conn.commit()
    if "interaction_mode" not in action_cols:
        conn.execute(
            "ALTER TABLE task_actions ADD COLUMN interaction_mode TEXT "
            "NOT NULL DEFAULT 'interaction'"
        )
        conn.commit()
    if "completed_at" not in action_cols:
        conn.execute("ALTER TABLE task_actions ADD COLUMN completed_at TEXT")
        conn.execute(
            "UPDATE task_actions SET completed_at = updated_at "
            "WHERE state IN ('ready','failed') AND completed_at IS NULL"
        )
        conn.commit()
    if "had_interaction" not in action_cols:
        conn.execute(
            "ALTER TABLE task_actions ADD COLUMN had_interaction INTEGER "
            "NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE task_actions SET had_interaction = 1 "
            "WHERE blocked_question IS NOT NULL "
            "OR answered_interaction IS NOT NULL"
        )
        conn.commit()
    for column in ("execution_requested_at", "delivery_confirmed_at"):
        if column not in action_cols:
            conn.execute(f"ALTER TABLE task_actions ADD COLUMN {column} TEXT")
            conn.commit()

    action_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_actions'"
    ).fetchone()
    action_definition = (action_sql[0] or "") if action_sql else ""
    if (
        "execute_unconfirmed" not in action_definition
        or "'calendar'" not in action_definition
        or "structured_payload" not in action_cols
        or "workiq_delivery_ref" not in action_cols
    ):
        _migrate_task_action_execution_states(conn)

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

    _migrate_identity_schema(conn)


def _add_optional_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        _value(row, "name", 1)
        for row in conn.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {_quote_identifier(table)} "
                f"ADD COLUMN {_quote_identifier(name)} {definition}"
            )


def _migrate_identity_schema(conn: sqlite3.Connection) -> None:
    provenance = {
        "evidence_kind": "TEXT",
        "evidence_ref": "TEXT",
        "observed_at": "TEXT",
        "confirmation_mode": "TEXT",
        "confirmed_at": "TEXT",
        "lookup_kind": "TEXT",
    }
    _add_optional_columns(conn, "person_alias", provenance)
    _add_optional_columns(conn, "task_person", provenance)

    marker = conn.execute(
        "SELECT id FROM person_backfill_state WHERE id=1"
    ).fetchone()
    if not marker:
        existing_identity = sum(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("person", "person_alias", "task_person")
        )
        status = "legacy_untracked" if existing_identity else "not_started"
        conn.execute(
            """
            INSERT INTO person_backfill_state (
                id, status, last_task_id, revision, updated_at
            ) VALUES (1, ?, 0, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            """,
            (status,),
        )
    conn.commit()


def _migrate_task_action_execution_states(conn: sqlite3.Connection) -> None:
    """Rebuild task_actions because SQLite cannot widen CHECKs in place."""
    columns = [
        row[1] for row in conn.execute("PRAGMA table_info(task_actions)").fetchall()
    ]
    current_columns = {
        "id", "task_id", "action_type", "cowork_revision", "state", "intent",
        "notes_snapshot", "redirect_text", "composed_prompt", "finding", "draft",
        "draft_edited", "destination_kind", "destination_ref", "conversation_id",
        "terminal_status", "tool_trace", "cost_credits", "error", "seen_at",
        "island_url", "delivery_channel", "destination_display",
        "destination_confirmed_at", "destination_source", "parent_action_id",
        "blocked_question", "answered_interaction", "interaction_mode",
        "completed_at", "had_interaction", "execution_requested_at",
        "delivery_confirmed_at", "structured_payload", "workiq_delivery_ref",
        "created_at", "updated_at",
    }
    copy_columns = ", ".join(column for column in columns if column in current_columns)
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            """
            CREATE TABLE task_actions_new (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                action_type      TEXT DEFAULT 'general',
                cowork_revision  INTEGER NOT NULL DEFAULT 0,
                state            TEXT NOT NULL DEFAULT 'previewing'
                                     CHECK (state IN (
                                         'previewing','ready','failed',
                                         'executing','executed','execute_unconfirmed'
                                     )),
                intent           TEXT,
                notes_snapshot   TEXT,
                redirect_text    TEXT,
                composed_prompt  TEXT,
                finding          TEXT,
                draft            TEXT,
                draft_edited     TEXT,
                destination_kind TEXT
                                     CHECK (destination_kind IS NULL OR destination_kind IN (
                                         'one_to_one','group','meeting','channel','unknown','none'
                                     )),
                destination_ref  TEXT,
                conversation_id  TEXT,
                terminal_status  TEXT,
                tool_trace       TEXT,
                cost_credits     REAL,
                error            TEXT,
                seen_at          TEXT,
                island_url       TEXT,
                delivery_channel TEXT
                                     CHECK (delivery_channel IS NULL OR delivery_channel IN ('teams','email','calendar')),
                destination_display TEXT,
                destination_confirmed_at TEXT,
                destination_source TEXT,
                parent_action_id INTEGER REFERENCES task_actions_new(id),
                blocked_question TEXT,
                answered_interaction TEXT,
                interaction_mode TEXT NOT NULL DEFAULT 'interaction'
                                      CHECK (interaction_mode IN ('interaction','no_interaction')),
                completed_at     TEXT,
                had_interaction  INTEGER NOT NULL DEFAULT 0
                                      CHECK (had_interaction IN (0,1)),
                execution_requested_at TEXT,
                delivery_confirmed_at TEXT,
                structured_payload TEXT,
                workiq_delivery_ref TEXT,
                created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );
            """
        )
        conn.execute(
            f"INSERT INTO task_actions_new ({copy_columns}) "
            f"SELECT {copy_columns} FROM task_actions"
        )
        conn.executescript(
            """
            DROP TABLE task_actions;
            ALTER TABLE task_actions_new RENAME TO task_actions;
            CREATE INDEX idx_task_actions_task_id ON task_actions(task_id);
            CREATE UNIQUE INDEX idx_task_actions_execution_parent
                ON task_actions(parent_action_id)
                WHERE state IN ('executing','executed','execute_unconfirmed');
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()


def init_db(conn: sqlite3.Connection | None = None):
    """Create all tables if they don't exist."""
    close = False
    if conn is None:
        conn = get_connection()
        close = True

    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_task_actions_execution_parent
            ON task_actions(parent_action_id)
            WHERE state IN ('executing','executed','execute_unconfirmed')
        """
    )
    conn.commit()

    # After the schema is settled, recover locators from links already stored.
    # Cheap and idempotent: it only touches rows whose column is still empty.
    backfill_source_locators(conn)

    if close:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('suggested','active','in_progress','waiting','snoozed','completed','dismissed','deleted')),
    snoozed_until   TEXT,
    parse_status    TEXT NOT NULL DEFAULT 'parsed'
                        CHECK (parse_status IN ('unparsed','queued','parsing','parsed','error')),
    raw_input       TEXT,
    error_message   TEXT,
    is_quick_hit    INTEGER NOT NULL DEFAULT 0,
    priority        INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    due_date        TEXT,
    committed_date  TEXT,
    source_type     TEXT DEFAULT 'manual'
                        CHECK (source_type IN ('email','meeting','chat','manual')),
    source_id       TEXT,
    source_url      TEXT,
    source_locator  TEXT,
    source_date     TEXT,
    source_snippet  TEXT,
    coaching_text   TEXT,
    action_type     TEXT DEFAULT 'general'
                        CHECK (action_type IN ('schedule-meeting','respond-email','review-document','follow-up','awaiting-response','prepare','teams-message','general')),
    skill_output    TEXT,
    cowork_prompt   TEXT,
    key_people      TEXT,
    related_meeting TEXT,
    user_notes      TEXT DEFAULT '',
    waiting_activity TEXT,
    suggestion_refreshed_at TEXT,
    cowork_revision INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS task_actions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action_type      TEXT DEFAULT 'general',
    cowork_revision  INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL DEFAULT 'previewing'
                         CHECK (state IN (
                             'previewing','ready','failed',
                             'executing','executed','execute_unconfirmed'
                         )),
    intent           TEXT,
    notes_snapshot   TEXT,
    redirect_text    TEXT,
    composed_prompt  TEXT,
    finding          TEXT,
    draft            TEXT,
    draft_edited     TEXT,
    destination_kind TEXT
                         CHECK (destination_kind IS NULL OR destination_kind IN ('one_to_one','group','meeting','channel','unknown','none')),
    destination_ref  TEXT,
    conversation_id  TEXT,
    terminal_status  TEXT,
    tool_trace       TEXT,
    cost_credits     REAL,
    error            TEXT,
    seen_at          TEXT,
    island_url       TEXT,
    delivery_channel TEXT
                         CHECK (delivery_channel IS NULL OR delivery_channel IN ('teams','email','calendar')),
    destination_display TEXT,
    destination_confirmed_at TEXT,
    destination_source TEXT,
    parent_action_id INTEGER REFERENCES task_actions(id),
    blocked_question TEXT,
    answered_interaction TEXT,
    interaction_mode TEXT NOT NULL DEFAULT 'interaction'
                          CHECK (interaction_mode IN ('interaction','no_interaction')),
    completed_at     TEXT,
    had_interaction  INTEGER NOT NULL DEFAULT 0
                          CHECK (had_interaction IN (0,1)),
    execution_requested_at TEXT,
    delivery_confirmed_at TEXT,
    structured_payload TEXT,
    workiq_delivery_ref TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS person (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name        TEXT NOT NULL,
    primary_email       TEXT,
    aad_object_id       TEXT UNIQUE,
    canonical_person_id INTEGER REFERENCES person(id),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS person_alias (
    person_id        INTEGER NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    alias_kind       TEXT NOT NULL
                         CHECK (alias_kind IN ('aad','email','upn','name')),
    alias_value      TEXT NOT NULL,
    confidence       TEXT NOT NULL
                         CHECK (confidence IN ('aad','email','user','inferred','name')),
    evidence_kind    TEXT,
    evidence_ref     TEXT,
    observed_at      TEXT,
    confirmation_mode TEXT,
    confirmed_at     TEXT,
    lookup_kind      TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (alias_kind, alias_value, person_id)
);

CREATE TABLE IF NOT EXISTS person_merge_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    losing_id    INTEGER NOT NULL REFERENCES person(id),
    winning_id   INTEGER NOT NULL REFERENCES person(id),
    reason       TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    undone_at    TEXT
);

CREATE TABLE IF NOT EXISTS task_person (
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    person_id        INTEGER NOT NULL REFERENCES person(id),
    role             TEXT NOT NULL
                         CHECK (role IN ('sender','key_people','attendee')),
    evidence_kind    TEXT,
    evidence_ref     TEXT,
    observed_at      TEXT,
    confirmation_mode TEXT,
    confirmed_at     TEXT,
    lookup_kind      TEXT,
    PRIMARY KEY (task_id, person_id, role)
);

CREATE TABLE IF NOT EXISTS person_backfill_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    status       TEXT NOT NULL
                     CHECK (status IN (
                         'not_started','legacy_untracked','in_progress','complete'
                     )),
    last_task_id INTEGER NOT NULL DEFAULT 0 CHECK (last_task_id >= 0),
    revision     INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    completed_at TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_backfill_deferred (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    person_index     INTEGER,
    role             TEXT NOT NULL CHECK (role IN ('sender','key_people')),
    lookup_kind      TEXT NOT NULL CHECK (lookup_kind IN ('aad_exact','email_exact')),
    query_value      TEXT NOT NULL,
    display_name     TEXT,
    task_fingerprint TEXT NOT NULL,
    defer_reason     TEXT NOT NULL
                         CHECK (defer_reason IN (
                             'not_found','ambiguous','external_unresolved',
                             'mcp_unavailable'
                         )),
    status           TEXT NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','resolved','stale')),
    attempts         INTEGER NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    last_attempt_at  TEXT NOT NULL,
    resolved_at      TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_parse_status ON tasks(parse_status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_task_context_task_id ON task_context(task_id);
CREATE INDEX IF NOT EXISTS idx_refresh_next ON refresh_schedule(next_refresh_at);
CREATE INDEX IF NOT EXISTS idx_task_actions_task_id ON task_actions(task_id);
CREATE INDEX IF NOT EXISTS idx_person_email ON person(primary_email);
CREATE INDEX IF NOT EXISTS idx_person_canonical ON person(canonical_person_id);
CREATE INDEX IF NOT EXISTS idx_person_alias_value ON person_alias(alias_value);
CREATE UNIQUE INDEX IF NOT EXISTS idx_person_alias_exact_identity
    ON person_alias(alias_kind, alias_value)
    WHERE alias_kind IN ('aad','email','upn')
      AND confidence IN ('aad','email','user');
CREATE INDEX IF NOT EXISTS idx_task_person_person ON task_person(person_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_person_backfill_deferred_slot
    ON person_backfill_deferred (
        task_id, role, ifnull(person_index,-1), lookup_kind, query_value,
        task_fingerprint
    );
CREATE INDEX IF NOT EXISTS idx_person_backfill_deferred_status
    ON person_backfill_deferred(status, task_id);
"""

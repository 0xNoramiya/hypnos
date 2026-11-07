"""SQLite storage with forward-only migrations.

The same file is opened by three kinds of process: the Hermes gateway (capture hooks, routing),
``hermes hypnos ...`` CLI commands, and the training worker. Everything uses WAL and a busy timeout.
Inside Hermes the path comes from ``plugins.plugin_storage.plugin_data_dir("hypnos")``; the worker is
handed the absolute path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DB_FILENAME = "data.db"

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE sessions (
    session_id        TEXT PRIMARY KEY,
    platform          TEXT,
    parent_session_id TEXT,
    linked_from       TEXT,
    turn_count        INTEGER NOT NULL DEFAULT 0,
    first_seen        REAL,
    last_seen         REAL
);

-- One episode per user turn.
CREATE TABLE episodes (
    id                 INTEGER PRIMARY KEY,
    turn_id            TEXT NOT NULL UNIQUE,
    session_id         TEXT NOT NULL,
    turn_index         INTEGER NOT NULL,
    platform           TEXT,
    model              TEXT,
    started_at         REAL NOT NULL,
    ended_at           REAL,
    status             TEXT NOT NULL DEFAULT 'open',
    user_message       TEXT,
    assistant_text     TEXT,
    correction_of      INTEGER REFERENCES episodes(id) ON DELETE SET NULL,
    toolset_hash       TEXT,
    family             TEXT,
    split              TEXT,
    source             TEXT NOT NULL DEFAULT 'live',
    excluded           INTEGER NOT NULL DEFAULT 0,
    consolidated_night INTEGER
);
CREATE INDEX episodes_session ON episodes(session_id, turn_index);
CREATE INDEX episodes_started ON episodes(started_at);

-- One step per action the assistant took inside a turn.
CREATE TABLE steps (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    step_index     INTEGER NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'tool',
    tool_call_id   TEXT,
    tool_name      TEXT,
    args_json      TEXT,
    text           TEXT,
    result_text    TEXT,
    exit_code      INTEGER,
    outcome        INTEGER NOT NULL DEFAULT 0,
    approval       TEXT,
    test_framework TEXT,
    tests_passed   INTEGER,
    tests_failed   INTEGER,
    duration_ms    REAL,
    created_at     REAL NOT NULL,
    UNIQUE (episode_id, step_index)
);
CREATE INDEX steps_tool_call ON steps(tool_call_id);

CREATE TABLE signals (
    id                 INTEGER PRIMARY KEY,
    created_at         REAL NOT NULL,
    session_id         TEXT,
    episode_id         INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
    step_id            INTEGER REFERENCES steps(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL,
    polarity           INTEGER NOT NULL,
    weight             REAL NOT NULL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    detail             TEXT,
    consolidated_night INTEGER
);
CREATE INDEX signals_unconsolidated ON signals(consolidated_night, created_at);

-- DPO pairs. Context is a reference (episode + step) so lineage and unlearning stay exact.
CREATE TABLE pairs (
    id                INTEGER PRIMARY KEY,
    created_at        REAL NOT NULL,
    source            TEXT NOT NULL,
    session_id        TEXT,
    context_episode   INTEGER REFERENCES episodes(id) ON DELETE CASCADE,
    context_step      INTEGER,
    chosen_json       TEXT NOT NULL,
    rejected_json     TEXT NOT NULL,
    chosen_step_id    INTEGER REFERENCES steps(id) ON DELETE CASCADE,
    rejected_step_id  INTEGER REFERENCES steps(id) ON DELETE CASCADE,
    confidence        REAL NOT NULL,
    gold              INTEGER NOT NULL DEFAULT 0,
    habit_id          INTEGER,
    night_id          INTEGER,
    detail            TEXT,
    excluded          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE habits (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    statement     TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'procedural',
    source        TEXT,
    family        TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    excluded      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE probes (
    id           INTEGER PRIMARY KEY,
    habit_id     INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    prompt_json  TEXT NOT NULL,
    checker_json TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE TABLE habit_results (
    habit_id      INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    brain_version INTEGER NOT NULL,
    with_text     INTEGER NOT NULL DEFAULT 0,
    passes        INTEGER NOT NULL,
    total         INTEGER NOT NULL,
    consolidated  INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    PRIMARY KEY (habit_id, brain_version, with_text)
);

CREATE TABLE brains (
    version        INTEGER PRIMARY KEY,
    parent_version INTEGER,
    base_model     TEXT NOT NULL,
    backend        TEXT,
    adapter_path   TEXT,
    manifest_hash  TEXT,
    manifest_json  TEXT,
    exam_json      TEXT,
    families_json  TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,
    night_id       INTEGER,
    created_at     REAL NOT NULL,
    promoted_at    REAL
);

CREATE TABLE nights (
    id              INTEGER PRIMARY KEY,
    started_at      REAL NOT NULL,
    ended_at        REAL,
    status          TEXT NOT NULL,
    trigger         TEXT,
    params_json     TEXT,
    checkpoint_json TEXT,
    report_json     TEXT,
    spend_json      TEXT,
    pid             INTEGER,
    brain_version   INTEGER
);

-- Stage events drive the hypnogram.
CREATE TABLE night_events (
    id          INTEGER PRIMARY KEY,
    night_id    INTEGER NOT NULL REFERENCES nights(id) ON DELETE CASCADE,
    ts          REAL NOT NULL,
    stage       TEXT NOT NULL,
    cycle       INTEGER,
    event       TEXT NOT NULL,
    detail_json TEXT
);
CREATE INDEX night_events_night ON night_events(night_id, id);

-- One row per LLM API call that passed through Hypnos routing.
CREATE TABLE routes (
    id                     INTEGER PRIMARY KEY,
    ts                     REAL NOT NULL,
    session_id             TEXT,
    turn_id                TEXT,
    api_call               INTEGER,
    family                 TEXT,
    mode                   TEXT NOT NULL,
    brain_version          INTEGER,
    frontier_prompt_tokens INTEGER,
    brain_prompt_tokens    INTEGER,
    tokens_saved           INTEGER,
    pruned_tokens          INTEGER,
    latency_ms             REAL,
    brain_latency_ms       REAL,
    agreed                 INTEGER,
    confidence             REAL,
    fallback_reason        TEXT
);
CREATE INDEX routes_turn ON routes(turn_id);
CREATE INDEX routes_ts ON routes(ts);

-- Tool schemas offered to the model, deduplicated by hash.
CREATE TABLE toolsets (
    hash       TEXT PRIMARY KEY,
    tools_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""),
    (2, """
-- REM dreams: practised variants of the day's mistakes, with their verifier and outcome.
CREATE TABLE dreams (
    id             INTEGER PRIMARY KEY,
    night_id       INTEGER,
    habit_id       INTEGER REFERENCES habits(id) ON DELETE CASCADE,
    family         TEXT,
    kind           TEXT,
    prompt_json    TEXT NOT NULL,
    checker_json   TEXT NOT NULL,
    chosen_json    TEXT,
    attempt_json   TEXT,
    attempt_passed INTEGER,
    nightmare      INTEGER NOT NULL DEFAULT 0,
    resolved       INTEGER,
    teacher        TEXT,
    verified_by    TEXT,
    created_at     REAL NOT NULL,
    excluded       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX dreams_night ON dreams(night_id);
"""),
]

LATEST = MIGRATIONS[-1][0]


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open the Hypnos database with WAL, foreign keys and a busy timeout."""
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            pass
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations, each in its own transaction. Returns the resulting version."""
    current = schema_version(conn)
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another process may have migrated while we waited for the lock.
            if schema_version(conn) >= version:
                conn.execute("COMMIT")
                continue
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(version),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def _split_sql(sql: str) -> Iterable[str]:
    lines = [line for line in sql.splitlines() if not line.strip().startswith("--")]
    for statement in "\n".join(lines).split(";"):
        if statement.strip():
            yield statement


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return row[0]


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)))


def now() -> float:
    return time.time()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default

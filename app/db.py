"""Thin sqlite persistence layer — the audit trail the dashboard reads from.

Every state transition in the system (finding discovered, issue filed, Devin
session created/updated, PR observed, failure) is written here first. This is
what lets an engineering leader answer "is this working?" without reading logs.
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    ecosystem TEXT NOT NULL,
    package TEXT NOT NULL,
    current_version TEXT NOT NULL,
    fixed_version TEXT,
    vuln_id TEXT,
    severity TEXT,
    summary TEXT,
    content_hash TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL REFERENCES findings(id),
    github_issue_number INTEGER NOT NULL UNIQUE,
    github_issue_url TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    github_issue_number INTEGER NOT NULL,
    devin_url TEXT,
    prompt TEXT,
    status TEXT NOT NULL DEFAULT 'working',
    pr_url TEXT,
    error TEXT,
    files_changed INTEGER,
    lockfile_only INTEGER,
    outcome TEXT,
    structured_output TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS naive_prs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id TEXT NOT NULL REFERENCES findings(id),
    package TEXT NOT NULL,
    github_issue_number INTEGER,
    pr_number INTEGER NOT NULL UNIQUE,
    pr_url TEXT NOT NULL,
    files_changed INTEGER,
    lockfile_only INTEGER,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT
);
"""

# Columns added after the initial release — additive, so existing databases
# (created before these fields existed) get migrated in place on startup.
_MIGRATIONS = [
    "ALTER TABLE sessions ADD COLUMN files_changed INTEGER",
    "ALTER TABLE sessions ADD COLUMN lockfile_only INTEGER",
    "ALTER TABLE sessions ADD COLUMN outcome TEXT",
    "ALTER TABLE sessions ADD COLUMN structured_output TEXT",
]


def _connect() -> sqlite3.Connection:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_conn = _connect()
with _lock:
    _conn.executescript(SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            _conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    _conn.commit()


@contextmanager
def cursor():
    with _lock:
        cur = _conn.cursor()
        try:
            yield cur
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def log_event(event_type: str, payload: dict | None = None) -> None:
    from datetime import datetime, timezone

    with cursor() as cur:
        cur.execute(
            "INSERT INTO events (ts, event_type, payload) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), event_type, json.dumps(payload or {})),
        )

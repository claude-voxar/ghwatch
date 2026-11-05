"""SQLite storage shared by the poller and every consumer session.

The database runs in WAL mode with autocommit so that one writer (the poller)
and many readers (watchers in other sessions) can work concurrently. Callers
that need atomic multi-statement writes use `transaction()`.
"""
import contextlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone

RETENTION_DAYS = 30
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- insertion order; consumer cursor
  id          TEXT UNIQUE NOT NULL,               -- n:<thread>:<updated_at> | e:<event id>
  source      TEXT NOT NULL,                      -- notification | received | own
  type        TEXT,
  reason      TEXT,
  repo        TEXT,
  actor       TEXT,
  title       TEXT,
  url         TEXT,
  created_at  TEXT NOT NULL,                      -- GitHub timestamp, UTC
  fetched_at  TEXT NOT NULL,
  actionable  INTEGER NOT NULL DEFAULT 0,
  payload     TEXT                                -- raw GitHub JSON
);
CREATE INDEX IF NOT EXISTS ix_events_created ON events(created_at);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""

EVENT_COLUMNS = ("id", "source", "type", "reason", "repo", "actor", "title", "url",
                 "created_at", "fetched_at", "actionable", "payload")


def default_home():
    return os.environ.get("GHWATCH_HOME", os.path.expanduser("~/.ghwatch"))


def utcnow():
    return datetime.now(timezone.utc)


def now_iso():
    return utcnow().strftime(TS_FORMAT)


def parse_ts(value):
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


def connect(path=None):
    """Open (and migrate) the database. `path` defaults to $GHWATCH_HOME/ghwatch.db."""
    if path is None:
        home = default_home()
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, "ghwatch.db")
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value):
    conn.execute("INSERT INTO state(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def insert_event(conn, row):
    """Insert a normalized event dict. Returns True if it was new."""
    values = tuple(row.get(c) for c in EVENT_COLUMNS)
    cur = conn.execute(
        f"INSERT OR IGNORE INTO events({', '.join(EVENT_COLUMNS)}) "
        f"VALUES({', '.join('?' * len(EVENT_COLUMNS))})", values)
    return cur.rowcount == 1


def get_event(conn, event_id):
    return conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()


def events_after(conn, seq):
    return conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (seq,)).fetchall()


def max_seq(conn):
    return conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def prune(conn, days=RETENTION_DAYS, now=None):
    """Delete events older than `days`. Returns the number removed."""
    cutoff = ((now or utcnow()) - timedelta(days=days)).strftime(TS_FORMAT)
    return conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,)).rowcount

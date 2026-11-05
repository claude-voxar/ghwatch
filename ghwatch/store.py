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
  payload     TEXT,                               -- raw GitHub JSON
  backfill    INTEGER NOT NULL DEFAULT 0          -- history from a repo's first sync; not streamed
);
CREATE INDEX IF NOT EXISTS ix_events_created ON events(created_at);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS consumers (
  name       TEXT PRIMARY KEY,                    -- one per Claude session / role
  filters    TEXT NOT NULL,                       -- JSON, see consumers.Filters
  cursor     INTEGER NOT NULL,                    -- last events.seq delivered by watch
  created_at TEXT NOT NULL,
  last_seen  TEXT
);
CREATE TABLE IF NOT EXISTS tracked_repos (
  repo       TEXT PRIMARY KEY,                    -- owner/name, lower-case
  added_by   TEXT NOT NULL,                       -- consumer that asked for it
  added_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
  event_id   TEXT PRIMARY KEY,                    -- at most one owner per item
  consumer   TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  done_at    TEXT
);
"""

EVENT_COLUMNS = ("id", "source", "type", "reason", "repo", "actor", "title", "url",
                 "created_at", "fetched_at", "actionable", "payload", "backfill")

# Columns added after the first release: (table, column, definition).
MIGRATIONS = (
    ("events", "backfill", "INTEGER NOT NULL DEFAULT 0"),
)


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
    migrate(conn)
    return conn


def migrate(conn):
    """Add columns that databases created by older versions lack."""
    for table, column, definition in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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


def retention_cutoff(days=RETENTION_DAYS, now=None):
    return ((now or utcnow()) - timedelta(days=days)).strftime(TS_FORMAT)


def insert_event(conn, row):
    """Insert a normalized event dict. Returns True if it was new.

    Events already past retention are ignored: storing them would only get
    them pruned and then re-inserted (and re-announced) on the next fetch.
    """
    if row["created_at"] < retention_cutoff():
        return False
    row = dict(row, backfill=int(row.get("backfill") or 0))
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
    cutoff = retention_cutoff(days, now)
    n = conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM claims WHERE event_id NOT IN (SELECT id FROM events)")
    return n

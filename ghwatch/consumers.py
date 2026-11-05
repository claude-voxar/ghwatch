"""Consumers: named subscribers (typically one per Claude session).

Each consumer has a saved filter and a cursor into events.seq, so several
sessions can read the same database independently and each sees every
matching item exactly once in `watch`, even across restarts.
"""
import fnmatch
import json

from ghwatch import store

GLOB_FIELDS = ("repo", "type", "reason", "source", "actor")


class Filters:
    """Values within a field are OR-ed; fields are AND-ed. Globs, case-insensitive.

    By default only actionable items match; `all_events` includes everything.
    """

    FIELDS = GLOB_FIELDS + ("match",)

    def __init__(self, all_events=False, **fields):
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise ValueError(f"unknown filter field(s): {', '.join(sorted(unknown))}")
        self.all_events = bool(all_events)
        self.fields = {k: [v.lower() for v in vals] for k, vals in fields.items() if vals}

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text or "{}"))

    def to_json(self):
        d = dict(self.fields)
        if self.all_events:
            d["all_events"] = True
        return json.dumps(d, sort_keys=True)

    def __bool__(self):
        return self.all_events or bool(self.fields)

    def __eq__(self, other):
        return isinstance(other, Filters) and self.to_json() == other.to_json()

    def matches(self, row):
        if not self.all_events and not row["actionable"]:
            return False
        for field in GLOB_FIELDS:
            pats = self.fields.get(field)
            if pats and not any(fnmatch.fnmatchcase((row[field] or "").lower(), p) for p in pats):
                return False
        subs = self.fields.get("match")
        if subs and not any(s in (row["title"] or "").lower() for s in subs):
            return False
        return True


class Consumer:
    def __init__(self, name, filters, cursor):
        self.name = name
        self.filters = filters
        self.cursor = cursor


def register(conn, name, filters=None):
    """Load consumer `name`, creating it if needed.

    A new consumer starts at the current end of the event log so it is not
    flooded with history. Passing non-empty `filters` replaces saved ones.
    """
    row = conn.execute("SELECT * FROM consumers WHERE name=?", (name,)).fetchone()
    now = store.now_iso()
    if row is None:
        f = filters or Filters()
        cursor = store.max_seq(conn)
        conn.execute("INSERT INTO consumers(name, filters, cursor, created_at, last_seen) "
                     "VALUES(?, ?, ?, ?, ?)", (name, f.to_json(), cursor, now, now))
        return Consumer(name, f, cursor)
    f = Filters.from_json(row["filters"])
    if filters and filters != f:
        f = filters
        conn.execute("UPDATE consumers SET filters=? WHERE name=?", (f.to_json(), name))
    conn.execute("UPDATE consumers SET last_seen=? WHERE name=?", (now, name))
    return Consumer(name, f, row["cursor"])


def take_new(conn, consumer):
    """Return matching rows added since the consumer's cursor and advance it."""
    rows = store.events_after(conn, consumer.cursor)
    if rows:
        consumer.cursor = rows[-1]["seq"]
    conn.execute("UPDATE consumers SET cursor=?, last_seen=? WHERE name=?",
                 (consumer.cursor, store.now_iso(), consumer.name))
    return [r for r in rows if consumer.filters.matches(r)]


def list_all(conn):
    return conn.execute("SELECT * FROM consumers ORDER BY name").fetchall()

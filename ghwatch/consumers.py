"""Consumers: named subscribers (typically one per Claude session).

Each consumer has a saved filter and a cursor into events.seq, so several
sessions can read the same database independently and each sees every
matching item exactly once in `watch`, even across restarts.
"""
import fnmatch
import json

from ghwatch import store

GLOB_FIELDS = ("repo", "type", "reason", "source", "actor")

# GitHub's events API often delivers branch-level events hours late. When the
# actor is our own login (every session here shares it), the session that did
# the push already knows; the late copy is just a wake-up with nothing to do.
ECHO_TYPES = ("PushEvent", "CreateEvent", "DeleteEvent")
LATE_ECHO_AFTER = 3600  # seconds between created_at and fetched_at


def parse_exclude(text):
    """'actor=bot,type=Issue*' -> {'actor': 'bot', 'type': 'issue*'} (one exclusion rule)."""
    rule = {}
    for part in text.split(","):
        field, sep, pattern = part.partition("=")
        field = field.strip().lower()
        if not sep or field not in GLOB_FIELDS or not pattern.strip():
            raise ValueError(f"bad --exclude rule {text!r}: use FIELD=GLOB[,FIELD=GLOB...] "
                             f"with FIELD one of {', '.join(GLOB_FIELDS)}")
        rule[field] = pattern.strip().lower()
    return rule


def _glob(row, field, pattern):
    return fnmatch.fnmatchcase((row[field] or "").lower(), pattern)


def is_late_echo(row, login):
    if not login or row["type"] not in ECHO_TYPES:
        return False
    if (row["actor"] or "").lower() != login.lower():
        return False
    delay = store.parse_ts(row["fetched_at"]) - store.parse_ts(row["created_at"])
    return delay.total_seconds() > LATE_ECHO_AFTER


class Filters:
    """Values within a field are OR-ed; fields are AND-ed. Globs, case-insensitive.

    By default only actionable items match; `all_events` includes everything.
    `skip_late_echoes` drops our own pushes/branch events that GitHub delivered
    more than LATE_ECHO_AFTER late (needs the login passed to `matches`).
    `exclude` is a list of rules ({field: glob}); a row matching every field
    of any rule is dropped, e.g. one actor's comments but not their PRs.
    """

    FIELDS = GLOB_FIELDS + ("match",)

    def __init__(self, all_events=False, skip_late_echoes=False, exclude=(), **fields):
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise ValueError(f"unknown filter field(s): {', '.join(sorted(unknown))}")
        self.all_events = bool(all_events)
        self.skip_late_echoes = bool(skip_late_echoes)
        self.exclude = [parse_exclude(r) if isinstance(r, str) else
                        {k: v.lower() for k, v in r.items()} for r in exclude]
        for rule in self.exclude:
            if not rule or set(rule) - set(GLOB_FIELDS):
                raise ValueError(f"bad exclude rule {rule!r}")
        self.fields = {k: [v.lower() for v in vals] for k, vals in fields.items() if vals}

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text or "{}"))

    def to_json(self):
        d = dict(self.fields)
        if self.all_events:
            d["all_events"] = True
        if self.skip_late_echoes:
            d["skip_late_echoes"] = True
        if self.exclude:
            d["exclude"] = self.exclude
        return json.dumps(d, sort_keys=True)

    def __bool__(self):
        return self.all_events or self.skip_late_echoes or bool(self.exclude) or bool(self.fields)

    def __eq__(self, other):
        return isinstance(other, Filters) and self.to_json() == other.to_json()

    def matches(self, row, login=None):
        if not self.all_events and not row["actionable"]:
            return False
        if self.skip_late_echoes and is_late_echo(row, login):
            return False
        for field in GLOB_FIELDS:
            pats = self.fields.get(field)
            if pats and not any(_glob(row, field, p) for p in pats):
                return False
        subs = self.fields.get("match")
        if subs and not any(s in (row["title"] or "").lower() for s in subs):
            return False
        for rule in self.exclude:
            if all(_glob(row, f, p) for f, p in rule.items()):
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


def take_new(conn, consumer, include_backfill=False):
    """Return matching rows added since the consumer's cursor and advance it.

    Backfill (history from a repo's first sync) is skipped unless asked for.
    """
    rows = store.events_after(conn, consumer.cursor)
    if rows:
        consumer.cursor = rows[-1]["seq"]
    conn.execute("UPDATE consumers SET cursor=?, last_seen=? WHERE name=?",
                 (consumer.cursor, store.now_iso(), consumer.name))
    login = store.get_state(conn, "login")
    return [r for r in rows
            if (include_backfill or not r["backfill"]) and consumer.filters.matches(r, login)]


def list_all(conn):
    return conn.execute("SELECT * FROM consumers ORDER BY name").fetchall()


def forget(conn, name):
    """Remove a consumer. Its claims stay so other sessions still see who handled what."""
    return conn.execute("DELETE FROM consumers WHERE name=?", (name,)).rowcount == 1

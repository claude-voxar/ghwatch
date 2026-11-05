"""Claims: which consumer owns an item, and whether it is done.

Several sessions may have overlapping filters. Before acting on an item a
session claims it; the primary key on event_id makes that atomic, so exactly
one session wins and the others see who has it.
"""
from ghwatch import store


class ClaimResult:
    def __init__(self, ok, row):
        self.ok = ok
        self.row = row

    @property
    def owner(self):
        return self.row["consumer"] if self.row else None


def get(conn, event_id):
    return conn.execute("SELECT * FROM claims WHERE event_id=?", (event_id,)).fetchone()


def claim(conn, event_id, consumer):
    """Claim an item. ok=False if another consumer already owns it."""
    if store.get_event(conn, event_id) is None:
        raise KeyError(event_id)
    conn.execute("INSERT OR IGNORE INTO claims(event_id, consumer, claimed_at) VALUES(?, ?, ?)",
                 (event_id, consumer, store.now_iso()))
    row = get(conn, event_id)
    return ClaimResult(row["consumer"] == consumer, row)


def done(conn, event_id, consumer):
    """Mark handled, claiming first if unowned. ok=False if someone else owns it."""
    with store.transaction(conn):
        result = claim(conn, event_id, consumer)
        if not result.ok or result.row["done_at"]:
            return ClaimResult(False, result.row)
        conn.execute("UPDATE claims SET done_at=? WHERE event_id=?", (store.now_iso(), event_id))
        return ClaimResult(True, get(conn, event_id))


def release(conn, event_id, consumer):
    """Give back an open claim. Returns True if one was released."""
    return conn.execute("DELETE FROM claims WHERE event_id=? AND consumer=? AND done_at IS NULL",
                        (event_id, consumer)).rowcount == 1


def is_done(conn, event_id):
    row = get(conn, event_id)
    return bool(row and row["done_at"])

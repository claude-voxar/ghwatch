import itertools

from ghwatch import store

_ids = itertools.count(1)


def memdb():
    return store.connect(":memory:")


def make_event(**overrides):
    now = store.now_iso()
    row = {
        "id": f"e:{next(_ids)}", "source": "notification", "type": "PullRequest",
        "reason": "review_requested", "repo": "acme/app", "actor": None,
        "title": "Fix login", "url": "https://github.com/acme/app/pull/1",
        "created_at": now, "fetched_at": now, "actionable": 1, "payload": "{}",
    }
    row.update(overrides)
    return row


def add(conn, **overrides):
    row = make_event(**overrides)
    store.insert_event(conn, row)
    return row

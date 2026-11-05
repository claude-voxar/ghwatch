"""The single background poller that feeds the shared database.

Only one poller may run per GHWATCH_HOME; an exclusive flock on
`poller.lock` enforces that. Any watcher can start one if none is running,
so the poller survives individual sessions coming and going.
"""
import fcntl
import logging
import os
import subprocess
import sys
import time

from ghwatch import normalize, store

MIN_INTERVAL = 60
MAX_INTERVAL = 300
BACKOFF = 1.5

log = logging.getLogger("ghwatch.poller")


# ------------------------------------------------------------------ locking

def lock_path(home):
    return os.path.join(home, "poller.lock")


def acquire_lock(home):
    """Return an open file holding the poller lock, or None if already held."""
    os.makedirs(home, exist_ok=True)
    fh = open(lock_path(home), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def is_running(home):
    fh = acquire_lock(home)
    if fh is None:
        return True
    fh.close()
    return False


def start_detached(home):
    """Spawn `ghwatch poll` in its own session so it outlives the caller."""
    os.makedirs(home, exist_ok=True)
    env = dict(os.environ, GHWATCH_HOME=home)
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = pkg_root + os.pathsep + env.get("PYTHONPATH", "")
    with open(os.path.join(home, "poller.log"), "a") as out:
        subprocess.Popen([sys.executable, "-m", "ghwatch", "poll"],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         env=env, start_new_session=True, close_fds=True)


# ------------------------------------------------------------------ polling

def next_interval(current, new_items, server_hint):
    """Fast after activity, back off when quiet, never below GitHub's hint."""
    interval = MIN_INTERVAL if new_items else min(MAX_INTERVAL, int(current * BACKOFF))
    return max(interval, min(server_hint, MAX_INTERVAL))


def poll_notifications(conn, gh, login):
    r = gh.request("GET", "/notifications?all=false&per_page=50",
                   if_modified_since=store.get_state(conn, "notif_last_modified"))
    if r.not_modified:
        return 0, r
    fetched = store.now_iso()
    new = sum(store.insert_event(conn, normalize.notification_row(n, fetched)) for n in r.body or [])
    if r.headers.get("Last-Modified"):
        store.set_state(conn, "notif_last_modified", r.headers["Last-Modified"])
    return new, r


def _event_poller(source, path_fmt):
    def poll(conn, gh, login):
        key = f"etag:{source}"
        r = gh.request("GET", path_fmt.format(login=login), etag=store.get_state(conn, key))
        if r.not_modified:
            return 0, r
        fetched = store.now_iso()
        events = sorted(r.body or [], key=lambda e: e["created_at"])
        new = sum(store.insert_event(conn, normalize.event_row(e, source, login, fetched))
                  for e in events)
        if r.headers.get("ETag"):
            store.set_state(conn, key, r.headers["ETag"])
        return new, r
    poll.__name__ = f"poll_{source}_events"
    return poll


SOURCES = (
    poll_notifications,
    _event_poller("received", "/users/{login}/received_events?per_page=100"),
    _event_poller("own", "/users/{login}/events?per_page=100"),
)


def poll_once(conn, gh, login, sources=SOURCES):
    """Poll every source once. Returns (new_items, max server poll hint).

    Each source commits independently, so one failing endpoint neither
    blocks the others nor leaves a half-written batch.
    """
    new_total, hint, ok = 0, MIN_INTERVAL, True
    for source in sources:
        try:
            with store.transaction(conn):
                n, resp = source(conn, gh, login)
            new_total += n
            hint = max(hint, resp.poll_interval(MIN_INTERVAL))
        except Exception as ex:  # noqa: BLE001 - keep polling the other sources
            ok = False
            log.warning("%s failed: %s", source.__name__, ex)
            store.set_state(conn, "last_error", f"{store.now_iso()} {source.__name__}: {ex}")
    pruned = store.prune(conn)
    now = store.now_iso()
    store.set_state(conn, "last_attempt", now)
    if ok:
        store.set_state(conn, "last_poll", now)
    log.info("poll: %d new, %d pruned", new_total, pruned)
    return new_total, hint


def run(home, gh, once=False, sleep=time.sleep):
    lock = acquire_lock(home)
    if lock is None:
        log.info("poller already running for %s; exiting", home)
        return False
    conn = store.connect(os.path.join(home, "ghwatch.db"))
    login = os.environ.get("GHWATCH_LOGIN") or gh.login()
    store.set_state(conn, "login", login)
    store.set_state(conn, "poller_pid", str(os.getpid()))
    interval = MIN_INTERVAL
    try:
        while True:
            new, hint = poll_once(conn, gh, login)
            interval = next_interval(interval, new, hint)
            store.set_state(conn, "interval", str(interval))
            if once:
                return True
            sleep(interval)
    finally:
        lock.close()

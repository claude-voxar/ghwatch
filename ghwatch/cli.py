"""Command line interface.

  poll [--once]               run the poller (exits if one is already running)
  ensure-poller               start a detached poller if none is running
  stop-poller / restart-poller  stop, or stop and start again (e.g. after an upgrade)
  watch -c NAME [filters]     Monitor stream: one stdout line per new matching item
  inbox -c NAME [filters]     list open matching items with claim status
  claim ID... -c NAME         take items; reports who has them if not you
  release ID... -c NAME       give claims back
  done ID... -c NAME [--read] mark handled (--read also marks the GitHub thread read)
  show ID                     full record incl. raw GitHub JSON
  consumers                   registered consumers and their filters
  forget-consumer NAME        remove a consumer (its claims are kept)
  track REPO... -c NAME       poll owner/name's event feed directly
  untrack REPO... / tracked   stop following / list followed repos

An exact --repo filter (no wildcards) on watch/inbox tracks that repo automatically.
  status                      poller health and counts

Filters (repeatable; OR within a flag, AND across flags; globs, case-insensitive):
  --repo 'owner/*' --type PullRequest --reason review_requested
  --source notification|received|own|repo --actor login --match 'title text'
  --all-events  include non-actionable activity (pushes, releases, ...)
A consumer's filters are saved on first use; passing filters again replaces them.
"""
import argparse
import json
import logging
import os
import sys
import time

from ghwatch import claims, consumers, poller, repos, store
from ghwatch.consumers import Filters
from ghwatch.github import GitHub

STALE_AFTER = 2 * poller.MAX_INTERVAL + 120  # seconds without a healthy poll
STARTUP_GRACE = 180


def home():
    return store.default_home()


def db():
    return store.connect(os.path.join(home(), "ghwatch.db"))


def format_row(row, claim=None):
    who = f" by {row['actor']}" if row["actor"] else ""
    why = f" [{row['reason']}]" if row["reason"] else ""
    status = ""
    if claim:
        status = f" | {'DONE' if claim['done_at'] else 'CLAIMED'} by {claim['consumer']}"
    return (f"GH {row['id']} | {row['repo']} | {row['type']}{why}{who} | "
            f"{row['title']} | {row['url']}{status}")


def filters_from(args):
    fields = {k: getattr(args, k) for k in Filters.FIELDS if getattr(args, k, None)}
    return Filters(all_events=args.all_events, **fields)


def verify_repo(repo):
    """False only when GitHub says the repo doesn't exist (or isn't visible)."""
    if os.environ.get("GHWATCH_SKIP_REPO_CHECK"):  # tests / offline use
        return True
    return repos.check_exists(GitHub(), repo) is not False


def auto_track(conn, consumer):
    """Follow every exact --repo a consumer filters on, so its events actually arrive."""
    tracked = {r["repo"] for r in repos.list_tracked(conn)}
    for pattern in consumer.filters.fields.get("repo", []):
        if not repos.is_exact(pattern) or pattern in tracked:
            continue
        if not verify_repo(pattern):
            print(f"ghwatch: {pattern} not found on GitHub; not tracking it", file=sys.stderr, flush=True)
        elif repos.track(conn, pattern, consumer.name):
            print(f"ghwatch: now tracking {pattern}", file=sys.stderr, flush=True)


def register(conn, args):
    consumer = consumers.register(conn, args.consumer, filters_from(args))
    auto_track(conn, consumer)
    return consumer


# ------------------------------------------------------------------ watch

def watch_step(conn, consumer):
    """One watch iteration: lines for new matching items not already done."""
    lines = []
    for row in consumers.take_new(conn, consumer):
        c = claims.get(conn, row["id"])
        if c and c["done_at"]:
            continue
        lines.append(format_row(row, c))
    return lines


def stale_message(conn, now, watching_since):
    """A warning line if the poller hasn't completed a healthy poll recently."""
    last = store.get_state(conn, "last_poll")
    if last is None:
        if now - watching_since < STARTUP_GRACE:
            return None
        age = "ever"
    else:
        seconds = now - store.parse_ts(last).timestamp()
        if seconds <= STALE_AFTER:
            return None
        age = f"{int(seconds)}s"
    return (f"GHWATCH-STALE no successful poll for {age}; "
            f"last_error={store.get_state(conn, 'last_error')}")


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
UPGRADED_MSG = ("GHWATCH-UPGRADED ghwatch code changed on disk; this watch has exited. "
                "Re-arm the Monitor with the same command to run the new version.")


def code_fingerprint(package_dir=PACKAGE_DIR):
    """Changes whenever any module of the package is modified, added or removed."""
    parts = []
    for name in sorted(os.listdir(package_dir)):
        if name.endswith(".py"):
            st = os.stat(os.path.join(package_dir, name))
            parts.append((name, st.st_mtime_ns, st.st_size))
    return tuple(parts)


def cmd_watch(args):
    conn = db()
    consumer = register(conn, args)
    if args.backlog:
        consumer.cursor = 0
    started = time.time()
    fingerprint = code_fingerprint()
    warned = False
    while True:
        if code_fingerprint() != fingerprint:
            # Long-running watchers would otherwise keep old code forever.
            print(UPGRADED_MSG, flush=True)
            return
        if not args.no_poller and not poller.is_running(home()):
            poller.start_detached(home())
        for line in watch_step(conn, consumer):
            print(line, flush=True)
        msg = stale_message(conn, time.time(), started)
        if msg and not warned:
            print(msg, flush=True)
        warned = bool(msg)
        time.sleep(args.interval)


# ------------------------------------------------------------------ other commands

def cmd_poll(args):
    logging.Formatter.converter = time.gmtime  # match the UTC timestamps in the DB
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt=store.TS_FORMAT)
    if not poller.run(home(), GitHub(), once=args.once):
        print("poller already running; exiting", file=sys.stderr)


def cmd_ensure_poller(args):
    if poller.is_running(home()):
        print("poller: running")
    else:
        poller.start_detached(home())
        print(f"poller: started (log: {os.path.join(home(), 'poller.log')})")


def cmd_stop_poller(args):
    if poller.running_pid(home()) is None:
        print("poller: not running")
    elif poller.stop(home()):
        print("poller: stopped")
    else:
        sys.exit("poller: did not stop within timeout")


def cmd_restart_poller(args):
    if not poller.stop(home()):
        sys.exit("poller: did not stop within timeout; not restarting")
    poller.start_detached(home())
    print(f"poller: restarted (log: {os.path.join(home(), 'poller.log')})")


def cmd_inbox(args):
    conn = db()
    consumer = register(conn, args)
    shown = 0
    for row in conn.execute("SELECT * FROM events ORDER BY created_at DESC"):
        if not consumer.filters.matches(row):
            continue
        c = claims.get(conn, row["id"])
        if c and c["done_at"] and not args.all:
            continue
        if c and c["consumer"] != args.consumer and args.unclaimed:
            continue
        print(f"{row['created_at']} {format_row(row, c)}")
        shown += 1
        if shown >= args.limit:
            break
    if not shown:
        print("(nothing open)")


def cmd_claim(args):
    conn = db()
    for event_id in args.ids:
        try:
            r = claims.claim(conn, event_id, args.consumer)
        except KeyError:
            print(f"{event_id}: no such event")
            continue
        if r.ok:
            print(f"{event_id}: claimed")
        else:
            done = " (done)" if r.row["done_at"] else ""
            print(f"{event_id}: ALREADY CLAIMED by {r.owner} at {r.row['claimed_at']}{done}")


def cmd_release(args):
    conn = db()
    for event_id in args.ids:
        ok = claims.release(conn, event_id, args.consumer)
        print(f"{event_id}: {'released' if ok else 'no open claim by you'}")


def cmd_done(args):
    conn = db()
    gh = GitHub() if args.read else None
    for event_id in args.ids:
        try:
            r = claims.done(conn, event_id, args.consumer)
        except KeyError:
            print(f"{event_id}: no such event")
            continue
        if not r.ok:
            state = "already done" if r.row["done_at"] else "claimed"
            print(f"{event_id}: not marked - {state} by {r.owner}")
            continue
        print(f"{event_id}: done")
        if gh and event_id.startswith("n:"):
            thread = event_id.split(":")[1]
            try:
                gh.mark_thread_read(thread)
                print(f"  GitHub thread {thread} marked read")
            except Exception as ex:  # noqa: BLE001
                print(f"  could not mark GitHub thread read: {ex}")


def cmd_show(args):
    conn = db()
    row = store.get_event(conn, args.id)
    if row is None:
        sys.exit(f"no such event: {args.id}")
    d = dict(row)
    d["payload"] = json.loads(d["payload"] or "null")
    c = claims.get(conn, args.id)
    d["claim"] = dict(c) if c else None
    print(json.dumps(d, indent=2))


def cmd_consumers(args):
    rows = consumers.list_all(db())
    for r in rows:
        print(f"{r['name']}: filters={r['filters']} cursor={r['cursor']} last_seen={r['last_seen']}")
    if not rows:
        print("(no consumers)")


def cmd_track(args):
    conn = db()
    for repo in args.repos:
        try:
            repos.normalize_name(repo)
        except ValueError as ex:
            print(f"{repo}: {ex}")
            continue
        if not verify_repo(repo):
            print(f"{repo}: not found on GitHub (typo, or no access); not tracking")
            continue
        added = repos.track(conn, repo, args.consumer)
        print(f"{repo}: {'tracking' if added else 'already tracked'}")


def cmd_untrack(args):
    conn = db()
    for repo in args.repos:
        print(f"{repo}: {'untracked' if repos.untrack(conn, repo) else 'was not tracked'}")


def cmd_tracked(args):
    conn = db()
    rows = repos.list_tracked(conn)
    for r in rows:
        err = repos.get_error(conn, r["repo"])
        print(f"{r['repo']} (added by {r['added_by']} at {r['added_at']})"
              + (f" ERROR: {err}" if err else ""))
    if not rows:
        print("(no tracked repos)")


def cmd_forget_consumer(args):
    ok = consumers.forget(db(), args.name)
    print(f"{args.name}: {'forgotten' if ok else 'no such consumer'}")


def cmd_status(args):
    conn = db()
    count = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    print(json.dumps({
        "home": home(),
        "poller_running": poller.is_running(home()),
        "login": store.get_state(conn, "login"),
        "last_poll": store.get_state(conn, "last_poll"),
        "interval_s": store.get_state(conn, "interval"),
        "last_error": store.get_state(conn, "last_error"),
        "events": count("SELECT count(*) FROM events"),
        "actionable": count("SELECT count(*) FROM events WHERE actionable=1"),
        "open_claims": count("SELECT count(*) FROM claims WHERE done_at IS NULL"),
        "consumers": count("SELECT count(*) FROM consumers"),
        "tracked_repos": count("SELECT count(*) FROM tracked_repos"),
        "oldest_event": count("SELECT min(created_at) FROM events"),
    }, indent=2))


# ------------------------------------------------------------------ parser

def build_parser():
    ap = argparse.ArgumentParser(prog="ghwatch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    def add(name, func, consumer=False, filters=False, ids=False):
        p = sp.add_parser(name)
        p.set_defaults(func=func)
        if consumer:
            p.add_argument("-c", "--consumer", required=True,
                           help="name of this session/role, e.g. 'reviews'")
        if filters:
            for field in Filters.FIELDS:
                p.add_argument(f"--{field}", action="append")
            p.add_argument("--all-events", action="store_true")
        if ids:
            p.add_argument("ids", nargs="+", metavar="ID")
        return p

    add("poll", cmd_poll).add_argument("--once", action="store_true")
    add("ensure-poller", cmd_ensure_poller)
    add("stop-poller", cmd_stop_poller)
    add("restart-poller", cmd_restart_poller)
    p = add("watch", cmd_watch, consumer=True, filters=True)
    p.add_argument("--interval", type=float, default=15, help="seconds between DB checks")
    p.add_argument("--backlog", action="store_true", help="replay all matching items first")
    p.add_argument("--no-poller", action="store_true", help="don't auto-start a poller")
    p = add("inbox", cmd_inbox, consumer=True, filters=True)
    p.add_argument("--all", action="store_true", help="include done items")
    p.add_argument("--unclaimed", action="store_true", help="hide items claimed by others")
    p.add_argument("--limit", type=int, default=50)
    add("claim", cmd_claim, consumer=True, ids=True)
    add("release", cmd_release, consumer=True, ids=True)
    add("done", cmd_done, consumer=True, ids=True).add_argument(
        "--read", action="store_true", help="also mark the GitHub notification thread read")
    add("show", cmd_show).add_argument("id")
    add("consumers", cmd_consumers)
    add("forget-consumer", cmd_forget_consumer).add_argument("name")
    add("track", cmd_track, consumer=True).add_argument("repos", nargs="+", metavar="OWNER/NAME")
    add("untrack", cmd_untrack).add_argument("repos", nargs="+", metavar="OWNER/NAME")
    add("tracked", cmd_tracked)
    add("status", cmd_status)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass

"""Command line interface.

  poll [--once]               run the poller (exits if one is already running)
  ensure-poller               start a detached poller if none is running
  stop-poller / restart-poller  stop, or stop and start again (e.g. after an upgrade)
  install-agent / uninstall-agent  run the poller under launchd (survives crashes/reboots)
  watch -c NAME [filters]     one stdout line per new matching item; with --until-event,
                              exit after the first batch (run in the background, re-run after)
  inbox -c NAME [filters]     list open matching items with claim status
  claim ID... -c NAME         take items; reports who has them if not you
  release ID... -c NAME       give claims back
  done ID... -c NAME [--read] mark handled (--read also marks the GitHub thread read)
  show ID                     full record incl. raw GitHub JSON
  consumers                   registered consumers and their filters
  forget-consumer NAME        remove a consumer (its claims are kept)
  track REPO... -c NAME       poll owner/name's event feed directly
  untrack REPO... / tracked   stop following / list followed repos

  scan-transcripts [--follow]  report ghwatch failures seen in other sessions' transcripts

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

from ghwatch import claims, consumers, launchd, poller, repos, store, transcripts
from ghwatch.consumers import Filters
from ghwatch.github import GitHub

STALE_AFTER = 2 * poller.MAX_INTERVAL + 120  # seconds without a healthy poll
STARTUP_GRACE = 180
STALE_REPEAT = 1800  # seconds between repeated stale warnings to one consumer


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
    return Filters(all_events=args.all_events,
                   skip_late_echoes=getattr(args, "skip_late_echoes", False), **fields)


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

def watch_step(conn, consumer, include_backfill=False):
    """One watch iteration: lines for new matching items not already done."""
    lines = []
    for row in consumers.take_new(conn, consumer, include_backfill):
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


def stale_warning_due(conn, consumer_name, stale, now):
    """Rate-limit stale warnings per consumer.

    With --until-event a session re-runs watch after every line it gets; without
    this, an outage would wake it every few seconds instead of every STALE_REPEAT.
    Recovery clears the mark, so the next outage is reported at once.
    """
    key = f"stale_warned:{consumer_name}"
    if not stale:
        conn.execute("DELETE FROM state WHERE key=?", (key,))
        return False
    last = store.get_state(conn, key)
    if last is not None and now - float(last) < STALE_REPEAT:
        return False
    store.set_state(conn, key, str(now))
    return True


PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
UPGRADED_MSG = ("GHWATCH-UPGRADED ghwatch code changed on disk; this watch has exited. "
                "Run the same command again to use the new version.")


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
    while True:
        if code_fingerprint() != fingerprint:
            # Long-running watchers would otherwise keep old code forever.
            print(UPGRADED_MSG, flush=True)
            return
        if not args.no_poller and not poller.is_running(home()):
            poller.start_detached(home())
        lines = watch_step(conn, consumer, include_backfill=args.backlog)
        now = time.time()
        msg = stale_message(conn, now, started)
        if stale_warning_due(conn, consumer.name, msg is not None, now):
            lines.append(msg)
        for line in lines:
            print(line, flush=True)
        if lines and args.until_event:
            return  # the caller handles these, then runs watch again
        time.sleep(args.interval)


# ------------------------------------------------------------------ other commands

def cmd_poll(args):
    logging.Formatter.converter = time.gmtime  # match the UTC timestamps in the DB
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt=store.TS_FORMAT)
    if not poller.run(home(), GitHub(), once=args.once, wait_for_lock=args.wait_lock):
        print("poller already running; exiting", file=sys.stderr)


def cmd_ensure_poller(args):
    if poller.is_running(home()):
        print("poller: running")
    elif launchd.is_installed():
        launchd.launchctl("kickstart", launchd.service())
        print("poller: started via launchd agent")
    else:
        poller.start_detached(home())
        print(f"poller: started (log: {os.path.join(home(), 'poller.log')})")


def cmd_stop_poller(args):
    if poller.running_pid(home()) is None:
        print("poller: not running")
    elif poller.stop(home()):
        print("poller: stopped")
        if launchd.is_installed():
            print("note: the launchd agent will start it again shortly; "
                  "use uninstall-agent to stop polling for good")
    else:
        sys.exit("poller: did not stop within timeout")


def cmd_restart_poller(args):
    if launchd.is_installed():
        if not launchd.restart(home()):
            sys.exit("poller: launchd agent did not take over the lock within timeout")
        print(f"poller: restarted via launchd agent (pid {launchd.agent_pid()})")
        return
    if not poller.stop(home()):
        sys.exit("poller: did not stop within timeout; not restarting")
    poller.start_detached(home())
    print(f"poller: restarted (log: {os.path.join(home(), 'poller.log')})")


def cmd_install_agent(args):
    path = launchd.install(home())
    if not launchd.restart(home()):
        sys.exit(f"installed {path}, but the agent did not take over the poller lock")
    print(f"launchd agent installed: {path} (pid {launchd.agent_pid()})")


def cmd_uninstall_agent(args):
    removed = launchd.uninstall()
    print("launchd agent removed" if removed else "launchd agent was not installed")
    if removed and not poller.is_running(home()):
        poller.start_detached(home())
        print("poller: started detached so polling continues")


def cmd_inbox(args):
    conn = db()
    consumer = register(conn, args)
    shown = 0
    login = store.get_state(conn, "login")
    for row in conn.execute("SELECT * FROM events ORDER BY created_at DESC"):
        if not consumer.filters.matches(row, login):
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


def cmd_scan_transcripts(args):
    state_path = os.path.join(home(), "transcript-scan.json")
    first_run = not os.path.exists(state_path)
    scanner = transcripts.Scanner(args.root, state_path, args.exclude_session or ())
    if first_run and not args.from_start:
        scanner.skip_history()
        scanner.save()
    fingerprint = code_fingerprint()
    while True:
        problems = scanner.scan()
        for problem in problems:
            print(problem.line(), flush=True)
        scanner.save()
        if not (args.follow or args.until_event) or (problems and args.until_event):
            return
        if code_fingerprint() != fingerprint:
            print(UPGRADED_MSG, flush=True)
            return
        time.sleep(args.interval)


def cmd_forget_consumer(args):
    ok = consumers.forget(db(), args.name)
    print(f"{args.name}: {'forgotten' if ok else 'no such consumer'}")


def cmd_status(args):
    conn = db()
    count = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    print(json.dumps({
        "home": home(),
        "poller_running": poller.is_running(home()),
        "poller_pid": poller.running_pid(home()),
        "launchd_agent": ({"installed": True, "pid": launchd.agent_pid()}
                          if launchd.is_installed() else {"installed": False}),
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
            p.add_argument("--skip-late-echoes", action="store_true",
                           help="ignore our own push/create/delete events that GitHub "
                                "delivered over an hour late")
        if ids:
            p.add_argument("ids", nargs="+", metavar="ID")
        return p

    p = add("poll", cmd_poll)
    p.add_argument("--once", action="store_true")
    p.add_argument("--wait-lock", action="store_true",
                   help="if another poller is active, wait and take over when it exits")
    add("ensure-poller", cmd_ensure_poller)
    add("stop-poller", cmd_stop_poller)
    add("restart-poller", cmd_restart_poller)
    add("install-agent", cmd_install_agent)
    add("uninstall-agent", cmd_uninstall_agent)
    p = add("watch", cmd_watch, consumer=True, filters=True)
    p.add_argument("--interval", type=float, default=15, help="seconds between DB checks")
    p.add_argument("--backlog", action="store_true", help="replay all matching items first")
    p.add_argument("--no-poller", action="store_true", help="don't auto-start a poller")
    p.add_argument("--until-event", action="store_true",
                   help="exit after printing the first batch of lines; run it as a background "
                        "command and start it again after handling them (no idle wake-ups)")
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
    p = add("scan-transcripts", cmd_scan_transcripts)
    p.add_argument("--follow", action="store_true", help="keep scanning (for a Monitor)")
    p.add_argument("--until-event", action="store_true",
                   help="keep scanning until something is reported, print it and exit")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--exclude-session", action="append", metavar="SESSION_ID",
                   help="ignore this session's transcript (e.g. the maintainer's own)")
    p.add_argument("--root", default=transcripts.DEFAULT_ROOT)
    p.add_argument("--from-start", action="store_true",
                   help="on first run, scan existing history instead of starting at the end")
    add("status", cmd_status)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        pass

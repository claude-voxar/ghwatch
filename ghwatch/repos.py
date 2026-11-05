"""Repositories the poller follows directly via /repos/{owner}/{name}/events.

Notifications and the user's activity feeds only cover repos the account
is subscribed to or acts in. Tracking a repo makes ghwatch see everything
that happens there, so a session can watch any repo it works on.
"""
import re

from ghwatch import store

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GLOB_CHARS = set("*?[")


def normalize_name(repo):
    repo = repo.strip().strip("/")
    if not REPO_RE.match(repo):
        raise ValueError(f"not an owner/name repository: {repo!r}")
    return repo.lower()


def is_exact(pattern):
    """True if a --repo filter names one repository rather than a glob."""
    return not (GLOB_CHARS & set(pattern)) and bool(REPO_RE.match(pattern))


def track(conn, repo, added_by):
    """Start following `repo`. Returns True if it was not tracked before."""
    return conn.execute("INSERT OR IGNORE INTO tracked_repos(repo, added_by, added_at) VALUES(?, ?, ?)",
                        (normalize_name(repo), added_by, store.now_iso())).rowcount == 1


def untrack(conn, repo):
    return conn.execute("DELETE FROM tracked_repos WHERE repo=?", (normalize_name(repo),)).rowcount == 1


def list_tracked(conn):
    return conn.execute("SELECT * FROM tracked_repos ORDER BY repo").fetchall()

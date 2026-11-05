"""Turn raw GitHub API objects into rows for the events table.

Pure functions only, so they are easy to test against recorded payloads.
"""
import json

# Notification reasons that mean someone wants something from the user.
# Everything else (e.g. "subscribed") is stored but not actionable.
ACTIONABLE_REASONS = frozenset({
    "review_requested", "mention", "team_mention", "assign", "author",
    "comment", "manual", "security_alert", "approval_requested",
    "ci_activity", "state_change", "invitation", "member_feature_requested",
})


def html_url(api_url):
    """Best-effort conversion of an api.github.com URL to its github.com page."""
    if not api_url:
        return None
    url = api_url.replace("https://api.github.com/repos/", "https://github.com/")
    return url.replace("/pulls/", "/pull/").replace("/commits/", "/commit/")


def notification_row(n, fetched_at):
    subject = n.get("subject") or {}
    repo = n.get("repository") or {}
    return {
        # A thread is re-reported every time it updates; key on updated_at so
        # each update (new comment, new review request) is a distinct item.
        "id": f"n:{n['id']}:{n['updated_at']}",
        "source": "notification",
        "type": subject.get("type"),
        "reason": n.get("reason"),
        "repo": repo.get("full_name"),
        "actor": None,
        "title": subject.get("title"),
        "url": html_url(subject.get("url")) or repo.get("html_url"),
        "created_at": n["updated_at"],
        "fetched_at": fetched_at,
        "actionable": int(n.get("reason") in ACTIONABLE_REASONS),
        "payload": json.dumps(n),
    }


def describe_event(e):
    """Return (one-line summary, html url or None) for an activity event."""
    p = e.get("payload") or {}
    t = e.get("type")
    obj = p.get("pull_request") or p.get("issue") or {}
    if t == "PushEvent":
        n = p.get("size") or len(p.get("commits") or [])
        branch = (p.get("ref") or "").replace("refs/heads/", "")
        return f"pushed {n} commit(s) to {branch}", None
    if t in ("IssueCommentEvent", "PullRequestReviewCommentEvent", "CommitCommentEvent"):
        summary = f"commented on #{obj.get('number', '')} {obj.get('title', '')}".strip()
        return summary, (p.get("comment") or {}).get("html_url")
    if t == "PullRequestReviewEvent":
        review = p.get("review") or {}
        return (f"reviewed ({review.get('state')}) #{obj.get('number')} {obj.get('title')}",
                review.get("html_url"))
    if obj:
        return f"{p.get('action', '')} #{obj.get('number')} {obj.get('title')}".strip(), obj.get("html_url")
    if t in ("CreateEvent", "DeleteEvent"):
        verb = "created" if t == "CreateEvent" else "deleted"
        return f"{verb} {p.get('ref_type')} {p.get('ref') or ''}".strip(), None
    if t == "ReleaseEvent":
        release = p.get("release") or {}
        return f"{p.get('action')} release {release.get('tag_name')}", release.get("html_url")
    return p.get("action") or t, None


def mentions(e, login):
    needle = "@" + login.lower()
    p = e.get("payload") or {}
    for key in ("comment", "review", "issue", "pull_request"):
        body = (p.get(key) or {}).get("body")
        if body and needle in body.lower():
            return True
    return False


def event_row(e, source, login, fetched_at):
    actor = (e.get("actor") or {}).get("login")
    repo = (e.get("repo") or {}).get("name")
    title, url = describe_event(e)
    # Someone else @-mentioning the user is actionable even in repos where
    # GitHub would not send a notification.
    actionable = actor != login and mentions(e, login)
    return {
        "id": f"e:{e['id']}",
        "source": source,
        "type": e.get("type"),
        "reason": "mention" if actionable else None,
        "repo": repo,
        "actor": actor,
        "title": title,
        "url": url or (f"https://github.com/{repo}" if repo else None),
        "created_at": e["created_at"],
        "fetched_at": fetched_at,
        "actionable": int(actionable),
        "payload": json.dumps(e),
    }

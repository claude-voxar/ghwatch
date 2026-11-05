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


def _subject(p):
    """(noun, number, title or None, html url or None) of the PR/issue an event is about.

    GitHub's events API may send trimmed objects with only url/id/number, so
    title and html_url are optional and the web URL is derived when missing.
    """
    if "pull_request" in p:
        obj, noun = p["pull_request"] or {}, "PR"
    else:
        obj = p.get("issue") or {}
        noun = "PR" if "pull_request" in obj else "issue"
    return noun, obj.get("number"), obj.get("title"), obj.get("html_url") or html_url(obj.get("url"))


def _label(noun, number, title):
    return f"{noun} #{number}" + (f" {title}" if title else "")


def describe_event(e):
    """Return (one-line summary, html url or None) for an activity event."""
    p = e.get("payload") or {}
    t = e.get("type")
    noun, number, title, url = _subject(p)
    if t == "PushEvent":
        n = p.get("size") or len(p.get("commits") or [])
        branch = (p.get("ref") or "").replace("refs/heads/", "")
        return (f"pushed {n} commit(s) to {branch}" if n else f"pushed to {branch}"), None
    if t in ("IssueCommentEvent", "PullRequestReviewCommentEvent", "CommitCommentEvent"):
        comment_url = (p.get("comment") or {}).get("html_url")
        if number is None:
            return "commented", comment_url
        return f"commented on {_label(noun, number, title)}", comment_url or url
    if t == "PullRequestReviewEvent":
        review = p.get("review") or {}
        return f"reviewed ({review.get('state')}) {_label(noun, number, title)}", review.get("html_url") or url
    if number is not None:
        return f"{p.get('action', '')} {_label(noun, number, title)}".strip(), url
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

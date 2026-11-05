import json
import unittest

from ghwatch import normalize

NOW = "2026-11-05T12:00:00Z"


def notification(reason="review_requested", **kw):
    n = {
        "id": "123", "updated_at": "2026-11-05T11:00:00Z", "reason": reason,
        "subject": {"type": "PullRequest", "title": "Fix login",
                    "url": "https://api.github.com/repos/acme/app/pulls/7"},
        "repository": {"full_name": "acme/app", "html_url": "https://github.com/acme/app"},
    }
    n.update(kw)
    return n


def event(type_, payload, actor="alice", id_="9"):
    return {"id": id_, "type": type_, "actor": {"login": actor},
            "repo": {"name": "acme/app"}, "created_at": "2026-11-05T10:00:00Z",
            "payload": payload}


class NotificationRowTest(unittest.TestCase):
    def test_fields(self):
        row = normalize.notification_row(notification(), NOW)
        self.assertEqual(row["id"], "n:123:2026-11-05T11:00:00Z")
        self.assertEqual(row["url"], "https://github.com/acme/app/pull/7")
        self.assertEqual(row["repo"], "acme/app")
        self.assertEqual(row["actionable"], 1)
        self.assertEqual(json.loads(row["payload"])["id"], "123")

    def test_subscribed_is_not_actionable(self):
        self.assertEqual(normalize.notification_row(notification("subscribed"), NOW)["actionable"], 0)

    def test_thread_update_gets_new_id(self):
        a = normalize.notification_row(notification(), NOW)
        b = normalize.notification_row(notification(updated_at="2026-11-05T11:30:00Z"), NOW)
        self.assertNotEqual(a["id"], b["id"])

    def test_missing_subject_url_falls_back_to_repo(self):
        n = notification()
        n["subject"]["url"] = None
        self.assertEqual(normalize.notification_row(n, NOW)["url"], "https://github.com/acme/app")


class EventRowTest(unittest.TestCase):
    def test_push_summary(self):
        e = event("PushEvent", {"ref": "refs/heads/main", "size": 3})
        row = normalize.event_row(e, "received", "me", NOW)
        self.assertEqual(row["title"], "pushed 3 commit(s) to main")
        self.assertEqual(row["url"], "https://github.com/acme/app")
        self.assertEqual(row["actionable"], 0)

    def test_mention_by_other_is_actionable(self):
        e = event("IssueCommentEvent", {
            "issue": {"number": 4, "title": "Bug"},
            "comment": {"body": "hey @Me can you look", "html_url": "https://x/c"}})
        row = normalize.event_row(e, "received", "me", NOW)
        self.assertEqual((row["actionable"], row["reason"]), (1, "mention"))
        self.assertEqual(row["url"], "https://x/c")
        self.assertEqual(row["title"], "commented on issue #4 Bug")

    def test_self_mention_is_not_actionable(self):
        e = event("IssueCommentEvent", {"comment": {"body": "@me"}}, actor="me")
        self.assertEqual(normalize.event_row(e, "own", "me", NOW)["actionable"], 0)

    def test_review_and_pr_summaries(self):
        review = event("PullRequestReviewEvent", {
            "pull_request": {"number": 2, "title": "T"}, "review": {"state": "approved"}})
        self.assertEqual(normalize.describe_event(review)[0], "reviewed (approved) PR #2 T")
        opened = event("PullRequestEvent", {"action": "opened",
                                            "pull_request": {"number": 5, "title": "New"}})
        self.assertEqual(normalize.describe_event(opened)[0], "opened PR #5 New")

    def test_trimmed_pr_payload(self):
        # Shape GitHub actually sends: no title, no html_url.
        e = event("PullRequestEvent", {"action": "merged", "number": 2, "pull_request": {
            "url": "https://api.github.com/repos/acme/app/pulls/2", "id": 1, "number": 2}})
        self.assertEqual(normalize.describe_event(e),
                         ("merged PR #2", "https://github.com/acme/app/pull/2"))

    def test_comment_on_pr_vs_issue(self):
        on_pr = event("IssueCommentEvent", {"issue": {"number": 3, "pull_request": {},
                                                      "url": "https://api.github.com/repos/acme/app/issues/3"}})
        self.assertEqual(normalize.describe_event(on_pr),
                         ("commented on PR #3", "https://github.com/acme/app/issues/3"))
        on_issue = event("IssueCommentEvent", {"issue": {"number": 4, "title": "Bug"}})
        self.assertEqual(normalize.describe_event(on_issue)[0], "commented on issue #4 Bug")

    def test_push_without_size(self):
        e = event("PushEvent", {"ref": "refs/heads/main", "head": "abc"})
        self.assertEqual(normalize.describe_event(e)[0], "pushed to main")

    def test_unknown_event_falls_back_to_type(self):
        self.assertEqual(normalize.describe_event(event("WatchEvent", {}))[0], "WatchEvent")


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import timedelta

from ghwatch import consumers, store
from ghwatch.consumers import Filters
from tests.helpers import add, make_event, memdb


class FiltersTest(unittest.TestCase):
    def test_default_matches_only_actionable(self):
        f = Filters()
        self.assertTrue(f.matches(make_event(actionable=1)))
        self.assertFalse(f.matches(make_event(actionable=0)))
        self.assertTrue(Filters(all_events=True).matches(make_event(actionable=0)))

    def test_glob_or_within_field_and_across_fields(self):
        f = Filters(repo=["acme/*"], reason=["mention", "review_requested"])
        self.assertTrue(f.matches(make_event(repo="Acme/App", reason="mention")))
        self.assertFalse(f.matches(make_event(repo="other/app", reason="mention")))
        self.assertFalse(f.matches(make_event(repo="acme/app", reason="assign")))

    def test_title_substring(self):
        f = Filters(match=["deploy"])
        self.assertTrue(f.matches(make_event(title="Fix Deploy script")))
        self.assertFalse(f.matches(make_event(title="Docs")))

    def test_null_field_never_matches_a_pattern(self):
        self.assertFalse(Filters(actor=["bob"]).matches(make_event(actor=None)))

    def test_json_roundtrip_and_unknown_field(self):
        f = Filters(all_events=True, repo=["a/b"])
        self.assertEqual(Filters.from_json(f.to_json()), f)
        with self.assertRaises(ValueError):
            Filters(colour=["red"])


class ExcludeTest(unittest.TestCase):
    f = Filters(all_events=True, exclude=["actor=bot,type=PullRequestReview*", "repo=acme/secret"])

    def test_rule_drops_rows_matching_all_its_fields(self):
        self.assertFalse(self.f.matches(make_event(actor="Bot", type="PullRequestReviewEvent")))
        self.assertFalse(self.f.matches(make_event(repo="acme/secret")))  # rules are OR-ed

    def test_partial_match_is_kept(self):
        self.assertTrue(self.f.matches(make_event(actor="bot", type="PullRequestEvent")))
        self.assertTrue(self.f.matches(make_event(actor="alice", type="PullRequestReviewEvent")))
        self.assertTrue(self.f.matches(make_event(actor=None, type="PullRequestReviewEvent")))

    def test_saved_with_filters(self):
        g = Filters.from_json(self.f.to_json())
        self.assertEqual(g, self.f)
        self.assertEqual(g.exclude[0], {"actor": "bot", "type": "pullrequestreview*"})

    def test_bad_rules_rejected(self):
        for bad in ["actor", "colour=red", "actor=", "actor=bot,"]:
            with self.assertRaises(ValueError, msg=bad):
                Filters(exclude=[bad])


def push(actor="me", late_by=7200, type="PushEvent"):
    """An event fetched `late_by` seconds after GitHub says it happened."""
    now = store.utcnow()
    return make_event(type=type, actor=actor, actionable=0, source="repo",
                      created_at=(now - timedelta(seconds=late_by)).strftime(store.TS_FORMAT),
                      fetched_at=now.strftime(store.TS_FORMAT))


class LateEchoTest(unittest.TestCase):
    f = Filters(all_events=True, skip_late_echoes=True)

    def test_own_late_branch_events_are_skipped(self):
        for t in consumers.ECHO_TYPES:
            self.assertFalse(self.f.matches(push(type=t), login="me"), t)
        self.assertFalse(self.f.matches(push(actor="Me"), login="me"))  # logins are case-insensitive

    def test_timely_or_foreign_or_other_types_still_match(self):
        self.assertTrue(self.f.matches(push(late_by=60), login="me"))
        self.assertTrue(self.f.matches(push(actor="alice"), login="me"))
        # a late PR event by our login may be another session's PR: never hidden
        self.assertTrue(self.f.matches(push(type="PullRequestEvent"), login="me"))

    def test_off_by_default_and_without_login(self):
        self.assertTrue(Filters(all_events=True).matches(push(), login="me"))
        self.assertTrue(self.f.matches(push(), login=None))

    def test_saved_with_filters(self):
        self.assertEqual(Filters.from_json(self.f.to_json()), self.f)
        self.assertTrue(Filters(skip_late_echoes=True))


class ConsumerTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()

    def test_new_consumer_skips_history(self):
        add(self.conn)
        c = consumers.register(self.conn, "a")
        self.assertEqual(consumers.take_new(self.conn, c), [])
        fresh = add(self.conn)
        self.assertEqual([r["id"] for r in consumers.take_new(self.conn, c)], [fresh["id"]])

    def test_each_item_delivered_once_across_restarts(self):
        c = consumers.register(self.conn, "a")
        add(self.conn)
        self.assertEqual(len(consumers.take_new(self.conn, c)), 1)
        restarted = consumers.register(self.conn, "a")
        self.assertEqual(consumers.take_new(self.conn, restarted), [])

    def test_consumers_are_independent(self):
        reviews = consumers.register(self.conn, "reviews", Filters(reason=["review_requested"]))
        api = consumers.register(self.conn, "api", Filters(repo=["acme/api"]))
        add(self.conn, reason="review_requested", repo="acme/app")
        add(self.conn, reason="mention", repo="acme/api")
        self.assertEqual([r["repo"] for r in consumers.take_new(self.conn, reviews)], ["acme/app"])
        self.assertEqual([r["reason"] for r in consumers.take_new(self.conn, api)], ["mention"])

    def test_non_matching_rows_still_advance_cursor(self):
        c = consumers.register(self.conn, "a", Filters(repo=["nope/*"]))
        add(self.conn)
        consumers.take_new(self.conn, c)
        self.assertEqual(consumers.register(self.conn, "a").cursor, 1)

    def test_filters_saved_and_replaced(self):
        consumers.register(self.conn, "a", Filters(repo=["x/*"]))
        self.assertEqual(consumers.register(self.conn, "a").filters, Filters(repo=["x/*"]))
        consumers.register(self.conn, "a", Filters(repo=["y/*"]))
        self.assertEqual(consumers.register(self.conn, "a").filters, Filters(repo=["y/*"]))

    def test_backfill_skipped_unless_requested(self):
        c = consumers.register(self.conn, "a")
        add(self.conn, id="hist", backfill=1)
        news = add(self.conn, id="news")
        self.assertEqual([r["id"] for r in consumers.take_new(self.conn, c)], [news["id"]])
        replay = consumers.register(self.conn, "b")
        replay.cursor = 0
        self.assertEqual([r["id"] for r in consumers.take_new(self.conn, replay, include_backfill=True)],
                         ["hist", "news"])

    def test_take_new_uses_stored_login_for_late_echoes(self):
        store.set_state(self.conn, "login", "me")
        c = consumers.register(self.conn, "a", Filters(all_events=True, skip_late_echoes=True))
        store.insert_event(self.conn, push())
        fresh = push(late_by=5)
        store.insert_event(self.conn, fresh)
        self.assertEqual([r["id"] for r in consumers.take_new(self.conn, c)], [fresh["id"]])

    def test_forget(self):
        consumers.register(self.conn, "a", Filters(repo=["x/*"]))
        self.assertTrue(consumers.forget(self.conn, "a"))
        self.assertFalse(consumers.forget(self.conn, "a"))
        self.assertEqual(consumers.list_all(self.conn), [])


if __name__ == "__main__":
    unittest.main()

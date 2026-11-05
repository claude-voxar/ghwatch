import unittest

from ghwatch import consumers
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

    def test_forget(self):
        consumers.register(self.conn, "a", Filters(repo=["x/*"]))
        self.assertTrue(consumers.forget(self.conn, "a"))
        self.assertFalse(consumers.forget(self.conn, "a"))
        self.assertEqual(consumers.list_all(self.conn), [])


if __name__ == "__main__":
    unittest.main()

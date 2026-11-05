import os
import tempfile
import unittest
from datetime import timedelta

from ghwatch import store
from tests.helpers import add, make_event, memdb


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()

    def test_insert_is_idempotent(self):
        row = make_event(id="e:dup")
        self.assertTrue(store.insert_event(self.conn, row))
        self.assertFalse(store.insert_event(self.conn, row))
        self.assertEqual(store.max_seq(self.conn), 1)

    def test_events_after_returns_in_insertion_order(self):
        a, b, c = add(self.conn), add(self.conn), add(self.conn)
        rows = store.events_after(self.conn, 1)
        self.assertEqual([r["id"] for r in rows], [b["id"], c["id"]])

    def test_prune_removes_only_rows_past_retention(self):
        now = store.utcnow()
        old = (now - timedelta(days=31)).strftime(store.TS_FORMAT)
        edge = (now - timedelta(days=29)).strftime(store.TS_FORMAT)
        add(self.conn, id="old", created_at=old)
        add(self.conn, id="recent", created_at=edge)
        self.assertEqual(store.prune(self.conn, now=now), 1)
        self.assertIsNone(store.get_event(self.conn, "old"))
        self.assertIsNotNone(store.get_event(self.conn, "recent"))

    def test_state_roundtrip_and_overwrite(self):
        self.assertEqual(store.get_state(self.conn, "k", "dflt"), "dflt")
        store.set_state(self.conn, "k", "1")
        store.set_state(self.conn, "k", "2")
        self.assertEqual(store.get_state(self.conn, "k"), "2")

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with store.transaction(self.conn):
                add(self.conn, id="rolled-back")
                raise RuntimeError
        self.assertIsNone(store.get_event(self.conn, "rolled-back"))

    def test_connect_defaults_to_ghwatch_home(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("GHWATCH_HOME")
            os.environ["GHWATCH_HOME"] = os.path.join(d, "home")
            try:
                store.connect().close()
                self.assertTrue(os.path.exists(os.path.join(d, "home", "ghwatch.db")))
            finally:
                if old is None:
                    del os.environ["GHWATCH_HOME"]
                else:
                    os.environ["GHWATCH_HOME"] = old


if __name__ == "__main__":
    unittest.main()

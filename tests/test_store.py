import os
import sqlite3
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

    def test_insert_ignores_events_past_retention(self):
        old = (store.utcnow() - timedelta(days=31)).strftime(store.TS_FORMAT)
        self.assertFalse(store.insert_event(self.conn, make_event(id="old", created_at=old)))
        self.assertIsNone(store.get_event(self.conn, "old"))

    def test_prune_removes_only_rows_past_retention(self):
        now = store.utcnow()
        add(self.conn, id="old", created_at=(now - timedelta(days=29, hours=12)).strftime(store.TS_FORMAT))
        add(self.conn, id="recent", created_at=(now - timedelta(days=1)).strftime(store.TS_FORMAT))
        # a day passes: "old" is 30.5 days old, "recent" 2 days
        self.assertEqual(store.prune(self.conn, now=now + timedelta(days=1)), 1)
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

    def test_migrates_database_from_before_backfill_column(self):
        legacy_events = """CREATE TABLE events (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, source TEXT NOT NULL,
          type TEXT, reason TEXT, repo TEXT, actor TEXT, title TEXT, url TEXT,
          created_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
          actionable INTEGER NOT NULL DEFAULT 0, payload TEXT)"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            old = sqlite3.connect(path)
            old.execute(legacy_events)
            old.execute("INSERT INTO events(id, source, created_at, fetched_at) VALUES('e:old', 'own', 't', 't')")
            old.commit()
            old.close()
            conn = store.connect(path)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
            self.assertIn("backfill", columns)
            self.assertEqual(store.get_event(conn, "e:old")["backfill"], 0)
            self.assertTrue(store.insert_event(conn, make_event(id="e:new", backfill=1)))
            self.assertEqual(store.get_event(conn, "e:new")["backfill"], 1)
            store.connect(path).close()  # migrating twice is a no-op

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

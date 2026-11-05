import os
import tempfile
import threading
import unittest
from datetime import timedelta

from ghwatch import claims, store
from tests.helpers import add, memdb


class ClaimsTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()
        self.ev = add(self.conn)["id"]

    def test_first_claim_wins(self):
        self.assertTrue(claims.claim(self.conn, self.ev, "a").ok)
        second = claims.claim(self.conn, self.ev, "b")
        self.assertFalse(second.ok)
        self.assertEqual(second.owner, "a")
        self.assertTrue(claims.claim(self.conn, self.ev, "a").ok)  # re-claim is idempotent

    def test_claim_unknown_event(self):
        with self.assertRaises(KeyError):
            claims.claim(self.conn, "missing", "a")

    def test_done_claims_implicitly_and_blocks_others(self):
        self.assertTrue(claims.done(self.conn, self.ev, "a").ok)
        self.assertTrue(claims.is_done(self.conn, self.ev))
        self.assertFalse(claims.done(self.conn, self.ev, "b").ok)
        self.assertFalse(claims.done(self.conn, self.ev, "a").ok)  # already done

    def test_done_refused_when_owned_by_other(self):
        claims.claim(self.conn, self.ev, "a")
        r = claims.done(self.conn, self.ev, "b")
        self.assertEqual((r.ok, r.owner), (False, "a"))
        self.assertFalse(claims.is_done(self.conn, self.ev))

    def test_release_only_own_open_claim(self):
        claims.claim(self.conn, self.ev, "a")
        self.assertFalse(claims.release(self.conn, self.ev, "b"))
        self.assertTrue(claims.release(self.conn, self.ev, "a"))
        self.assertTrue(claims.claim(self.conn, self.ev, "b").ok)

    def test_prune_drops_orphaned_claims(self):
        old = (store.utcnow() - timedelta(days=40)).strftime(store.TS_FORMAT)
        stale = add(self.conn, created_at=old)["id"]
        claims.claim(self.conn, stale, "a")
        store.prune(self.conn)
        self.assertIsNone(claims.get(self.conn, stale))


class ConcurrentClaimTest(unittest.TestCase):
    def test_exactly_one_winner_across_connections(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            ev = add(store.connect(path))["id"]
            results = []

            def worker(name):
                results.append(claims.claim(store.connect(path), ev, name).ok)

            threads = [threading.Thread(target=worker, args=(f"c{i}",)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(results.count(True), 1)


if __name__ == "__main__":
    unittest.main()

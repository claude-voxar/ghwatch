import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta

from ghwatch import cli, claims, consumers, store
from ghwatch.consumers import Filters
from tests.helpers import add, memdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WatchStepTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()

    def test_emits_new_matching_items_once(self):
        c = consumers.register(self.conn, "a", Filters(repo=["acme/*"]))
        hit = add(self.conn, repo="acme/app", title="Fix login")
        add(self.conn, repo="other/x")
        lines = cli.watch_step(self.conn, c)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"GH {hit['id']} | acme/app | PullRequest [review_requested]"))
        self.assertEqual(cli.watch_step(self.conn, c), [])

    def test_skips_done_and_annotates_claimed(self):
        c = consumers.register(self.conn, "a")
        done = add(self.conn)
        claimed = add(self.conn)
        claims.done(self.conn, done["id"], "b")
        claims.claim(self.conn, claimed["id"], "b")
        lines = cli.watch_step(self.conn, c)
        self.assertEqual(len(lines), 1)
        self.assertIn(claimed["id"], lines[0])
        self.assertTrue(lines[0].endswith("| CLAIMED by b"))


class StaleMessageTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()
        self.now = store.utcnow()

    def test_grace_period_before_first_poll(self):
        t = self.now.timestamp()
        self.assertIsNone(cli.stale_message(self.conn, t, t - 10))
        self.assertIn("for ever", cli.stale_message(self.conn, t, t - cli.STARTUP_GRACE - 1))

    def test_recent_poll_is_healthy_old_poll_is_stale(self):
        t = self.now.timestamp()
        store.set_state(self.conn, "last_poll", (self.now - timedelta(seconds=30)).strftime(store.TS_FORMAT))
        self.assertIsNone(cli.stale_message(self.conn, t, t))
        store.set_state(self.conn, "last_poll", (self.now - timedelta(hours=1)).strftime(store.TS_FORMAT))
        store.set_state(self.conn, "last_error", "boom")
        msg = cli.stale_message(self.conn, t, t)
        self.assertTrue(msg.startswith("GHWATCH-STALE"))
        self.assertIn("boom", msg)


class EndToEndTest(unittest.TestCase):
    """Drive the real entry point against a temporary GHWATCH_HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, GHWATCH_HOME=self.tmp.name)
        self.conn = store.connect(os.path.join(self.tmp.name, "ghwatch.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "ghwatch"), *args],
                           capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_two_sessions_claim_and_inbox(self):
        ev = add(self.conn, repo="acme/app")["id"]
        self.assertIn("claimed", self.run_cli("claim", ev, "-c", "reviews"))
        self.assertIn("ALREADY CLAIMED by reviews", self.run_cli("claim", ev, "-c", "mobile"))
        self.assertIn("CLAIMED by reviews", self.run_cli("inbox", "-c", "mobile"))
        self.assertIn("(nothing open)", self.run_cli("inbox", "-c", "mobile", "--unclaimed"))
        self.assertIn("not marked - claimed by reviews", self.run_cli("done", ev, "-c", "mobile"))
        self.assertIn(": done", self.run_cli("done", ev, "-c", "reviews"))
        self.assertIn("(nothing open)", self.run_cli("inbox", "-c", "reviews"))
        self.assertIn("DONE by reviews", self.run_cli("inbox", "-c", "reviews", "--all"))

    def test_watch_streams_new_items(self):
        consumers.register(self.conn, "w", Filters(reason=["mention"]))
        add(self.conn, reason="mention", title="ping me")
        add(self.conn, reason="assign")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "bin", "ghwatch"), "watch", "-c", "w",
             "--no-poller", "--interval", "0.2"],
            stdout=subprocess.PIPE, text=True, env=self.env)
        try:
            line = proc.stdout.readline()
        finally:
            proc.kill()
            proc.wait()
        self.assertIn("[mention]", line)
        self.assertIn("ping me", line)

    def test_show_and_status(self):
        ev = add(self.conn)["id"]
        self.assertIn('"claim": null', self.run_cli("show", ev))
        self.assertIn('"events": 1', self.run_cli("status"))


if __name__ == "__main__":
    unittest.main()

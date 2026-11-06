import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta

from ghwatch import cli, claims, consumers, repos, store
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


class StaleRateLimitTest(unittest.TestCase):
    def test_warns_once_per_window_and_resets_on_recovery(self):
        conn = memdb()
        t = 1_000_000.0
        self.assertTrue(cli.stale_warning_due(conn, "a", True, t))
        self.assertFalse(cli.stale_warning_due(conn, "a", True, t + 60))       # re-armed watch
        self.assertTrue(cli.stale_warning_due(conn, "b", True, t + 60))        # other consumer
        self.assertTrue(cli.stale_warning_due(conn, "a", True, t + cli.STALE_REPEAT + 1))
        self.assertFalse(cli.stale_warning_due(conn, "a", False, t + cli.STALE_REPEAT + 2))
        self.assertTrue(cli.stale_warning_due(conn, "a", True, t + cli.STALE_REPEAT + 3))


class EndToEndTest(unittest.TestCase):
    """Drive the real entry point against a temporary GHWATCH_HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, GHWATCH_HOME=self.tmp.name, GHWATCH_SKIP_REPO_CHECK="1")
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

    def test_skip_late_echoes_flag_is_saved_and_applied(self):
        store.set_state(self.conn, "login", "me")
        old = (store.utcnow() - timedelta(hours=3)).strftime(store.TS_FORMAT)
        add(self.conn, type="PushEvent", actor="me", title="pushed to old-branch",
            created_at=old, actionable=0)
        add(self.conn, type="PushEvent", actor="me", title="pushed to fresh", actionable=0)
        out = self.run_cli("inbox", "-c", "x", "--all-events", "--skip-late-echoes")
        self.assertIn("pushed to fresh", out)
        self.assertNotIn("old-branch", out)
        self.assertTrue(consumers.register(self.conn, "x").filters.skip_late_echoes)

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

    def test_watch_until_event_blocks_then_exits_after_first_batch(self):
        consumers.register(self.conn, "u", Filters(reason=["mention"]))
        store.set_state(self.conn, "last_poll", store.now_iso())
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "bin", "ghwatch"), "watch", "-c", "u",
             "--no-poller", "--interval", "0.1", "--until-event"],
            stdout=subprocess.PIPE, text=True, env=self.env)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(timeout=1)  # nothing to report: keeps waiting, no output
            with store.transaction(self.conn):  # one batch, whatever the loop timing
                add(self.conn, reason="mention", title="first")
                add(self.conn, reason="mention", title="second")
            out, _ = proc.communicate(timeout=10)
        finally:
            proc.kill()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("first", out)
        self.assertIn("second", out)
        # next run continues from the cursor: nothing is repeated
        add(self.conn, reason="mention", title="third")
        out = self.run_cli("watch", "-c", "u", "--no-poller", "--interval", "0.1", "--until-event")
        self.assertNotIn("first", out)
        self.assertIn("third", out)

    def test_watch_exits_when_code_changes(self):
        repo = os.path.join(self.tmp.name, "repo")
        shutil.copytree(ROOT, repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        proc = subprocess.Popen(
            [sys.executable, os.path.join(repo, "bin", "ghwatch"), "watch", "-c", "u",
             "--no-poller", "--interval", "0.1"],
            stdout=subprocess.PIPE, text=True, env=self.env)
        try:
            time.sleep(1)
            with open(os.path.join(repo, "ghwatch", "store.py"), "a") as fh:
                fh.write("\n# upgraded\n")
            out, _ = proc.communicate(timeout=10)
        finally:
            proc.kill()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("GHWATCH-UPGRADED", out)

    def test_track_commands_and_auto_track(self):
        self.assertIn("tracking", self.run_cli("track", "acme/lib", "-c", "a"))
        self.assertIn("already tracked", self.run_cli("track", "ACME/lib", "-c", "a"))
        self.assertIn("not an owner/name", self.run_cli("track", "acme", "-c", "a"))
        self.run_cli("inbox", "-c", "b", "--repo", "acme/app", "--repo", "acme/*")
        tracked = self.run_cli("tracked")
        self.assertIn("acme/app (added by b", tracked)
        self.assertNotIn("acme/*", tracked)
        self.assertIn("untracked", self.run_cli("untrack", "acme/lib"))
        self.assertEqual([r["repo"] for r in repos.list_tracked(self.conn)], ["acme/app"])
        repos.set_error(self.conn, "acme/app", "HTTP Error 404")
        self.assertIn("ERROR: ", self.run_cli("tracked"))

    def test_scan_transcripts_skips_history_on_first_run(self):
        root = os.path.join(self.tmp.name, "projects", "p")
        os.makedirs(root)
        path = os.path.join(root, "s.jsonl")

        def append(text):
            with open(path, "a") as fh:
                fh.write(json.dumps({"type": "user", "sessionId": "abc", "uuid": text,
                                     "message": {"content": text}}) + "\n")

        append("GHWATCH-STALE no successful poll for old")
        args = ("scan-transcripts", "--root", os.path.dirname(root))
        self.assertEqual(self.run_cli(*args), "")
        append("GHWATCH-STALE no successful poll for new")
        self.assertIn("poll for new", self.run_cli(*args))
        self.assertEqual(self.run_cli(*args), "")
        self.assertEqual(self.run_cli(*args, "--exclude-session", "abc"), "")
        append("GHWATCH-STALE no successful poll for later")
        out = self.run_cli(*args, "--until-event", "--interval", "0.1")  # returns once found
        self.assertIn("poll for later", out)

    def test_show_and_status(self):
        ev = add(self.conn)["id"]
        self.assertIn('"claim": null', self.run_cli("show", ev))
        self.assertIn('"events": 1', self.run_cli("status"))


if __name__ == "__main__":
    unittest.main()

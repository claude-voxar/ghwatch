import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from ghwatch import poller, store
from ghwatch.github import Response
from tests.helpers import memdb


def notif(id_, reason="mention", updated="2026-11-05T10:00:00Z"):
    return {"id": id_, "updated_at": updated, "reason": reason,
            "subject": {"type": "Issue", "title": "t", "url": None},
            "repository": {"full_name": "acme/app", "html_url": "https://github.com/acme/app"}}


class FakeGitHub:
    """Routes request paths to canned Responses and records conditional headers."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, path, etag=None, if_modified_since=None):
        self.calls.append((path.split("?")[0], etag, if_modified_since))
        r = self.routes[path.split("?")[0]]
        if isinstance(r, Exception):
            raise r
        return r.pop(0) if isinstance(r, list) else r

    def login(self):
        return "me"


def ok(body, **headers):
    return Response(200, body, headers)


NOT_MODIFIED = Response(304, None, {})


class NextIntervalTest(unittest.TestCase):
    def test_activity_resets_to_minimum(self):
        self.assertEqual(poller.next_interval(300, 3, 60), 60)

    def test_quiet_backs_off_to_cap(self):
        intervals, i = [], 60
        for _ in range(6):
            i = poller.next_interval(i, 0, 60)
            intervals.append(i)
        self.assertEqual(intervals, [90, 135, 202, 300, 300, 300])

    def test_server_hint_is_respected_but_capped(self):
        self.assertEqual(poller.next_interval(60, 5, 120), 120)
        self.assertEqual(poller.next_interval(60, 5, 9999), 300)


class PollOnceTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()

    def gh(self, **overrides):
        routes = {"/notifications": ok([notif("1"), notif("2", "subscribed")],
                                       **{"Last-Modified": "LM", "X-Poll-Interval": "60"}),
                  "/users/me/received_events": ok([], ETag="R1"),
                  "/users/me/events": NOT_MODIFIED}
        routes.update(overrides)
        return FakeGitHub(routes)

    def test_stores_items_and_conditional_state(self):
        new, hint = poller.poll_once(self.conn, self.gh(), "me")
        self.assertEqual((new, hint), (2, 60))
        self.assertEqual(store.get_state(self.conn, "notif_last_modified"), "LM")
        self.assertEqual(store.get_state(self.conn, "etag:received"), "R1")
        self.assertIsNotNone(store.get_state(self.conn, "last_poll"))

    def test_second_poll_sends_conditional_headers_and_dedupes(self):
        poller.poll_once(self.conn, self.gh(), "me")
        gh = self.gh()
        new, _ = poller.poll_once(self.conn, gh, "me")
        self.assertEqual(new, 0)
        self.assertIn(("/notifications", None, "LM"), gh.calls)
        self.assertIn(("/users/me/received_events", "R1", None), gh.calls)

    def test_one_failing_source_does_not_block_others(self):
        gh = self.gh(**{"/users/me/received_events": RuntimeError("boom")})
        new, _ = poller.poll_once(self.conn, gh, "me")
        self.assertEqual(new, 2)
        self.assertIn("boom", store.get_state(self.conn, "last_error"))
        self.assertIsNone(store.get_state(self.conn, "last_poll"))  # not a fully healthy poll

    def test_failed_source_does_not_advance_its_etag(self):
        def bad(conn, gh, login):
            store.set_state(conn, "etag:x", "half-written")
            raise RuntimeError("after write")
        bad.__name__ = "bad"
        poller.poll_once(self.conn, self.gh(), "me", sources=(bad,))
        self.assertIsNone(store.get_state(self.conn, "etag:x"))

    def test_server_hint_propagates(self):
        gh = self.gh(**{"/notifications": ok([], **{"X-Poll-Interval": "180"})})
        self.assertEqual(poller.poll_once(self.conn, gh, "me")[1], 180)


class LockTest(unittest.TestCase):
    def test_second_process_cannot_take_lock(self):
        with tempfile.TemporaryDirectory() as home:
            held = poller.acquire_lock(home)
            self.assertIsNotNone(held)
            # flock is per open-file-description, so check from a real second process
            code = textwrap.dedent(f"""
                import sys; sys.path.insert(0, {os.getcwd()!r})
                from ghwatch import poller
                print(poller.acquire_lock({home!r}) is None)
            """)
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "True", out.stderr)
            self.assertTrue(poller.is_running(home))
            held.close()
            self.assertFalse(poller.is_running(home))

    def test_run_exits_when_lock_held(self):
        with tempfile.TemporaryDirectory() as home:
            held = poller.acquire_lock(home)
            try:
                code = textwrap.dedent(f"""
                    import sys; sys.path.insert(0, {os.getcwd()!r})
                    from ghwatch import poller
                    print(poller.run({home!r}, gh=None, once=True))
                """)
                out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
                self.assertEqual(out.stdout.strip(), "False", out.stderr)
            finally:
                held.close()

    def test_run_once_polls_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as home:
            gh = FakeGitHub({"/notifications": ok([notif("1")]),
                             "/users/me/received_events": NOT_MODIFIED,
                             "/users/me/events": NOT_MODIFIED})
            self.assertTrue(poller.run(home, gh, once=True))
            conn = store.connect(os.path.join(home, "ghwatch.db"))
            self.assertEqual(store.max_seq(conn), 1)
            self.assertEqual(store.get_state(conn, "login"), "me")
            self.assertFalse(poller.is_running(home))


class StopTest(unittest.TestCase):
    def test_stop_terminates_a_real_poller_process(self):
        with tempfile.TemporaryDirectory() as home:
            code = textwrap.dedent(f"""
                import sys, time; sys.path.insert(0, {os.getcwd()!r})
                from ghwatch import poller
                lock = poller.acquire_lock({home!r})
                print("ready", flush=True)
                time.sleep(60)
            """)
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                self.assertEqual(poller.running_pid(home), proc.pid)
                self.assertTrue(poller.stop(home, timeout=5))
                self.assertIsNone(poller.running_pid(home))
                self.assertIsNotNone(proc.wait(timeout=5))
            finally:
                proc.kill()

    def test_stop_when_nothing_running(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertIsNone(poller.running_pid(home))
            self.assertTrue(poller.stop(home))


if __name__ == "__main__":
    unittest.main()

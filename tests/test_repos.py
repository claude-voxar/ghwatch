import unittest
import urllib.error

from ghwatch import repos
from tests.helpers import memdb


class ReposTest(unittest.TestCase):
    def setUp(self):
        self.conn = memdb()

    def test_track_is_idempotent_and_case_insensitive(self):
        self.assertTrue(repos.track(self.conn, "Acme/App", "a"))
        self.assertFalse(repos.track(self.conn, "acme/app", "b"))
        self.assertEqual([r["repo"] for r in repos.list_tracked(self.conn)], ["acme/app"])

    def test_untrack(self):
        repos.track(self.conn, "acme/app", "a")
        self.assertTrue(repos.untrack(self.conn, "ACME/app"))
        self.assertFalse(repos.untrack(self.conn, "acme/app"))

    def test_rejects_non_repo_names(self):
        for bad in ("acme", "acme/app/x", "acme/*", "", "a b/c"):
            with self.assertRaises(ValueError, msg=bad):
                repos.track(self.conn, bad, "a")

    def test_is_exact(self):
        self.assertTrue(repos.is_exact("acme/app"))
        self.assertFalse(repos.is_exact("acme/*"))
        self.assertFalse(repos.is_exact("acme/ap?"))
        self.assertFalse(repos.is_exact("acme"))


class FakeGH:
    def __init__(self, exc=None):
        self.exc = exc

    def request(self, method, path):
        if self.exc:
            raise self.exc


class CheckExistsTest(unittest.TestCase):
    def test_outcomes(self):
        self.assertTrue(repos.check_exists(FakeGH(), "a/b"))
        self.assertFalse(repos.check_exists(FakeGH(urllib.error.HTTPError("u", 404, "nf", {}, None)), "a/b"))
        self.assertIsNone(repos.check_exists(FakeGH(urllib.error.HTTPError("u", 502, "x", {}, None)), "a/b"))
        self.assertIsNone(repos.check_exists(FakeGH(OSError("offline")), "a/b"))


if __name__ == "__main__":
    unittest.main()

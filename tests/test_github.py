import io
import json
import unittest
import urllib.error

from ghwatch.github import GitHub


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeResp(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(json.dumps(body).encode() if body is not None else b"")
        self.status = status
        self.headers = headers or {}


def http_error(code):
    return urllib.error.HTTPError("u", code, "msg", {"X-Poll-Interval": "90"}, None)


class GitHubTest(unittest.TestCase):
    def test_sends_auth_and_conditional_headers(self):
        http = FakeHTTP(FakeResp(200, [1], {"ETag": "abc"}))
        r = GitHub(lambda: "tok", http).request("GET", "/x", etag="e1", if_modified_since="lm")
        req = http.requests[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(req.get_header("If-none-match"), "e1")
        self.assertEqual(req.get_header("If-modified-since"), "lm")
        self.assertEqual((r.status, r.body, r.headers["ETag"]), (200, [1], "abc"))

    def test_304_is_a_response_not_an_error(self):
        r = GitHub(lambda: "t", FakeHTTP(http_error(304))).request("GET", "/x")
        self.assertTrue(r.not_modified)
        self.assertEqual(r.poll_interval(60), 90)

    def test_401_drops_cached_token(self):
        tokens = iter(["old", "new"])
        http = FakeHTTP(http_error(401), FakeResp(200, {}))
        client = GitHub(lambda: next(tokens), http)
        with self.assertRaises(urllib.error.HTTPError):
            client.request("GET", "/x")
        client.request("GET", "/x")
        self.assertEqual(http.requests[1].get_header("Authorization"), "Bearer new")

    def test_next_path_from_link_header(self):
        link = ('<https://api.github.com/user/1/events?per_page=100&page=2>; rel="next", '
                '<https://api.github.com/user/1/events?per_page=100&page=3>; rel="last"')
        r = GitHub(lambda: "t", FakeHTTP(FakeResp(200, [], {"Link": link}))).request("GET", "/")
        self.assertEqual(r.next_path, "/user/1/events?per_page=100&page=2")
        last_page = GitHub(lambda: "t", FakeHTTP(FakeResp(200, [], {"Link": '<x>; rel="prev"'}))).request("GET", "/")
        self.assertIsNone(last_page.next_path)

    def test_poll_interval_defaults_on_garbage(self):
        r = GitHub(lambda: "t", FakeHTTP(FakeResp(200, {}, {"X-Poll-Interval": "x"}))).request("GET", "/")
        self.assertEqual(r.poll_interval(60), 60)


if __name__ == "__main__":
    unittest.main()

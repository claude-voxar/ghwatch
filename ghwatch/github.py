"""Minimal GitHub REST client using the `gh` CLI's token.

Supports conditional requests (ETag / If-Modified-Since): a 304 does not
count against the rate limit, which keeps frequent polling cheap.
"""
import json
import re
import subprocess
import urllib.error
import urllib.request

API = "https://api.github.com"


class Response:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers

    @property
    def not_modified(self):
        return self.status == 304

    @property
    def next_path(self):
        """API path of the next page from the Link header, or None."""
        m = re.search(r'<([^>]+)>;\s*rel="next"', self.headers.get("Link") or "")
        if not m:
            return None
        url = m.group(1)
        return url[len(API):] if url.startswith(API) else url

    def poll_interval(self, default):
        try:
            return int(self.headers.get("X-Poll-Interval", default))
        except (TypeError, ValueError):
            return default


def gh_token():
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


class GitHub:
    def __init__(self, token_provider=gh_token, opener=urllib.request.urlopen):
        self._token_provider = token_provider
        self._token = None
        self._open = opener

    def _token_value(self):
        if self._token is None:
            self._token = self._token_provider()
        return self._token

    def request(self, method, path, etag=None, if_modified_since=None):
        req = urllib.request.Request(API + path, method=method)
        req.add_header("Authorization", f"Bearer {self._token_value()}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ghwatch")
        if etag:
            req.add_header("If-None-Match", etag)
        if if_modified_since:
            req.add_header("If-Modified-Since", if_modified_since)
        try:
            with self._open(req, timeout=30) as r:
                raw = r.read()
                return Response(r.status, json.loads(raw) if raw else None, dict(r.headers))
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return Response(304, None, dict(e.headers))
            if e.code == 401:
                self._token = None  # token may have been refreshed by `gh`; re-read next call
            raise

    def login(self):
        return self.request("GET", "/user").body["login"]

    def mark_thread_read(self, thread_id):
        self.request("PATCH", f"/notifications/threads/{thread_id}")

import json
import os
import tempfile
import unittest

from ghwatch import transcripts
from ghwatch.transcripts import Scanner


def tool_use(id_, command, name="Bash", session="s1"):
    return {"type": "assistant", "sessionId": session, "cwd": "/w", "timestamp": "T",
            "message": {"content": [{"type": "tool_use", "id": id_, "name": name,
                                     "input": {"command": command}}]}}


def tool_result(id_, text, is_error=False, session="s1"):
    item = {"type": "tool_result", "tool_use_id": id_, "content": [{"type": "text", "text": text}]}
    if is_error:
        item["is_error"] = True
    return {"type": "user", "sessionId": session, "cwd": "/w", "timestamp": "T",
            "message": {"content": [item]}}


def notice(text, session="s1"):
    return {"type": "user", "sessionId": session, "cwd": "/w", "timestamp": "T", "uuid": text,
            "message": {"content": text}}


class FailureDetailTest(unittest.TestCase):
    def test_traceback_reports_exception_line(self):
        out = "Traceback (most recent call last):\n  File x\nsqlite3.OperationalError: database is locked\n"
        self.assertEqual(transcripts.failure_detail(out, False),
                         "sqlite3.OperationalError: database is locked")

    def test_argparse_error(self):
        out = "usage: ghwatch ...\nghwatch watch: error: the following arguments are required: -c\n"
        self.assertIn("arguments are required", transcripts.failure_detail(out, True))

    def test_normal_outputs_are_fine(self):
        for ok in ("n:1:t: claimed", "n:1:t: ALREADY CLAIMED by a at T", "(nothing open)",
                   "n:1: not marked - claimed by b", '{"events": 3}'):
            self.assertIsNone(transcripts.failure_detail(ok, False), ok)

    def test_is_error_without_pattern_uses_first_line(self):
        self.assertEqual(transcripts.failure_detail("weird\nmore", True), "weird")


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "projects")
        os.makedirs(os.path.join(self.root, "proj"))
        self.path = os.path.join(self.root, "proj", "s1.jsonl")
        self.state = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *entries, raw=None):
        with open(self.path, "a") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
            if raw:
                fh.write(raw)

    def scanner(self, **kw):
        return Scanner(self.root, self.state, **kw)

    def test_pairs_ghwatch_command_with_failing_result(self):
        self.write(tool_use("t1", "~/ghwatch/bin/ghwatch inbox -c a"),
                   tool_result("t1", "Traceback (most recent call last):\nKeyError: 'x'", True))
        [p] = self.scanner().scan()
        self.assertIn("session=s1", p.line())
        self.assertIn('cmd="~/ghwatch/bin/ghwatch inbox -c a"', p.line())
        self.assertIn("KeyError: 'x'", p.line())
        self.assertTrue(p.where.endswith("s1.jsonl:2"))

    def test_ignores_successes_and_unrelated_commands(self):
        self.write(tool_use("t1", "ghwatch claim x -c a"), tool_result("t1", "x: claimed"),
                   tool_use("t2", "ls /nope"), tool_result("t2", "No such file", True))
        self.assertEqual(self.scanner().scan(), [])

    def test_reading_ghwatch_files_is_not_running_ghwatch(self):
        readme = "- **`GHWATCH-STALE ...`** means no successful poll has completed\nraise HTTPError(...)"
        cmds = ["cd ~/ghwatch && ls; cat README.md", "grep -rn Error ~/ghwatch/ghwatch",
                "cat ~/ghwatch/ghwatch/github.py", "cd ~/ghwatch-dev && make test"]
        for i, cmd in enumerate(cmds):
            self.write(tool_use(f"r{i}", cmd), tool_result(f"r{i}", readme, True))
        self.assertEqual(self.scanner().scan(), [])

    def test_recognises_real_invocations(self):
        cmds = ["~/ghwatch/bin/ghwatch inbox -c a", "cd x && ghwatch claim e:1 -c a",
                "GHWATCH_HOME=/t python3 -m ghwatch poll --once", "~/ghwatch/bin/deploy"]
        for i, cmd in enumerate(cmds):
            self.write(tool_use(f"v{i}", cmd), tool_result(f"v{i}", "database is locked", True))
        self.assertEqual(len(self.scanner().scan()), len(cmds))

    def test_monitor_stale_notice(self):
        self.write(notice("<task-notification><event>GHWATCH-STALE no successful poll for 900s; last_error=x</event></task-notification>"))
        [p] = self.scanner().scan()
        self.assertIn("GHWATCH-STALE no successful poll for 900s", p.detail)

    def test_excluded_session(self):
        self.write(tool_use("t1", "ghwatch status", session="me"), tool_result("t1", "Error", True, session="me"))
        self.assertEqual(self.scanner(exclude_sessions=["me"]).scan(), [])

    def test_incremental_partial_lines_and_persistence(self):
        self.write(tool_use("t1", "~/ghwatch/bin/ghwatch status"), raw='{"type": "user", "sessio')
        s = self.scanner()
        self.assertEqual(s.scan(), [])
        s.save()
        # finish the partial line with a failing result, scan with a fresh scanner
        with open(self.path, "a") as fh:
            fh.write('nId": "s1", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", '
                     '"is_error": true, "content": "database is locked"}]}}\n')
        [p] = self.scanner().scan()
        self.assertEqual(p.detail, "database is locked")
        self.assertTrue(p.where.endswith(":2"))

    def test_no_repeats_across_restarts(self):
        self.write(notice("GHWATCH-STALE no successful poll for 1s"))
        s = self.scanner()
        self.assertEqual(len(s.scan()), 1)
        s.save()
        s2 = self.scanner()
        s2.state["offsets"] = {}  # even when re-reading the file
        self.assertEqual(s2.scan(), [])

    def test_skip_history_then_new_files_read_from_start(self):
        self.write(notice("GHWATCH-STALE no successful poll for old"))
        s = self.scanner()
        s.skip_history()
        self.assertEqual(s.scan(), [])
        other = os.path.join(self.root, "proj", "sub", "s2.jsonl")
        os.makedirs(os.path.dirname(other))
        with open(other, "w") as fh:
            fh.write(json.dumps(notice("GHWATCH-STALE no successful poll for new", session="s2")) + "\n")
        [p] = s.scan()
        self.assertEqual(p.session, "s2")


if __name__ == "__main__":
    unittest.main()

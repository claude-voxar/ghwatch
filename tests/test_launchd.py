import os
import plistlib
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from ghwatch import launchd


class FakeLaunchctl:
    """Records launchctl calls; `responses` maps the subcommand to (rc, stdout)."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, capture_output, text):
        self.calls.append(argv[1:])
        rc, out = self.responses.get(argv[1], (0, ""))
        if callable(out):
            out = out()
        return subprocess.CompletedProcess(argv, rc, out, "boom" if rc else "")


class PlistTest(unittest.TestCase):
    def test_contents(self):
        p = launchd.build_plist("/h", python="/py", pkg_root="/repo", gh_path="/opt/homebrew/bin/gh")
        self.assertEqual(p["ProgramArguments"], ["/py", "/repo/bin/ghwatch", "poll", "--wait-lock"])
        self.assertTrue(p["KeepAlive"])
        self.assertTrue(p["RunAtLoad"])
        self.assertEqual(p["EnvironmentVariables"]["GHWATCH_HOME"], "/h")
        self.assertTrue(p["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:"))
        self.assertEqual(p["StandardErrorPath"], "/h/poller.log")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agents = os.path.join(self.tmp.name, "LaunchAgents")
        self.home = os.path.join(self.tmp.name, "home")

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_writes_plist_and_bootstraps(self):
        run = FakeLaunchctl()
        path = launchd.install(self.home, self.agents, run=run)
        with open(path, "rb") as fh:
            self.assertEqual(plistlib.load(fh)["Label"], launchd.LABEL)
        self.assertEqual([c[0] for c in run.calls], ["bootout", "bootstrap"])
        self.assertEqual(run.calls[1][2], path)
        self.assertTrue(launchd.is_installed(self.agents))

    def test_install_reports_bootstrap_failure(self):
        with self.assertRaises(RuntimeError):
            launchd.install(self.home, self.agents, run=FakeLaunchctl(bootstrap=(5, "")))

    def test_uninstall(self):
        launchd.install(self.home, self.agents, run=FakeLaunchctl())
        self.assertTrue(launchd.uninstall(self.agents, run=FakeLaunchctl()))
        self.assertFalse(launchd.is_installed(self.agents))
        self.assertFalse(launchd.uninstall(self.agents, run=FakeLaunchctl()))


class AgentPidTest(unittest.TestCase):
    def test_parses_launchctl_print(self):
        out = "gui/501/x = {\n\tactive count = 1\n\tpid = 4242\n\tstate = running\n}"
        self.assertEqual(launchd.agent_pid(run=FakeLaunchctl(print=(0, out))), 4242)

    def test_not_running_or_not_loaded(self):
        self.assertIsNone(launchd.agent_pid(run=FakeLaunchctl(print=(0, "state = waiting"))))
        self.assertIsNone(launchd.agent_pid(run=FakeLaunchctl(print=(113, ""))))


class RestartTest(unittest.TestCase):
    def test_retires_watcher_poller_until_agent_holds_lock(self):
        holders = iter([111, 111, 222])  # watcher poller, then agent takes over
        kills = []
        run = FakeLaunchctl(print=(0, "pid = 222"))
        with mock.patch("ghwatch.poller.running_pid", lambda home: next(holders)):
            ok = launchd.restart("/h", run=run, kill=lambda pid, sig: kills.append((pid, sig)),
                                 sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(run.calls[0], ["kickstart", "-k", launchd.service()])
        self.assertEqual(kills, [(111, signal.SIGTERM), (111, signal.SIGTERM)])

    def test_gives_up_after_timeout(self):
        run = FakeLaunchctl(print=(0, "pid = 222"))
        with mock.patch("ghwatch.poller.running_pid", lambda home: None):
            self.assertFalse(launchd.restart("/h", run=run, kill=lambda *a: None,
                                             sleep=lambda s: None, timeout=1))

    def test_kickstart_failure_raises(self):
        with self.assertRaises(RuntimeError):
            launchd.restart("/h", run=FakeLaunchctl(kickstart=(3, "")), sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()

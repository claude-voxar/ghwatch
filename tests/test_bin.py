"""bin/ghwatch runs only ghwatch's own .venv; bin/setup builds it from .tool-versions."""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        shutil.copytree(ROOT, self.repo, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
        self.elsewhere = os.path.join(self.tmp.name, "other-project")
        os.makedirs(self.elsewhere)
        # Minimal PATH, called from another project's directory: the wrapper must
        # not depend on whatever python3 (or asdf shim) that resolves to.
        self.env = dict(os.environ, PATH="/usr/bin:/bin", GHWATCH_HOME=os.path.join(self.tmp.name, "h"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_bin(self, name, *args, **env):
        return subprocess.run(["/bin/sh", os.path.join(self.repo, "bin", name), *args],
                              cwd=self.elsewhere, env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=120)

    def pin(self, version):
        with open(os.path.join(self.repo, ".tool-versions"), "w") as fh:
            fh.write(f"python {version}\n")

    def test_ghwatch_without_venv_explains_and_does_not_fall_back(self):
        r = self.run_bin("ghwatch", "status")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no Python environment", r.stderr)
        self.assertIn("bin/setup", r.stderr)

    def test_ghwatch_uses_venv_from_any_directory(self):
        os.makedirs(os.path.join(self.repo, ".venv", "bin"))
        os.symlink(sys.executable, os.path.join(self.repo, ".venv", "bin", "python"))
        r = self.run_bin("ghwatch", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: ghwatch", r.stdout)

    def test_setup_builds_venv_from_pinned_python_once(self):
        self.pin(platform.python_version())
        r = self.run_bin("setup", GHWATCH_BASE_PYTHON=sys.executable)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("built .venv", r.stdout)
        self.assertIn("already on", self.run_bin("setup", GHWATCH_BASE_PYTHON=sys.executable).stdout)
        r = self.run_bin("ghwatch", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_setup_refuses_an_interpreter_other_than_the_pin(self):
        self.pin("3.0.1")
        r = self.run_bin("setup", GHWATCH_BASE_PYTHON=sys.executable)
        self.assertEqual(r.returncode, 1)
        self.assertIn("need Python 3.0.1", r.stderr)
        self.assertIn("asdf install python 3.0.1", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".venv")))

    def test_repo_pins_a_python_version(self):
        with open(os.path.join(ROOT, ".tool-versions")) as fh:
            self.assertRegex(fh.read(), r"(?m)^python 3\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()

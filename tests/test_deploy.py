import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class DeployTest(unittest.TestCase):
    """Runs bin/deploy against a throwaway origin + production clone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.origin, self.work, self.prod = (os.path.join(t, d) for d in ("origin.git", "work", "prod"))
        ignore = ("--exclude=.git", "--exclude=__pycache__")
        os.makedirs(self.work)
        subprocess.run(["tar", "-C", ROOT, *ignore, "-cf", os.path.join(t, "src.tar"), "."], check=True)
        subprocess.run(["tar", "-C", self.work, "-xf", os.path.join(t, "src.tar")], check=True)
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        git(t, "clone", "-q", "--bare", self.work, self.origin)
        git(self.work, "remote", "add", "origin", self.origin)
        git(t, "clone", "-q", self.origin, self.prod)
        self.skills = os.path.join(t, "claude-skills")
        self.env = dict(os.environ, GHWATCH_HOME=os.path.join(t, "home"), CLAUDE_SKILLS_DIR=self.skills,
                        GHWATCH_DEPLOY_TEST_CMD="python3 -m compileall -q ghwatch")

    def tearDown(self):
        self.tmp.cleanup()

    def push_change(self, text):
        with open(os.path.join(self.work, "ghwatch", "store.py"), "a") as fh:
            fh.write(text)
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change")
        git(self.work, "push", "-q", "origin", "main")

    def deploy(self, *args):
        return subprocess.run([os.path.join(self.prod, "bin", "deploy"), *args], cwd=self.prod,
                              env=self.env, capture_output=True, text=True)

    def test_noop_then_fast_forward(self):
        self.assertIn("already at", self.deploy().stdout)
        self.push_change("\n# ok\n")
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("->", r.stdout)
        self.assertEqual(git(self.prod, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "main"))

    def test_links_skills(self):
        self.push_change("\n# ok\n")
        self.assertEqual(self.deploy().returncode, 0)
        link = os.path.join(self.skills, "watch-repo")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(self.prod, "skills", "watch-repo")))
        self.assertTrue(os.path.exists(os.path.join(link, "SKILL.md")))

    def test_does_not_replace_a_real_skill_dir(self):
        mine = os.path.join(self.skills, "watch-repo")
        os.makedirs(mine)
        self.push_change("\n# ok\n")
        r = self.deploy()
        self.assertEqual(r.returncode, 0)
        self.assertIn("not a symlink; leaving it alone", r.stderr)
        self.assertFalse(os.path.islink(mine))

    def test_new_deploy_steps_run_in_the_same_deploy(self):
        path = os.path.join(self.work, "bin", "deploy")
        with open(path) as fh:
            script = fh.read()
        marker = 'echo "deploy: ${before:0:7} -> ${after:0:7}"'
        self.assertIn(marker, script)
        with open(path, "w") as fh:
            fh.write(script.replace(marker, 'echo "NEW STEP RAN from=${before:0:7}"\n' + marker))
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "new step")
        git(self.work, "push", "-q", "origin", "main")
        before = git(self.prod, "rev-parse", "--short=7", "HEAD").strip()
        r = self.deploy()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("continuing with the new version", r.stdout)
        self.assertIn(f"NEW STEP RAN from={before}", r.stdout)

    def test_failing_tests_roll_back(self):
        before = git(self.prod, "rev-parse", "HEAD")
        self.push_change("\ndef broken(:\n")
        r = self.deploy()
        self.assertEqual(r.returncode, 1)
        self.assertIn("rolling back", r.stderr)
        self.assertEqual(git(self.prod, "rev-parse", "HEAD"), before)

    def test_refuses_dirty_or_non_main(self):
        with open(os.path.join(self.prod, "README.md"), "a") as fh:
            fh.write("local edit")
        self.assertIn("local changes", self.deploy().stderr)
        git(self.prod, "checkout", "-q", "--", "README.md")
        git(self.prod, "switch", "-q", "-c", "other")
        self.assertIn("must be on main", self.deploy().stderr)


if __name__ == "__main__":
    unittest.main()

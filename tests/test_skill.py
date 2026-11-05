"""Keep the watch-repo skill in sync with the CLI it documents."""
import os
import re
import unittest

from ghwatch import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, "skills")


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return dict(line.split(": ", 1) for line in m.group(1).splitlines()) if m else {}


class SkillTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(SKILLS, "watch-repo", "SKILL.md")) as fh:
            self.text = fh.read()

    def test_every_skill_has_valid_frontmatter(self):
        for name in os.listdir(SKILLS):
            with open(os.path.join(SKILLS, name, "SKILL.md")) as fh:
                meta = frontmatter(fh.read())
            self.assertEqual(meta.get("name"), name)
            self.assertGreater(len(meta.get("description", "")), 50)

    def test_documented_subcommands_exist(self):
        parser = cli.build_parser()
        subparsers = next(a for a in parser._actions if a.dest == "cmd").choices
        used = set(re.findall(r"(?:\$G|bin/ghwatch) ([a-z][a-z-]+)", self.text))
        self.assertTrue(used)
        self.assertEqual(used - set(subparsers), set())

    def test_documented_watch_flags_exist(self):
        watch = next(a for a in cli.build_parser()._actions if a.dest == "cmd").choices["watch"]
        known = {opt for a in watch._actions for opt in a.option_strings}
        used = set(re.findall(r"(--[a-z][a-z-]+)", self.text)) - {"--read"}
        self.assertEqual(used - known, set())


if __name__ == "__main__":
    unittest.main()

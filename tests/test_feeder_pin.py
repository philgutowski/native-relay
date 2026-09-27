"""The feeder's checkout warning and `feed --pin` (issue 35).

A feeder launches the runner from its own tree at every cycle, so started from a git checkout it
runs whatever that checkout holds. These cases build a throwaway repo standing in for that
checkout, point `runner_tree` at it, and never touch the real one.
"""
import io
import os
import tempfile
import unittest
from unittest import mock

import _paths
import _repo
from relay import cli, feeder, gitread

ENTRY = "skills/relay/scripts/relay_cli.py"
# Stands in for the runner in the extract: it says which argv it got and exits 7.
FAKE_ENTRY = "import sys\nprint('extract ran', ' '.join(sys.argv[1:]))\nsys.exit(7)\n"


class Case(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = os.path.realpath(holder.name)
        self.home = os.path.join(self.base, "home")
        os.makedirs(self.home)
        self.checkout = _repo.make_repo(self.base, files={ENTRY: FAKE_ENTRY})

    def env(self):
        return dict(os.environ, HOME=self.home)


class Detection(Case):
    def test_a_work_tree_is_found_from_any_depth(self):
        deep = os.path.join(self.checkout, "skills", "relay", "scripts")
        self.assertEqual(gitread.work_tree_root(deep), self.checkout)

    def test_a_plain_directory_is_not_a_work_tree(self):
        plain = os.path.join(self.base, "plain")
        os.makedirs(plain)
        self.assertIsNone(gitread.work_tree_root(plain))

    def test_the_warning_names_the_tree_and_the_pinned_path(self):
        text = feeder.checkout_warning(self.checkout)
        self.assertIn(self.checkout, text)
        self.assertIn("--pin", text)

    def test_no_warning_outside_a_work_tree(self):
        plain = os.path.join(self.base, "plain")
        os.makedirs(plain)
        self.assertIsNone(feeder.checkout_warning(plain))


class Extract(Case):
    def test_the_extract_holds_head_and_no_git_metadata(self):
        destination, dirty = feeder.pin_extract(self.checkout, self.home)
        sha = gitread.rev_parse(self.checkout, "HEAD")[:12]
        self.assertEqual(destination, os.path.join(self.home, ".relay", "extracts",
                                                   "native-relay-" + sha))
        self.assertTrue(os.path.isfile(os.path.join(destination, ENTRY)))
        self.assertFalse(os.path.exists(os.path.join(destination, ".git")))
        self.assertIsNone(dirty)
        self.assertIsNone(feeder.checkout_warning(destination))

    def test_an_existing_extract_is_reused_untouched(self):
        destination, _ = feeder.pin_extract(self.checkout, self.home)
        marker = os.path.join(destination, "marker")
        with open(marker, "w") as handle:
            handle.write("a running feeder may be reading here")
        again, _ = feeder.pin_extract(self.checkout, self.home)
        self.assertEqual(again, destination)
        self.assertTrue(os.path.exists(marker))

    def test_uncommitted_work_is_reported_and_left_out(self):
        with open(os.path.join(self.checkout, "wip.txt"), "w") as handle:
            handle.write("half finished\n")
        destination, dirty = feeder.pin_extract(self.checkout, self.home)
        self.assertIn("wip.txt", dirty)
        self.assertFalse(os.path.exists(os.path.join(destination, "wip.txt")))

    def test_no_partial_directory_is_left_behind(self):
        feeder.pin_extract(self.checkout, self.home)
        names = os.listdir(os.path.join(self.home, ".relay", "extracts"))
        self.assertEqual(len(names), 1, names)


class Verb(Case):
    def call(self, *flags):
        manifest = os.path.join(self.base, "m.toml")
        with open(manifest, "w") as handle:
            handle.write("")
        args = cli.build_parser().parse_args(["feed", manifest] + list(flags))
        out = io.StringIO()
        with mock.patch.object(feeder, "runner_tree", return_value=self.checkout):
            code = cli.cmd_feed(args, self.env(), out)
        return code, out.getvalue()

    def test_pin_relaunches_from_the_extract_with_restart_and_passes_the_exit_code(self):
        code, text = self.call("--pin", "--notify")
        self.assertEqual(code, 7, text)
        self.assertIn("pinned extract: %s" % os.path.join(self.home, ".relay", "extracts"), text)
        self.assertIn("extract ran feed", text)
        self.assertIn("--restart", text)
        self.assertIn("--notify", text)
        self.assertNotIn("--pin", text.split("extract ran", 1)[1])

    def test_pin_with_dry_run_does_not_ask_a_running_feeder_to_leave(self):
        code, text = self.call("--pin", "--dry-run")
        self.assertIn("--dry-run", text)
        self.assertNotIn("--restart", text)

    def test_pin_says_what_the_extract_leaves_out(self):
        with open(os.path.join(self.checkout, "wip.txt"), "w") as handle:
            handle.write("half finished\n")
        _, text = self.call("--pin")
        self.assertIn("uncommitted changes", text)

    def test_starting_from_a_checkout_without_pin_warns_the_operator(self):
        code, text = self.call("--dry-run")
        self.assertIn("warning: this feeder launches the runner from %s" % self.checkout, text)


if __name__ == "__main__":
    unittest.main()

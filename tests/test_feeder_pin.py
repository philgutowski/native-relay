"""The feeder's checkout warning and `feed --pin` (issues 35 and 48).

A feeder launches the runner from its own tree at every cycle, so started from a git checkout it
runs whatever that checkout holds. These cases build a throwaway repo standing in for that
checkout, point `runner_tree` at it, and never touch the real one. The manifest names that same
repo, which is the self hosted shape: while a task is in flight the checkout sits on the task
branch, and `--pin` must extract the default branch's commit, never HEAD.
"""
import io
import os
import tempfile
import time
import unittest
from unittest import mock

import _paths
import _repo
from relay import cli, feeder, gitread, manifest as mf
from test_run import MANIFEST

ENTRY = "skills/relay/scripts/relay_cli.py"
# Stands in for the runner in the extract: it says which argv it got and exits 7.
FAKE_ENTRY = "import sys\nprint('extract ran', ' '.join(sys.argv[1:]))\nsys.exit(7)\n"
MANIFEST_HEAD = MANIFEST.split("[[tasks]]")[0]


class Case(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = os.path.realpath(holder.name)
        self.home = os.path.join(self.base, "home")
        os.makedirs(self.home)
        self.checkout = _repo.make_repo(self.base, files={ENTRY: FAKE_ENTRY})
        self.manifest_path = os.path.join(self.base, "m.toml")
        self.write_manifest()

    def write_manifest(self, repo=None, default_branch="main"):
        text = MANIFEST_HEAD.replace("__REPO__", repo or self.checkout)
        text = text.replace('default_branch = "main"\n',
                            'default_branch = "%s"\n' % default_branch if default_branch else "")
        with open(self.manifest_path, "w") as handle:
            handle.write(text)

    def env(self):
        return dict(os.environ, HOME=self.home)

    def plan(self):
        return feeder.pin_plan(self.checkout, self.home, mf.load(self.manifest_path,
                                                                 allow_no_tasks=True))

    def extracts(self):
        return os.path.join(self.home, ".relay", "extracts")

    def on_task_branch(self):
        """Put the checkout on `relay/9` with one commit main does not have, as a self hosted
        run's task process leaves it mid task. Returns main's sha."""
        main = gitread.rev_parse(self.checkout, "main")
        _repo.git(self.checkout, "checkout", "-q", "-b", "relay/9")
        with open(os.path.join(self.checkout, "unmerged.txt"), "w") as handle:
            handle.write("not gated, not reviewed\n")
        _repo.git(self.checkout, "add", "-A")
        _repo.git(self.checkout, "commit", "-q", "-m", "work in flight")
        return main


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
    def test_the_extract_holds_the_default_branch_and_no_git_metadata(self):
        pin = self.plan()
        destination = feeder.pin_extract(self.checkout, pin)
        sha = gitread.rev_parse(self.checkout, "main")
        self.assertEqual(destination, os.path.join(self.extracts(), "native-relay-" + sha[:12]))
        self.assertEqual(pin.short, sha[:feeder.PIN_SHA_LENGTH])
        self.assertTrue(os.path.isfile(os.path.join(destination, ENTRY)))
        self.assertFalse(os.path.exists(os.path.join(destination, ".git")))
        self.assertIsNone(pin.uncommitted)
        self.assertIsNone(feeder.checkout_warning(destination))

    def test_the_name_is_the_one_git_prints_for_short_twelve(self):
        short = _repo.git(self.checkout, "rev-parse", "--short=12", "main").stdout.strip()
        self.assertEqual(os.path.basename(self.plan().destination), "native-relay-" + short)

    def test_a_checkout_on_a_task_branch_pins_the_default_branch_not_head(self):
        main = self.on_task_branch()
        pin = self.plan()
        self.assertEqual((pin.branch, pin.sha, pin.head_branch), ("main", main, "relay/9"))
        destination = feeder.pin_extract(self.checkout, pin)
        self.assertFalse(os.path.exists(os.path.join(destination, "unmerged.txt")))

    def test_an_existing_extract_is_reused_untouched(self):
        destination = feeder.pin_extract(self.checkout, self.plan())
        marker = os.path.join(destination, "marker")
        with open(marker, "w") as handle:
            handle.write("a running feeder may be reading here")
        again = self.plan()
        self.assertTrue(again.exists)
        self.assertEqual(feeder.pin_extract(self.checkout, again), destination)
        self.assertTrue(os.path.exists(marker))

    def test_uncommitted_work_is_reported_and_left_out(self):
        with open(os.path.join(self.checkout, "wip.txt"), "w") as handle:
            handle.write("half finished\n")
        pin = self.plan()
        destination = feeder.pin_extract(self.checkout, pin)
        self.assertIn("wip.txt", pin.uncommitted)
        self.assertFalse(os.path.exists(os.path.join(destination, "wip.txt")))

    def test_no_partial_directory_is_left_behind(self):
        feeder.pin_extract(self.checkout, self.plan())
        self.assertEqual(len(os.listdir(self.extracts())), 1)

    def test_the_plan_writes_nothing(self):
        self.plan()
        self.assertFalse(os.path.exists(os.path.join(self.home, ".relay")))

    def test_a_manifest_aimed_at_another_repo_pins_this_trees_own_default_branch(self):
        other = _repo.make_repo(self.base, name="target")
        self.write_manifest(repo=other, default_branch="trunk")
        self.on_task_branch()
        self.assertEqual(self.plan().branch, "main")


class PartialSweep(Case):
    """Issue #61: a partial folder `pin_extract` leaves behind survives a kill or a power loss,
    since neither leaves its own `finally` a turn to run. `sweep_partials` is the cleanup for
    that, run at the top of every real extraction."""

    def make_partial(self, pin, suffix, age_seconds=None):
        os.makedirs(os.path.dirname(pin.destination), exist_ok=True)
        partial = pin.destination + suffix
        os.makedirs(partial)
        if age_seconds is not None:
            stamp = time.time() - age_seconds
            os.utime(partial, (stamp, stamp))
        return partial

    def test_a_partial_left_by_an_interrupted_attempt_is_swept(self):
        """A kill or a power loss mid extract leaves exactly this: a `.partial-<pid>` directory
        with no process left to finish or clean it up."""
        pin = self.plan()
        stale = self.make_partial(pin, ".partial-999999",
                                  age_seconds=feeder.PARTIAL_MAX_AGE_SECONDS + 3600)
        feeder.pin_extract(self.checkout, pin)
        self.assertFalse(os.path.exists(stale))

    def test_a_partial_still_being_written_is_left_alone(self):
        """A fresh partial's mtime keeps moving as `tar` adds to it, so one from a `--pin`
        running at this same moment must not be swept out from under it."""
        pin = self.plan()
        fresh = self.make_partial(pin, ".partial-888888")
        feeder.pin_extract(self.checkout, pin)
        self.assertTrue(os.path.exists(fresh))

    def test_the_sweep_runs_even_when_the_destination_already_exists(self):
        pin = feeder.pin_extract(self.checkout, self.plan())
        pin = self.plan()
        stale = self.make_partial(pin, ".partial-777777",
                                  age_seconds=feeder.PARTIAL_MAX_AGE_SECONDS + 3600)
        feeder.pin_extract(self.checkout, pin)
        self.assertFalse(os.path.exists(stale))

    def test_a_destination_without_its_entry_file_is_replaced(self):
        """A destination directory left over from an earlier failure, real but incomplete: the
        entry file check, not just an existence check, decides whether to re-extract."""
        pin = self.plan()
        os.makedirs(pin.destination)
        with open(os.path.join(pin.destination, "garbage"), "w") as handle:
            handle.write("leftover from a previous failure\n")
        destination = feeder.pin_extract(self.checkout, pin)
        self.assertTrue(os.path.isfile(os.path.join(destination, ENTRY)))
        self.assertFalse(os.path.exists(os.path.join(destination, "garbage")))


class Unresolvable(Case):
    def test_no_default_branch_anywhere_is_refused_with_a_sentence(self):
        self.write_manifest(default_branch=None)
        _repo.git(self.checkout, "remote", "set-head", "origin", "-d")
        with self.assertRaisesRegex(OSError, "no default branch to pin"):
            self.plan()

    def test_another_repo_and_no_origin_head_is_refused_with_the_fix(self):
        self.write_manifest(repo=_repo.make_repo(self.base, name="target"))
        _repo.git(self.checkout, "remote", "set-head", "origin", "-d")
        with self.assertRaisesRegex(OSError, "not this checkout.*remote set-head origin"):
            self.plan()

    def test_a_default_branch_with_no_local_ref_is_refused(self):
        self.write_manifest(default_branch="trunk")
        with self.assertRaisesRegex(OSError, "trunk has no local branch"):
            self.plan()

    def test_the_manifest_default_is_used_before_origin_head(self):
        _repo.git(self.checkout, "branch", "release")
        self.write_manifest(default_branch="release")
        self.assertEqual(self.plan().branch, "release")


class Verb(Case):
    def call(self, *flags, tree=None):
        args = cli.build_parser().parse_args(["feed", self.manifest_path] + list(flags))
        out = io.StringIO()
        with mock.patch.object(feeder, "runner_tree", return_value=tree or self.checkout):
            code = cli.cmd_feed(args, self.env(), out)
        return code, out.getvalue()

    def test_pin_relaunches_from_the_extract_with_restart_and_passes_the_exit_code(self):
        code, text = self.call("--pin", "--notify")
        self.assertEqual(code, 7, text)
        self.assertIn("pinned extract: %s" % self.extracts(), text)
        self.assertIn("extract ran feed", text)
        self.assertIn("--restart", text)
        self.assertIn("--notify", text)
        self.assertNotIn("--pin", text.split("extract ran", 1)[1])

    def test_pin_on_a_task_branch_names_the_default_branch_and_the_branch_left_out(self):
        main = self.on_task_branch()
        code, text = self.call("--pin")
        self.assertEqual(code, 7, text)
        self.assertIn("(main at %s)" % main[:12], text)
        self.assertIn("the checkout sits on relay/9 at ", text)
        self.assertIn(", not main;", text)
        extract = os.path.join(self.extracts(), "native-relay-" + main[:12])
        self.assertFalse(os.path.exists(os.path.join(extract, "unmerged.txt")))

    def test_pin_with_dry_run_creates_nothing_and_says_what_it_would_extract(self):
        main = gitread.rev_parse(self.checkout, "main")
        with mock.patch.object(feeder, "Feeder") as loop:
            loop.return_value.run.return_value = 0
            code, text = self.call("--pin", "--dry-run")
        self.assertEqual(code, 0, text)
        self.assertIn("would pin: %s (main at %s)" % (
            os.path.join(self.extracts(), "native-relay-" + main[:12]), main[:12]), text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".relay")))
        self.assertNotIn("extract ran", text)
        self.assertTrue(loop.call_args.kwargs["dry_run"])

    def test_pin_with_dry_run_and_detach_refuses_the_pair_and_extracts_nothing(self):
        # Issue #60: the dry run branch printed what it would pin, then fell through to
        # `_detach_feeder`, which started a real feeder from the checkout, the exact tree `--pin`
        # exists to avoid. The pair is refused before the pin plan is even read.
        code, text = self.call("--pin", "--dry-run", "--detach")
        self.assertEqual(code, cli.EXIT_CONFIG, text)
        self.assertIn("--dry-run and --detach do not combine", text)
        self.assertNotIn("would pin", text)
        self.assertNotIn("extract ran", text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".relay")))

    def test_a_detached_head_at_the_default_branch_gets_no_branch_note(self):
        _repo.git(self.checkout, "checkout", "-q", "--detach", "main")
        _, text = self.call("--pin")
        self.assertNotIn("the checkout sits", text)

    def test_a_local_default_branch_behind_origin_is_named(self):
        _repo.git(self.checkout, "commit", "-q", "--allow-empty", "-m", "landed elsewhere")
        _repo.git(self.checkout, "push", "-q", "origin", "main")
        _repo.git(self.checkout, "reset", "-q", "--hard", "HEAD~1")
        _, text = self.call("--pin")
        self.assertIn("origin/main has commits the local main does not", text)

    def test_pin_refuses_when_the_default_branch_cannot_be_resolved(self):
        self.write_manifest(default_branch=None)
        _repo.git(self.checkout, "remote", "set-head", "origin", "-d")
        code, text = self.call("--pin")
        self.assertEqual(code, cli.EXIT_CONFIG, text)
        self.assertIn("could not pin an extract", text)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".relay")))

    def test_pin_from_an_extract_takes_over_with_restart_and_extracts_nothing(self):
        extract = feeder.pin_extract(self.checkout, self.plan())
        before = sorted(os.listdir(self.extracts()))
        with mock.patch.object(feeder, "Feeder") as loop, \
                mock.patch.object(feeder, "wait_for_lock") as wait, \
                mock.patch.object(feeder, "acquire_lock") as acquire:
            loop.return_value.run.return_value = 0
            code, text = self.call("--pin", "--once", tree=extract)
        self.assertEqual(code, 0, text)
        wait.assert_called_once()
        acquire.assert_not_called()
        self.assertNotIn("warning:", text)
        self.assertNotIn("pinned extract", text)
        self.assertEqual(sorted(os.listdir(self.extracts())), before)

    def test_pin_says_what_the_extract_leaves_out(self):
        with open(os.path.join(self.checkout, "wip.txt"), "w") as handle:
            handle.write("half finished\n")
        _, text = self.call("--pin")
        self.assertIn("uncommitted changes", text)

    def test_starting_from_a_checkout_without_pin_warns_the_operator(self):
        with mock.patch.object(feeder, "Feeder") as loop:
            loop.return_value.run.return_value = 0
            code, text = self.call("--dry-run")
        self.assertIn("warning: this feeder launches the runner from %s" % self.checkout, text)


if __name__ == "__main__":
    unittest.main()

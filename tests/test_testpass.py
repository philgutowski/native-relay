"""U5 of the browser test loop plan: one pass end to end through `testpass.run` and the `test`
verb, over the stub `claude`, a real temporary repository, a markdown tracker under a manifest
that does not push, and a feeder sidecar with the loop switched on.

The stub is driven through its existing queue entries: the Test process replays a transcript
ending in a `relay-test-report` block, the Filing process replays one ending in a `relay-filed`
block with a `git.sh` that appends the lines to the tracker and commits them, the way the
Closeout's tests commit through the same hook. No test launches a model or touches a network.
"""
import io
import json
import os
import subprocess
import tempfile
import time
import unittest

import _paths
import _repo
from relay import (cli, contracts, feeder, filing, gitread, manifest as mf, state, testbrief,
                   testloop, testpass)

FIXTURE = os.path.join(_paths.FIXTURES_DIR, "manifests", "complete.toml")
TRANSCRIPTS = os.path.join(_paths.FIXTURES_DIR, "transcripts")

TRACKER_MD = "# Tasks\n\n- [ ] T-1 Add the brief renderer\n"

TOUR_MD = """# Tour of the example app

The driver is `tools/drive.py`, run from the shell.

## Search

Open Search and search for a word that matches three items. Three rows is right; fewer is a
defect.

## Invoices

Open an invoice and reach the Send step. Check that it renders; never press Send.

## Settings

Open Settings and check every field shows its saved value.
"""

SIDECAR = """\
[models]
default = "sonnet"
allowed = ["fable", "opus", "sonnet"]
[test_loop]
enabled = true
tour = "docs/tour.md"
url = "http://127.0.0.1:8765"
prepare = %(prepare)s
prepare_timeout_seconds = %(prepare_timeout)d
labels = ["loop"]
max_cards_per_pass = %(cap)d
%(extra)s
"""


def finding(number, area="Search", severity="high", card=None, **changes):
    shape = {
        "title": "Finding %d: the %s page drops its last row" % (number, area.lower()),
        "severity": severity,
        "kind": "defect",
        "area": area,
        "design": False,
        "cause": {"file": "app/%s.py" % area.lower(), "line": 40 + number, "verdict": "defect"},
        "steps": ["Open %s" % area, "Load a set of three items"],
        "expected": "Three rows",
        "observed": "Two rows, the third is missing",
        "done_when": ["A set of three items shows three rows on %s" % area],
        "card": card,
    }
    shape.update(changes)
    return shape


def report_text(findings, status="ran", reason="", approval_steps=()):
    payload = {"status": status, "reason": reason, "findings": list(findings),
               "approval_steps": list(approval_steps)}
    return ("Toured the app.\n\n```%s\n%s\n```\n"
            % (contracts.TEST_REPORT_FENCE_TAG, json.dumps(payload, indent=1)))


def filed_text(entries):
    return ("Filed them.\n\n```%s\n%s\n```\n"
            % (contracts.FILED_FENCE_TAG, json.dumps(entries)))


def filing_sh(lines, extra=""):
    """The stub's git hook for a Filing process: append the lines to the tracker in the
    checkout it runs in and commit that file alone, as the markdown instructions say."""
    body = "\n".join(lines)
    return ("set -e\n"
            "cat >> tracker.md <<'EOF'\n%s\nEOF\n"
            "%s"
            "git add -A\n"
            "git commit -q -m \"file findings\"\n" % (body, extra))


class PassCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _repo.make_repo(self.tmp.name, files={"tracker.md": TRACKER_MD,
                                                          "docs/tour.md": TOUR_MD,
                                                          "README.md": "# fixture\n"})
        self.home = os.path.join(self.tmp.name, "home")
        self.queue = os.path.join(self.tmp.name, "queue")
        os.makedirs(self.home)
        os.makedirs(self.queue)
        self.manifest_path = os.path.join(self.tmp.name, "run.toml")
        with open(FIXTURE) as handle:
            text = handle.read().replace("__REPO__", self.repo)
        self.write_manifest(text.replace('mode = "local_merge"', 'mode = "local_merge"\npush = false'))
        self.sidecar_path = feeder.paths_for(self.manifest_path).config
        self.prepare_log = os.path.join(self.tmp.name, "prepare.env")
        self.write_sidecar()
        self.entry = 0

    def write_manifest(self, text):
        with open(self.manifest_path, "w") as handle:
            handle.write(text)
        self.manifest = mf.load(self.manifest_path, allow_no_tasks=True)

    def write_sidecar(self, prepare=None, prepare_timeout=30, cap=10, extra=""):
        if prepare is None:
            # A prepare that records what it was told, so a test can read the environment.
            prepare = ["bash", "-c", 'echo "$RELAY_TEST_COMMIT $RELAY_TEST_URL" > "%s"'
                       % self.prepare_log]
        with open(self.sidecar_path, "w") as handle:
            handle.write(SIDECAR % {"prepare": json.dumps(prepare),
                                    "prepare_timeout": prepare_timeout, "cap": cap,
                                    "extra": extra})
        self.config = feeder.load_config(self.sidecar_path)

    def base_env(self):
        return dict(os.environ, HOME=self.home, RELAY_STUB_QUEUE=self.queue,
                    RELAY_TEST_CHECKOUT=self.repo,
                    PATH=_paths.STUB_DIR + os.pathsep + os.environ.get("PATH", ""))

    def transcript(self, text, name):
        path = os.path.join(self.tmp.name, name + ".jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": text}]}}) + "\n")
        return path

    def queue_entry(self, fixture, git_sh=None, sleep=0):
        self.entry += 1
        entry_dir = os.path.join(self.queue, str(self.entry))
        os.makedirs(entry_dir)
        with open(os.path.join(entry_dir, "entry.json"), "w") as handle:
            json.dump({"fixture": fixture, "exit": 0, "sleep": sleep}, handle)
        if git_sh:
            with open(os.path.join(entry_dir, "git.sh"), "w") as handle:
                handle.write(git_sh)

    def test_process(self, findings=None, text=None, git_sh=None, sleep=0):
        text = text if text is not None else report_text(findings or [])
        self.queue_entry(self.transcript(text, "test-%d" % (self.entry + 1)), git_sh=git_sh,
                         sleep=sleep)

    def filing_process(self, entries, lines, extra=""):
        self.queue_entry(self.transcript(filed_text(entries), "filing-%d" % (self.entry + 1)),
                         git_sh=filing_sh(lines, extra))

    def entries_taken(self):
        return sum(1 for name in os.listdir(self.queue) if name.isdigit()
                   and os.path.exists(os.path.join(self.queue, name, ".taken")))

    def run_pass(self, request=None, launch_kwargs=None, **kwargs):
        out = io.StringIO()
        outcome = testpass.run(self.manifest, self.config, request or testpass.Request(),
                               self.base_env(), out=out, home=self.home,
                               launch_kwargs=dict({"sigkill_grace_seconds": 1},
                                                  **(launch_kwargs or {})), **kwargs)
        return outcome, out.getvalue()

    def call(self, *argv):
        out = io.StringIO()
        code = cli.main(list(argv), env=self.base_env(), out=out)
        return code, out.getvalue()

    def tracker(self):
        with open(os.path.join(self.repo, "tracker.md")) as handle:
            return handle.read()

    def store(self):
        return state.StateStore(self.manifest_path, self.repo, home=self.home)

    def worktrees(self):
        return subprocess.run(["git", "-C", self.repo, "worktree", "list", "--porcelain"],
                              capture_output=True, text=True, check=True).stdout

    def assert_checkout_clean(self):
        self.assertEqual(gitread.current_branch(self.repo), "main")
        self.assertEqual(gitread.status_porcelain(self.repo), "")
        self.assertEqual(self.worktrees().count("worktree "), 1)
        self.assertFalse(os.path.exists(self.store().path("worktrees", "pass-1")))

    def paths(self):
        return testpass.paths_for(self.manifest_path)


class TourAndFiling(PassCase):
    def test_a_tour_with_two_high_findings_files_both_and_the_record_names_both_ids(self):
        self.test_process([finding(1), finding(2, area="Invoices")],
                          text=report_text([finding(1), finding(2, area="Invoices")],
                                           approval_steps=["Send the invoice"]))
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"},
                             {"finding": 2, "action": "filed", "id": "T-3"}],
                            ["- [ ] T-2 Finding 1 [loop]", "- [ ] T-3 Finding 2 [loop]"])
        tested = gitread.rev_parse(self.repo, "main")
        outcome, text = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_OK, text)
        record = outcome.record
        self.assertEqual(record["status"], testloop.RAN)
        self.assertEqual(record["kind"], testloop.TOUR)
        # The tested commit is main before the filing commit moved it.
        self.assertEqual(record["commit"], tested)
        self.assertEqual(record["checkout"]["head_before"], tested)
        self.assertEqual([entry["id"] for entry in record["filed"]], ["T-2", "T-3"])
        self.assertEqual([entry["area"] for entry in record["filed"]], ["Search", "Invoices"])
        self.assertEqual([entry["cause_file"] for entry in record["filed"]],
                         ["app/search.py", "app/invoices.py"])
        self.assertEqual([item["outcome"] for item in record["findings"]], ["file", "file"])
        self.assertEqual([item["filed_id"] for item in record["findings"]], ["T-2", "T-3"])
        self.assertEqual(record["approval_steps"], ["Send the invoice"])
        self.assertEqual(record["notes"], [])
        self.assertEqual(record["over_cap"], [])
        self.assertTrue(record["worktree_removed"])
        self.assertTrue(os.path.exists(record["transcripts"]["test"]))
        self.assertTrue(os.path.exists(record["transcripts"]["filing"]))
        self.assertEqual(record["checkout"]["head_before"], record["checkout"]["head_after"])
        # The path is printed last, and the record on disk is the record returned.
        self.assertEqual(text.rstrip("\n").splitlines()[-1], outcome.path)
        with open(outcome.path) as handle:
            self.assertEqual(json.load(handle), record)
        self.assertEqual(os.path.basename(outcome.path), "pass-1.json")
        # The tracker carries both lines at main, where the adapter reads them.
        self.assertIn("- [ ] T-2", gitread.show(self.repo, "main", "tracker.md"))
        self.assertIn("- [ ] T-3", gitread.show(self.repo, "main", "tracker.md"))
        self.assert_checkout_clean()
        # Nothing beside the manifest but the pass record.
        self.assertFalse(os.path.exists(self.paths().lows))
        self.assertFalse(os.path.exists(self.paths().findings))
        with open(self.prepare_log) as handle:
            self.assertEqual(handle.read().split(), [record["commit"], "http://127.0.0.1:8765"])

    def test_a_second_pass_takes_the_next_number(self):
        for _ in range(2):
            self.test_process([finding(1, severity="low")])
        first, _ = self.run_pass()
        second, _ = self.run_pass()
        self.assertEqual(os.path.basename(first.path), "pass-1.json")
        self.assertEqual(os.path.basename(second.path), "pass-2.json")
        self.assertEqual(second.record["pass"], 2)

    def test_fourteen_findings_file_ten_and_the_record_lists_four_over_the_cap(self):
        """Covers AE3."""
        findings = [finding(n, severity="high" if n % 2 else "medium") for n in range(1, 15)]
        self.test_process(findings)
        ids = ["T-%d" % (n + 1) for n in range(1, 11)]
        self.filing_process([{"finding": n, "action": "filed", "id": ids[n - 1]}
                             for n in range(1, 11)],
                            ["- [ ] %s Finding %d [loop]" % (ids[n - 1], n) for n in range(1, 11)])
        outcome, text = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_OK, text)
        record = outcome.record
        self.assertEqual(len(record["filed"]), 10)
        self.assertEqual(len(record["over_cap"]), 4)
        self.assertEqual(record["over_budget"], [])
        outcomes = [item["outcome"] for item in record["findings"]]
        self.assertEqual(outcomes.count(testloop.FILE), 10)
        self.assertEqual(outcomes.count(testloop.OVER_CAP), 4)
        # Highest severity first: every high is filed, and the four over the cap are mediums.
        for item in record["findings"]:
            if item["severity"] == "high":
                self.assertEqual(item["outcome"], testloop.FILE, item)
        with open(record["briefs"]["filing"]) as handle:
            brief = handle.read()
        self.assertEqual(brief.count("### Finding "), 10)

    def test_a_budget_tighter_than_the_cap_names_the_rest_as_over_the_budget(self):
        """Covers AE11's pass half: with 2 cards left, 5 high findings file 2."""
        self.test_process([finding(n) for n in range(1, 6)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"},
                             {"finding": 2, "action": "filed", "id": "T-3"}],
                            ["- [ ] T-2 Finding 1 [loop]", "- [ ] T-3 Finding 2 [loop]"])
        outcome, _ = self.run_pass(testpass.Request(budget=2))
        self.assertEqual(len(outcome.record["filed"]), 2)
        self.assertEqual(len(outcome.record["over_budget"]), 3)
        self.assertEqual(outcome.record["budget"], 2)

    def test_a_report_with_only_lows_appends_them_to_the_lows_file_and_files_none(self):
        self.test_process([finding(1, severity="low"), finding(2, severity="low", area="Settings")])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_OK)
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(len(outcome.record["lows"]), 2)
        self.assertEqual(self.entries_taken(), 1)
        with open(self.paths().lows) as handle:
            lows = handle.read()
        self.assertIn("## Pass 1, tour of", lows)
        self.assertIn("Finding 1: the search page drops its last row (Search, app/search.py line 41)",
                      lows)
        self.assertIn("Finding 2", lows)
        self.assertEqual(self.tracker(), TRACKER_MD)

    def test_lows_beside_serious_findings_go_to_the_lows_file_and_the_serious_ones_are_filed(self):
        self.test_process([finding(1, severity="low"), finding(2)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 2 [loop]"])
        outcome, _ = self.run_pass()
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])
        self.assertEqual(outcome.record["lows"], [finding(1)["title"]])
        with open(outcome.record["briefs"]["filing"]) as handle:
            brief = handle.read()
        self.assertIn("### Finding 1\n\nTitle: Finding 2", brief)
        self.assertNotIn(finding(1)["title"], brief)

    def test_a_commented_entry_is_recorded_as_a_comment_and_not_a_new_card(self):
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "commented", "id": "T-1"}],
                            ["  - 2026-09-28 seen again on Search"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(outcome.record["commented"], [{"id": "T-1", "finding": 1}])
        self.assertEqual(outcome.record["findings"][0]["action"], "commented")
        self.assertEqual(outcome.record["findings"][0]["filed_id"], "T-1")

    def test_a_filed_claim_naming_a_card_that_existed_before_the_pass_is_a_note(self):
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-1"}],
                            ["  - 2026-09-28 a comment instead"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(len(outcome.record["notes"]), 1)
        self.assertIn("existed before this pass", outcome.record["notes"][0])

    def test_a_confirmed_card_the_ready_source_does_not_return_is_noted(self):
        """A line filed already checked is readable, so it confirms, and the markdown ready
        read returns open lines only, so it is exactly a card the queue will never offer."""
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [x] T-2 Finding 1 [loop] (abc1234)"])
        outcome, _ = self.run_pass()
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])
        self.assertEqual(len(outcome.record["notes"]), 1)
        self.assertIn("card T-2 was filed and confirmed, but the ready source does not return it",
                      outcome.record["notes"][0])

    def test_a_markdown_filing_commit_that_also_touched_a_source_file_is_reset_and_noted(self):
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"],
                            extra="mkdir -p src\necho 'fixed = True' > src/search.py\n")
        head = gitread.rev_parse(self.repo, "HEAD")
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_OK)
        self.assertEqual(gitread.rev_parse(self.repo, "HEAD"), head)
        self.assertEqual(outcome.record["filed"], [])
        notes = "\n".join(outcome.record["notes"])
        self.assertIn("src/search.py", notes)
        self.assertIn("reset to %s" % head[:12], notes)
        # The card it claimed is not on main after the reset, so the claim is a note too.
        self.assertIn("card T-2 claimed filed could not be read", notes)
        self.assert_checkout_clean()

    def test_a_markdown_filing_that_leaves_the_tracker_uncommitted_is_reset_and_noted_as_such(self):
        """Code review: the in scope but uncommitted shape is a different sentence from a path
        outside the bound, as `_run_closeout` tells the two apart."""
        self.test_process([finding(1)])
        self.queue_entry(self.transcript(filed_text([{"finding": 1, "action": "filed",
                                                      "id": "T-2"}]), "filing-2"),
                         git_sh="echo '- [ ] T-2 Finding 1 [loop]' >> tracker.md\n")
        head = gitread.rev_parse(self.repo, "HEAD")
        outcome, _ = self.run_pass()
        self.assertEqual(gitread.rev_parse(self.repo, "HEAD"), head)
        self.assertEqual(outcome.record["filed"], [])
        notes = "\n".join(outcome.record["notes"])
        self.assertIn("left tracker.md changed and uncommitted", notes)
        self.assertNotIn("outside", notes)
        self.assert_checkout_clean()

    def test_a_filing_process_with_no_block_files_nothing_and_notes_the_error(self):
        self.test_process([finding(1)])
        self.queue_entry(self.transcript("I filed it and forgot the block.", "filing-2"),
                         git_sh=filing_sh(["- [ ] T-2 Finding 1 [loop]"]))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertEqual(outcome.record["filed"], [])
        self.assertIn(contracts.FILED_FENCE_TAG, "\n".join(outcome.record["notes"]))


class ReportOnly(PassCase):
    def test_report_only_writes_both_findings_to_the_findings_file_and_launches_no_filing(self):
        """Covers AE6."""
        self.test_process([finding(1), finding(2, area="Invoices", severity="medium")])
        outcome, _ = self.run_pass(testpass.Request(report_only=True))
        self.assertEqual(outcome.exit_code, testpass.EXIT_OK)
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertTrue(outcome.record["report_only"])
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(outcome.record["transcripts"]["filing"], None)
        self.assertEqual(self.entries_taken(), 1)
        with open(self.paths().findings) as handle:
            text = handle.read()
        self.assertIn("## Pass 1, tour of", text)
        self.assertIn("[high] file: Finding 1: the search page drops its last row", text)
        self.assertIn("[medium] file: Finding 2: the invoices page drops its last row", text)
        self.assertIn("app/invoices.py line 42", text)
        self.assertEqual(self.tracker(), TRACKER_MD)
        self.assertEqual(gitread.show(self.repo, "main", "tracker.md"), TRACKER_MD)

    def test_the_sidecars_report_only_setting_is_the_same_switch_as_the_flag(self):
        """Code review, R23: a loop the sidecar set to report only files nothing on a hand run
        that forgot the flag."""
        self.write_sidecar(extra="report_only = true")
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        outcome, _ = self.run_pass(testpass.Request(report_only=False))
        self.assertTrue(outcome.record["report_only"])
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(self.entries_taken(), 1)
        self.assertEqual(self.tracker(), TRACKER_MD)
        self.assertTrue(os.path.exists(self.paths().findings))

    def test_the_verb_runs_a_report_only_tour_and_prints_the_record_path_last(self):
        self.test_process([finding(1)])
        code, text = self.call("test", self.manifest_path, "--tour", "--report-only")
        self.assertEqual(code, cli.EXIT_OK, text)
        path = text.rstrip("\n").splitlines()[-1]
        self.assertEqual(path, os.path.join(self.paths().directory, "pass-1.json"))
        with open(path) as handle:
            record = json.load(handle)
        self.assertEqual(record["status"], testloop.RAN)
        self.assertTrue(record["report_only"])
        self.assertTrue(os.path.exists(self.paths().findings))


class Prepare(PassCase):
    def test_a_prepare_that_exits_one_writes_not_run_launches_nothing_and_files_nothing(self):
        """Covers AE5."""
        self.write_sidecar(prepare=["bash", "-c", "echo serving the old commit; exit 1"])
        self.test_process([finding(1)])
        outcome, text = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIn("prepare exited 1", outcome.record["reason"])
        self.assertIn("serving the old commit", outcome.record["reason"])
        self.assertEqual(self.entries_taken(), 0)
        self.assertEqual(outcome.record["transcripts"], {"test": None, "filing": None})
        self.assertEqual(self.tracker(), TRACKER_MD)
        self.assertIn("pass 1 not_run", text)
        self.assertEqual(text.rstrip("\n").splitlines()[-1], outcome.path)
        self.assert_checkout_clean()

    def test_a_prepare_that_outlives_its_timeout_has_its_group_ended_and_writes_not_run(self):
        pid_file = os.path.join(self.tmp.name, "grandchild.pid")
        self.write_sidecar(prepare=["bash", "-c", "sleep 60 & echo $! > %s; wait" % pid_file],
                           prepare_timeout=1)
        self.test_process([finding(1)])
        outcome, _ = self.run_pass(prepare_kwargs={"grace_seconds": 1})
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIn("prepare timed out after 1 seconds", outcome.record["reason"])
        self.assertEqual(self.entries_taken(), 0)
        with open(pid_file) as handle:
            grandchild = int(handle.read().strip())
        deadline = time.monotonic() + 5
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                alive = False
            else:
                time.sleep(0.1)
        self.assertFalse(alive, "the prepare command's grandchild outlived the timeout")

    def test_a_missing_prepare_program_is_not_run_with_the_error(self):
        self.write_sidecar(prepare=["/nonexistent/serve-at"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIn("prepare could not run", outcome.record["reason"])

    def test_a_lost_lease_ends_the_prepare_group_at_once_rather_than_at_its_bound(self):
        """Code review: a prepare command moving a server in a checkout another runner now
        owns is ended when the lease is lost, as a launched process is, not after its
        timeout."""
        pid_file = os.path.join(self.tmp.name, "grandchild.pid")
        log = os.path.join(self.tmp.name, "prepare.log")
        started = time.monotonic()
        ok, sentence, seconds = testpass.prepare(
            ["bash", "-c", "echo moving; sleep 60 & echo $! > %s; wait" % pid_file],
            self.repo, "abc1234", "http://127.0.0.1:8765", 600, log, self.base_env(),
            heartbeat=lambda: False, heartbeat_interval=0.2, grace_seconds=1,
            tick_seconds=0.1)
        self.assertFalse(ok)
        self.assertIn("the lease was lost while prepare ran", sentence)
        self.assertIn("last output: moving", sentence)
        self.assertLess(time.monotonic() - started, 30)
        with open(pid_file) as handle:
            grandchild = int(handle.read().strip())
        deadline = time.monotonic() + 5
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                alive = False
            else:
                time.sleep(0.1)
        self.assertFalse(alive, "the prepare command's grandchild outlived the lost lease")

    def test_the_prepare_log_holds_the_whole_output(self):
        self.write_sidecar(prepare=["bash", "-c", "echo one; echo two; exit 3"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["reason"], "prepare exited 3; last output: two")
        with open(self.store().path("logs", "pass-1.prepare.log")) as handle:
            self.assertEqual(handle.read(), "one\ntwo\n")


class TestProcessOutcomes(PassCase):
    def test_a_test_process_with_no_report_writes_failed_with_the_parse_error(self):
        self.queue_entry(os.path.join(TRANSCRIPTS, "test_report_none.jsonl"))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn(contracts.TEST_REPORT_FENCE_TAG, outcome.record["reason"])
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(self.entries_taken(), 1)

    def test_a_malformed_report_is_failed_naming_json(self):
        self.queue_entry(os.path.join(TRANSCRIPTS, "test_report_malformed.jsonl"))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("not valid JSON", outcome.record["reason"])

    def test_a_not_run_report_is_recorded_not_run_with_its_reason(self):
        self.queue_entry(os.path.join(TRANSCRIPTS, "test_report_not_run.jsonl"))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIn("session file is missing", outcome.record["reason"])
        self.assertEqual(self.entries_taken(), 1)

    def test_the_fixture_report_files_through_the_whole_pass(self):
        """The fixture `_make.py` writes is the shape a real process ends with."""
        self.queue_entry(os.path.join(TRANSCRIPTS, "test_report.jsonl"))
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"},
                             {"finding": 2, "action": "filed", "id": "T-3"}],
                            ["- [ ] T-2 Finding 1 [loop]", "- [ ] T-3 Finding 2 [loop]"])
        outcome, _ = self.run_pass()
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2", "T-3"])
        self.assertEqual(outcome.record["approval_steps"], ["Send the invoice"])

    def test_a_worktree_that_cannot_be_added_fails_the_pass_and_the_record_on_disk_says_so(self):
        """Code review: the record is written after the worktree `finally`, so the file the
        Feeder reads carries the removal outcome and its note."""
        dest = self.store().path("worktrees", "pass-1")
        os.makedirs(dest)
        with open(os.path.join(dest, "leftover.txt"), "w") as handle:
            handle.write("from a killed pass\n")
        self.test_process([finding(1)])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("could not add worktree", outcome.record["reason"])
        self.assertEqual(self.entries_taken(), 0)
        with open(outcome.path) as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk, outcome.record)
        self.assertFalse(on_disk["worktree_removed"])
        self.assertIn("could not be removed", "\n".join(on_disk["notes"]))

    def test_the_detached_worktree_is_gone_after_a_pass_including_one_that_failed(self):
        self.queue_entry(os.path.join(TRANSCRIPTS, "test_report_none.jsonl"))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertTrue(outcome.record["worktree_removed"])
        self.assert_checkout_clean()
        # The Test process ran in the worktree, not the checkout: its transcript slug is the
        # worktree's path.
        self.assertIn(contracts.slug_for(os.path.realpath(self.store().path("worktrees", "pass-1"))),
                      outcome.record["transcripts"]["test"])

    def test_a_test_process_that_leaves_the_checkout_dirty_fails_the_pass_and_files_nothing(self):
        self.test_process([finding(1)],
                          git_sh='echo "half written" >> "$RELAY_TEST_CHECKOUT/README.md"\n')
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("the checkout changed while the test process ran", outcome.record["reason"])
        self.assertIn("README.md", outcome.record["reason"])
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(self.entries_taken(), 1)
        self.assertEqual(outcome.record["checkout"]["status_before"], "")
        self.assertIn("README.md", outcome.record["checkout"]["status_after"])

    def test_a_test_process_that_moves_the_checkouts_head_fails_the_pass(self):
        self.test_process([finding(1)],
                          git_sh='cd "$RELAY_TEST_CHECKOUT" && git commit -q --allow-empty '
                                 '-m "moved by the test process"\n')
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("HEAD", outcome.record["reason"])
        self.assertNotEqual(outcome.record["checkout"]["head_before"],
                            outcome.record["checkout"]["head_after"])

    def test_a_timed_out_test_process_is_failed_naming_the_timeout(self):
        self.test_process([finding(1)], sleep=30)
        outcome, _ = self.run_pass(timeout_overrides={"test_seconds": 1})
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("timed out after 1 seconds", outcome.record["reason"])
        self.assertEqual(self.entries_taken(), 1)
        self.assert_checkout_clean()

    def test_a_finding_naming_an_unknown_area_is_invalid_and_not_filed(self):
        self.test_process([finding(1, area="Nowhere"), finding(2)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 2 [loop]"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["invalid"], [finding(1, area="Nowhere")["title"]])
        invalid = [item for item in outcome.record["findings"]
                   if item["outcome"] == testpass.OUTCOME_INVALID]
        self.assertEqual(len(invalid), 1)
        self.assertIn("not a heading of the tour document", invalid[0]["problem"])
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])
        with open(outcome.record["briefs"]["filing"]) as handle:
            brief = handle.read()
        self.assertNotIn("Nowhere", brief)
        self.assertEqual(brief.count("### Finding "), 1)

    def test_a_finding_in_a_stopped_area_is_dropped_and_not_filed(self):
        self.test_process([finding(1), finding(2, area="Invoices")])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 2 [loop]"])
        # The name is compared flattened, as the headings are (code review).
        outcome, _ = self.run_pass(testpass.Request(stopped_areas=("  Search ",)))
        self.assertEqual(outcome.record["stopped_areas"], ["Search"])
        self.assertEqual(outcome.record["dropped"], [finding(1)["title"]])
        self.assertEqual([entry["area"] for entry in outcome.record["filed"]], ["Invoices"])
        with open(outcome.record["briefs"]["test"]) as handle:
            brief = handle.read()
        self.assertIn(testbrief.STOPPED_AREAS_LEAD, brief)
        self.assertIn("- Search", brief)


class PlanArea(PassCase):
    def test_plan_area_files_one_attended_planning_card_for_that_area_outside_the_cap(self):
        """Covers AE7."""
        self.write_sidecar(cap=1)
        self.test_process([finding(1), finding(2, area="Invoices")])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"},
                             {"finding": 2, "action": "filed", "id": "T-3"}],
                            ["- [ ] T-2 Finding 1 [loop]",
                             "- [ ] T-3 Plan the Settings area after repeated patches [loop] [attended]"])
        outcome, _ = self.run_pass(testpass.Request(plan_areas=("Settings",)))
        record = outcome.record
        self.assertEqual(record["planning"], ["Settings"])
        self.assertEqual(len(record["over_cap"]), 1)
        self.assertEqual([entry["id"] for entry in record["filed"]], ["T-2", "T-3"])
        self.assertEqual([entry["attended"] for entry in record["filed"]], [False, True])
        self.assertEqual(record["filed"][1]["area"], "Settings")
        self.assertEqual(record["filed"][1]["cause_file"], "docs/tour.md")
        with open(record["briefs"]["filing"]) as handle:
            brief = handle.read()
        self.assertEqual(brief.count("### Finding "), 2)
        self.assertIn(filing.ATTENDED_LINE, brief)
        self.assertIn("Title: Plan the Settings area after repeated patches", brief)
        self.assertIn("Cause: docs/tour.md, line 14, intended", brief)

    def test_a_plan_area_alone_files_the_planning_card_with_no_other_finding(self):
        self.test_process([])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Plan the Search area after repeated patches [loop] [attended]"])
        outcome, _ = self.run_pass(testpass.Request(plan_areas=("Search",)))
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])
        self.assertTrue(outcome.record["filed"][0]["attended"])

    def test_the_planning_finding_has_the_shape_the_filing_brief_validates(self):
        shape = testpass.plan_finding("Search", TOUR_MD, "docs/tour.md")
        self.assertEqual(testloop.validate_finding(shape), [])
        self.assertTrue(shape[filing.ATTENDED_KEY])
        self.assertEqual(shape["cause"], {"file": "docs/tour.md", "line": 5, "verdict": "intended"})


class Rendering(PassCase):
    def test_cards_renders_the_named_cards_and_tour_renders_the_tour_document(self):
        self.test_process([finding(1, card="T-1")])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        outcome, _ = self.run_pass(testpass.Request(kind=testloop.CHECK, cards=("T-1",)))
        self.assertEqual(outcome.record["kind"], testloop.CHECK)
        self.assertEqual(outcome.record["cards"], ["T-1"])
        with open(outcome.record["briefs"]["test"]) as handle:
            check = handle.read()
        self.assertIn(testbrief.CHECK_INSTRUCTION, check)
        self.assertIn("### Card T-1\n\nAdd the brief renderer", check)
        self.assertIn("## Search", check)
        self.assertTrue(outcome.record["findings"][0]["card_sent"])
        self.assertTrue(outcome.record["filed"][0]["card_sent"])

        self.test_process([finding(1)])
        outcome, _ = self.run_pass(testpass.Request(report_only=True))
        with open(outcome.record["briefs"]["test"]) as handle:
            tour = handle.read()
        self.assertIn(testbrief.TOUR_INSTRUCTION, tour)
        self.assertIn("## Search", tour)
        self.assertNotIn("Landed cards to check", tour)
        self.assertNotIn("card_sent", outcome.record["findings"][0])

    def test_a_check_finding_naming_a_card_the_pass_did_not_send_is_marked(self):
        self.test_process([finding(1, card="T-9"), finding(2, card=None)])
        outcome, _ = self.run_pass(testpass.Request(kind=testloop.CHECK, cards=("T-1",),
                                                    report_only=True))
        self.assertEqual([item["card_sent"] for item in outcome.record["findings"]],
                         [False, False])

    def test_the_test_process_runs_with_the_sidecars_tools_and_the_gh_denial(self):
        seen = {}

        def popen(args, **kwargs):
            seen.setdefault("args", list(args))
            return subprocess.Popen(args, **kwargs)

        self.write_sidecar(extra='allowed_tools = ["Bash", "Read"]\nmodel = "opus"\n'
                                 'effort = "low"')
        self.test_process([finding(1, severity="low")])
        outcome, _ = self.run_pass(launch_kwargs={"popen": popen, "sigkill_grace_seconds": 1})
        self.assertEqual(outcome.record["status"], testloop.RAN)
        args = seen["args"]
        self.assertEqual(args[args.index("--allowedTools") + 1], "Bash,Read")
        disallowed = args[args.index("--disallowedTools") + 1].split(",")
        self.assertIn("Bash(gh *)", disallowed)
        for pattern in contracts.CLOSEOUT_DISALLOWED_EXTRA + ("Bash(rm -rf*)",):
            self.assertIn(pattern, disallowed)
        self.assertEqual(args[args.index("--model") + 1], "opus")
        self.assertEqual(args[args.index("--effort") + 1], "low")

    def test_the_command_line_model_overrides_the_sidecars(self):
        seen = {}

        def popen(args, **kwargs):
            seen.setdefault("args", list(args))
            return subprocess.Popen(args, **kwargs)

        self.test_process([finding(1, severity="low")])
        self.run_pass(testpass.Request(model="fable"),
                      launch_kwargs={"popen": popen, "sigkill_grace_seconds": 1})
        args = seen["args"]
        self.assertEqual(args[args.index("--model") + 1], "fable")

    def test_the_filing_process_runs_on_the_closeout_model_in_the_checkout(self):
        seen = []

        def popen(args, **kwargs):
            seen.append((list(args), kwargs.get("cwd")))
            return subprocess.Popen(args, **kwargs)

        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        self.run_pass(launch_kwargs={"popen": popen, "sigkill_grace_seconds": 1})
        self.assertEqual(len(seen), 2)
        test_args, test_cwd = seen[0]
        filing_args, filing_cwd = seen[1]
        self.assertEqual(os.path.realpath(test_cwd),
                         os.path.realpath(self.store().path("worktrees", "pass-1")))
        self.assertEqual(os.path.realpath(filing_cwd), os.path.realpath(self.repo))
        self.assertEqual(filing_args[filing_args.index("--model") + 1], self.manifest.closeout.model)


class Refusals(PassCase):
    def test_a_sidecar_without_test_loop_is_refused_with_a_sentence_naming_the_table(self):
        with open(self.sidecar_path, "w") as handle:
            handle.write('[models]\ndefault = "sonnet"\n')
        self.config = feeder.load_config(self.sidecar_path)
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("[test_loop]", outcome.message)
        self.assertIsNone(outcome.path)
        code, text = self.call("test", self.manifest_path, "--tour")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("[test_loop]", text)
        self.assertFalse(os.path.exists(self.paths().directory))

    def test_no_sidecar_at_all_is_refused_by_the_verb(self):
        os.unlink(self.sidecar_path)
        code, text = self.call("test", self.manifest_path, "--tour")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("no feeder sidecar", text)

    def test_a_markdown_tracker_under_a_manifest_that_pushes_is_refused_before_anything_launches(self):
        with open(FIXTURE) as handle:
            self.write_manifest(handle.read().replace("__REPO__", self.repo))
        self.test_process([finding(1)])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("does not push", outcome.message)
        self.assertEqual(self.entries_taken(), 0)
        self.assertIsNone(self.store().lease())
        code, text = self.call("test", self.manifest_path, "--tour")
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("push = false", text)

    def test_a_default_backend_other_than_claude_is_refused(self):
        with open(self.manifest_path) as handle:
            text = handle.read()
        self.write_manifest(text.replace("[project]", '[defaults]\nbackend = "grok"\n\n[project]', 1))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("grok", outcome.message)
        self.assertIn("claude only", outcome.message)

    def test_a_github_loop_whose_labels_lack_the_ready_labels_value_is_refused(self):
        with open(self.manifest_path) as handle:
            text = handle.read()
        text = text.replace('adapter = "markdown"\nfile = "tracker.md"',
                            'adapter = "github"\nowner = "example-org"\nproject_number = 4\n'
                            'status_field = "Done"')
        self.write_manifest(text)
        # `[ready]` is its own table, so it comes after `[test_loop]`'s last key.
        self.write_sidecar(extra='[ready]\nlabels = ["ready", "loop"]')
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("test_loop.labels lacks the [ready] labels value ready", outcome.message)
        self.write_sidecar(extra='[ready]\nlabels = ["loop"]')
        self.assertIsNone(testpass.refusal(self.manifest, self.config, testpass.Request()))
        self.write_sidecar()
        self.assertIn("no ready labels are configured",
                      testpass.refusal(self.manifest, self.config, testpass.Request()))

    def test_an_unknown_model_a_bad_budget_and_an_empty_check_are_refused(self):
        self.assertIn("--model", testpass.refusal(self.manifest, self.config,
                                                  testpass.Request(model="haiku")))
        self.assertIn("--budget", testpass.refusal(self.manifest, self.config,
                                                   testpass.Request(budget=-1)))
        self.assertIn("--cards", testpass.refusal(self.manifest, self.config,
                                                  testpass.Request(kind=testloop.CHECK)))

    def test_a_stopped_or_plan_area_that_is_not_a_heading_is_refused(self):
        for request in (testpass.Request(stopped_areas=("Nowhere",)),
                        testpass.Request(plan_areas=("Nowhere",))):
            outcome, _ = self.run_pass(request)
            self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
            self.assertIn("'Nowhere' is not a heading of the tour document", outcome.message)
        self.assertEqual(self.entries_taken(), 0)

    def test_a_card_the_adapter_cannot_read_is_refused(self):
        outcome, _ = self.run_pass(testpass.Request(kind=testloop.CHECK, cards=("T-9",)))
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("card T-9 could not be read", outcome.message)

    def test_a_missing_tour_document_is_refused(self):
        _repo.git(self.repo, "rm", "-q", "docs/tour.md")
        _repo.git(self.repo, "commit", "-q", "-m", "drop the tour")
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("docs/tour.md could not be read", outcome.message)

    def test_a_dirty_checkout_or_one_off_the_default_branch_is_refused_before_the_lease(self):
        """Code review: the filing step bounds its commit with a reset, which would take an
        operator's uncommitted work with it, so the pass asks for the Runner's own preflight."""
        with open(os.path.join(self.repo, "docs", "tour.md"), "a") as handle:
            handle.write("\n## Reports\n")
        self.test_process([finding(1)])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("uncommitted changes", outcome.message)
        self.assertIn("clean checkout", outcome.message)
        self.assertEqual(self.entries_taken(), 0)
        self.assertIsNone(self.store().lease())
        _repo.git(self.repo, "checkout", "-q", "--", "docs/tour.md")
        _repo.git(self.repo, "checkout", "-q", "-b", "feature")
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_CONFIG)
        self.assertIn("on feature, not main", outcome.message)

    def test_a_held_lease_exits_three_and_launches_nothing(self):
        other = state.StateStore(self.manifest_path, self.repo, home=self.home, pid=999999)
        other.acquire()
        self.test_process([finding(1)])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_LEASE)
        self.assertIn("another runner holds the lease", outcome.message)
        self.assertEqual(self.entries_taken(), 0)
        self.assertIsNone(outcome.path)
        # The other holder's lease is untouched.
        self.assertEqual(self.store().lease().get("holder_pid"), 999999)
        code, text = self.call("test", self.manifest_path, "--tour")
        self.assertEqual(code, cli.EXIT_LEASE)

    def test_the_lease_is_released_after_a_pass_of_every_outcome(self):
        self.write_sidecar(prepare=["false"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIsNone(self.store().lease())


if __name__ == "__main__":
    unittest.main()

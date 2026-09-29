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
from datetime import datetime
from unittest import mock

import _paths
import _repo
from relay import (adapters, cli, contracts, feeder, filing, gitread, launch, manifest as mf,
                   state, testbrief, testloop, testpass)

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


def report_text(findings, status="ran", reason="", approval_steps=(), untoured=None):
    payload = {"status": status, "reason": reason, "findings": list(findings),
               "approval_steps": list(approval_steps)}
    if untoured is not None:
        payload["untoured"] = list(untoured)
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

    def queue_entry(self, fixture, git_sh=None, sleep=0, stream=None):
        """One stub entry. `fixture` is the transcript the stub writes under HOME; None writes
        none, which is how a test stands in for a CLI whose transcript landed somewhere the
        runner does not look (issue #113). `stream` is echoed to stdout, so it lands in the
        run's own stdout log."""
        self.entry += 1
        entry_dir = os.path.join(self.queue, str(self.entry))
        os.makedirs(entry_dir)
        entry = {"exit": 0, "sleep": sleep}
        if fixture:
            entry["fixture"] = fixture
        if stream:
            entry["stream"] = stream
        with open(os.path.join(entry_dir, "entry.json"), "w") as handle:
            json.dump(entry, handle)
        if git_sh:
            with open(os.path.join(entry_dir, "git.sh"), "w") as handle:
                handle.write(git_sh)

    def test_process(self, findings=None, text=None, git_sh=None, sleep=0, **report):
        text = text if text is not None else report_text(findings or [], **report)
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
        # A reset filing is a filing that did not complete, so the pass is failed (issue #115).
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(gitread.rev_parse(self.repo, "HEAD"), head)
        self.assertEqual(outcome.record["filed"], [])
        reason = outcome.record["reason"]
        self.assertIn("src/search.py", reason)
        self.assertIn("reset to %s" % head[:12], reason)
        # The card it claimed is not on main after the reset, so the claim is a note too.
        self.assertIn("card T-2 claimed filed could not be read", "\n".join(outcome.record["notes"]))
        self.assert_checkout_clean()

    def test_a_markdown_filing_that_leaves_the_tracker_uncommitted_is_reset_and_failed_as_such(self):
        """Code review: the in scope but uncommitted shape is a different sentence from a path
        outside the bound, as `_run_closeout` tells the two apart."""
        self.test_process([finding(1)])
        self.queue_entry(self.transcript(filed_text([{"finding": 1, "action": "filed",
                                                      "id": "T-2"}]), "filing-2"),
                         git_sh="echo '- [ ] T-2 Finding 1 [loop]' >> tracker.md\n")
        head = gitread.rev_parse(self.repo, "HEAD")
        outcome, _ = self.run_pass()
        self.assertEqual(gitread.rev_parse(self.repo, "HEAD"), head)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["filed"], [])
        self.assertIn("left tracker.md changed and uncommitted", outcome.record["reason"])
        self.assertNotIn("outside", outcome.record["reason"])
        self.assert_checkout_clean()

    def test_a_filing_process_with_no_block_fails_the_pass_naming_the_block(self):
        """Issue #115: a pass that filed nothing because its block could not be read is not a
        pass whose findings produced no card, so it is failed rather than ran."""
        self.test_process([finding(1)])
        self.queue_entry(self.transcript("I filed it and forgot the block.", "filing-2"),
                         git_sh=filing_sh(["- [ ] T-2 Finding 1 [loop]"]))
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["filed"], [])
        self.assertIn(contracts.FILED_FENCE_TAG, outcome.record["reason"])
        self.assertIn("could not be read", outcome.record["reason"])
        with open(outcome.path) as handle:
            self.assertEqual(json.load(handle)["status"], testloop.FAILED)

    def test_a_filing_process_that_times_out_fails_the_pass_naming_the_timeout(self):
        self.test_process([finding(1)])
        self.queue_entry(self.transcript(filed_text([{"finding": 1, "action": "filed",
                                                      "id": "T-2"}]), "filing-2"),
                         git_sh=filing_sh(["- [ ] T-2 Finding 1 [loop]"]), sleep=30)
        outcome, _ = self.run_pass(timeout_overrides={"filing_seconds": 1})
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"], "the filing process timed out after 1 seconds")
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(self.entries_taken(), 2)
        self.assert_checkout_clean()

    def test_a_filing_brief_the_scan_refuses_fails_the_pass_with_one_clause_and_no_launch(self):
        """Issue #120: `filing.run` refuses a rendered brief the launch scan hits and launches
        nothing. The pass reason is the launch clause plus the refusal once, not the block's
        error doubled onto it, and the queue's filing entry is never taken."""
        self.test_process([finding(1)])
        self.queue_entry(self.transcript(filed_text([]), "filing-2"))
        hits = [{"source": "brief", "path": ".claude/skills/design/SKILL.md"}]
        with mock.patch.object(filing.brief, "scan", return_value=hits):
            outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"],
                         "the filing process could not be launched: " + filing.scan_refusal(hits))
        self.assertEqual(outcome.record["reason"].count("filing brief names"), 1)
        self.assertNotIn("no filing process was launched", outcome.record["reason"])
        self.assertNotIn("block could not be read", "\n".join(outcome.record["notes"]))
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(self.entries_taken(), 1)
        self.assert_checkout_clean()

    def test_a_filing_process_that_could_not_launch_fails_the_pass(self):
        """The cause the stub cannot stage on its own, answered by `filing.run` itself: it is
        the pass's reason, and the block's own error is not."""
        self.test_process([finding(1)])
        launched = launch.LaunchResult(session_id="filing",
                                       launch_error="could not start claude: not found")
        answer = filing.FilingResult(filing.Filed(error="no assistant record"),
                                     launch_result=launched)
        with mock.patch.object(filing, "run", return_value=answer):
            outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"], "the filing process could not be launched: "
                                                   "could not start claude: not found")
        self.assertEqual(outcome.record["filed"], [])
        # Issue #120: a process that never launched left no block to read, so the block's own
        # error is not a second note beside the launch error.
        self.assertNotIn("block could not be read", "\n".join(outcome.record["notes"]))
        self.assert_checkout_clean()

    def test_a_lost_lease_during_filing_fails_the_pass_with_no_scope_check_and_no_reset(self):
        """Issue #117: with the Lease gone another runner may have merged into the checkout, so
        a scope check would name its paths and the reset would remove its merge. The pass ends
        `failed` on the lost Lease and leaves the checkout exactly as it found it, the other
        runner's commit included. The card the process filed before the Lease went is still
        read back and recorded (code review), so the next tour does not file it again."""
        self.test_process([finding(1)])
        launched = launch.LaunchResult(session_id="filing", lease_lost=True)
        answer = filing.FilingResult(
            filing.Filed(entries=({"finding": 1, "action": "filed", "id": "T-2"},)),
            launch_result=launched)

        def commit(message):
            subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
            subprocess.run(["git", "-C", self.repo, "commit", "-q", "-m", message], check=True)

        def filed_then_another_runner_merged(*args, **kwargs):
            with open(os.path.join(self.repo, "tracker.md"), "a") as handle:
                handle.write("- [ ] T-2 Finding 1 [loop]\n")
            commit("file findings")
            # Another runner's merge lands a source file once the Lease has gone.
            os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
            with open(os.path.join(self.repo, "src", "merged.py"), "w") as handle:
                handle.write("merged = True\n")
            commit("another runner")
            return answer

        with mock.patch.object(filing, "run", side_effect=filed_then_another_runner_merged), \
                mock.patch.object(testpass.gitwrite, "closeout_scope_check") as scope_check:
            outcome, _ = self.run_pass()
        scope_check.assert_not_called()
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"], "the lease was lost while the filing process ran")
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])
        self.assertIn("no scope check and no reset", "\n".join(outcome.record["notes"]))
        self.assertEqual(gitread.show(self.repo, "HEAD", "src/merged.py"), "merged = True\n")
        self.assertEqual(gitread.status_porcelain(self.repo), "")

    def test_a_second_filing_cause_is_a_note_beside_the_first_as_the_reason(self):
        """Code review: a timeout headlines, and the reset the scope check made of what the
        process left in the checkout is still on the record."""
        self.test_process([finding(1)])
        launched = launch.LaunchResult(session_id="filing", timed_out=True)
        answer = filing.FilingResult(filing.Filed(error="timed out"), launch_result=launched)

        def leave_a_change(*args, **kwargs):
            with open(os.path.join(self.repo, "tracker.md"), "a") as handle:
                handle.write("- [ ] T-2 half written\n")
            return answer

        with mock.patch.object(filing, "run", side_effect=leave_a_change):
            outcome, _ = self.run_pass(timeout_overrides={"filing_seconds": 5})
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"], "the filing process timed out after 5 seconds")
        notes = "\n".join(outcome.record["notes"])
        self.assertIn("left tracker.md changed and uncommitted", notes)
        self.assert_checkout_clean()

    def test_ran_is_kept_only_when_the_block_was_read_and_the_ids_confirmed(self):
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertEqual(outcome.record["reason"], "")
        self.assertIsNotNone(outcome.record["read_from"]["filing"])
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])

    def adapter_whose_read_fails(self, card_id, fail):
        """The markdown adapter over this repo, with `read` answering `fail(card_id)` for the
        one id, the way a `gh` or Jira read that timed out answers after a filing that did file
        (issue #125). Every other id reads as the tracker holds it."""
        adapter = adapters.build(self.manifest, env=self.base_env())
        real_read = adapter.read

        def read(task_id):
            return fail(task_id) if str(task_id) == card_id else real_read(task_id)

        adapter.read = read
        return adapter

    def test_a_filed_claim_the_tracker_read_skips_fails_the_pass_naming_the_count(self):
        """Issue #125: the process filed the card and printed its block, and the read back was
        skipped. The pass is failed rather than ran with no card, so the loop does not stop on
        open findings the card answered."""
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        adapter = self.adapter_whose_read_fails(
            "T-2", lambda task_id: {"id": task_id, "skipped": "gh timed out"})
        outcome, _ = self.run_pass(adapter=adapter)
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"],
                         "1 of the 1 claim the filing block names could not be read back from "
                         "the tracker, 1 of them claimed filed")
        self.assertEqual(outcome.record["filed"], [])
        self.assertIn("card T-2 claimed filed could not be read: gh timed out",
                      "\n".join(outcome.record["notes"]))
        with open(outcome.path) as handle:
            self.assertEqual(json.load(handle)["status"], testloop.FAILED)

    def test_a_commented_claim_whose_read_raises_fails_the_pass(self):
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "commented", "id": "T-1"}],
                            ["  - 2026-09-28 seen again on Search"])

        def raise_error(task_id):
            raise OSError("rate limited")

        outcome, _ = self.run_pass(adapter=self.adapter_whose_read_fails("T-1", raise_error))
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("1 of the 1 claim", outcome.record["reason"])
        self.assertEqual(outcome.record["commented"], [])

    def test_an_unread_filed_claim_beside_a_confirmed_comment_fails_the_pass(self):
        """Code review: the confirmed comment is no new card, so recorded `ran` the tour would
        stop on open findings that the unread card answered."""
        self.test_process([finding(1), finding(2, area="Invoices")])
        self.filing_process([{"finding": 1, "action": "commented", "id": "T-1"},
                             {"finding": 2, "action": "filed", "id": "T-2"}],
                            ["  - 2026-09-28 seen again on Search", "- [ ] T-2 Finding 2 [loop]"])
        adapter = self.adapter_whose_read_fails(
            "T-2", lambda task_id: {"id": task_id, "skipped": "gh timed out"})
        outcome, _ = self.run_pass(adapter=adapter)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"],
                         "1 of the 2 claims the filing block names could not be read back from "
                         "the tracker, 1 of them claimed filed")
        self.assertEqual(outcome.record["commented"], [{"id": "T-1", "finding": 1}])

    def test_an_unread_filed_claim_beside_a_confirmed_card_fails_and_keeps_the_confirmed(self):
        """The confirmed card stays in the record, which the Feeder enters in its filed map on a
        failed pass too, so the next tour does not file it again."""
        self.test_process([finding(1), finding(2, area="Invoices")])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"},
                             {"finding": 2, "action": "filed", "id": "T-3"}],
                            ["- [ ] T-2 Finding 1 [loop]", "- [ ] T-3 Finding 2 [loop]"])
        adapter = self.adapter_whose_read_fails(
            "T-3", lambda task_id: {"id": task_id, "skipped": "gh timed out"})
        outcome, _ = self.run_pass(adapter=adapter)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("1 of the 2 claims", outcome.record["reason"])
        self.assertEqual([entry["id"] for entry in outcome.record["filed"]], ["T-2"])

    def test_an_unread_claim_after_a_lost_lease_leaves_the_lease_as_the_reason(self):
        """The lost Lease headlines, the per claim note says what went unread, and no count
        sentence is added beside a filing failure."""
        self.test_process([finding(1)])
        launched = launch.LaunchResult(session_id="filing", lease_lost=True)
        answer = filing.FilingResult(
            filing.Filed(entries=({"finding": 1, "action": "filed", "id": "T-9"},)),
            launch_result=launched)
        adapter = self.adapter_whose_read_fails(
            "T-9", lambda task_id: {"id": task_id, "skipped": "gh timed out"})
        with mock.patch.object(filing, "run", return_value=answer):
            outcome, _ = self.run_pass(adapter=adapter)
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"], "the lease was lost while the filing process ran")
        notes = "\n".join(outcome.record["notes"])
        self.assertIn("card T-9 claimed filed could not be read: gh timed out", notes)
        self.assertNotIn("the filing block names could not be read back", notes)

    def test_a_claim_read_back_as_existing_before_the_pass_leaves_the_pass_ran(self):
        """A read that answered is not a failed read: the pre existing card is a note and the
        pass stays ran, as it did before issue #125."""
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-1"}],
                            ["  - 2026-09-28 a comment instead"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.RAN)


class ReadFromTheLog(PassCase):
    """Issue #113. A CLI running under `CLAUDE_CONFIG_DIR` writes its transcript under that
    directory's projects folder, where neither the runner's prediction nor its glob looks, while
    the stdout log holds the same final message under stream-json. The stub stands in for that
    with an entry that writes no fixture and echoes the transcript lines as its stream."""

    def log_only(self, text, name, git_sh=None):
        self.queue_entry(None, git_sh=git_sh, stream=self.transcript(text, name))

    def test_a_test_report_in_the_stdout_log_alone_is_read_and_the_record_names_the_log(self):
        self.log_only(report_text([finding(1), finding(2, area="Invoices")]), "test-1")
        outcome, _ = self.run_pass(testpass.Request(report_only=True))
        record = outcome.record
        self.assertEqual(outcome.exit_code, testpass.EXIT_OK, record["reason"])
        self.assertEqual(record["status"], testloop.RAN)
        self.assertFalse(os.path.exists(record["transcripts"]["test"]))
        self.assertEqual(record["read_from"]["test"],
                         self.store().path("logs", "pass-1.test.stdout.log"))
        self.assertEqual([item["title"][:9] for item in record["findings"]],
                         ["Finding 1", "Finding 2"])
        with open(self.paths().findings) as handle:
            self.assertIn("Finding 2: the invoices page", handle.read())

    def test_a_filed_block_in_the_stdout_log_alone_is_read_and_the_cards_are_confirmed(self):
        self.test_process([finding(1), finding(2, area="Invoices")])
        self.log_only(filed_text([{"finding": 1, "action": "filed", "id": "T-2"},
                                  {"finding": 2, "action": "filed", "id": "T-3"}]),
                      "filing-2", git_sh=filing_sh(["- [ ] T-2 Finding 1", "- [ ] T-3 Finding 2"]))
        outcome, _ = self.run_pass()
        record = outcome.record
        self.assertEqual(record["status"], testloop.RAN, record["reason"])
        self.assertEqual([entry["id"] for entry in record["filed"]], ["T-2", "T-3"])
        self.assertFalse(os.path.exists(record["transcripts"]["filing"]))
        self.assertEqual(record["read_from"]["filing"],
                         self.store().path("logs", "pass-1.filing.stdout.log"))
        self.assertEqual(record["read_from"]["test"], record["transcripts"]["test"])
        self.assertTrue(os.path.exists(record["transcripts"]["test"]))
        self.assertEqual([note for note in record["notes"] if "could not be read" in note], [])

    def test_a_process_with_no_assistant_record_anywhere_is_the_one_no_transcript_failure(self):
        self.queue_entry(None)
        outcome, _ = self.run_pass(testpass.Request(report_only=True))
        record = outcome.record
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(record["status"], testloop.FAILED)
        self.assertIn("left no transcript to read", record["reason"])
        self.assertEqual(record["read_from"], {"test": None, "filing": None})
        self.assertFalse(os.path.exists(self.paths().findings))
        self.assert_checkout_clean()

    def test_a_transcript_that_opened_with_no_assistant_record_fails_naming_the_transcript(self):
        """The one boundary the guard removal does not widen: a transcript at the predicted
        path is the process's own file, and the reader takes nothing past it, so the reason
        names it and not the log (code review)."""
        empty = os.path.join(self.tmp.name, "empty.jsonl")
        with open(empty, "w") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        self.queue_entry(empty, stream=self.transcript(report_text([finding(1)]), "stream-1"))
        outcome, _ = self.run_pass(testpass.Request(report_only=True))
        record = outcome.record
        self.assertEqual(record["status"], testloop.FAILED)
        self.assertIn("left no transcript to read", record["reason"])
        self.assertIn("no assistant record", record["reason"])
        self.assertNotIn("stdout log", record["reason"])
        self.assertTrue(os.path.exists(record["transcripts"]["test"]))
        self.assertIsNone(record["read_from"]["test"])

    def test_a_transcript_at_the_predicted_path_is_still_the_file_named(self):
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1"])
        outcome, _ = self.run_pass()
        record = outcome.record
        self.assertEqual(record["status"], testloop.RAN, record["reason"])
        self.assertEqual(record["read_from"], record["transcripts"])
        self.assertTrue(all(os.path.exists(path) for path in record["read_from"].values()))


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
            os.path.join(self.tmp.name, "prepare-cwd"), heartbeat=lambda: False, heartbeat_interval=0.2, grace_seconds=1,
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

    def test_prepare_runs_outside_the_checkout_and_is_told_where_the_checkout_is(self):
        """Issue #117: `prepare` runs from the state directory, never the checkout, and reads
        the checkout's path from its environment."""
        told = os.path.join(self.tmp.name, "prepare.where")
        self.write_sidecar(prepare=["bash", "-c", 'pwd -P > "%s"; echo "$RELAY_TEST_REPO" >> "%s"'
                                    % (told, told)])
        self.test_process([])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.RAN, outcome.record["reason"])
        with open(told) as handle:
            cwd, repo = handle.read().splitlines()
        self.assertEqual(cwd, os.path.realpath(self.store().path(testpass.PREPARE_DIR)))
        self.assertFalse(cwd.startswith(os.path.realpath(self.repo) + os.sep))
        self.assertEqual(repo, self.repo)
        self.assert_checkout_clean()

    def test_a_prepare_that_leaves_a_file_in_the_checkout_is_not_run_naming_it(self):
        """Issue #117: a leftover reached through the checkout's path is named as prepare's
        before the snapshot and before any process launches, rather than blamed on the Filing
        process and reset around."""
        self.write_sidecar(prepare=["bash", "-c", 'mkdir -p "$RELAY_TEST_REPO/run" && '
                                    'echo 4242 > "$RELAY_TEST_REPO/run/server.pid"; '
                                    'echo started > "$RELAY_TEST_REPO/server.log"'])
        self.test_process([finding(1)])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        head = gitread.rev_parse(self.repo, "HEAD")
        outcome, text = self.run_pass()
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIn("prepare left the checkout changed at run/server.pid",
                      outcome.record["reason"])
        self.assertEqual(self.entries_taken(), 0)
        self.assertEqual(outcome.record["transcripts"], {"test": None, "filing": None})
        self.assertEqual(outcome.record["checkout"], {})
        self.assertEqual(gitread.rev_parse(self.repo, "HEAD"), head)
        self.assertEqual(self.tracker(), TRACKER_MD)
        self.assertIn("pass 1 not_run", text)
        # Nothing was reset or removed: the leftovers are still there for the operator.
        self.assertTrue(os.path.exists(os.path.join(self.repo, "server.log")))
        self.assertIsNone(self.store().lease())

    def test_a_prepare_that_moves_the_checkout_off_the_default_branch_is_not_run(self):
        """Code review: a clean tree is not enough. A prepare that detaches or commits in the
        checkout through `RELAY_TEST_REPO` would have the Filing process commit where the
        Runner never merges, so it is named before any process launches."""
        cases = [
            (["bash", "-c", 'git -C "$RELAY_TEST_REPO" checkout -q --detach "$RELAY_TEST_COMMIT"'],
             "prepare moved the checkout to HEAD at "),
            (["bash", "-c", 'git -C "$RELAY_TEST_REPO" commit -q --allow-empty -m moved'],
             "prepare moved the checkout to main at "),
        ]
        for command, expected in cases:
            with self.subTest(expected=expected):
                subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"], check=True)
                self.write_sidecar(prepare=command)
                self.test_process([finding(1)])
                taken = self.entries_taken()
                outcome, _ = self.run_pass()
                self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
                self.assertIn(expected, outcome.record["reason"])
                self.assertIn("must leave the checkout where it found it", outcome.record["reason"])
                self.assertEqual(self.entries_taken(), taken)

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

    def test_the_planning_finding_is_marked_attended_in_the_record_and_set_aside_by_the_rules(self):
        """Issue #115: the record's finding entry carries the mark the Feeder hands
        `should_stop`, and the rules read it as no serious finding, so a tour with only lows
        and a plan area reads as clean."""
        self.test_process([finding(1, severity="low")])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Plan the Search area after repeated patches [loop] [attended]"])
        outcome, _ = self.run_pass(testpass.Request(plan_areas=("Search",)))
        entries = outcome.record["findings"]
        self.assertEqual([entry["attended"] for entry in entries], [False, True])
        self.assertTrue(testloop.is_attended(entries[1]))
        self.assertFalse(testloop.is_attended(entries[0]))
        started = datetime(2026, 9, 28, 9, 0)
        self.assertEqual(testloop.should_stop(
            testloop.PassResult(kind=testloop.TOUR, status=outcome.record["status"],
                                findings=tuple(entries), new_cards=0),
            rounds=1, started_at=started, now=started, cards_filed=0), testloop.STOP_CLEAN)


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


class Untoured(PassCase):
    """Issue #121: the pass record carries the areas the Test process could not reach, each a
    heading of the tour document, and a report that reached no area is `not_run`."""

    def test_a_partial_tour_is_ran_with_the_untoured_areas_in_the_record_and_files_what_it_found(self):
        self.test_process([finding(1)], reason="Invoices and Settings sit behind a sign in",
                          untoured=["Invoices", " Settings "])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        outcome, _ = self.run_pass()
        record = outcome.record
        self.assertEqual(record["status"], testloop.RAN, record["reason"])
        self.assertEqual(record["untoured"], ["Invoices", "Settings"])
        self.assertEqual([entry["id"] for entry in record["filed"]], ["T-2"])
        with open(outcome.path) as handle:
            self.assertEqual(json.load(handle)["untoured"], ["Invoices", "Settings"])

    def test_a_report_without_the_key_records_an_empty_list(self):
        self.test_process([])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertEqual(outcome.record["untoured"], [])

    def test_a_report_that_reached_no_area_is_not_run_with_its_reason_and_files_nothing(self):
        # The findings it reported on the way are not filed: a pass that did not run files
        # nothing, and this one saw no area to find anything in.
        # The document's title is a heading, and no process lists it: the three areas under
        # it are every area there is to reach.
        self.assertEqual(testbrief.areas(TOUR_MD), ("Search", "Invoices", "Settings"))
        self.test_process([finding(1)], reason="every page answered with the sign in form",
                          untoured=["Search", "Invoices", "Settings"])
        outcome, _ = self.run_pass()
        record = outcome.record
        self.assertEqual(outcome.exit_code, testpass.EXIT_HALTED)
        self.assertEqual(record["status"], testloop.NOT_RUN)
        self.assertIn("could reach no area of the tour document", record["reason"])
        self.assertIn("every page answered with the sign in form", record["reason"])
        self.assertEqual(record["untoured"], ["Search", "Invoices", "Settings"])
        self.assertEqual(record["findings"], [])
        self.assertEqual(record["filed"], [])
        self.assertEqual(self.entries_taken(), 1)
        self.assertEqual(self.tracker(), TRACKER_MD)

    def test_reaching_one_area_of_three_is_ran_not_not_run(self):
        self.test_process([], untoured=["Invoices", "Settings"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertEqual(outcome.record["untoured"], ["Invoices", "Settings"])

    def test_an_untoured_name_that_is_not_a_heading_fails_the_pass_naming_it(self):
        # Not dropped like an invalid finding: dropping it would read the tour as more
        # complete than the process said, which is the misreading the key exists to prevent.
        self.test_process([finding(1)], untoured=["Nowhere"])
        self.filing_process([{"finding": 1, "action": "filed", "id": "T-2"}],
                            ["- [ ] T-2 Finding 1 [loop]"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertEqual(outcome.record["reason"],
                         "untoured area 'Nowhere' is not a heading of the tour document")
        self.assertEqual(outcome.record["untoured"], [])
        self.assertEqual(outcome.record["filed"], [])
        self.assertEqual(self.entries_taken(), 1)

    def test_check_untoured_flattens_keeps_each_name_once_and_leaves_a_stopped_area_out(self):
        headings = testbrief.headings(TOUR_MD)
        self.assertEqual(testpass.check_untoured(["  Search ", "Search", "Settings"], headings),
                         (["Search", "Settings"], None))
        self.assertEqual(testpass.check_untoured(["Search", "Settings"], headings,
                                                 stopped_areas=("Settings",)),
                         (["Search"], None))
        checked, sentence = testpass.check_untoured(["Search", "Cart"], headings)
        self.assertIsNone(checked)
        self.assertEqual(sentence, "untoured area 'Cart' is not a heading of the tour document")

    def test_a_stopped_area_listed_as_untoured_is_skipped_not_unreached(self):
        """Code review: the brief tells the process to skip a stopped area, and a process that
        lists it has skipped it. Left in, a loop with a stopped area could never stop clean."""
        self.test_process([], untoured=["Settings"])
        outcome, _ = self.run_pass(testpass.Request(stopped_areas=("Settings",)))
        self.assertEqual(outcome.record["status"], testloop.RAN)
        self.assertEqual(outcome.record["untoured"], [])

    def test_reaching_nothing_but_a_stopped_area_is_not_run(self):
        # Search and Invoices are every area left to reach once Settings is stopped.
        self.test_process([], reason="the sign in form again", untoured=["Search", "Invoices"])
        outcome, _ = self.run_pass(testpass.Request(stopped_areas=("Settings",)))
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertIn("could reach no area", outcome.record["reason"])
        self.assertEqual(outcome.record["untoured"], ["Search", "Invoices"])

    def test_a_not_run_report_carries_its_untoured_areas_in_the_same_shape(self):
        # Code review: the process's own not_run and the one the pass synthesizes for a report
        # that reached nothing agree on what the record holds.
        self.test_process([], status="not_run", reason="the session file is missing",
                          untoured=["Search", " Invoices ", "Settings"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.NOT_RUN)
        self.assertEqual(outcome.record["reason"], "the session file is missing")
        self.assertEqual(outcome.record["untoured"], ["Search", "Invoices", "Settings"])
        self.test_process([], status="not_run", reason="the session file is missing",
                          untoured=["Nowhere"])
        outcome, _ = self.run_pass()
        self.assertEqual(outcome.record["status"], testloop.FAILED)
        self.assertIn("'Nowhere'", outcome.record["reason"])

    def test_the_findings_file_lists_the_untoured_areas_in_report_only_mode(self):
        self.test_process([finding(1)], reason="Settings never loaded",
                          untoured=["Settings"])
        outcome, _ = self.run_pass(testpass.Request(report_only=True))
        self.assertEqual(outcome.record["status"], testloop.RAN)
        with open(self.paths().findings) as handle:
            text = handle.read()
        self.assertIn("## Pass 1, tour of", text)
        self.assertIn("- untoured: Settings\n", text)
        self.assertLess(text.index("- untoured: Settings"), text.index("[high] file:"))
        self.assertEqual(self.entries_taken(), 1)

    def test_a_report_only_pass_that_reached_every_area_writes_no_untoured_line(self):
        self.test_process([finding(1)])
        self.run_pass(testpass.Request(report_only=True))
        with open(self.paths().findings) as handle:
            self.assertNotIn("untoured", handle.read())


if __name__ == "__main__":
    unittest.main()

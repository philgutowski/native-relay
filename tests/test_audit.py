"""The card audit (stale cards, 2026-09-08): which cards disagree with the record and with git.

Every case drives `audit.build` with a fake adapter and a real state store, so the rules are
tested against the shapes the run loop actually writes. Git is real too, for the one rule that
reads it: a terminal card whose record has not landed is honest when a commit on the default
branch since the baseline names the task.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

import _paths
import _repo
from _fakes import FakeAdapter
from relay import audit, contracts, state


IN_REVIEW = "In Progress"


class AuditCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.repo = _repo.make_repo(self.tmp.name)
        self.manifest_path = os.path.join(self.tmp.name, "manifest.toml")
        open(self.manifest_path, "w").close()
        self.store = state.StateStore(self.manifest_path, self.repo, home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, *task_ids):
        return SimpleNamespace(
            project=SimpleNamespace(repo=self.repo, default_branch="main", branch_prefix="relay/"),
            tracker=SimpleNamespace(in_review_status=IN_REVIEW),
            tasks=[SimpleNamespace(id=task_id) for task_id in task_ids])

    def head(self):
        return _repo.git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def build(self, statuses, task_ids=("T-1",), live=False):
        return audit.build(self.manifest(*task_ids), self.store, FakeAdapter(statuses=statuses),
                           live=live)

    def classes(self, findings):
        return [(finding["class"], finding["task"]) for finding in findings]


class Agreement(AuditCase):
    def test_a_landed_record_on_a_terminal_card_agrees(self):
        self.store.upsert("T-1", status=contracts.STATUS_LANDED, landing_ref="a" * 40)
        self.assertEqual(self.build({"T-1": {"status": "Done", "terminal": True}}), [])

    def test_a_blocked_record_on_a_todo_card_agrees(self):
        self.store.upsert("T-1", status=contracts.STATUS_BLOCKED)
        self.assertEqual(self.build({"T-1": {"status": "Todo"}}), [])

    def test_a_task_with_no_record_on_a_todo_card_agrees(self):
        self.assertEqual(self.build({"T-1": {"status": "Todo"}}), [])


class StaleInReview(AuditCase):
    def test_a_blocked_record_on_an_in_review_card_is_stale_and_names_where_it_goes(self):
        self.store.upsert("T-1", status=contracts.STATUS_BLOCKED, baseline_tracker_status="Todo")
        findings = self.build({"T-1": {"status": IN_REVIEW}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_STALE_IN_REVIEW, "T-1")])
        self.assertIn("`Todo`", findings[0]["text"])
        self.assertIn("blocked", findings[0]["text"])

    def test_a_card_with_no_record_at_in_review_is_stale_with_a_generic_destination(self):
        findings = self.build({"T-1": {"status": IN_REVIEW}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_STALE_IN_REVIEW, "T-1")])
        self.assertIn("its todo status", findings[0]["text"])

    def test_the_in_review_comparison_ignores_case(self):
        self.store.upsert("T-1", status=contracts.STATUS_HALTED)
        findings = self.build({"T-1": {"status": IN_REVIEW.lower()}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_STALE_IN_REVIEW, "T-1")])

    def test_a_record_in_flight_under_a_live_run_is_not_stale(self):
        self.store.upsert("T-1", status=contracts.STATUS_RUNNING)
        self.assertEqual(self.build({"T-1": {"status": IN_REVIEW}}, live=True), [])

    def test_a_record_in_flight_with_no_live_run_is_a_dead_runner_and_stale(self):
        self.store.upsert("T-1", status=contracts.STATUS_MERGING)
        findings = self.build({"T-1": {"status": IN_REVIEW}}, live=False)
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_STALE_IN_REVIEW, "T-1")])

    def test_a_baseline_equal_to_in_review_falls_back_to_the_generic_destination(self):
        self.store.upsert("T-1", status=contracts.STATUS_BLOCKED,
                          baseline_tracker_status=IN_REVIEW)
        findings = self.build({"T-1": {"status": IN_REVIEW}})
        self.assertIn("its todo status", findings[0]["text"])


class Reopened(AuditCase):
    def test_a_landed_record_on_a_non_terminal_card_was_reopened(self):
        self.store.upsert("T-1", status=contracts.STATUS_LANDED, landing_ref="abcdef0" * 6)
        findings = self.build({"T-1": {"status": "Todo", "terminal": False}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_REOPENED, "T-1")])
        self.assertIn("abcdef0abcde", findings[0]["text"])
        self.assertIn("Todo", findings[0]["text"])


class ClosedUnlanded(AuditCase):
    def test_a_terminal_card_on_a_halted_record_with_nothing_since_the_baseline(self):
        self.store.upsert("T-1", status=contracts.STATUS_HALTED, baseline_sha=self.head())
        findings = self.build({"T-1": {"status": "Done", "terminal": True}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_CLOSED_UNLANDED, "T-1")])
        self.assertIn("nothing landed", findings[0]["text"])

    def test_a_terminal_card_with_no_record_at_all_is_reported(self):
        findings = self.build({"T-1": {"status": "Done", "terminal": True}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_CLOSED_UNLANDED, "T-1")])

    def test_a_commit_naming_the_task_since_the_baseline_makes_the_close_honest(self):
        """The same link `verify.hand_landing` reads: an operator who finished the task by hand
        and closed the card has not left a stale board, and startup re-verify promotes the
        record on the next run."""
        baseline = self.head()
        _repo.git(self.repo, "commit", "--allow-empty", "-q", "-m", "Finish T-1 by hand")
        self.store.upsert("T-1", status=contracts.STATUS_HALTED, baseline_sha=baseline)
        self.assertEqual(self.build({"T-1": {"status": "Done", "terminal": True}}), [])

    def test_a_commit_naming_another_task_does_not_count(self):
        baseline = self.head()
        _repo.git(self.repo, "commit", "--allow-empty", "-q", "-m", "Finish T-10 by hand")
        self.store.upsert("T-1", status=contracts.STATUS_HALTED, baseline_sha=baseline)
        findings = self.build({"T-1": {"status": "Done", "terminal": True}})
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_CLOSED_UNLANDED, "T-1")])


class Unreadable(AuditCase):
    def test_a_card_the_adapter_cannot_read_is_its_own_finding_and_the_audit_continues(self):
        self.store.upsert("T-2", status=contracts.STATUS_BLOCKED)
        findings = self.build({"T-2": {"status": IN_REVIEW}}, task_ids=("T-1", "T-2"))
        self.assertEqual(self.classes(findings), [
            (contracts.AUDIT_UNREADABLE, "T-1"), (contracts.AUDIT_STALE_IN_REVIEW, "T-2")])

    def test_an_adapter_that_raises_is_unreadable_rather_than_an_exception(self):
        class Exploding(FakeAdapter):
            def status(self, task_id):
                raise RuntimeError("the tracker is down")

        findings = audit.build(self.manifest("T-1"), self.store, Exploding())
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_UNREADABLE, "T-1")])
        self.assertIn("the tracker is down", findings[0]["text"])

    def test_a_store_that_cannot_be_read_is_one_run_level_finding(self):
        class Broken:
            def records(self):
                raise OSError("state.json is unreadable")

        findings = audit.build(self.manifest("T-1"), Broken(), FakeAdapter())
        self.assertEqual(self.classes(findings), [(contracts.AUDIT_FAILED, None)])


class Lines(unittest.TestCase):
    def test_no_findings_says_every_card_agrees(self):
        self.assertEqual(audit.lines([]), ["cards: every card agrees with its record"])

    def test_findings_print_a_count_then_one_indented_line_each(self):
        out = audit.lines([{"text": "T-1's card reads X"}, {"text": "T-2's card reads Y"}],
                          at="2026-09-08T12:00:00+00:00")
        self.assertEqual(out[0], "cards: 2 stale card(s) (audited 2026-09-08T12:00:00+00:00)")
        self.assertEqual(out[1], "    T-1's card reads X")
        # Four spaces: `status` prints its per task lines at two, and a finding opening with a
        # task id must not read as one of them.
        self.assertFalse(out[1].startswith("  T-"))

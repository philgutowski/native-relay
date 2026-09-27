"""Issue #43: a landed card's project item that never reached the terminal status.

GitHub keeps two truths about one card, the issue's state and its project item's status, and
`status` answers terminal from a closed issue alone. So a Closeout that closed the issue and
skipped or failed the item edit landed with the item still in review, and the run end audit
reported every card as agreeing with its record. Every case here drives the real GitHubAdapter
through a `gh` fake that holds the two truths apart, so the reads under test are the production
ones and only the transport is replaced.
"""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import _paths
import _repo
from _fakes import FakeAdapter
from relay import adapters, audit, closeout, contracts, state, summary
from relay.adapters import github as gh_adapter


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


REPO = "example-org/relay-target"


class TwoTruths:
    """A `run(args, timeout=None)` for `gh`. `issues` maps an issue number to its state, and
    `board` maps an issue number to its item's status on the declared project; a number absent
    from `board` is an issue the project does not carry. `foreign` is the same for another
    repository's issues on the same project, listed first. `board_failure` fails the board read
    alone, the shape that matters: `gh issue view` needs no project scope and item-list does.
    `total` overrides the board's totalCount, to model a board past item-list's limit."""

    def __init__(self, issues, board, board_failure=None, foreign=None, total=None,
                 repo_failure=None):
        self.issues = dict(issues)
        self.board = dict(board)
        self.board_failure = board_failure
        self.foreign = dict(foreign or {})
        self.total = total
        self.repo_failure = repo_failure
        self.calls = []

    @staticmethod
    def _item(number, status, repository):
        item = {"id": "PVTI_%s_%s" % (repository, number),
                "content": {"type": "Issue", "number": int(number), "title": "t",
                            "repository": repository}}
        if status is not None:
            item["status"] = status
        return item

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if args[:3] == ["gh", "repo", "view"]:
            if self.repo_failure:
                return _Proc(1, "", self.repo_failure)
            return _Proc(0, json.dumps({"id": "R_1", "nameWithOwner": REPO}))
        if args[:3] == ["gh", "project", "item-list"]:
            if self.board_failure:
                return _Proc(1, "", self.board_failure)
            items = ([self._item(n, s, "example-org/elsewhere") for n, s in self.foreign.items()]
                     + [self._item(n, s, REPO) for n, s in self.board.items()])
            total = len(items) if self.total is None else self.total
            return _Proc(0, json.dumps({"items": items, "totalCount": total}))
        if args[:3] == ["gh", "issue", "view"]:
            number = args[3]
            if number not in self.issues:
                return _Proc(1, "", "no issue %s" % number)
            return _Proc(0, json.dumps({"id": "I_%s" % number, "title": "t", "body": "",
                                        "state": self.issues[number], "comments": []}))
        raise AssertionError("unexpected gh call: %s" % args)


def _manifest(repo=".", status_field="Done", task_ids=("12",)):
    return SimpleNamespace(
        project=SimpleNamespace(repo=repo, default_branch="main", branch_prefix="relay/"),
        tracker=SimpleNamespace(adapter="github", owner="example-org", project_number=6,
                                status_field=status_field, in_review_status="In review"),
        tasks=[SimpleNamespace(id=task_id) for task_id in task_ids])


def _adapter(run, status_field="Done"):
    return gh_adapter.GitHubAdapter(_manifest(status_field=status_field), run=run)


class BoardLag(unittest.TestCase):
    """The adapter read itself, beside the `status` it does not change."""

    def test_a_closed_issue_is_terminal_while_its_item_lags(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "In review"})
        adapter = _adapter(run)
        self.assertTrue(adapter.status("12")["terminal"], "a closed issue stays terminal")
        lag, reason = adapters.board_lag(adapter, "12")
        self.assertIsNone(reason)
        self.assertEqual(lag, {"card_status": "In review", "terminal_status": "Done"})

    def test_an_item_at_the_terminal_status_does_not_lag_and_case_is_ignored(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "done"})
        self.assertEqual(adapters.board_lag(_adapter(run), "12"), (None, None))

    def test_an_issue_the_declared_project_does_not_carry_has_no_item_to_lag(self):
        run = TwoTruths({"12": "CLOSED"}, {"13": "In review"})
        self.assertEqual(adapters.board_lag(_adapter(run), "12"), (None, None))

    def test_an_item_on_the_project_with_no_status_lags(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": None})
        lag, _ = adapters.board_lag(_adapter(run), "12")
        self.assertEqual(lag["card_status"], "no status")

    def test_no_status_field_means_no_terminal_column_and_no_board_read(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "In review"})
        self.assertEqual(adapters.board_lag(_adapter(run, status_field=None), "12"), (None, None))
        self.assertEqual(run.calls, [])

    def test_a_board_that_cannot_be_read_is_a_reason_not_a_clean_read(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "Done"}, board_failure="missing project scope")
        lag, reason = adapters.board_lag(_adapter(run), "12")
        self.assertIsNone(lag)
        self.assertIn("missing project scope", reason)

    def test_another_repositorys_item_with_the_same_number_is_not_this_issues_item(self):
        """A project can carry more than one repository, so a number alone matches the wrong
        issue: listed first, the foreign item would hide this one's lag or invent one."""
        run = TwoTruths({"12": "CLOSED"}, {"12": "In review"}, foreign={"12": "Done"})
        lag, _ = adapters.board_lag(_adapter(run), "12")
        self.assertEqual(lag["card_status"], "In review")
        run = TwoTruths({"12": "CLOSED"}, {"12": "Done"}, foreign={"12": "In review"})
        self.assertEqual(adapters.board_lag(_adapter(run), "12"), (None, None))

    def test_a_board_past_the_item_limit_is_a_reason_rather_than_an_absence(self):
        run = TwoTruths({"12": "CLOSED"}, {"13": "Done"}, total=600)
        lag, reason = adapters.board_lag(_adapter(run), "12")
        self.assertIsNone(lag)
        self.assertIn("600", reason)

    def test_a_repository_that_cannot_be_named_is_a_reason(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "In review"}, repo_failure="not a repo")
        lag, reason = adapters.board_lag(_adapter(run), "12")
        self.assertIsNone(lag)
        self.assertIn("not a repo", reason)

    def test_the_repository_is_read_once_per_adapter(self):
        run = TwoTruths({"12": "CLOSED", "13": "CLOSED"}, {"12": "Done", "13": "Done"})
        adapter = _adapter(run)
        adapters.board_lag(adapter, "12")
        adapters.board_lag(adapter, "13")
        self.assertEqual(sum(1 for call in run.calls if call[:3] == ["gh", "repo", "view"]), 1)

    def test_a_shared_cache_makes_one_board_read_for_many_cards(self):
        """Issue #61: a caller checking several landed cards in one pass, the run end audit's
        own shape, used to make one full `gh project item-list` per card. A shared cache dict
        makes the second call reuse the first's read."""
        run = TwoTruths({"12": "CLOSED", "13": "CLOSED"}, {"12": "In review", "13": "Done"})
        adapter = _adapter(run)
        cache = {}
        lag_12, _ = adapters.board_lag(adapter, "12", cache=cache)
        lag_13, _ = adapters.board_lag(adapter, "13", cache=cache)
        self.assertEqual(lag_12["card_status"], "In review")
        self.assertIsNone(lag_13)
        self.assertEqual(sum(1 for call in run.calls if call[:3] == ["gh", "project", "item-list"]), 1)

    def test_a_failed_read_is_not_cached_so_a_later_call_can_retry(self):
        """Issue #61's own review of the cache fix: caching a failure the same way a success is
        cached would let one transient gh error poison every card sharing the pass's cache,
        turning one card's unreadable board into every card's."""
        run = TwoTruths({"12": "CLOSED", "13": "CLOSED"}, {"12": "Done", "13": "Done"},
                        board_failure="rate limited")
        adapter = _adapter(run)
        cache = {}
        lag, reason = adapters.board_lag(adapter, "12", cache=cache)
        self.assertIsNone(lag)
        self.assertIn("rate limited", reason)
        run.board_failure = None
        lag, reason = adapters.board_lag(adapter, "13", cache=cache)
        self.assertIsNone(reason)
        self.assertIsNone(lag)
        self.assertEqual(
            sum(1 for call in run.calls if call[:3] == ["gh", "project", "item-list"]), 2)

    def test_no_cache_reads_the_board_fresh_every_call_as_before(self):
        run = TwoTruths({"12": "CLOSED", "13": "CLOSED"}, {"12": "Done", "13": "Done"})
        adapter = _adapter(run)
        adapters.board_lag(adapter, "12")
        adapters.board_lag(adapter, "13")
        self.assertEqual(sum(1 for call in run.calls if call[:3] == ["gh", "project", "item-list"]), 2)

    def test_an_adapter_with_one_status_per_card_has_nothing_to_check(self):
        self.assertEqual(adapters.board_lag(FakeAdapter(), "T-1"), (None, None))

    def test_the_read_adds_no_public_method(self):
        public = {attr for attr in dir(_adapter(TwoTruths({}, {})))
                  if not attr.startswith("_")
                  and callable(getattr(_adapter(TwoTruths({}, {})), attr))}
        self.assertEqual(public, set(adapters.INTERFACE))


class OpenIssueStatus(unittest.TestCase):
    """Issue #78: an open issue's status once a board column is declared. It fell back to the
    issue state, so an item with no Status recorded `OPEN` as its baseline and a blocked
    Closeout was told to move the item back to a column the board does not have."""

    def test_an_item_on_the_board_with_no_status_reads_no_status(self):
        result = _adapter(TwoTruths({"12": "OPEN"}, {"12": None})).status("12")
        self.assertIsNone(result["status"])
        self.assertFalse(result["terminal"])
        self.assertIsNone(result["skipped"])

    def test_an_issue_the_declared_project_does_not_carry_reads_no_status(self):
        result = _adapter(TwoTruths({"12": "OPEN"}, {"13": "Todo"})).status("12")
        self.assertIsNone(result["status"])
        self.assertFalse(result["terminal"])
        self.assertIsNone(result["skipped"])

    def test_an_item_with_a_status_reads_that_status(self):
        result = _adapter(TwoTruths({"12": "OPEN"}, {"12": "Todo"})).status("12")
        self.assertEqual(result["status"], "Todo")

    def test_no_status_field_keeps_the_issue_state_and_reads_no_board(self):
        run = TwoTruths({"12": "OPEN"}, {"12": None})
        self.assertEqual(_adapter(run, status_field=None).status("12")["status"], "OPEN")
        self.assertFalse(any(call[:3] == ["gh", "project", "item-list"] for call in run.calls))

    def test_a_closed_issue_still_reads_closed_and_terminal(self):
        result = _adapter(TwoTruths({"12": "CLOSED"}, {"12": None})).status("12")
        self.assertEqual(result["status"], "CLOSED")
        self.assertTrue(result["terminal"])

    def test_the_baseline_an_empty_item_records_is_unknown(self):
        """The launch records what `status()` read, and the Closeout's move follows the record,
        so the whole chain is `status()` through `launch_baseline` to the two refusal rules."""
        manifest = _manifest()
        read = _adapter(TwoTruths({"12": "OPEN"}, {"12": None})).status("12")
        record = {"baseline_tracker_status": closeout.launch_baseline(manifest, {},
                                                                       read["status"])}
        self.assertIsNone(closeout.return_to_for(manifest, record))
        self.assertTrue(closeout.baseline_unknown(manifest, record))


class ItemConfirmedTerminal(unittest.TestCase):
    """Issue #61's own review: the narrower answer `board_lag` cannot give, since its
    "nothing to report" covers both a confirmed item and one off the board entirely."""

    def test_an_item_on_the_board_at_the_terminal_status_is_confirmed(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "Done"})
        confirmed, reason = adapters.item_confirmed_terminal(_adapter(run), "12")
        self.assertTrue(confirmed)
        self.assertIsNone(reason)

    def test_an_item_off_the_board_is_not_confirmed(self):
        run = TwoTruths({"12": "CLOSED"}, {"13": "Done"})
        confirmed, reason = adapters.item_confirmed_terminal(_adapter(run), "12")
        self.assertFalse(confirmed)
        self.assertIsNone(reason)

    def test_an_item_still_lagging_is_not_confirmed(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "In review"})
        confirmed, reason = adapters.item_confirmed_terminal(_adapter(run), "12")
        self.assertFalse(confirmed)
        self.assertIsNone(reason)

    def test_an_unreadable_board_is_not_confirmed_and_carries_a_reason(self):
        run = TwoTruths({"12": "CLOSED"}, {"12": "Done"}, board_failure="no scope")
        confirmed, reason = adapters.item_confirmed_terminal(_adapter(run), "12")
        self.assertFalse(confirmed)
        self.assertIn("no scope", reason)

    def test_an_adapter_with_one_status_per_card_has_nothing_to_confirm(self):
        self.assertEqual(adapters.item_confirmed_terminal(FakeAdapter(), "T-1"), (False, None))


class ConfirmBoardTerminal(unittest.TestCase):
    """The read after a landed Closeout, the same shape as `closeout.read_back`."""

    def confirm(self, run):
        return closeout.confirm_board_terminal(_adapter(run), _manifest(), "12")

    def test_a_lagging_item_is_a_finding_naming_the_terminal_status(self):
        finding = self.confirm(TwoTruths({"12": "CLOSED"}, {"12": "In review"}))
        self.assertEqual(finding["class"], contracts.BOARD_ITEM_NOT_TERMINAL)
        self.assertEqual(finding["card_status"], "In review")
        self.assertEqual(finding["terminal_status"], "Done")
        line = summary.cause_line(finding["class"], finding)
        self.assertEqual(line, "landed, but its project item reads In review after the closeout; "
                               "move 12 to Done by hand")

    def test_an_item_at_the_terminal_status_confirms(self):
        self.assertIsNone(self.confirm(TwoTruths({"12": "CLOSED"}, {"12": "Done"})))

    def test_an_issue_off_the_declared_project_confirms(self):
        self.assertIsNone(self.confirm(TwoTruths({"12": "CLOSED"}, {})))

    def test_an_unreadable_board_is_a_finding_rather_than_a_confirmation(self):
        finding = self.confirm(TwoTruths({"12": "CLOSED"}, {}, board_failure="gh exploded"))
        self.assertEqual(finding["class"], contracts.BOARD_ITEM_NOT_TERMINAL)
        self.assertEqual(finding["card_status"], "unreadable")
        self.assertIn("gh exploded", finding["evidence"])

    def test_an_unreadable_board_says_why_instead_of_telling_the_operator_to_move_it(self):
        """Issue #61: the template for this class always said "reads {card_status}... move
        {task} to {terminal_status} by hand", which on an unreadable read told the operator to
        move an "unreadable" item, a status nobody confirmed it was not already at. The cause
        sits only in the evidence, so the rendered line has to come from there instead."""
        finding = self.confirm(TwoTruths({"12": "CLOSED"}, {}, board_failure="gh exploded"))
        line = summary.cause_line(finding["class"], finding)
        self.assertEqual(line, finding["evidence"])
        self.assertIn("gh exploded", line)
        self.assertNotIn("by hand", line)

    def test_a_board_read_that_raises_is_a_finding_rather_than_an_exception(self):
        class Exploding(FakeAdapter):
            def _board_lag(self, task_id):
                raise RuntimeError("down")

        finding = closeout.confirm_board_terminal(Exploding(), _manifest(), "12")
        self.assertEqual(finding["class"], contracts.BOARD_ITEM_NOT_TERMINAL)
        self.assertIn("down", finding["evidence"])

    def test_the_finding_is_a_check_by_hand_in_the_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            path = os.path.join(tmp, "manifest.toml")
            open(path, "w").close()
            store = state.StateStore(path, os.path.join(tmp, "repo"), home=home)
            finding = self.confirm(TwoTruths({"12": "CLOSED"}, {"12": "In review"}))
            store.upsert("12", status=contracts.STATUS_LANDED, halt_class=contracts.HALT_LANDED,
                         landing_ref="a" * 40, findings=[finding])
            store.write_terminal(contracts.RUN_COMPLETED)
            manifest = SimpleNamespace(path=path, project=SimpleNamespace(repo=tmp),
                                       tasks=[SimpleNamespace(id="12")], shipping_push=True)
            data = summary.build(manifest, store)
        checks = [check for check in data["pending_checks"]
                  if check["kind"] == "board_item_not_terminal"]
        self.assertEqual(len(checks), 1, data["pending_checks"])
        self.assertIn("move 12 to Done by hand", checks[0]["text"])


class Audit(unittest.TestCase):
    """The same read between runs, from `audit.build` with no run in flight."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        self.repo = _repo.make_repo(self.tmp.name)
        path = os.path.join(self.tmp.name, "manifest.toml")
        open(path, "w").close()
        self.store = state.StateStore(path, self.repo, home=home)
        self.store.upsert("12", status=contracts.STATUS_LANDED, landing_ref="2c68d07" + "0" * 33)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, run, item_seen=None):
        manifest = _manifest(repo=self.repo)
        return audit.build(manifest, self.store, gh_adapter.GitHubAdapter(manifest, run=run),
                           item_seen=item_seen)

    def test_a_landed_record_whose_item_lags_is_reported(self):
        findings = self.build(TwoTruths({"12": "CLOSED"}, {"12": "In review"}))
        self.assertEqual([(f["class"], f["task"]) for f in findings],
                         [(contracts.AUDIT_ITEM_NOT_TERMINAL, "12")])
        self.assertIn("CLOSED", findings[0]["text"])
        self.assertIn("In review", findings[0]["text"])
        self.assertIn("`Done`", findings[0]["text"])

    def test_a_landed_record_whose_item_reached_the_terminal_status_agrees(self):
        self.assertEqual(self.build(TwoTruths({"12": "CLOSED"}, {"12": "Done"})), [])

    def test_a_landed_record_off_the_declared_project_agrees(self):
        self.assertEqual(self.build(TwoTruths({"12": "CLOSED"}, {"13": "In review"})), [])

    def test_an_unreadable_board_is_an_unreadable_card(self):
        findings = self.build(TwoTruths({"12": "CLOSED"}, {}, board_failure="no scope"))
        self.assertEqual([f["class"] for f in findings], [contracts.AUDIT_UNREADABLE])
        self.assertIn("project item", findings[0]["text"])

    def test_an_unlanded_record_on_a_closed_issue_is_not_this_finding(self):
        self.store.upsert("12", status=contracts.STATUS_BLOCKED, landing_ref=None)
        findings = self.build(TwoTruths({"12": "CLOSED"}, {"12": "In review"}))
        self.assertNotIn(contracts.AUDIT_ITEM_NOT_TERMINAL, [f["class"] for f in findings])

    def test_two_landed_records_share_one_board_read(self):
        """Issue #61: before the fix, each landed task's check made its own full
        `gh project item-list`, so a run end audit over many landed cards bounded its pass at
        30 seconds times the landed count rather than once."""
        self.store.upsert("13", status=contracts.STATUS_LANDED, landing_ref="3d79e18" + "0" * 33)
        manifest = _manifest(repo=self.repo, task_ids=("12", "13"))
        run = TwoTruths({"12": "CLOSED", "13": "CLOSED"}, {"12": "In review", "13": "Done"})
        audit.build(manifest, self.store, gh_adapter.GitHubAdapter(manifest, run=run))
        self.assertEqual(
            sum(1 for call in run.calls if call[:3] == ["gh", "project", "item-list"]), 1)

    def test_an_open_cards_status_read_shares_the_same_board_cache_as_a_landed_items_lag(self):
        """Issue #61's own review of the fix above: `status()` itself reads the project board
        for any open, non closed card once `status_field` is declared, and the audit's per task
        loop calls `status()` for every task up front, landed or not. Sharing the cache with
        `_item_lag` alone left that call making its own board read per non landed card."""
        self.store.upsert("13", status=contracts.STATUS_BLOCKED)
        manifest = _manifest(repo=self.repo, task_ids=("12", "13"))
        run = TwoTruths({"12": "CLOSED", "13": "OPEN"}, {"12": "In review", "13": "Todo"})
        audit.build(manifest, self.store, gh_adapter.GitHubAdapter(manifest, run=run))
        self.assertEqual(
            sum(1 for call in run.calls if call[:3] == ["gh", "project", "item-list"]), 1)

    def test_a_reopened_landed_card_with_an_empty_item_says_no_status(self):
        """Issue #78: an open issue's empty item now reads None rather than `OPEN`, and the
        reopened sentence names that as words rather than printing `None`."""
        findings = self.build(TwoTruths({"12": "OPEN"}, {"12": None}))
        self.assertEqual([f["class"] for f in findings], [contracts.AUDIT_REOPENED])
        self.assertIn("its card reads no status", findings[0]["text"])
        self.assertNotIn("None", findings[0]["text"])
        self.assertNotIn("OPEN", findings[0]["text"])

    def test_item_seen_marks_a_landed_item_read_cleanly_as_terminal(self):
        """Issue #61: this is what lets the Runner retire `confirm_board_terminal`'s own
        record finding once a later audit confirms the item, the way #64 already retires
        `card_in_review_by_run`."""
        item_seen = {}
        self.build(TwoTruths({"12": "CLOSED"}, {"12": "Done"}), item_seen=item_seen)
        self.assertEqual(item_seen, {"12": True})

    def test_item_seen_stays_empty_for_a_lagging_or_unreadable_item(self):
        item_seen = {}
        self.build(TwoTruths({"12": "CLOSED"}, {"12": "In review"}), item_seen=item_seen)
        self.assertEqual(item_seen, {})
        item_seen = {}
        self.build(TwoTruths({"12": "CLOSED"}, {}, board_failure="no scope"), item_seen=item_seen)
        self.assertEqual(item_seen, {})

    def test_item_seen_stays_empty_when_the_item_is_off_the_board_rather_than_confirmed(self):
        """Issue #61's own review: `board_lag`'s "nothing to report" also covers an item the
        project no longer carries at all, which is not the same claim as reading the item at
        the terminal status. Only the second should let a later audit retire the stale
        `confirm_board_terminal` finding a closeout wrote while the item still lagged."""
        item_seen = {}
        self.build(TwoTruths({"12": "CLOSED"}, {"13": "Done"}), item_seen=item_seen)
        self.assertEqual(item_seen, {})


if __name__ == "__main__":
    unittest.main()

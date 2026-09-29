"""U6 of the browser test loop plan: the Feeder runs the loop.

Every case drives the real feeder loop over `test_feeder.FeederCase`, with its fake runner, fake
summary, and fake board, and one more fake: `run_test_pass`, which answers each pass with a
scripted pass record the way `relay test` would leave one. A card a scripted pass files is put
on the fake board's ready list, unless the case marks it as one the ready source does not
return, so the next Cycle's ready read finds it as it would find a card the Filing process
created. Nothing here launches a process or reads a network.
"""
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

import _paths
from relay import feeder, testloop
from test_feeder import FeederCase, card

LOOP_ON = feeder.TestLoop(enabled=True, tour="docs/tour.md", url="http://127.0.0.1:5173",
                          prepare=("scripts/serve-at.sh",))

# The event words a Feeder with the loop off writes on an ordinary run: today's, and no other.
TODAYS_EVENTS = {feeder.EVENT_STARTED, feeder.EVENT_CYCLE_STARTED, feeder.EVENT_CYCLE_RESULT,
                 feeder.EVENT_WAITING, feeder.EVENT_LEAVING}


def finding(severity="high", area="Search", card_id=None, title=None):
    """One finding as the pass record lists it, the shape `testpass._finding_entry` writes."""
    return {"title": title or "%s finding in %s" % (severity, area), "severity": severity,
            "kind": "defect", "area": area, "design": False, "card": card_id,
            "cause_file": "src/search.py", "attended": False,
            "outcome": testloop.FILE if severity != "low" else testloop.OUTCOME_LOW}


def filed(card_id, area="Search", parent=None, cause="src/search.py", attended=False,
          unready=False):
    """One confirmed filed card as the pass record lists it. `unready` is the case's own mark,
    never a record key the verb writes: the fake leaves that card off the ready list."""
    entry = {"id": str(card_id), "finding": 1, "area": area, "design": False,
             "cause_file": cause, "card": parent, "card_sent": parent is not None,
             "attended": attended}
    if unready:
        entry["unready"] = True
    return entry


class LoopCase(FeederCase):
    """A FeederCase whose Deps carry a fake `run_test_pass`. `pass_script` holds one entry per
    pass, each a dict merged over a pass that ran and found nothing, or a callable given the
    call and returning that dict. With the script spent, a tour finds nothing (a clean tour)
    and a check finds nothing."""

    def setUp(self):
        super().setUp()
        self.adapter.ready_cards = []
        self.pass_script, self.passes, self.order = [], [], []
        self.before_run = lambda: self.order.append(("run", self.listed()))
        # The ids a scripted pass filed off the board. With `derive_on_hook`, the pre cycle hook
        # puts them on it, the way a board whose hook derives ready labels would.
        self.off_board, self.derive_on_hook = [], False

    def _run_test_pass(self, manifest_path, kind, cards=(), stopped_areas=(), plan_areas=(),
                       budget=None, model=None):
        call = {"manifest": manifest_path, "kind": kind, "cards": list(cards),
                "stopped": list(stopped_areas), "plan": list(plan_areas), "budget": budget,
                "model": model, "listed": self.listed()}
        self.passes.append(call)
        self.order.append(("pass", kind))
        script = self.pass_script.pop(0) if self.pass_script else {}
        if callable(script):
            script = script(call)
        number = len(self.passes)
        record = {"pass": number, "id": "pass-%d" % number, "kind": kind, "cards": list(cards),
                  "status": testloop.RAN, "reason": "", "findings": [], "filed": [],
                  "commented": [], "transcripts": {"test": "/t/pass-%d.test.jsonl" % number,
                                                   "filing": None},
                  "read_from": {"test": "/t/pass-%d.test.stdout.log" % number},
                  "exit_code": 0, "record_path": "/t/pass-%d.json" % number}
        record.update(script)
        for entry in record["filed"]:
            if entry.pop("unready", False):
                self.off_board.append(entry["id"])
            elif not entry.get("attended"):
                self.adapter.ready_cards.append(card(entry["id"]))
        return record

    def _derive(self, args, cwd, timeout):
        """The pre cycle hook of a board that derives ready labels: every card filed off the
        board so far reaches the ready list."""
        self.order.append(("pre_cycle", list(self.off_board)))
        self.adapter.ready_cards += [card(card_id) for card_id in self.off_board]
        self.off_board = []
        return subprocess.CompletedProcess(list(args), 0, "", "")

    def deps(self):
        deps = super().deps()
        deps.run_test_pass = self._run_test_pass
        if self.derive_on_hook:
            deps.run_command = self._derive
        return deps

    def loop_config(self, **loop):
        base = dict(batch=3)
        base.update(loop.pop("config", {}))
        return feeder.Config(test_loop=feeder.TestLoop(**dict(vars(LOOP_ON), **loop)), **base)

    def feed_loop(self, **loop):
        return self.feed(self.loop_config(**loop))

    def kinds(self):
        return [(call["kind"], call["cards"]) for call in self.passes]

    def loop(self):
        return self.state()["test_loop"]

    def seed(self, **loop):
        """Write a state file whose `test_loop` is `loop` over a fresh one, as a feeder that ran
        before would have left it."""
        state = feeder.new_state()
        state["test_loop"] = dict(feeder.new_loop_state(self.clock), **loop)
        self.write(self.paths.state, json.dumps(state))


def tour_filing(*entries, severity="high"):
    return {"findings": [finding(severity)], "filed": list(entries)}


class LoopOff(LoopCase):
    def test_with_the_loop_off_events_and_state_are_todays(self):
        # Covers AE8: the default sidecar, a Cycle that lands, and a drained queue after it.
        self.adapter.ready_cards = [card(1), card(2)]
        self.plans = [{}, {}]
        self.assertEqual(self.feed(), 0)
        self.assertEqual(self.passes, [])
        state = self.state()
        self.assertNotIn("test_loop", state)
        self.assertEqual(set(state), set(feeder.new_state()) | {"process", "last_event",
                                                                 "last_cycle"})
        self.assertLessEqual({event["event"] for event in self.events()}, TODAYS_EVENTS)
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_the_loop_off_never_touches_run_test_pass(self):
        # A Deps with no test pass at all is today's Deps, and the loop off never reaches it.
        self.adapter.ready_cards = [card(1)]
        self.plans = [{}]
        deps = FeederCase.deps(self)
        self.assertIsNone(deps.run_test_pass)
        loop = feeder.Feeder(self.paths, feeder.Config(), deps, self.base_env(), io.StringIO())
        self.assertEqual(loop.run(), 0)
        self.assertEqual(self.runs, [["1"]])

    def test_a_dry_run_with_the_loop_on_starts_no_pass(self):
        self.adapter.ready_cards = [card(1)]
        self.assertEqual(self.feed(self.loop_config(), dry_run=True), 0)
        self.assertEqual(self.passes, [])


class CallSites(LoopCase):
    def test_the_first_cycle_runs_a_tour_before_appending(self):
        self.adapter.ready_cards = [card(1)]
        self.pass_script = [tour_filing(filed(10))]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.order[0], ("pass", testloop.TOUR))
        self.assertEqual(self.passes[0]["listed"], [])
        # The tour's own card reached the first batch, because the ready read came after it.
        self.assertEqual(self.runs[0], ["1", "10"])
        self.assertEqual(self.loop()["rounds"], 1)

    def test_a_landed_generation_1_card_is_checked_and_a_generation_2_card_is_not(self):
        # Covers AE1 too: the check of 10 files 11, which is the last generation.
        self.pass_script = [tour_filing(filed(10)),
                            {"findings": [finding(card_id="10")], "filed": [filed(11, parent="10")]}]
        self.plans = [{}, {}]
        self.feed_loop()
        self.assertEqual(self.kinds(), [(testloop.TOUR, []), (testloop.CHECK, ["10"])])
        self.assertEqual(self.runs, [["10"], ["10", "11"]])
        loop = self.loop()
        self.assertEqual(loop["filed"]["10"]["generation"], 1)
        self.assertEqual(loop["filed"]["11"]["generation"], 2)
        self.assertIn("landed only last generation cards [11]", self.log_text())

    def test_a_check_takes_only_the_landed_cards_and_the_budget_left(self):
        self.adapter.ready_cards = [card(1), card(2)]
        self.pass_script = [tour_filing(filed(10))]
        self.plans = [{"2": "halted"}]
        self.feed_loop()
        check = self.passes[1]
        self.assertEqual(check["kind"], testloop.CHECK)
        self.assertEqual(sorted(check["cards"]), ["1", "10"])
        self.assertEqual(check["budget"], 29)
        self.assertEqual(self.passes[0]["budget"], 30)

    def test_a_drained_queue_tours_and_goes_round_when_the_tour_filed(self):
        self.pass_script = [tour_filing(filed(10)), {}, tour_filing(filed(12))]
        self.plans = [{}, {}]
        self.feed_loop()
        self.assertEqual(self.kinds(), [(testloop.TOUR, []), (testloop.CHECK, ["10"]),
                                        (testloop.TOUR, []), (testloop.CHECK, ["12"])])
        self.assertEqual(self.runs, [["10"], ["10", "12"]])
        self.assertIn("the drain tour filed [12], going round", self.log_text())
        self.assertEqual(self.loop()["rounds"], 2)

    def test_a_drain_tour_with_only_lows_stops_clean_and_the_feeder_leaves(self):
        # Covers AE2.
        self.pass_script = [tour_filing(filed(10)), {},
                            {"findings": [finding("low"), finding("low", area="Cart")]}]
        self.plans = [{}, {}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_CLEAN)
        stopped = self.events(feeder.EVENT_TEST_LOOP_STOPPED)
        self.assertEqual([event["reason"] for event in stopped], [testloop.STOP_CLEAN])
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")
        self.assertTrue(any("stopped, clean" in note for note in self.notes), self.notes)

    def test_every_pass_writes_one_test_pass_event_with_its_transcript(self):
        self.pass_script = [tour_filing(filed(10))]
        self.plans = [{}]
        self.feed_loop()
        events = self.events(feeder.EVENT_TEST_PASS)
        self.assertEqual([(event["kind"], event["status"]) for event in events],
                         [(testloop.TOUR, testloop.RAN), (testloop.CHECK, testloop.RAN)])
        self.assertEqual(events[0]["filed"], ["10"])
        self.assertEqual(events[0]["transcripts"]["test"], "/t/pass-1.test.jsonl")
        self.assertEqual(events[0]["read_from"], {"test": "/t/pass-1.test.stdout.log"})
        self.assertEqual(events[0]["record_path"], "/t/pass-1.json")
        self.assertEqual(events[0]["pass_number"], 1)


class StopRules(LoopCase):
    def test_the_seventh_tour_is_never_started_with_max_rounds_6(self):
        counter = iter(range(100, 200))

        def tour_or_check(call):
            if call["kind"] == testloop.TOUR:
                return tour_filing(filed(next(counter)))
            return {}

        self.pass_script = [tour_or_check] * 40
        self.plans = [{}] * 20
        self.feed_loop()
        tours = [call for call in self.passes if call["kind"] == testloop.TOUR]
        self.assertEqual(len(tours), 6)
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_ROUNDS)
        # The sixth tour's card was still built, and never checked: the loop had stopped.
        self.assertIn("105", self.runs[-1])
        self.assertNotIn(["105"], [call["cards"] for call in self.passes])

    def test_a_loop_started_24_hours_ago_stops_at_the_next_pass_point(self):
        self.seed(started_at=(self.clock - timedelta(hours=24)).isoformat(timespec="seconds"),
                  rounds=1)
        self.adapter.ready_cards = [card(1)]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.passes, [])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_CLOCK)
        self.assertEqual(self.runs, [["1"]])

    def test_report_only_runs_one_tour_and_stops_the_loop(self):
        # Covers AE6.
        self.adapter.ready_cards = [card(1)]
        self.pass_script = [{"findings": [finding(), finding(area="Cart")], "report_only": True}]
        self.plans = [{}, {}]
        self.feed_loop(report_only=True)
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_REPORT_ONLY)
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_a_stopped_loop_starts_no_pass_while_the_feeder_builds_what_it_filed(self):
        # Two cause files, so U7's same file rule does not split the batch.
        self.pass_script = [tour_filing(filed(10), filed(11, cause="src/cart.py"))]
        self.plans = [{}, {}]
        self.feed_loop(max_cards_total=2)
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_BUDGET)
        self.assertEqual(self.runs, [["10", "11"]])
        self.assertEqual(len(self.events(feeder.EVENT_TEST_LOOP_STOPPED)), 1)

    def test_a_tour_whose_findings_all_went_to_open_cards_stops_on_open_findings(self):
        # Covers AE10 at the Feeder: commented, not filed, is no new card.
        # Issue #120: three findings may comment on one card, and the pass entry names the
        # card once, in block order.
        self.pass_script = [{"findings": [finding(), finding(), finding()],
                             "commented": [{"id": "7", "finding": 1}, {"id": "8", "finding": 2},
                                           {"id": "7", "finding": 3}]}]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_OPEN_FINDINGS)
        self.assertEqual(self.loop()["passes"][0]["commented"], ["7", "8"])

    def test_a_tour_that_filed_only_a_planning_card_beside_dropped_findings_stops_on_open_findings(self):
        """Issue #115: the attended planning card is a person's, not a new card. Without this
        the tour read as productive, the drain tour found no ready card, and the feeder left
        on the empty queue with no stop record and no notice."""
        # A loop that toured before, with an empty queue, so the one pass is the drain tour.
        self.seed(rounds=1, stopped_areas=["Search"])
        planning = dict(finding(title="Plan the Search area after repeated patches"),
                        attended=True, cause_file="docs/tour.md")
        dropped = dict(finding(), outcome=testloop.DROPPED)
        self.pass_script = [{"findings": [dropped, planning],
                             "filed": [filed(14, attended=True, cause="docs/tour.md")]}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_OPEN_FINDINGS)
        self.assertEqual(self.loop()["passes"][0]["filed"], ["14"])
        self.assertTrue(self.loop()["filed"]["14"]["attended"])
        self.assertEqual(len(self.events(feeder.EVENT_TEST_LOOP_STOPPED)), 1)
        self.assertTrue(any("stopped, open_findings" in note for note in self.notes), self.notes)

    def test_a_tour_with_only_lows_and_a_planning_card_stops_clean(self):
        self.seed(rounds=1, stopped_areas=["Search"])
        planning = dict(finding(title="Plan the Search area after repeated patches"),
                        attended=True, cause_file="docs/tour.md")
        self.pass_script = [{"findings": [finding("low", area="Cart"), planning],
                             "filed": [filed(14, attended=True, cause="docs/tour.md")]}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_CLEAN)
        self.assertEqual(self.loop()["planned_areas"], ["Search"])

    def test_a_planning_card_counts_toward_neither_the_budget_nor_the_budget_stop(self):
        """R17's budget is spent by the cards the loop asks the feeder to build; the planning
        card is filed outside the per pass cap and outside the budget too."""
        self.seed(rounds=1, stopped_areas=["Search"])
        planning = dict(finding(title="Plan the Search area after repeated patches"),
                        attended=True, cause_file="docs/tour.md")
        self.pass_script = [{"findings": [finding(area="Cart"), planning],
                             "filed": [filed(14, attended=True, cause="docs/tour.md"),
                                       filed(15, area="Cart", cause="src/cart.py")]},
                            {}]
        self.plans = [{}]
        self.feed_loop(max_cards_total=2)
        tour, check = self.passes[:2]
        self.assertEqual(tour["budget"], 2)
        # One card against a budget of two after the tour, so the check has one left and the
        # loop did not stop on the budget.
        self.assertEqual((check["kind"], check["cards"], check["budget"]),
                         (testloop.CHECK, ["15"], 1))
        self.assertEqual(feeder.loop_cards_filed(self.loop()), 1)
        self.assertEqual(set(self.loop()["filed"]), {"14", "15"})
        self.assertNotEqual((self.loop()["stop"] or {}).get("reason"), testloop.STOP_BUDGET)


class Areas(LoopCase):
    def test_an_area_at_the_patch_cap_is_stopped_and_planned_once(self):
        # Covers AE7. Cards 10 and 11 were checked with a finding in Search already; the check
        # of 12 files a third.
        loop_filed = {str(n): {"generation": 1, "area": "Search", "design": False,
                               "cause_file": "src/search.py"} for n in (10, 11, 12)}
        self.seed(rounds=1, filed=loop_filed, checks={"10": ["Search"], "11": ["Search"]})
        self.adapter.ready_cards = [card(12)]
        self.pass_script = [
            {"findings": [finding(card_id="12")], "filed": [filed(13, parent="12", unready=True)]},
            {"findings": [finding(area="Cart")],
             "filed": [filed(14, attended=True), filed(15, area="Cart")]},
            {}]
        self.plans = [{}, {}]
        self.feed_loop()
        check, tour, last = self.passes
        self.assertEqual((check["stopped"], check["plan"]), ([], []))
        self.assertEqual((tour["stopped"], tour["plan"]), (["Search"], ["Search"]))
        self.assertEqual((last["kind"], last["cards"]), (testloop.CHECK, ["15"]))
        self.assertEqual((last["stopped"], last["plan"]), (["Search"], []))
        loop = self.loop()
        self.assertEqual(loop["patches"], {"Search": 3})
        self.assertEqual(loop["stopped_areas"], ["Search"])
        self.assertEqual(loop["planned_areas"], ["Search"])
        self.assertTrue(any("Search area took 3 patches" in note for note in self.notes))
        # The attended planning card is a person's: no notice that the ready source lacks it.
        # 13 is left off the board by this case, and is the one card such a notice names.
        unready = [note for note in self.notes if "ready source does not return" in note]
        self.assertEqual(len(unready), 1, self.notes)
        self.assertIn("filed 13 ", unready[0])
        self.assertNotIn("14", unready[0])

    def test_a_stopped_area_no_longer_a_heading_is_not_passed_on(self):
        # The fixture repository's README is the tour document here: its one heading is
        # "fixture", so a stopped "Search" left from an older tour document would be refused by
        # `relay test` at every pass.
        self.seed(rounds=1, stopped_areas=["Search", "fixture"])
        self.adapter.ready_cards = [card(1)]
        self.plans = [{}]
        self.feed_loop(tour="README.md")
        self.assertEqual((self.passes[0]["stopped"], self.passes[0]["plan"]),
                         (["fixture"], ["fixture"]))
        self.assertTrue(any("Search are no longer headings" in note for note in self.notes))

    def test_a_planning_card_confirmed_on_a_pass_that_then_failed_is_not_asked_for_again(self):
        """Code review on issue #115: on a tracker outside the checkout the planning card exists
        when the filing then fails on a scope reset, so the area is planned and the next tour
        does not file a second card for it."""
        self.seed(rounds=1, stopped_areas=["Search"])
        self.adapter.ready_cards = [card(1), card(2)]
        failed = {"status": testloop.FAILED, "findings": [finding(area="Cart")],
                  "reason": "the filing process changed src/x.py in the checkout, outside any "
                            "path; the checkout was reset to abc and nothing it filed there counts",
                  "filed": [filed(14, attended=True, cause="docs/tour.md")]}
        self.pass_script = [failed, {}]
        self.plans = [{"2": "halted"}, {"2": "landed"}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["plan"] for call in self.passes], [["Search"], []])
        self.assertEqual(self.loop()["planned_areas"], ["Search"])
        self.assertEqual(self.loop()["passes"][0]["planned"], ["Search"])
        self.assertEqual(self.loop()["passes"][0]["status"], testloop.FAILED)

    def test_a_plan_area_on_a_pass_that_did_not_run_is_asked_for_again(self):
        self.seed(rounds=1, stopped_areas=["Search"])
        self.adapter.ready_cards = [card(1), card(2)]
        self.pass_script = [{"status": testloop.NOT_RUN, "reason": "prepare exited 1"}, {}]
        self.plans = [{"2": "halted"}, {"2": "landed"}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["plan"] for call in self.passes], [["Search"], ["Search"]])
        self.assertEqual(self.loop()["planned_areas"], ["Search"])


class Failures(LoopCase):
    def test_a_not_run_pass_is_notified_once_and_is_no_round(self):
        self.adapter.ready_cards = [card(1), card(2)]
        not_run = {"status": testloop.NOT_RUN, "reason": "prepare exited 1; last output: down"}
        self.pass_script = [not_run, {}, not_run, {}]
        self.plans = [{}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual(self.kinds(), [(testloop.TOUR, []), (testloop.CHECK, ["1"]),
                                        (testloop.TOUR, []), (testloop.CHECK, ["2"])])
        self.assertEqual(self.loop()["rounds"], 0)
        self.assertIsNone(self.loop()["stop"])
        notices = [note for note in self.notes if "was not run" in note]
        self.assertEqual(len(notices), 1, self.notes)
        self.assertIn("prepare exited 1", notices[0])

    def test_a_streak_of_failures_is_notified_once_per_kind_and_every_pass_is_logged(self):
        # The reasons differ, as a real one's last output line does, and still one notice each
        # for the tour and the check: never one per pass.
        self.adapter.ready_cards = [card(1), card(2)]
        self.pass_script = [{"status": testloop.FAILED, "reason": "timed out at 09:01"},
                            {"status": testloop.FAILED, "reason": "no report block, pass 2"},
                            {"status": testloop.FAILED, "reason": "timed out at 10:12"},
                            {"status": testloop.FAILED, "reason": "no report block, pass 4"}]
        self.plans = [{}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        failed = [note for note in self.notes if "test pass failed" in note]
        self.assertEqual(len(failed), 2, self.notes)
        self.assertIn("timed out at 10:12", self.log_text())
        reported = [key for key in self.state()["reported"] if key.startswith("test_pass:")]
        self.assertEqual(sorted(reported), ["test_pass:check:failed", "test_pass:tour:failed"])

    def test_a_failed_filing_step_is_notified_once_counts_no_round_and_does_not_stop_the_loop(self):
        """Issue #115: a pass whose Filing step did not complete comes back `failed` with the
        filing sentence and serious findings and no filed card. It is not a tour whose findings
        produced no card, so the loop goes on, and the next tour is asked for the same thing."""
        self.adapter.ready_cards = [card(1), card(2)]
        failed = {"status": testloop.FAILED, "findings": [finding(), finding(area="Cart")],
                  "reason": "the filing process timed out after 600 seconds"}
        self.pass_script = [failed, {}, tour_filing(filed(10))]
        self.plans = [{}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        # A tour that failed is no tour that ran, so the next cycle tours again first, the way
        # it does after a `not_run` tour.
        self.assertEqual(self.kinds()[:3], [(testloop.TOUR, []), (testloop.CHECK, ["1"]),
                                            (testloop.TOUR, [])])
        self.assertTrue(any("10" in run for run in self.runs), self.runs)
        loop = self.loop()
        self.assertEqual(loop["passes"][0]["status"], testloop.FAILED)
        self.assertEqual(loop["passes"][0]["reason"], "the filing process timed out after 600 seconds")
        self.assertEqual(loop["passes"][0]["filed"], [])
        self.assertEqual(loop["passes"][0]["planned"], [])
        ran_tours = [entry for entry in loop["passes"]
                     if entry["kind"] == testloop.TOUR and entry["status"] == testloop.RAN]
        self.assertEqual(loop["rounds"], len(ran_tours))
        self.assertGreaterEqual(len(ran_tours), 1)
        stopped = self.events(feeder.EVENT_TEST_LOOP_STOPPED)
        self.assertNotIn(testloop.STOP_OPEN_FINDINGS, [event["reason"] for event in stopped])
        notices = [note for note in self.notes if "tour test pass failed" in note]
        self.assertEqual(len(notices), 1, self.notes)
        self.assertIn("the filing process timed out", notices[0])

    def test_a_failure_after_a_pass_of_that_kind_ran_is_news_again(self):
        self.adapter.ready_cards = [card(1), card(2)]
        failed = {"status": testloop.FAILED, "reason": "the test process timed out"}
        self.pass_script = [tour_filing(filed(10)), failed, {}, failed]
        self.plans = [{}, {}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        # The failed check of 1 carries it to the next check (issue #116).
        self.assertEqual([call["cards"] for call in self.passes[1:4]],
                         [["1"], ["1", "2"], ["10"]])
        self.assertEqual(len([note for note in self.notes if "test pass failed" in note]), 2)

    def test_a_start_tour_that_did_not_run_is_not_repeated_before_leaving(self):
        # The queue is empty and nothing has run since the start tour, so a drain tour would
        # only try the same commit again.
        self.pass_script = [{"status": testloop.NOT_RUN, "reason": "prepare exited 1"}]
        self.plans = [{}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertIn("a tour already ran since the last run", self.log_text())
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_a_check_that_did_not_run_carries_its_cards_to_the_next_check(self):
        self.adapter.ready_cards = [card(1), card(2)]
        self.pass_script = [tour_filing(filed(10)),
                            {"status": testloop.NOT_RUN, "reason": "prepare exited 1"}, {}]
        self.plans = [{}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:3]], [["1"], ["1", "2"]])
        self.assertEqual(self.loop()["unchecked"], [])

    def test_a_check_that_ran_and_failed_carries_its_cards_to_the_next_check(self):
        """Issue #116: a check whose Test process timed out or lost the Lease left a record,
        and its landed card is checked at the next pass point, not left to a later tour."""
        self.adapter.ready_cards = [card(1), card(2)]
        self.pass_script = [tour_filing(filed(10)),
                            {"status": testloop.FAILED,
                             "reason": "the test process timed out after 3600 seconds"}, {}]
        self.plans = [{}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:3]], [["1"], ["1", "2"]])
        self.assertEqual(self.loop()["unchecked"], [])
        self.assertIn("the check of [1] failed, [1] waits for the next check", self.log_text())
        self.assertEqual(len([note for note in self.notes if "check test pass failed" in note]),
                         1, self.notes)
        self.assertEqual(self.loop()["retried"], [])

    def test_a_card_whose_check_fails_twice_is_carried_once_then_dropped(self):
        # Code review on #116: a card whose area hangs the app would otherwise be carried into
        # every check after it, and every one of them would time out.
        self.adapter.ready_cards = [card(1), card(2), card(3)]
        timed_out = {"status": testloop.FAILED,
                     "reason": "the test process timed out after 3600 seconds"}
        self.pass_script = [tour_filing(filed(10)), dict(timed_out), dict(timed_out), {}]
        self.plans = [{}, {}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:4]],
                         [["1"], ["1", "2"], ["2", "3"]])
        self.assertIn("[1] failed a check twice and is not checked", self.log_text())
        self.assertEqual(self.loop()["retried"], [])

    def test_a_killed_verb_with_no_record_is_carried_not_dropped(self):
        # A signal leaves no record either, and says nothing about the cards.
        self.adapter.ready_cards = [card(1), card(2)]
        killed = {"status": testloop.FAILED, "exit_code": -15, "record_path": None,
                  "reason": "relay test exited -15: no output"}
        self.pass_script = [tour_filing(filed(10)), killed, {}]
        self.plans = [{}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:3]], [["1"], ["1", "2"]])

    def test_a_check_refused_with_no_record_drops_its_cards_with_one_notice(self):
        # `relay test` refuses a card it cannot read before any pass starts, exit 1 and no
        # record; carried, that card would refuse every check after it.
        self.adapter.ready_cards = [card(1), card(2), card(3)]
        refused = {"status": testloop.FAILED, "exit_code": 1, "record_path": None,
                   "reason": "relay test exited 1: card 1 could not be read: gone"}
        self.pass_script = [tour_filing(filed(10)), refused, dict(refused), {}]
        self.plans = [{}, {}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:4]], [["1"], ["2"], ["3"]])
        self.assertIn("the check of [1] was refused before a pass started", self.log_text())
        notices = [note for note in self.notes if "check test pass failed" in note]
        self.assertEqual(len(notices), 1, self.notes)

    def test_a_held_post_cycle_hook_runs_no_check_and_the_cards_wait(self):
        self.adapter.ready_cards = [card(1)]
        self.pass_script = [tour_filing(filed(10))]
        self.plans = [{}, {}]
        config = self.loop_config(config={"post_cycle_command": ("false",),
                                          "post_cycle_hold": True})
        self.assertEqual(self.feed(config), feeder.EXIT_HALTED)
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(sorted(self.loop()["unchecked"]), ["1", "10"])

    def test_a_drain_tour_whose_cards_the_ready_source_lacks_notifies_once_and_leaves(self):
        self.pass_script = [tour_filing(filed(10)), {}, tour_filing(filed(11, unready=True))]
        self.plans = [{}, {}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual([kind for kind, _ in self.kinds()],
                         [testloop.TOUR, testloop.CHECK, testloop.TOUR])
        notices = [note for note in self.notes if "ready source does not return" in note]
        self.assertEqual(len(notices), 1)
        self.assertIn("11", notices[0])
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_a_record_with_an_unknown_status_reads_as_failed(self):
        self.pass_script = [{"status": "exploded"}]
        self.plans = [{}]
        self.adapter.ready_cards = [card(1)]
        self.feed_loop()
        self.assertEqual(self.loop()["passes"][0]["status"], testloop.FAILED)
        self.assertEqual(self.loop()["rounds"], 0)


class DerivedReady(LoopCase):
    """Issue #116: on a board whose `pre_cycle` hook derives ready labels, a filed card is
    ready only after that hook, so the ready read that judges a pass's cards follows it."""

    def setUp(self):
        super().setUp()
        self.derive_on_hook = True

    def derived_config(self, **config):
        return self.loop_config(config=dict(config, pre_cycle_command=("derive",)))

    def test_a_start_tour_card_the_hook_makes_ready_is_built_in_the_first_batch(self):
        self.adapter.ready_cards = [card(1)]
        self.pass_script = [tour_filing(filed(10, unready=True))]
        self.plans = [{}]
        self.feed(self.derived_config())
        self.assertEqual(self.order[:3], [("pass", testloop.TOUR), ("pre_cycle", ["10"]),
                                          ("run", ["1", "10"])])
        self.assertEqual([note for note in self.notes if "ready source does not return" in note],
                         [])
        self.assertEqual(self.loop()["awaiting_ready"], [])

    def test_a_drain_tour_card_the_hook_makes_ready_is_built_instead_of_leaving(self):
        self.seed(rounds=1)
        self.pass_script = [tour_filing(filed(12, unready=True)), {}]
        self.plans = [{}]
        self.feed(self.derived_config())
        self.assertEqual(self.runs, [["12"]])
        self.assertEqual(self.kinds(), [(testloop.TOUR, []), (testloop.CHECK, ["12"])])
        self.assertIn("the drain tour filed [12], going round", self.log_text())
        self.assertEqual([note for note in self.notes if "ready source does not return" in note],
                         [])

    def test_a_drain_tour_card_the_hook_does_not_make_ready_is_notified_and_the_feeder_leaves(self):
        # The hook runs and still leaves the card off the board: one notice, no second tour.
        self.derive_on_hook = False
        self.seed(rounds=1)
        self.pass_script = [tour_filing(filed(12, unready=True))]
        self.feed(self.loop_config(config={"pre_cycle_command": ("true",)}))
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(self.runs, [])
        notices = [note for note in self.notes if "ready source does not return" in note]
        self.assertEqual(len(notices), 1, self.notes)
        self.assertIn("filed 12 ", notices[0])
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_an_unready_drain_tour_card_costs_no_fresh_idle_waits(self):
        # Code review on #116: going round must not reset the idle count, or a card the ready
        # source never returns would buy every idle wait again before the feeder leaves.
        self.derive_on_hook = False
        self.seed(rounds=1)
        self.pass_script = [tour_filing(filed(12, unready=True))]
        self.feed(self.loop_config(config={"idle_waits_max": 2}))
        self.assertEqual(self.sleeps, [1800, 1800])
        self.assertEqual(self.kinds(), [(testloop.TOUR, [])])
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_a_failed_hook_judges_nothing_and_keeps_the_cards_for_a_clean_read(self):
        # Code review on #116: a read after a failed hook is no evidence about derived labels,
        # so no notice, and the card waits for a read that is.
        self.derive_on_hook = False
        self.adapter.ready_cards = [card(1)]
        self.pass_script = [tour_filing(filed(10, unready=True))]
        self.plans = [{}]
        self.feed(self.loop_config(config={"pre_cycle_command": ("false",)}))
        self.assertEqual(self.runs, [["1"]])
        self.assertEqual([note for note in self.notes if "ready source does not return" in note],
                         [])
        self.assertEqual(self.loop()["awaiting_ready"], ["10"])
        loop = feeder.Feeder(self.paths, self.loop_config(), self.deps(), self.base_env(),
                             io.StringIO())
        loop.state = loop._load_state()
        loop.check_awaiting([card(1), card(10)])
        self.assertEqual(loop.state["test_loop"]["awaiting_ready"], [])
        self.assertEqual([note for note in self.notes if "ready source does not return" in note],
                         [])


class Untoured(LoopCase):
    """Issue #121: a tour that could not reach every area is never read as clean, and the
    feeder notifies once naming the areas."""

    PARTIAL = {"findings": [finding("low"), finding("low", area="Cart")],
               "untoured": ["Invoices", "Settings"],
               "reason": "Invoices and Settings sit behind a sign in"}

    def test_a_drain_tour_that_missed_areas_does_not_stop_the_loop_clean_and_notifies_once(self):
        # The live shape: a drain tour with only lows that saw three of ten areas. Without the
        # key it stopped the loop on the clean reason.
        self.pass_script = [tour_filing(filed(10)), {}, dict(self.PARTIAL)]
        self.plans = [{}, {}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual([kind for kind, _ in self.kinds()],
                         [testloop.TOUR, testloop.CHECK, testloop.TOUR])
        loop = self.loop()
        self.assertIsNone(loop["stop"])
        self.assertEqual(self.events(feeder.EVENT_TEST_LOOP_STOPPED), [])
        self.assertEqual(loop["rounds"], 2)
        self.assertEqual(loop["passes"][-1]["untoured"], ["Invoices", "Settings"])
        self.assertEqual(self.events(feeder.EVENT_TEST_PASS)[-1]["untoured"],
                         ["Invoices", "Settings"])
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")
        notices = [note for note in self.notes if "could not reach" in note]
        self.assertEqual(len(notices), 1, self.notes)
        self.assertIn("Invoices, Settings", notices[0])
        self.assertIn("never read as clean", notices[0])
        self.assertIn("untoured [Invoices, Settings]", self.log_text())
        self.assertIn("sit behind a sign in", self.log_text())

    def tours(self, *scripts):
        """Script the tours in order and answer every check with a pass that found nothing,
        whatever order the feeder interleaves them in."""
        queue = list(scripts)
        self.pass_script = [lambda call: (dict(queue.pop(0)) if queue else {})
                            if call["kind"] == testloop.TOUR else {}] * 20

    def test_the_same_missed_areas_on_two_tours_notify_once(self):
        # The start tour and the drain tour both miss the same areas: one notice, not two.
        self.adapter.ready_cards = [card(1)]
        self.tours(self.PARTIAL, self.PARTIAL)
        self.plans = [{}, {}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual([kind for kind, _ in self.kinds()],
                         [testloop.TOUR, testloop.CHECK, testloop.TOUR])
        notices = [note for note in self.notes if "could not reach" in note]
        self.assertEqual(len(notices), 1, self.notes)
        self.assertIsNone(self.loop()["stop"])
        self.assertIn("test_pass:tour:untoured", self.state()["reported"])

    def test_a_full_tour_between_makes_the_same_missed_areas_news_again(self):
        # The start tour misses them, the drain tour reaches every area and files a card, and
        # the drain tour after that misses them again: two notices.
        self.adapter.ready_cards = [card(1)]
        self.tours(self.PARTIAL, tour_filing(filed(10)), self.PARTIAL)
        self.plans = [{}, {}, {}]
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual([kind for kind, _ in self.kinds()],
                         [testloop.TOUR, testloop.CHECK, testloop.TOUR, testloop.CHECK,
                          testloop.TOUR])
        notices = [note for note in self.notes if "could not reach" in note]
        self.assertEqual(len(notices), 2, self.notes)

    def test_a_partial_tour_with_a_serious_finding_that_made_no_card_stops_on_open_findings(self):
        partial = dict(self.PARTIAL, findings=[finding()], commented=[{"id": "7", "finding": 1}])
        self.pass_script = [tour_filing(filed(10)), {}, partial]
        self.plans = [{}, {}]
        self.feed_loop()
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_OPEN_FINDINGS)

    def test_a_not_run_tour_that_reached_no_area_is_notified_as_not_run_and_no_round(self):
        # `relay test` records a report that lists every area as `not_run`; the feeder reads
        # that record the way it reads any pass that did not run.
        not_run = {"status": testloop.NOT_RUN, "untoured": ["Search", "Invoices"],
                   "reason": "the test process could reach no area of the tour document: "
                             "every page was the sign in form"}
        self.pass_script = [not_run]
        self.plans = [{}]
        self.adapter.ready_cards = [card(1)]
        self.feed_loop()
        self.assertEqual(self.loop()["rounds"], 0)
        self.assertIsNone(self.loop()["stop"])
        self.assertEqual(len([note for note in self.notes if "was not run" in note]), 1)
        self.assertEqual([note for note in self.notes if "could not reach" in note], [])

    def unreadable(self, value):
        """Code review: the pass checked every name before writing the list, so any other
        shape is a record the loop cannot read, and it fails closed like an unknown status,
        counting no round and never stopping the loop clean."""
        self.pass_script = [tour_filing(filed(10)), {}, {"untoured": value}]
        self.plans = [{}, {}]
        self.feed_loop()
        entry = self.loop()["passes"][-1]
        self.assertEqual(entry["status"], testloop.FAILED)
        self.assertIn("untoured list is unreadable", entry["reason"])
        self.assertEqual(entry["untoured"], [])
        self.assertIsNone(self.loop()["stop"])
        self.assertEqual(self.loop()["rounds"], 1)

    def test_a_record_whose_untoured_is_a_string_reads_as_failed_never_as_clean(self):
        self.unreadable("Invoices")

    def test_a_record_whose_untoured_holds_a_null_reads_as_failed(self):
        self.unreadable(["Invoices", None])

    def test_a_record_whose_untoured_holds_a_number_reads_as_failed(self):
        self.unreadable([1])

    def test_the_same_missed_areas_with_a_reworded_reason_notify_once(self):
        # Code review: a process words its reason anew each pass, so the notice carries the
        # areas alone and the log keeps each reason.
        self.adapter.ready_cards = [card(1)]
        reworded = dict(self.PARTIAL, reason="the sign in wall still hides two areas")
        self.tours(self.PARTIAL, reworded)
        self.plans = [{}, {}]
        self.assertEqual(self.feed_loop(), 0)
        notices = [note for note in self.notes if "could not reach" in note]
        self.assertEqual(len(notices), 1, self.notes)
        self.assertNotIn("sign in", notices[0])
        self.assertIn("the log has the reason", notices[0])
        self.assertIn("still hides two areas", self.log_text())

    def test_a_partial_check_goes_on_and_is_notified_once_by_its_own_kind(self):
        self.pass_script = [tour_filing(filed(10)),
                            {"untoured": ["Search"], "reason": "Search never loaded"}]
        self.plans = [{}, {}]
        self.feed_loop()
        self.assertEqual(self.kinds()[1], (testloop.CHECK, ["10"]))
        self.assertEqual(self.loop()["unchecked"], [])
        notices = [note for note in self.notes if "check test pass could not reach" in note]
        self.assertEqual(len(notices), 1, self.notes)


class Models(LoopCase):
    def marked(self, model="opus", hours=3):
        return {model: {"since": self.clock.isoformat(timespec="seconds"),
                        "until": (self.clock + timedelta(hours=hours)).isoformat(
                            timespec="seconds"), "source": "fallback_hours"}}

    def test_a_pass_while_the_loop_model_is_marked_runs_on_its_fallback(self):
        state = feeder.new_state()
        state["exhausted"] = self.marked()
        self.write(self.paths.state, json.dumps(state))
        self.adapter.ready_cards = [card(1)]
        self.plans = [{}]
        self.feed(self.loop_config(config={"model_fallback": {"opus": "sonnet"}}))
        self.assertEqual([call["model"] for call in self.passes], ["sonnet"])

    def test_a_pass_whose_whole_chain_is_held_waits_for_the_next_pass_point(self):
        state = feeder.new_state()
        state["exhausted"] = self.marked()
        self.write(self.paths.state, json.dumps(state))
        self.adapter.ready_cards = [card(1, description="**Model:** sonnet")]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.passes, [])
        self.assertIn("waits for the next pass point: opus is held", self.log_text())
        self.assertEqual(self.runs, [["1"]])

    def test_a_drain_tour_withheld_for_a_held_loop_model_waits_for_it_under_model_held(self):
        """Issue #116: the queue is empty and the loop model is held with no free fallback.
        The feeder waits for the mark, not leave on the empty queue, then tours."""
        self.clock_moves = True
        self.seed(rounds=1)
        state = self.state()
        state["exhausted"] = self.marked(hours=0.25)
        self.write(self.paths.state, json.dumps(state))
        self.assertEqual(self.feed_loop(), 0)
        self.assertEqual(self.sleeps, [900])
        waits = self.events(feeder.EVENT_WAITING)
        self.assertEqual([(event["reason"], event["seconds"]) for event in waits],
                         [("model_held", 900)])
        self.assertIn("the drain tour waits 900s for a mark along the test pass model's chain",
                      self.log_text())
        # The mark expired during the wait, so the tour ran on the loop model, found nothing,
        # stopped the loop clean, and only then did the feeder leave.
        self.assertEqual([(call["kind"], call["model"]) for call in self.passes],
                         [(testloop.TOUR, "opus")])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_CLEAN)
        self.assertEqual(self.events(feeder.EVENT_LEAVING)[-1]["reason"], "empty_queue")

    def test_a_held_drain_tour_under_once_leaves_on_the_wait_not_the_empty_queue(self):
        self.seed(rounds=1)
        state = self.state()
        state["exhausted"] = self.marked()
        self.write(self.paths.state, json.dumps(state))
        self.assertEqual(self.feed(self.loop_config(), once=True), 0)
        self.assertEqual(self.passes, [])
        leaving = self.events(feeder.EVENT_LEAVING)[-1]
        self.assertEqual(leaving["reason"], "once")
        self.assertIn("model_held", leaving["message"])

    def test_the_loop_model_is_passed_when_nothing_is_marked(self):
        self.adapter.ready_cards = [card(1)]
        self.plans = [{}]
        self.feed_loop(model="sonnet")
        self.assertEqual({call["model"] for call in self.passes}, {"sonnet"})


class Restart(LoopCase):
    def test_rounds_the_filed_map_and_the_stop_record_survive_a_restart(self):
        self.pass_script = [tour_filing(filed(10))]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.kinds(), [(testloop.TOUR, []), (testloop.CHECK, ["10"])])
        os.unlink(self.paths.stop)
        # A second feeder reads the loop back: no second start tour, and 10 is known.
        self.adapter.ready_cards.append(card(11))
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.kinds()[2:], [(testloop.CHECK, ["11"])])
        loop = self.loop()
        self.assertEqual(loop["rounds"], 1)
        self.assertEqual(set(loop["filed"]), {"10"})
        self.assertEqual(len(loop["passes"]), 3)

    def test_a_stop_record_read_back_starts_no_pass(self):
        self.seed(rounds=2, stop={"reason": testloop.STOP_CLEAN, "at": "then", "pass": 4})
        self.adapter.ready_cards = [card(1)]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.passes, [])
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_CLEAN)
        self.assertEqual(self.runs, [["1"]])


class Helpers(unittest.TestCase):
    def test_pass_argv_puts_cards_last_and_names_every_flag(self):
        self.assertEqual(
            feeder.pass_argv(testloop.CHECK, cards=("10", 11), stopped_areas=("Search",),
                             plan_areas=("Search",), budget=4, model="sonnet"),
            ["--stopped-area", "Search", "--plan-area", "Search", "--budget", "4",
             "--model", "sonnet", "--cards", "10", "11"])
        self.assertEqual(feeder.pass_argv(testloop.TOUR, budget=0), ["--budget", "0", "--tour"])

    def test_the_pass_argv_parses_under_the_real_test_verb(self):
        from relay import cli
        args = cli.build_parser().parse_args(
            ["test", "m.toml"] + feeder.pass_argv(testloop.CHECK, cards=("10",),
                                                  stopped_areas=("Two words",), budget=3,
                                                  model="opus"))
        self.assertEqual((args.cards, args.stopped_areas, args.budget, args.model, args.tour),
                         (["10"], ["Two words"], 3, "opus", False))

    def test_read_pass_output_reads_the_record_the_last_line_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pass-3.json")
            with open(path, "w") as handle:
                json.dump({"pass": 3, "status": "not_run", "reason": "prepare exited 1"}, handle)
            record = feeder.read_pass_output(2, "pass 3 not_run: prepare exited 1\n%s\n" % path)
        self.assertEqual((record["pass"], record["status"], record["exit_code"],
                          record["record_path"]), (3, "not_run", 2, path))

    def test_read_pass_output_without_a_record_reads_as_not_run_or_failed(self):
        lease = feeder.read_pass_output(3, "another runner holds the lease: pid 9\n")
        self.assertEqual(lease["status"], testloop.NOT_RUN)
        self.assertIn("another runner holds the lease", lease["reason"])
        refused = feeder.read_pass_output(1, "test_loop.enabled needs test_loop.url\n")
        self.assertEqual(refused["status"], testloop.FAILED)
        self.assertIn("exited 1", refused["reason"])
        gone = feeder.read_pass_output(0, "/nowhere/pass-1.json\n")
        self.assertEqual(gone["status"], testloop.FAILED)
        self.assertIn("could not be read", gone["reason"])
        self.assertEqual(feeder.read_pass_output(0, "")["status"], testloop.FAILED)

    def test_the_real_run_test_pass_streams_the_output_and_reads_the_record(self):
        # A stand in for `relay_cli.py`: it prints its arguments and a record path, exits 2.
        with tempfile.TemporaryDirectory() as tmp:
            record = os.path.join(tmp, "pass-1.json")
            with open(record, "w") as handle:
                json.dump({"pass": 1, "status": "not_run", "reason": "prepare exited 1"}, handle)
            entry = os.path.join(tmp, "entry.py")
            with open(entry, "w") as handle:
                handle.write("import sys\nprint('args', ' '.join(sys.argv[1:]))\n"
                             "print(%r)\nsys.exit(2)\n" % record)
            sink = os.path.join(tmp, "out.log")
            deps_env = dict(os.environ)
            with open(sink, "wb") as out, mock.patch.object(feeder, "runner_entry",
                                                            return_value=entry):
                deps = feeder.build_deps(feeder.Config(caffeinate=False), deps_env,
                                         child_stdout=out)
                answer = deps.run_test_pass("/m.toml", testloop.CHECK, cards=("7",),
                                            budget=5, model="opus")
            with open(sink, encoding="utf-8") as handle:
                streamed = handle.read()
        self.assertEqual((answer["status"], answer["exit_code"], answer["record_path"]),
                         ("not_run", 2, record))
        self.assertIn("args test /m.toml --budget 5 --model opus --cards 7", streamed)
        self.assertIn(record, streamed)

    def test_every_stop_word_has_its_own_sentence(self):
        loop = feeder.new_loop_state(__import__("datetime").datetime(2026, 9, 28))
        words = (testloop.STOP_CLEAN, testloop.STOP_OPEN_FINDINGS, testloop.STOP_ROUNDS,
                 testloop.STOP_CLOCK, testloop.STOP_BUDGET, testloop.STOP_REPORT_ONLY)
        sentences = {feeder.loop_stop_sentence(word, loop, LOOP_ON) for word in words}
        self.assertEqual(len(sentences), len(words))
        self.assertFalse(any(word in sentences for word in words))


if __name__ == "__main__":
    unittest.main()

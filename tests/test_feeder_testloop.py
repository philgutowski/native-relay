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
                  "exit_code": 0, "record_path": "/t/pass-%d.json" % number}
        record.update(script)
        for entry in record["filed"]:
            if not entry.pop("unready", False) and not entry.get("attended"):
                self.adapter.ready_cards.append(card(entry["id"]))
        return record

    def deps(self):
        deps = super().deps()
        deps.run_test_pass = self._run_test_pass
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
        self.pass_script = [{"findings": [finding(), finding()],
                             "commented": [{"id": "7", "finding": 1}, {"id": "8", "finding": 2}]}]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_OPEN_FINDINGS)
        self.assertEqual(self.loop()["passes"][0]["commented"], ["7", "8"])


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

    def test_a_failure_after_a_pass_of_that_kind_ran_is_news_again(self):
        self.adapter.ready_cards = [card(1), card(2)]
        failed = {"status": testloop.FAILED, "reason": "the test process timed out"}
        self.pass_script = [tour_filing(filed(10)), failed, {}, failed]
        self.plans = [{}, {}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:4]], [["1"], ["2"], ["10"]])
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

    def test_a_check_that_failed_does_not_carry_its_cards(self):
        self.adapter.ready_cards = [card(1), card(2)]
        self.pass_script = [tour_filing(filed(10)),
                            {"status": testloop.FAILED, "reason": "card 1 could not be read"}, {}]
        self.plans = [{}, {}, {}]
        self.feed(self.loop_config(config={"batch": 1}))
        self.assertEqual([call["cards"] for call in self.passes[1:3]], [["1"], ["2"]])

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

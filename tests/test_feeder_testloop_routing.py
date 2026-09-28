"""U7 of the browser test loop plan: design routing, same file batching, and `feed --status`.

The routing and batching cases drive the real feeder loop over `test_feeder_testloop.LoopCase`,
whose fake `run_test_pass` answers each pass with a scripted pass record; a card a scripted pass
files lands on the fake board's ready list, as a card the Filing process created would. The
status cases seed a state file and a sidecar and read them through `feed --status`, the way an
operator does. Nothing here launches a process or reads a network.
"""
import io
import json
import unittest
from datetime import timedelta

import _paths
from relay import cli, feeder, testloop
from test_feeder import card
from test_feeder_testloop import LoopCase, finding

ON = ('[test_loop]\nenabled = true\ntour = "docs/tour.md"\nurl = "http://127.0.0.1:5173"\n'
      'prepare = ["scripts/serve-at.sh"]\n')


def filed(card_id, cause="src/search.py", design=False, parent=None):
    """One confirmed filed card as the pass record lists it, with its cause file and design
    flag, the two facts U7's rules read."""
    return {"id": str(card_id), "finding": 1, "area": "Search", "design": design,
            "cause_file": cause, "card": parent, "card_sent": parent is not None,
            "attended": False}


def tour(*entries):
    return {"findings": [finding()], "filed": list(entries)}


class DesignRouting(LoopCase):
    def test_a_design_card_routes_to_the_design_model(self):
        self.pass_script = [tour(filed(10, design=True), filed(11, cause="src/cart.py"))]
        self.plans = [{}]
        self.feed_loop(design_model="fable")
        self.assertEqual(self.runs[0], ["10", "11"])
        self.assertEqual(self.models(), {"10": "fable", "11": "opus"})

    def test_a_routing_file_line_wins_over_the_design_model(self):
        self.write(self.paths.routing, "10 sonnet\n")
        self.pass_script = [tour(filed(10, design=True))]
        self.plans = [{}]
        self.feed_loop(design_model="fable")
        self.assertEqual(self.models(), {"10": "sonnet"})

    def test_a_design_card_with_no_design_model_routes_as_today(self):
        self.pass_script = [tour(filed(10, design=True))]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.models(), {"10": "opus"})

    def test_the_design_model_wins_over_a_body_line(self):
        # Set above the body line on purpose, so it holds where a ready source returns no body.
        config = feeder.Config(test_loop=feeder.TestLoop(design_model="fable"))
        asking = card(10, description="**Model:** sonnet")
        self.assertEqual(feeder.choose_model(asking, {}, config, frozenset({"10"})),
                         ("fable", None))
        self.assertEqual(feeder.choose_model(asking, {"10": "opus"}, config, frozenset({"10"})),
                         ("opus", None))
        self.assertEqual(feeder.choose_model(asking, {}, config), ("sonnet", None))

    def test_a_card_the_loop_did_not_file_routes_as_today(self):
        self.adapter.ready_cards = [card(1, description="**Model:** sonnet"), card(2)]
        self.pass_script = [tour(filed(10, design=True))]
        self.plans = [{}]
        self.feed_loop(design_model="fable")
        self.assertEqual(self.models(), {"1": "sonnet", "2": "opus", "10": "fable"})

    def test_a_stopped_loop_still_routes_its_design_cards(self):
        # The feeder goes on building what a stopped loop filed (R20), on the design model.
        self.pass_script = [tour(filed(10, design=True))]
        self.plans = [{}]
        self.feed_loop(design_model="fable", max_cards_total=1)
        self.assertEqual(self.loop()["stop"]["reason"], testloop.STOP_BUDGET)
        self.assertEqual(self.models(), {"10": "fable"})


class SameFileBatching(LoopCase):
    def test_two_filed_cards_with_one_cause_file_never_share_a_batch(self):
        # Covers AE9: the first is appended, the second held until the first settles.
        self.pass_script = [tour(filed(10, cause="src/a.py"), filed(11, cause="src/a.py"))]
        self.plans = [{}, {}]
        self.feed_loop()
        self.assertEqual(self.runs, [["10"], ["10", "11"]])
        self.assertEqual(self.log_text().count("holding 11 out of this batch until 10 settles"),
                         1)
        self.assertIn("cause file src/a.py", self.log_text())

    def test_a_halted_card_still_holds_its_cause_file(self):
        # Listed and unsettled: a halted Task runs again, so the second waits for it.
        self.pass_script = [tour(filed(10, cause="src/a.py"), filed(11, cause="src/a.py"))]
        self.plans = [{"10": "halted"}, {}, {}]
        self.feed_loop()
        self.assertEqual(self.runs, [["10"], ["10"], ["10", "11"]])
        # Logged once for the hold, not once per Cycle it lasts through.
        self.assertEqual(self.log_text().count("holding 11 out of this batch"), 1)

    def test_a_held_card_takes_no_room_in_the_batch(self):
        self.pass_script = [tour(filed(10, cause="src/a.py"), filed(11, cause="src/a.py"),
                                 filed(12, cause="src/b.py"))]
        self.plans = [{}, {}]
        self.feed_loop(config={"batch": 2})
        self.assertEqual(self.runs[0], ["10", "12"])

    def test_a_filed_card_whose_file_matches_a_card_the_loop_did_not_file_is_not_held(self):
        # Card 1 fixes src/search.py too, but the loop did not file it, so it records no cause
        # file and holds nothing: both share the batch.
        self.adapter.ready_cards = [card(1, description="Cause: src/search.py line 3")]
        self.pass_script = [tour(filed(10, cause="src/search.py"))]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.runs[0], ["1", "10"])
        self.assertNotIn("holding", self.log_text())

    def test_a_refused_card_holds_no_cause_file(self):
        # 10 is refused on the model it routes to and dropped from the batch, so it never
        # settles; 11 shares its file and must not wait on it.
        self.write(self.paths.state, json.dumps(dict(feeder.new_state(),
                                                     refused={"10": "opus"})))
        self.pass_script = [tour(filed(10, cause="src/a.py"), filed(11, cause="src/a.py"))]
        self.plans = [{}]
        self.feed_loop()
        self.assertEqual(self.runs[0], ["11"])
        self.assertNotIn("holding", self.log_text())

    def test_one_file_spelled_two_ways_is_one_file(self):
        self.pass_script = [tour(filed(10, cause="src/a.py"), filed(11, cause="./src//a.py "))]
        self.plans = [{}, {}]
        self.feed_loop()
        self.assertEqual(self.runs, [["10"], ["10", "11"]])

    def test_the_card_a_held_card_waits_on_is_the_first_in_natural_order(self):
        filed_map = {task_id: {"cause_file": "src/a.py"} for task_id in ("9", "10", "11")}
        for unsettled in ({"10", "9"}, {"9", "10"}, ["10", "9"]):
            _, _, same_file = feeder.select([card(11)], {"9", "10"}, feeder.Config(), {}, 2, {},
                                            filed=filed_map, unsettled=unsettled)
            self.assertEqual(same_file, {"11": "9"})

    def test_cards_the_loop_did_not_file_batch_as_today(self):
        filed_map = {"10": {"cause_file": "src/a.py"}}
        cards = [card(1), card(2), card(3)]
        fresh, batch, same_file = feeder.select(cards, set(), feeder.Config(batch=3), {}, 0, {},
                                                filed=filed_map, unsettled={"10"})
        self.assertEqual([entry["id"] for entry in batch], ["1", "2", "3"])
        self.assertEqual(same_file, {})

    def test_select_holds_against_an_unsettled_listed_card(self):
        filed_map = {"10": {"cause_file": "src/a.py"}, "11": {"cause_file": "src/a.py"},
                     "12": {"cause_file": None}}
        cards = [card(11), card(12)]
        fresh, batch, same_file = feeder.select(cards, {"10"}, feeder.Config(batch=3), {}, 1, {},
                                                filed=filed_map, unsettled={"10"})
        self.assertEqual([entry["id"] for entry in fresh], ["11", "12"])
        self.assertEqual([entry["id"] for entry in batch], ["12"])
        self.assertEqual(same_file, {"11": "10"})


class Status(LoopCase):
    def call(self, *flags):
        args = cli.build_parser().parse_args(["feed", self.manifest_path] + list(flags))
        out = io.StringIO()
        code = cli.cmd_feed(args, self.base_env(), out, deps=self.deps())
        return code, out.getvalue()

    def seed_loop(self):
        self.seed(started_at=(self.clock - timedelta(hours=3)).isoformat(timespec="seconds"),
                  rounds=2,
                  passes=[{"pass": 1, "kind": "tour", "status": "ran", "filed": ["10", "11"]},
                          {"pass": 2, "kind": "check", "status": "ran", "filed": ["12"]},
                          {"pass": 3, "kind": "tour", "status": "not_run", "filed": []}],
                  filed={"10": {"generation": 1}, "11": {"generation": 1},
                         "12": {"generation": 2}},
                  stopped_areas=["Search"],
                  stop={"reason": testloop.STOP_ROUNDS, "message": "2 tours ran",
                        "at": "2026-09-19T08:40:00", "pass": 3})

    def test_with_the_loop_off_status_prints_no_loop_block(self):
        self.plans = [{}]
        self.feed()
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertNotIn("test loop", text)
        self.assertIsNone(json.loads(self.call("--status", "--json")[1])["test_loop"])

    def test_a_sidecar_that_switches_the_loop_off_shows_no_block_over_old_state(self):
        self.seed_loop()
        self.write(self.paths.config, ON.replace("enabled = true", "enabled = false"))
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertNotIn("test loop", text)

    def test_with_the_loop_on_status_prints_each_field(self):
        self.write(self.paths.config, ON + "report_only = true\nmax_rounds = 4\nmax_hours = 12\n")
        self.seed_loop()
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        # Measured to the stop at 08:40, not to the present, so a stopped loop's hours hold.
        self.assertIn("test loop: on, report only, round 2 of 4, 2.8 hours used since "
                      "2026-09-19T05:50:00, of 12", text)
        self.assertIn("test loop cards filed per pass: #1 tour ran 2, #2 check ran 1, "
                      "#3 tour not_run 0", text)
        self.assertIn("test loop filed cards: 3, generation 1: 2, generation 2: 1", text)
        self.assertIn("test loop stopped areas: [Search]", text)
        self.assertIn("test loop stopped at 2026-09-19T08:40:00, round_cap: 2 tours ran", text)
        loop = json.loads(self.call("--status", "--json")[1])["test_loop"]
        self.assertEqual((loop["enabled"], loop["report_only"], loop["rounds"],
                          loop["max_rounds"], loop["hours_used"], loop["max_hours"],
                          loop["cards_filed"], loop["generations"], loop["stopped_areas"],
                          loop["stop"]["reason"]),
                         (True, True, 2, 4, 2.8, 12, 3, {"1": 2, "2": 1}, ["Search"],
                          testloop.STOP_ROUNDS))

    def test_a_running_loop_counts_its_hours_to_the_present(self):
        self.write(self.paths.config, ON)
        self.seed(started_at=(self.clock - timedelta(hours=3)).isoformat(timespec="seconds"))
        self.assertIn("round 0 of 6, 3.0 hours used", self.call("--status")[1])

    def test_a_damaged_loop_state_is_shown_and_never_raised_on(self):
        self.write(self.paths.config, ON)
        state = feeder.new_state()
        state["test_loop"] = {"started_at": "yesterday", "rounds": 1,
                              "passes": [{"pass": 1, "kind": "tour", "status": "ran",
                                          "filed": 3}, "junk"],
                              "filed": {"10": {"generation": "one"}, "11": 5},
                              "stopped_areas": 5, "stop": "no"}
        self.write(self.paths.state, json.dumps(state))
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertIn("hours used unreadable, the start is 'yesterday'", text)
        self.assertIn("test loop cards filed per pass: #1 tour ran 0", text)
        self.assertIn("test loop filed cards: 2, generation unreadable: 2", text)
        self.assertIn("test loop stopped areas: []", text)
        self.assertIn("test loop stop: none yet", text)
        # Plain `status` reads the same report for its feeder line.
        self.assertTrue(cli._feeder_line(self.manifest_path).startswith("feeder: "))

    def test_a_loop_on_with_no_pass_yet_says_so(self):
        self.write(self.paths.config, ON)
        self.write(self.paths.state, json.dumps(feeder.new_state()))
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertIn("test loop: on, round 0 of 6, no pass yet, of 24", text)
        self.assertIn("test loop cards filed per pass: no pass yet", text)
        self.assertIn("test loop stop: none yet", text)

    def test_a_loop_the_feeder_ran_reads_back_in_status(self):
        # The state a real loop left, not a seeded one: a tour filed 10 and its check ran.
        self.write(self.paths.config, ON)
        self.pass_script = [tour(filed(10))]
        self.plans = [{}]
        self.feed_loop()
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertIn("test loop cards filed per pass: #1 tour ran 1, #2 check ran 0", text)
        self.assertIn("test loop filed cards: 1, generation 1: 1", text)

    def test_status_survives_a_sidecar_it_cannot_read_beside_loop_state(self):
        self.seed_loop()
        self.write(self.paths.config, "[test_loop]\nenable = true\n")
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertIn("test loop: the sidecar could not be read", text)
        self.assertIn("test loop: state only, round 2 of 6", text)


if __name__ == "__main__":
    unittest.main()

"""The feeder (feeder plan): the loop that keeps one manifest running.

Almost every case drives the real loop, the real manifest edits, a real temp repository and the
real manifest validation, with three things faked: the run itself (`run_cycle` returns an exit
code and moves the fake records), the summary read, and the tracker's ready list. `sleep` is
injected everywhere, so nothing here waits. One case at the end swaps the fakes out and runs a
whole cycle through the real runner over the stub `claude`, which is the only proof that the
feeder and the runner agree on exit codes and on the summary's shape.
"""
import io
import json
import os
import re
import subprocess
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import _paths
import _repo
from _fakes import FakeAdapter
from relay import cli, feeder, manifest as mf, manifestedit
from test_run import MANIFEST, RunCase

HEAD = MANIFEST.split("[[tasks]]")[0]


def card(task_id, title=None, description="", labels=()):
    return {"id": str(task_id), "title": title or "card %s" % task_id,
            "description": description, "labels": tuple(labels)}


def halted(wall, halt_class="unclean_exit", cause="the tree was left dirty"):
    return {"status": "halted", "wall_seconds": wall, "class": halt_class, "cause": cause}


# A plan entry that leaves the task's record exactly as it was: the run never reached it.
UNREACHED = "unreached"

# The last two lines a task prints when the account's limit for its model is already spent, cut
# down from a real log. The CLI streams its own limit message as the only turn and ends.
LIMIT_RESULT = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                "terminal_reason": "api_error", "api_error_status": 429,
                "result": "You've reached your Fable limit. Switch to another model."}
LIMIT_LOG = "\n".join(json.dumps(line) for line in (
    {"type": "assistant", "error": "rate_limit", "api_error": "model_requires_usage_credits",
     "message": {"content": [{"type": "text", "text": LIMIT_RESULT["result"]}]}},
    LIMIT_RESULT)) + "\n"
# A model the account cannot reach ends the same way, with a 404 beside the same reason.
MISSING_MODEL_LOG = json.dumps(dict(LIMIT_RESULT, api_error_status=404,
                                    result="There's an issue with the selected model.")) + "\n"


class FeederCase(RunCase):
    """A manifest with no tasks yet, a fake board, and a fake runner that follows a script.

    `plans` is the script: one entry per `relay run` the loop makes. An int is that run's exit
    code and nothing else happens. A dict maps a task id to a status word or to a whole record,
    and every listed task the dict does not name lands. When the script runs out, the fake
    runner drops the stop file, which is how every looping case ends.
    """

    def setUp(self):
        super().setUp()
        self.head = HEAD.replace("__REPO__", self.repo)
        with open(self.manifest_path, "w") as handle:
            handle.write(self.head)
        self.paths = feeder.paths_for(self.manifest_path)
        self.adapter = FakeAdapter(ready=[card(n) for n in (1, 2, 3, 4, 5)])
        self.plans, self.runs, self.sleeps, self.notes = [], [], [], []
        self.retries = []        # the ids each run was asked to retry with --retry-blocked
        self.records, self.lease = {}, []
        self.run_record = {}     # the summary's run level keys: run_status, halt_task, halt_class
        self.before_run = None
        self.clock = datetime(2026, 9, 19, 8, 50)     # what `now` answers; a case may move it

    def text(self):
        with open(self.manifest_path, encoding="utf-8") as handle:
            return handle.read()

    def listed(self):
        return manifestedit.task_ids(self.text())

    def models(self):
        return {task.id: task.model for task in mf.load(self.manifest_path).tasks}

    def log_text(self):
        with open(self.paths.log, encoding="utf-8") as handle:
            return handle.read()

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def _run_cycle(self, manifest_path, retry_ids=()):
        if self.before_run:
            self.before_run()
        listed = self.listed()
        self.runs.append(listed)
        self.retries.append(list(retry_ids))
        plan = self.plans.pop(0) if self.plans else {}
        if not self.plans:
            feeder.request_stop(self.paths)
        if isinstance(plan, int):
            return plan
        excluded = manifestedit.excluded_ids(self.text())
        models = manifestedit.task_models(self.text())
        self.ran_on = getattr(self, "ran_on", []) + [dict(models)]
        for task_id in listed:
            status = self.records.get(task_id, {}).get("status")
            # The runner steps over a blocked record unless this run names it (issue #39).
            if status == "landed" or (status == "blocked" and task_id not in retry_ids):
                continue
            outcome = plan.get(task_id, "landed")
            if outcome == UNREACHED:
                continue
            record = dict(outcome) if isinstance(outcome, dict) else {"status": outcome,
                                                                      "wall_seconds": 1500}
            if task_id in excluded:
                record = {"status": "excluded"}
            # The real summary carries the model each task ran on, and every launch restamps
            # `started_at`.
            self.records[task_id] = dict({"model": models.get(task_id),
                                          "started_at": "run %d" % len(self.runs)},
                                         **record, id=task_id)
        return 2 if any(r["status"] == "halted" for r in self.records.values()) else 0

    def deps(self):
        return feeder.Deps(
            sleep=self.sleeps.append, now=lambda: self.clock,
            run_cycle=self._run_cycle,
            read_summary=lambda manifest: dict(self.run_record,
                                               tasks=list(self.records.values())),
            lease_held=lambda manifest: self.lease.pop(0) if self.lease else False,
            build_adapter=lambda manifest: self.adapter,
            run_command=lambda args, cwd, timeout: subprocess.run(
                list(args), cwd=cwd, capture_output=True, text=True, check=False),
            notifier=self.notes.append)

    def feed(self, config=None, **kwargs):
        self.out = io.StringIO()
        loop = feeder.Feeder(self.paths, config or feeder.Config(), self.deps(), self.base_env(),
                             self.out, **kwargs)
        return loop.run()


class Selection(FeederCase):
    def test_only_ready_unlisted_cards_are_appended_in_order_file_order(self):
        self.write(self.manifest_path, self.head + '[[tasks]]\nid = "4"\nmodel = "opus"\n'
                                              'effort = "high"\n')
        self.write(self.paths.order, "# priority, highest first\n5  # five\n2\n")
        self.adapter.ready_cards = [card(1), card(2), card(3, labels=("attended",)), card(4),
                                    card(5), card(6), card(10)]
        config = feeder.Config(batch=4, denied_ids=("6",), denied_labels=("attended",))
        self.plans = [{}]
        self.assertEqual(self.feed(config), 0)
        # 4 was listed already and held one place of the four; 3 and 6 are denied; 5 and 2 are
        # ranked by the order file and 1 sorts ahead of 10 by number, not as text.
        self.assertEqual(self.runs, [["4", "5", "2", "1"]])

    def test_room_in_the_batch_shrinks_by_the_tasks_still_unsettled(self):
        self.plans = [{"2": halted(5000)}, {"2": halted(5000)}, {}]
        self.feed()
        self.assertEqual(self.runs[0], ["1", "2", "3"])
        # 2 halted once and will relaunch, so the second cycle has room for two, not three.
        self.assertEqual(self.runs[1], ["1", "2", "3", "4", "5"])

    def test_natural_key_orders_numbers_as_numbers(self):
        ids = ["T-10", "T-9", "112", "20", "T-9a"]
        self.assertEqual(sorted(ids, key=feeder.natural_key), ["20", "112", "T-9", "T-9a", "T-10"])

    def test_the_ready_command_is_the_escape_hatch_and_takes_gh_shaped_json(self):
        payload = [{"number": 41, "title": "from a script", "body": "**Model:** fable",
                    "labels": [{"name": "unit"}]}]
        config = feeder.Config(ready_command=("python3", "-c",
                                              "print(%r)" % json.dumps(payload)))
        self.plans = [{}]
        self.feed(config)
        self.assertEqual(self.runs, [["41"]])
        self.assertEqual(self.models(), {"41": "fable"})
        self.assertNotIn(("ready", {}), self.adapter.calls)

    def test_an_unreadable_ready_source_offers_nothing_and_does_not_crash(self):
        self.adapter.ready_reason = "gh exited 1: HTTP 502"
        self.assertEqual(self.feed(), 1)
        self.assertEqual(self.runs, [])
        self.assertIn("the ready source could not be read", self.log_text())
        self.assertIn("HTTP 502", self.log_text())


class Halts(FeederCase):
    def test_a_second_halt_excludes_with_a_reason_and_a_first_does_not(self):
        self.plans = [{"2": halted(5000)}, {"2": halted(4000, "gate_failed", 'the gate said "no"')},
                      {}]
        self.feed()
        after_first = self.runs[1]
        self.assertIn("2", after_first)
        tasks = {task.id: task for task in mf.load(self.manifest_path).tasks}
        self.assertTrue(tasks["2"].excluded)
        self.assertIn("excluded by the feeder after 2 halts, last class gate_failed",
                      tasks["2"].reason)
        self.assertIn('the gate said "no"', tasks["2"].reason)
        self.assertFalse(tasks["1"].excluded)
        self.assertTrue(mf.validate(mf.load(self.manifest_path)).ok)
        self.assertTrue(any("2 excluded after 2 halts" in note for note in self.notes), self.notes)

    def test_a_cycle_of_quick_deaths_waits_and_those_halts_are_not_counted(self):
        quick = {"1": halted(40), "2": halted(12), "3": halted(9)}
        self.plans = [quick, {}]
        self.feed()
        self.assertIn(1800, self.sleeps)
        self.assertIn("reading that as a usage limit", self.log_text())
        with open(self.paths.state) as handle:
            state = json.load(handle)
        self.assertEqual(state["halts"], {})
        self.assertEqual(state["limit_waits"], 0)     # reset by the good cycle that followed
        self.assertEqual(manifestedit.excluded_ids(self.text()), set())

    def test_the_usage_limit_allowance_runs_out_and_the_feeder_leaves_with_2(self):
        quick = {"1": halted(40), "2": halted(12), "3": halted(9)}
        self.plans = [quick] * 6
        code = self.feed(feeder.Config(limit_waits_max=2))
        self.assertEqual(code, 2)
        self.assertEqual(len(self.runs), 3)
        self.assertEqual(self.sleeps, [1800, 1800])
        self.assertTrue(any("Not a usage limit" in note for note in self.notes))

    def test_one_landing_in_the_cycle_means_it_was_not_a_usage_limit(self):
        self.plans = [{"1": halted(40), "2": halted(12)}, {}]
        self.feed()
        with open(self.paths.state) as handle:
            self.assertEqual(json.load(handle)["halts"], {"1": 1, "2": 1})

    def test_a_halt_that_never_launched_a_process_is_a_real_halt(self):
        # No wall time means pre flight refused before any process started, a stale branch for
        # one. A usage limit cannot stop a process that never ran.
        self.assertFalse(feeder.looks_like_usage_limit([halted(None)], [], feeder.Config()))
        self.assertTrue(feeder.looks_like_usage_limit([halted(30)], [], feeder.Config()))
        self.assertFalse(feeder.looks_like_usage_limit([halted(30), halted(900)], [],
                                                       feeder.Config()))

    def test_a_run_scoped_halt_stops_the_feeder_and_is_never_counted(self):
        # origin moved under the runner. Counting that would exclude card 1 next cycle, then
        # the card after it, for a fault that belongs to none of them.
        self.plans = [{"1": halted(2000, "remote_advanced")}, {}, {}]
        self.run_record = {"run_status": "halted", "halt_task": "1",
                           "halt_class": "remote_advanced"}
        self.assertEqual(self.feed(), 1)
        self.assertEqual(len(self.runs), 1)
        with open(self.paths.state) as handle:
            self.assertEqual(json.load(handle)["halts"], {})
        self.assertTrue(any("class remote_advanced" in note for note in self.notes), self.notes)

    def test_a_state_file_that_cannot_be_read_is_exit_1_not_a_traceback(self):
        self.write(self.paths.state, "{not json")
        args = cli.build_parser().parse_args(["feed", self.manifest_path, "--once"])
        out = io.StringIO()
        self.assertEqual(cli.cmd_feed(args, self.base_env(), out, deps=self.deps()), 1)
        self.assertIn("holds the halt counts", out.getvalue())

    def test_a_skipped_card_is_reported_once_with_its_reason(self):
        skip = {"status": "skipped", "skip_reason": "the card text names a .claude/ path"}
        self.plans = [{"2": skip}, {"2": skip}, {}]
        self.feed()
        hits = [note for note in self.notes if "2 was skipped by the runner" in note]
        self.assertEqual(len(hits), 1, self.notes)
        self.assertIn(".claude/ path", hits[0])
        self.assertIn("cycle result: landed ['1', '3'], halted [], blocked [], skipped ['2']",
                      self.log_text())
        # Settled for room, so it never held a place the next card needed.
        self.assertEqual(self.runs[1], ["1", "2", "3", "4", "5"])


class ModelFallback(FeederCase):
    """Rule 3 per model: a quick death on a model with a fallback moves the task, not waits."""
    CONFIG = feeder.Config(model_fallback={"fable": "opus"})

    def state(self):
        with open(self.paths.state) as handle:
            return json.load(handle)

    def test_a_fable_quick_death_beside_opus_landings_falls_back_to_opus(self):
        self.write(self.paths.routing, "2 fable\n4 fable\n")
        self.plans = [{"2": halted(8)}, {}]
        self.feed(self.CONFIG)
        # Cycle one ran 2 on fable and it died in seconds while 1 and 3 landed on opus.
        self.assertEqual(self.ran_on[0], {"1": "opus", "2": "fable", "3": "opus"})
        # The halt is not counted and the task relaunched on opus in the next run.
        self.assertEqual(self.state()["halts"], {})
        self.assertEqual(self.ran_on[1]["2"], "opus")
        self.assertEqual(self.state()["exhausted"], {"fable": "2026-09-19T08:50:00"})
        # 4 is routed to fable and was appended on opus while fable is marked.
        self.assertEqual(self.models(), {"1": "opus", "2": "opus", "3": "opus", "4": "opus",
                                         "5": "opus"})
        self.assertIn("card 4 is routed to fable, marked exhausted at 2026-09-19T08:50:00, "
                      "appending it on opus", self.log_text())
        self.assertEqual(self.sleeps, [])
        self.assertTrue(mf.validate(mf.load(self.manifest_path)).ok)
        hits = [note for note in self.notes if "reading that as fable's usage limit" in note]
        self.assertEqual(len(hits), 1, self.notes)
        self.assertIn("2 died inside 600s on fable", hits[0])
        self.assertIn("moved to opus", hits[0])

    def test_a_cycle_where_every_quick_death_has_a_fallback_moves_rather_than_waits(self):
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": halted(8)}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.state()["limit_waits"], 0)
        self.assertEqual([ran["1"] for ran in self.ran_on], ["fable", "opus"])

    def test_the_mark_expires_after_fallback_hours_and_a_new_quick_death_marks_it_again(self):
        self.adapter.ready_cards = [card(n) for n in range(1, 8)]
        self.write(self.paths.routing, "2 fable\n4 fable\n6 fable\n")

        def six_hours_pass():
            self.clock = self.clock + timedelta(hours=6)
        self.before_run = six_hours_pass
        self.plans = [{"2": halted(8)}, {}, {"6": halted(8)}, {}]
        self.feed(self.CONFIG)
        # Marked at the end of run one (14:50); run two appends 4 on opus at 14:50, still inside
        # five hours; by run three the clock reads 20:50 and 6 runs on fable again.
        self.assertEqual(self.ran_on[1]["4"], "opus")
        self.assertEqual(self.ran_on[2]["6"], "fable")
        self.assertIn("fable was marked exhausted at 2026-09-19T14:50:00, over 5h ago, routing "
                      "to it again", self.log_text())
        # It died fast on fable again, so it is marked again and moved.
        self.assertEqual(self.ran_on[3]["6"], "opus")
        marks = [note for note in self.notes if "reading that as fable's usage limit" in note]
        self.assertEqual(len(marks), 2, self.notes)
        self.assertIn("6 died inside 600s on fable", marks[1])
        self.assertEqual(self.state()["halts"], {})

    def test_off_by_default_a_fable_quick_death_beside_landings_is_counted(self):
        self.write(self.paths.routing, "2 fable\n")
        self.plans = [{"2": halted(8)}, {}]
        self.feed()
        self.assertEqual(self.state()["halts"], {"2": 1})
        self.assertEqual(self.state()["exhausted"], {})
        self.assertEqual(self.models()["2"], "fable")
        self.assertNotIn("usage limit", self.log_text())

    def test_a_slow_halt_or_one_that_never_launched_is_never_a_model_limit(self):
        self.write(self.paths.routing, "1 fable\n2 fable\n")
        self.plans = [{"1": halted(5000), "2": halted(None)}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.state()["halts"], {"1": 1, "2": 1})
        self.assertEqual(self.state()["exhausted"], {})

    def test_a_fallback_that_is_itself_exhausted_leads_to_the_whole_cycle_wait(self):
        # opus is already marked, and falls back to fable, so every card is appended on fable.
        # Then fable dies too: its fallback, opus, is marked and opus's own leads back to fable,
        # which must end the chain rather than send the tasks round in a circle.
        self.write(self.paths.state, json.dumps(dict(feeder.new_state(),
                                                     exhausted={"opus": "2026-09-19T08:00:00"})))
        config = feeder.Config(model_fallback={"fable": "opus", "opus": "fable"})
        quick = {"1": halted(8), "2": halted(9), "3": halted(7)}
        self.plans = [quick, {}]
        self.feed(config)
        self.assertEqual(self.ran_on[0], {"1": "fable", "2": "fable", "3": "fable"})
        self.assertEqual(self.sleeps, [1800])
        self.assertIn("reading that as a usage limit, waiting", self.log_text())
        self.assertEqual(self.state()["halts"], {})
        self.assertEqual(self.models(), {"1": "fable", "2": "fable", "3": "fable"})

    def test_a_quick_death_with_no_fallback_beside_one_with_keeps_the_whole_cycle_wait(self):
        self.write(self.paths.routing, "1 fable\n2 sonnet\n")
        self.adapter.ready_cards = [card(1), card(2)]
        self.plans = [{"1": halted(8), "2": halted(9)}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.sleeps, [1800])
        self.assertEqual(self.models(), {"1": "fable", "2": "sonnet"})
        self.assertEqual(self.state()["exhausted"], {})

    def test_the_fallback_chain_never_loops(self):
        table = {"fable": "opus", "opus": "sonnet", "sonnet": "fable"}
        self.assertEqual(feeder.resolve_fallback("fable", table, {"fable"}), "opus")
        self.assertEqual(feeder.resolve_fallback("fable", table, {"fable", "opus"}), "sonnet")
        self.assertIsNone(feeder.resolve_fallback("fable", table, {"fable", "opus", "sonnet"}))
        self.assertIsNone(feeder.resolve_fallback("haiku", table, {"haiku"}))

    def test_the_move_is_one_parsed_change_to_that_task(self):
        text = (self.head + '[[tasks]]\nid = "1"\nmodel = "fable"  # why\neffort = "high"\n\n'
                '[[tasks]]\nid = "2"\neffort = "high"\n')
        moved = manifestedit.set_model(text, "1", "opus")
        self.assertEqual(manifestedit.task_models(moved), {"1": "opus"})
        added = manifestedit.set_model(moved, "2", "opus")
        self.assertEqual(manifestedit.task_models(added), {"1": "opus", "2": "opus"})
        self.assertIsNone(manifestedit.set_model(added, "2", "opus"))
        with self.assertRaises(manifestedit.EditError):
            manifestedit.set_model(text, "9", "opus")


class BlockedLimit(FeederCase):
    """Issue #39. A limit death the runner recorded as blocked, not halted, falls back too."""
    CONFIG = feeder.Config(model_fallback={"fable": "opus"})

    def state(self):
        with open(self.paths.state) as handle:
            return json.load(handle)

    def blocked(self, wall, log=LIMIT_LOG, halt_class="no_envelope"):
        """A blocked record the way the summary reports it, its stdout log written for real."""
        path = None
        if log is not None:
            path = os.path.join(self.tmp.name, "log-%d.stdout.log" % len(os.listdir(self.tmp.name)))
            self.write(path, log)
        return {"status": "blocked", "wall_seconds": wall, "class": halt_class,
                "log_path": path}

    def test_a_blocked_limit_death_falls_back_and_only_it_is_retried(self):
        self.adapter.ready_cards = [card(1), card(2), card(3)]
        self.write(self.paths.routing, "2 fable\n")
        self.plans = [{"1": self.blocked(900, log=None, halt_class="blocked_envelope"),
                       "2": self.blocked(4)}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.ran_on[0]["2"], "fable")
        # The second run names 2 alone; 1 is an ordinary blocked task and stays blocked.
        self.assertEqual(self.retries, [[], ["2"]])
        self.assertEqual(self.ran_on[1]["2"], "opus")
        self.assertEqual(self.records["2"]["status"], "landed")
        self.assertEqual(self.records["1"]["status"], "blocked")
        self.assertEqual(self.state()["exhausted"], {"fable": "2026-09-19T08:50:00"})
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertEqual(self.state()["halts"], {})
        self.assertIn("1 blocked; a later run will not retry it", self.log_text())
        self.assertNotIn("2 blocked;", self.log_text())
        hits = [note for note in self.notes if "reading that as fable's usage limit" in note]
        self.assertEqual(len(hits), 1, self.notes)
        self.assertIn("2 read blocked and relaunch with --retry-blocked", hits[0])
        self.assertIn("relaunching blocked ['2'] with --retry-blocked", self.log_text())

    def test_a_quick_blocked_death_with_no_result_line_is_read_by_the_time_rule(self):
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": self.blocked(4, log="")}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.retries, [[], ["1"]])
        self.assertEqual(self.ran_on[1]["1"], "opus")

    def test_a_result_line_that_is_not_a_429_is_not_a_usage_limit(self):
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": self.blocked(4, log=MISSING_MODEL_LOG)}]
        self.feed(self.CONFIG)
        self.assertEqual(self.models(), {"1": "fable"})
        self.assertEqual(self.state()["exhausted"], {})
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertIn("1 blocked; a later run will not retry it without --retry-blocked 1",
                      self.log_text())

    def test_a_slow_blocked_task_or_another_class_is_never_a_usage_limit(self):
        self.adapter.ready_cards = [card(1), card(2)]
        self.write(self.paths.routing, "1 fable\n2 fable\n")
        self.plans = [{"1": self.blocked(5000), "2": self.blocked(4, halt_class="timeout")}]
        self.feed(self.CONFIG)
        self.assertEqual(self.models(), {"1": "fable", "2": "fable"})
        self.assertEqual(self.state()["exhausted"], {})

    def test_off_by_default_a_blocked_limit_death_stays_blocked(self):
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": self.blocked(4)}]
        self.feed()
        self.assertEqual(self.models(), {"1": "fable"})
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertIn("1 blocked;", self.log_text())

    def test_a_blocked_limit_death_in_a_whole_cycle_wait_retries_where_it_was(self):
        # 1 halted fast on sonnet, which has no fallback, and nothing landed: the whole cycle
        # is waited out. 2 waits with it and relaunches on fable, as a halt would.
        self.adapter.ready_cards = [card(1), card(2)]
        self.write(self.paths.routing, "1 sonnet\n2 fable\n")
        self.plans = [{"1": halted(8), "2": self.blocked(4)}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.sleeps, [1800])
        self.assertEqual(self.retries, [[], ["2"]])
        self.assertEqual(self.ran_on[1]["2"], "fable")
        self.assertEqual(self.state()["exhausted"], {})
        self.assertIn("['2'] blocked on a usage limit and will be retried after the wait",
                      self.log_text())

    def test_blocked_limit_deaths_whose_fallback_is_exhausted_are_waited_out(self):
        # Issue #45. opus is marked, so fable's deaths have no free fallback. Before the fix
        # nothing halted, no wait ran, every task was reported blocked, fable stayed unmarked,
        # and the next cycle appended 4 and 5 on fable to die the same way.
        self.write(self.paths.state, json.dumps(dict(feeder.new_state(),
                                                     exhausted={"opus": "2026-09-19T08:00:00"})))
        self.write(self.paths.routing, "1 fable\n2 fable\n3 fable\n4 fable\n5 fable\n")
        self.plans = [{"1": self.blocked(4), "2": self.blocked(5), "3": self.blocked(6)}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.ran_on[0], {"1": "fable", "2": "fable", "3": "fable"})
        self.assertEqual(self.sleeps, [1800])
        self.assertIn("reading that as a usage limit, waiting", self.log_text())
        self.assertIn("['1', '2', '3'] blocked on a usage limit and will be retried after the "
                      "wait", self.log_text())
        # After the wait the three are retried where they were, and they hold the whole batch,
        # so no new card is appended on fable.
        self.assertEqual(self.retries, [[], ["1", "2", "3"]])
        self.assertEqual(self.runs[1], ["1", "2", "3"])
        self.assertEqual(self.models(), {"1": "fable", "2": "fable", "3": "fable"})
        self.assertEqual([self.records[n]["status"] for n in "123"], ["landed"] * 3)
        self.assertEqual(self.state()["exhausted"], {"opus": "2026-09-19T08:00:00"})
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertEqual(self.state()["halts"], {})
        self.assertNotIn("blocked; a later run will not retry it", self.log_text())

    def test_two_models_that_fall_back_to_each_other_and_both_block_are_waited_out(self):
        config = feeder.Config(batch=2, model_fallback={"fable": "opus", "opus": "fable"})
        self.adapter.ready_cards = [card(1), card(2), card(3), card(4)]
        self.write(self.paths.routing, "1 fable\n2 opus\n3 fable\n4 opus\n")
        self.plans = [{"1": self.blocked(4), "2": self.blocked(5)}, {}]
        self.feed(config)
        self.assertEqual(self.ran_on[0], {"1": "fable", "2": "opus"})
        self.assertEqual(self.sleeps, [1800])
        # Neither is sent to the other, neither model is marked, and 3 and 4 wait their turn.
        self.assertEqual(self.retries, [[], ["1", "2"]])
        self.assertEqual(self.runs[1], ["1", "2"])
        self.assertEqual(self.models(), {"1": "fable", "2": "opus"})
        self.assertEqual(self.state()["exhausted"], {})
        self.assertEqual(self.state()["halts"], {})
        self.assertNotIn("blocked; a later run will not retry it", self.log_text())

    def test_a_blocked_death_with_no_free_fallback_holds_a_movable_halt_to_the_wait(self):
        # 1 halted on fable, whose fallback sonnet is free; 2 blocked on opus, whose fallback
        # haiku is marked and leads nowhere. Nothing landed and one death has nowhere to go, so
        # the whole cycle rule decides for both, as it does for two halts: neither moves.
        config = feeder.Config(allowed_models=("fable", "opus", "sonnet", "haiku"),
                               model_fallback={"fable": "sonnet", "opus": "haiku"})
        self.write(self.paths.state, json.dumps(dict(feeder.new_state(),
                                                     exhausted={"haiku": "2026-09-19T08:00:00"})))
        self.adapter.ready_cards = [card(1), card(2)]
        self.write(self.paths.routing, "1 fable\n2 opus\n")
        self.plans = [{"1": halted(8), "2": self.blocked(4)}, {}]
        self.feed(config)
        self.assertEqual(self.sleeps, [1800])
        self.assertEqual(self.retries, [[], ["2"]])
        self.assertEqual(self.models(), {"1": "fable", "2": "opus"})
        self.assertEqual(self.state()["exhausted"], {"haiku": "2026-09-19T08:00:00"})
        self.assertEqual(self.state()["halts"], {})

    def test_blocked_limit_deaths_with_no_free_fallback_run_out_the_waits_and_stop(self):
        self.write(self.paths.state, json.dumps(dict(feeder.new_state(),
                                                     exhausted={"opus": "2026-09-19T08:00:00"})))
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": self.blocked(4)}, {"1": self.blocked(5)}, {"1": self.blocked(6)}]
        code = self.feed(feeder.Config(model_fallback={"fable": "opus"}, limit_waits_max=2))
        self.assertEqual(code, 2)
        self.assertEqual(self.sleeps, [1800, 1800])
        self.assertEqual(self.retries, [[], ["1"], ["1"]])
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertIn("1 blocked; a later run will not retry it", self.log_text())

    def test_a_retry_the_run_never_reached_keeps_its_place(self):
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": self.blocked(4)}, {"1": UNREACHED}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.retries, [[], ["1"], ["1"]])
        self.assertEqual(self.records["1"]["status"], "landed")
        # One move and one mark, not a second one for the unchanged record.
        marks = [note for note in self.notes if "usage limit" in note]
        self.assertEqual(len(marks), 1, self.notes)

    def test_a_retry_that_dies_on_its_fallback_too_moves_on_or_stays_blocked(self):
        config = feeder.Config(model_fallback={"fable": "opus", "opus": "sonnet"})
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")
        self.plans = [{"1": self.blocked(4)}, {"1": self.blocked(5)}, {"1": self.blocked(6)}]
        self.feed(config)
        self.assertEqual([ran["1"] for ran in self.ran_on], ["fable", "opus", "sonnet"])
        # sonnet has no fallback of its own, so the third death is an ordinary block.
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertIn("1 blocked; a later run will not retry it", self.log_text())

    def test_a_retry_refused_before_launch_is_a_counted_halt_not_another_quick_death(self):
        # The retry's pre flight refused it: the record reads halted but keeps the blocked
        # attempt's four seconds and its stamp, since no process was launched to restamp them.
        self.adapter.ready_cards = [card(1)]
        self.write(self.paths.routing, "1 fable\n")

        def refuse_at_preflight():
            if len(self.runs) == 1:
                self.records["1"] = dict(self.records["1"], status="halted",
                                         **{"class": "unclean_exit"})
        self.before_run = refuse_at_preflight
        self.plans = [{"1": self.blocked(4)}, {"1": UNREACHED}]
        self.feed(self.CONFIG)
        self.assertEqual(self.retries, [[], ["1"]])
        self.assertEqual(self.state()["halts"], {"1": 1})
        marks = [note for note in self.notes if "usage limit" in note]
        self.assertEqual(len(marks), 1, self.notes)

    def test_a_retry_that_blocks_again_is_reported_again(self):
        self.write(self.manifest_path, self.head + '[[tasks]]\nid = "7"\nmodel = "opus"\n'
                                                   'effort = "high"\n')
        self.adapter.ready_cards = []
        self.records["7"] = {"id": "7", "status": "blocked", "started_at": "old"}
        self.write(self.paths.state, json.dumps(dict(feeder.new_state(), reported={
            "blocked:7": "7 blocked; a later run will not retry it without --retry-blocked 7"})))
        self.plans = [{"7": {"status": "blocked", "wall_seconds": 900,
                             "class": "blocked_envelope"}}]
        self.out = io.StringIO()
        feeder.Feeder(self.paths, feeder.Config(), self.deps(), self.base_env(), self.out,
                      once=True, retry_blocked=("7",)).run()
        self.assertEqual(self.retries, [["7"]])
        self.assertIn("7 blocked; a later run will not retry it", self.log_text())

    def test_an_excluded_id_asked_for_by_hand_is_named_and_not_queued(self):
        self.write(self.manifest_path, self.head + '[[tasks]]\nid = "7"\nmodel = "opus"\n'
                                                   'effort = "high"\nexcluded = true\n'
                                                   'reason = "held back by hand"\n')
        self.adapter.ready_cards = []
        self.records["7"] = {"id": "7", "status": "blocked", "started_at": "old"}
        self.out = io.StringIO()
        feeder.Feeder(self.paths, feeder.Config(), self.deps(), self.base_env(), self.out,
                      once=True, retry_blocked=("7",)).run()
        self.assertIn("7 is excluded in the manifest; nothing to retry", self.log_text())
        self.assertNotIn("queued", self.log_text())

    def test_a_whole_cycle_wait_that_runs_out_queues_no_retry(self):
        self.adapter.ready_cards = [card(1), card(2)]
        self.write(self.paths.routing, "1 sonnet\n2 fable\n")
        self.plans = [{"1": halted(8), "2": self.blocked(4)}]
        self.feed(feeder.Config(model_fallback={"fable": "opus"}, limit_waits_max=0))
        self.assertEqual(self.state()["retry_blocked"], {})
        self.assertIn("2 blocked; a later run will not retry it", self.log_text())
        self.assertIn("Not a usage limit", self.log_text())

    def test_the_result_line_is_found_under_a_torn_first_line(self):
        tail = LIMIT_LOG[7:]
        self.assertEqual(feeder.result_event(tail)["api_error_status"], 429)
        self.assertIsNone(feeder.result_event('{"type": "assistant"}\nnot json\n'))
        # The log holds every attempt; an earlier attempt's result is not this one's.
        earlier = json.dumps({"type": "result", "subtype": "success"}) + "\n"
        init = json.dumps({"type": "system", "subtype": "init"}) + "\n"
        self.assertIsNone(feeder.result_event(earlier + init + '{"type": "assistant"}\n'))
        self.assertEqual(feeder.result_event(earlier + init + LIMIT_LOG)["api_error_status"],
                         429)
        config = feeder.Config()
        task = {"class": "no_envelope", "wall_seconds": 4}
        self.assertTrue(feeder.blocked_by_usage_limit(task, config, LIMIT_LOG))
        self.assertTrue(feeder.blocked_by_usage_limit(task, config, ""))
        self.assertFalse(feeder.blocked_by_usage_limit(task, config, MISSING_MODEL_LOG))
        self.assertFalse(feeder.blocked_by_usage_limit(dict(task, wall_seconds=None), config,
                                                       LIMIT_LOG))

    def test_the_real_run_cycle_names_each_retried_id_and_never_the_bare_flag(self):
        seen = []

        def fake_run(command, **_kwargs):
            seen.append(command)
            return SimpleNamespace(returncode=0)

        deps = feeder.build_deps(feeder.Config(caffeinate=False), self.base_env())
        real = feeder.subprocess.run
        feeder.subprocess.run = fake_run
        try:
            deps.run_cycle("/m.toml", ["7", "12"])
            deps.run_cycle("/m.toml", [])
        finally:
            feeder.subprocess.run = real
        tail = seen[0][seen[0].index("/m.toml") + 1:]
        self.assertEqual(tail[0::2], ["--retry-blocked", "--retry-blocked"])
        self.assertEqual(sorted(tail[1::2]), ["12", "7"])
        self.assertNotIn("--retry-blocked", seen[1])


class Exits(FeederCase):
    def test_the_stop_file_is_honoured_between_cycles(self):
        self.plans = [{}, {}, {}]
        self.before_run = lambda: feeder.request_stop(self.paths)
        self.assertEqual(self.feed(), 0)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("stop file present, leaving", self.log_text())

    def test_exit_3_waits_for_the_lease_and_goes_round_again(self):
        self.plans = [3, {}]
        self.assertEqual(self.feed(), 0)
        self.assertEqual(self.sleeps, [600])
        self.assertEqual(len(self.runs), 2)

    def test_exit_1_stops_the_feeder_and_says_so(self):
        self.plans = [1, {}]
        self.assertEqual(self.feed(), 1)
        self.assertEqual(len(self.runs), 1)
        self.assertTrue(any("relay refused the manifest" in note for note in self.notes))

    def test_a_dirty_checkout_stops_the_loop_before_anything_is_appended(self):
        self.write(os.path.join(self.repo, "scratch.txt"), "somebody is working here\n")
        self.assertEqual(self.feed(), 1)
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.runs, [])
        self.assertIn("uncommitted changes: ?? scratch.txt", self.log_text())

    def test_a_checkout_off_its_default_branch_stops_the_loop(self):
        _repo.git(self.repo, "checkout", "-q", "-b", "somebody/elses")
        self.assertEqual(self.feed(), 1)
        self.assertIn("the checkout is on somebody/elses, not main", self.log_text())
        self.assertEqual(self.runs, [])

    def test_nothing_is_appended_while_a_runner_holds_the_lease(self):
        self.lease = [True, False]
        self.plans = [{}]
        self.feed()
        self.assertEqual(self.sleeps, [600])
        self.assertIn("a runner holds the lease on this manifest, not appending",
                      self.log_text())
        self.assertEqual(len(self.runs), 1)

    def test_an_empty_queue_ends_the_feeder_at_once_by_default(self):
        self.adapter.ready_cards = []
        self.assertEqual(self.feed(), 0)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.runs, [])
        self.assertIn("the queue is empty, leaving", self.log_text())
        self.assertIn("the queue is empty, leaving", self.notes[-1])

    def test_a_feeder_leaves_after_the_run_that_empties_the_queue(self):
        # Cycle one appends and runs 1 and 2; they land; cycle two finds nothing and leaves
        # without a sleep. The fake runner's stop file is removed so only the idle rule ends it.
        self.adapter.ready_cards = [card(1), card(2)]
        self.before_run = lambda: setattr(self.adapter, "ready_cards", [])
        self.plans = [{}, {}]
        self.assertEqual(self.feed(), 0)
        self.assertEqual(self.runs, [["1", "2"]])
        self.assertEqual(self.sleeps, [])

    def test_idle_waits_max_keeps_an_idle_feeder_waiting_that_many_times(self):
        self.adapter.ready_cards = []
        self.assertEqual(self.feed(feeder.Config(idle_waits_max=3)), 0)
        self.assertEqual(self.sleeps, [1800, 1800, 1800])
        self.assertEqual(self.runs, [])
        # A new feeder starts its own count, whatever the last one left in the state file.
        self.assertEqual(self.feed(feeder.Config(idle_waits_max=3)), 0)
        self.assertEqual(self.sleeps, [1800] * 6)

    def test_a_feeder_that_leaves_on_the_stop_file_hands_no_partial_count_to_the_next(self):
        self.adapter.ready_reason = "gh exited 1: HTTP 502"
        original = self.deps

        def deps():
            built = original()
            built.sleep = lambda seconds: (self.sleeps.append(seconds),
                                           feeder.request_stop(self.paths))
            return built
        self.deps = deps
        self.assertEqual(self.feed(), 0)                   # one failed read, then the stop file
        self.assertEqual(self.sleeps, [1800])
        os.unlink(self.paths.stop)
        self.deps = original
        self.assertEqual(self.feed(), 1)                   # three of its own, not two
        self.assertEqual(self.sleeps, [1800, 1800, 1800])

    def test_once_keeps_the_streak_counts_between_cycles(self):
        self.adapter.ready_reason = "gh exited 1: HTTP 502"
        self.assertEqual(self.feed(once=True), 0)
        self.assertEqual(self.feed(once=True), 0)
        self.assertEqual(self.feed(once=True), 1)
        self.assertEqual(self.sleeps, [])

    def test_ready_cards_that_validate_refused_are_not_an_empty_queue(self):
        # grok-4.6 is allowed by this sidecar and belongs to grok; the manifest runs on claude.
        self.write(self.paths.routing, "1 grok-4.6\n")
        self.adapter.ready_cards = [card(1)]
        config = feeder.Config(allowed_models=("fable", "opus", "sonnet", "grok-4.6"))
        self.assertEqual(self.feed(config), 1)
        self.assertEqual(self.runs, [])
        self.assertNotIn("the queue is empty", self.log_text())
        self.assertIn("every ready card was refused with the model it is routed to, and nothing "
                      "is left to run: 1", self.log_text())

    def test_a_ready_source_that_is_not_configured_is_refused_before_a_cycle(self):
        github = SimpleNamespace(tracker=SimpleNamespace(adapter="github"))
        self.assertIsNone(feeder.ready_source_problem(github, feeder.Config(
            ready_source={"labels": ["ready"]})))
        self.assertIsNone(feeder.ready_source_problem(github, feeder.Config(
            ready_command=("true",))))
        self.assertIn("no ready labels are configured",
                      feeder.ready_source_problem(github, feeder.Config()))
        github.tracker.adapter = "jira"     # a plain record, so this is allowed here
        self.assertIn("no ready query is configured",
                      feeder.ready_source_problem(github, feeder.Config(ready_source={"jql": " "})))
        github.tracker.adapter = "markdown"
        self.assertIsNone(feeder.ready_source_problem(github, feeder.Config()))
        # And in the loop: a github manifest with no sidecar at all stops before any cycle.
        self.write(self.manifest_path, re.sub(
            r"\[tracker\].*?\n\n", '[tracker]\nadapter = "github"\nowner = "x"\n'
            'project_number = 7\nstatus_field = "Done"\nin_review_status = "In review"\n\n',
            self.head, flags=re.S))
        self.assertEqual(self.feed(), 1)
        self.assertEqual(self.sleeps, [])
        self.assertIn("stopping: no ready labels are configured", self.log_text())

    def test_an_unreadable_source_is_not_an_empty_queue(self):
        self.adapter.ready_reason = "gh exited 1: HTTP 502"
        self.assertEqual(self.feed(), 1)
        # Two waits, then the third failure in a row stops it for a person with exit 1.
        self.assertEqual(self.sleeps, [1800, 1800])
        self.assertNotIn("the queue is empty", self.log_text())
        self.assertIn("could not be read for 3 cycles in a row", self.log_text())

    def test_a_read_that_recovers_to_empty_leaves_as_an_empty_queue(self):
        # The first read fails and the board answers after one wait, empty.
        self.adapter.ready_reason = "gh exited 1: HTTP 502"
        original = self.deps

        def deps():
            built = original()
            built.sleep = lambda seconds: (self.sleeps.append(seconds),
                                           setattr(self.adapter, "ready_reason", None))
            return built
        self.deps = deps
        self.adapter.ready_cards = []
        self.assertEqual(self.feed(), 0)
        self.assertEqual(self.sleeps, [1800])
        self.assertIn("the queue is empty, leaving", self.log_text())

    def test_once_runs_one_cycle_and_never_sleeps(self):
        self.plans = [{"1": halted(20), "2": halted(20), "3": halted(20)}, {}, {}]
        self.assertEqual(self.feed(once=True), 0)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.sleeps, [])

    def test_a_dry_run_writes_nothing_and_runs_nothing(self):
        self.write(self.paths.routing, "2 fable\n")
        before = sorted(os.listdir(self.tmp.name))
        self.assertEqual(self.feed(dry_run=True), 0)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), before)
        self.assertEqual(self.listed(), [])
        self.assertEqual(self.runs, [])
        self.assertEqual(self.notes, [])
        self.assertIn("would offer 2 on fable", self.out.getvalue())


class Routing(FeederCase):
    CONFIG = feeder.Config(allowed_models=("fable", "opus", "sonnet", "grok-4.6"))

    def test_the_file_beats_the_body_line_and_the_body_line_beats_the_default(self):
        self.adapter.ready_cards = [card(1, description="notes\n**Model:** sonnet\nmore"),
                                    card(2, description="**Model:** sonnet"), card(3)]
        self.write(self.paths.routing, "# id, model, why\n1 fable  # a new seam\n")
        self.plans = [{}]
        self.feed(self.CONFIG)
        self.assertEqual(self.models(), {"1": "fable", "2": "sonnet", "3": "opus"})

    def test_a_name_outside_the_allowed_set_is_ignored_and_logged(self):
        self.adapter.ready_cards = [card(1), card(2, description="**Model:** fabel")]
        self.write(self.paths.routing, "1 opuss\n")
        self.plans = [{}]
        self.feed()
        self.assertEqual(self.models(), {"1": "opus", "2": "opus"})
        self.assertIn("the routing file names 'opuss' for 1, not an allowed model, ignored",
                      self.log_text())
        self.assertIn("card 2 asks for model 'fabel' in its body, not an allowed model, ignored",
                      self.log_text())

    def test_the_routing_file_is_read_fresh_at_every_append(self):
        self.adapter.ready_cards = [card(n) for n in (1, 2, 3, 4)]
        self.plans = [{}, {}]
        self.before_run = lambda: self.write(self.paths.routing, "4 fable\n")
        self.feed()
        self.assertEqual(self.models()["4"], "fable")

    def test_an_append_that_fails_validate_is_rolled_back_and_the_rest_still_go_in(self):
        # grok-4.6 is allowed by this sidecar and belongs to grok, while the manifest's tasks
        # run on claude: the allowed set let it through, and validate is the second line.
        self.write(self.paths.routing, "2 grok-4.6\n")
        self.plans = [{}, {}]
        self.feed(self.CONFIG)
        self.assertEqual(self.runs[0], ["1", "3"])
        self.assertNotIn("2", self.listed())
        self.assertTrue(mf.validate(mf.load(self.manifest_path)).ok)
        self.assertIn("2 on grok-4.6 was not appended", self.log_text())
        self.assertIn("belongs to backend grok", self.log_text())
        # Remembered, so it is not offered and refused again every ninety minutes.
        self.assertEqual(sum("was not appended" in note for note in self.notes), 1)


class Sidecar(FeederCase):
    def test_no_sidecar_means_every_default(self):
        self.assertEqual(feeder.load_config(self.paths.config), feeder.Config())

    def test_a_sidecar_sets_what_it_names_and_leaves_the_rest(self):
        self.write(self.paths.config, '[feeder]\nbatch = 5\n[models]\ndefault = "sonnet"\n'
                   '[deny]\nids = [29, "T-4"]\nlabels = ["attended"]\n'
                   '[ready]\nlabels = ["unit", "ready"]\n'
                   '[hooks]\npre_cycle = ["python3", "scripts/board.py", "sync"]\n')
        config = feeder.load_config(self.paths.config)
        self.assertEqual((config.batch, config.default_model, config.max_halts), (5, "sonnet", 2))
        self.assertEqual(config.denied_ids, ("29", "T-4"))
        self.assertEqual(config.ready_source, {"labels": ["unit", "ready"]})
        self.assertEqual(config.pre_cycle_command, ("python3", "scripts/board.py", "sync"))

    def test_every_problem_is_named_at_once(self):
        self.write(self.paths.config, '[feeder]\nbacth = 5\nbatch = 0\n'
                   '[hooks]\npre_cycle = "scripts/board.py sync"\n'
                   '[models]\ndefault = "haiku"\n[ready]\ncommand = "gh issue list"\n')
        with self.assertRaises(feeder.ConfigError) as caught:
            feeder.load_config(self.paths.config)
        message = str(caught.exception)
        for expected in ("feeder.bacth is not a feeder setting", "batch must be a positive integer",
                         "pre_cycle_command must be an array of strings (an argument list, never "
                         "a shell string)", "ready.command must be an array of strings"):
            self.assertIn(expected, message)

    def test_idle_waits_max_may_be_zero_and_not_negative(self):
        self.write(self.paths.config, '[waits]\nidle_waits_max = 0\n')
        self.assertEqual(feeder.load_config(self.paths.config).idle_waits_max, 0)
        self.write(self.paths.config, '[waits]\nidle_waits_max = -1\nlimit_waits_max = 0\n')
        with self.assertRaises(feeder.ConfigError) as caught:
            feeder.load_config(self.paths.config)
        self.assertIn("idle_waits_max must be zero or a positive integer", str(caught.exception))
        self.assertIn("limit_waits_max must be a positive integer", str(caught.exception))

    def test_a_default_model_outside_the_allowed_set_is_refused(self):
        self.write(self.paths.config, '[models]\ndefault = "haiku"\n')
        with self.assertRaises(feeder.ConfigError) as caught:
            feeder.load_config(self.paths.config)
        self.assertIn("models.default 'haiku' is not in models.allowed", str(caught.exception))

    def test_a_model_fallback_is_read_and_checked_against_the_allowed_set(self):
        self.write(self.paths.config, '[models]\nfallback = { fable = "opus" }\n'
                                      'fallback_hours = 3\n')
        config = feeder.load_config(self.paths.config)
        self.assertEqual((config.model_fallback, config.fallback_hours), ({"fable": "opus"}, 3))
        self.assertEqual(feeder.Config().model_fallback, {})
        self.assertEqual(feeder.Config().fallback_hours, 5)
        self.write(self.paths.config, '[models]\nfallback = { haiku = "opus", fable = "fable", '
                                      'sonnet = "gpt" }\nfallback_hours = 0\n')
        with self.assertRaises(feeder.ConfigError) as caught:
            feeder.load_config(self.paths.config)
        message = str(caught.exception)
        for expected in ("fallback_hours must be a positive integer",):
            self.assertIn(expected, message)
        self.write(self.paths.config, '[models]\nfallback = { haiku = "opus", fable = "fable", '
                                      'sonnet = "gpt" }\n')
        with self.assertRaises(feeder.ConfigError) as caught:
            feeder.load_config(self.paths.config)
        message = str(caught.exception)
        for expected in ("models.fallback names 'haiku', which is not in models.allowed",
                         "models.fallback names 'gpt', which is not in models.allowed",
                         "models.fallback sends 'fable' to itself"):
            self.assertIn(expected, message)
        for bad in ('fallback = "opus"', 'fallback = { fable = 3 }'):
            self.write(self.paths.config, "[models]\n%s\n" % bad)
            with self.assertRaises(feeder.ConfigError) as caught:
                feeder.load_config(self.paths.config)
            self.assertIn("models.fallback must be a table", str(caught.exception))

    def test_the_pre_cycle_command_runs_in_the_target_repo_and_its_failure_is_not_fatal(self):
        marker = os.path.join(self.tmp.name, "ran-in")
        script = "import os,sys; open(%r,'w').write(os.getcwd()); sys.exit(4)" % marker
        self.plans = [{}]
        self.assertEqual(self.feed(feeder.Config(pre_cycle_command=("python3", "-c", script))), 0)
        with open(marker) as handle:
            self.assertEqual(os.path.realpath(handle.read()), os.path.realpath(self.repo))
        self.assertIn("the pre cycle command exited 4", self.log_text())
        self.assertEqual(len(self.runs), 1)

    def test_every_path_derives_from_the_manifest_stem(self):
        paths = feeder.paths_for("/x/manifests/cratekit-parity.toml")
        self.assertEqual(paths.config, "/x/manifests/cratekit-parity.feeder.toml")
        self.assertEqual(paths.stop, "/x/manifests/cratekit-parity.feeder.stop")
        self.assertEqual(paths.order, "/x/manifests/cratekit-parity.order")
        self.assertEqual(paths.routing, "/x/manifests/cratekit-parity.models")


class Verb(FeederCase):
    def call(self, *flags, deps=None):
        args = cli.build_parser().parse_args(["feed", self.manifest_path] + list(flags))
        out = io.StringIO()
        code = cli.cmd_feed(args, self.base_env(), out, deps=deps or self.deps())
        return code, out.getvalue()

    def test_the_start_line_and_the_feeder_log_both_carry_the_checkout_warning(self):
        with mock.patch.object(feeder, "checkout_warning", return_value="edits reach cycles"):
            code, text = self.call("--dry-run")
        self.assertEqual(code, 0, text)
        self.assertEqual(text.count("warning: edits reach cycles"), 2, text)

    def test_stop_drops_the_stop_file_and_touches_nothing_else(self):
        code, text = self.call("--stop")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.paths.stop))
        self.assertIn("leaves after its current cycle", text)

    def test_a_second_feeder_on_one_manifest_is_refused_with_3(self):
        held = feeder.acquire_lock(self.paths)
        try:
            code, text = self.call("--once")
        finally:
            held.close()
        self.assertEqual(code, 3)
        self.assertIn("another feeder holds", text)
        self.assertEqual(self.runs, [])

    def test_restart_asks_waits_for_the_old_feeder_to_leave_and_kills_nothing(self):
        held = feeder.acquire_lock(self.paths)
        seen = []

        def sleep(seconds):
            # The old feeder meets the stop file between cycles and leaves on its own.
            seen.append((seconds, os.path.exists(self.paths.stop)))
            held.close()

        deps = self.deps()
        deps.sleep = sleep
        self.plans = [{}]
        code, _ = self.call("--restart", "--once", deps=deps)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(feeder.RESTART_POLL_SECONDS, True)])
        self.assertEqual(len(self.runs), 1)
        self.assertIn("restart requested", self.log_text())
        self.assertIn("the old feeder is gone, starting", self.log_text())

    def test_retry_blocked_queues_that_one_blocked_task_and_no_other(self):
        self.write(self.manifest_path, self.head + "".join(
            '[[tasks]]\nid = "%s"\nmodel = "opus"\neffort = "high"\n\n' % n for n in (7, 8)))
        self.adapter.ready_cards = []
        for task_id in ("7", "8"):
            self.records[task_id] = {"id": task_id, "status": "blocked", "started_at": "old"}
        self.plans = [{}]
        code, text = self.call("--retry-blocked", "7", "--once")
        self.assertEqual(code, 0, text)
        self.assertEqual(self.retries, [["7"]])
        self.assertEqual(self.records["7"]["status"], "landed")
        self.assertEqual(self.records["8"]["status"], "blocked")
        self.assertIn("7 is queued for a retry at the operator's request", self.log_text())

    def test_retry_blocked_refuses_an_id_the_manifest_does_not_list(self):
        code, text = self.call("--retry-blocked", "99", "--once")
        self.assertEqual(code, 1)
        self.assertIn("--retry-blocked names 99, not a task in the manifest", text)
        self.assertEqual(self.runs, [])

    def test_a_bad_sidecar_is_exit_1_before_anything_runs(self):
        self.write(self.paths.config, "[feeder]\nbatch = -1\n")
        code, text = self.call("--once")
        self.assertEqual(code, 1)
        self.assertIn("batch must be a positive integer", text)
        self.assertEqual(self.runs, [])


class Watch(FeederCase):
    """Issue #36: whether this manifest's feeder is running, and what its last cycle did, from
    the files beside the manifest, never from a process listing."""

    def events(self, paths=None):
        with open((paths or self.paths).events, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def state(self, paths=None):
        with open((paths or self.paths).state, encoding="utf-8") as handle:
            return json.load(handle)

    def call(self, *flags, deps=None):
        args = cli.build_parser().parse_args(["feed", self.manifest_path] + list(flags))
        out = io.StringIO()
        code = cli.cmd_feed(args, self.base_env(), out, deps=deps or self.deps())
        return code, out.getvalue()

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def record(self, paths, **process):
        body = dict({"pid": os.getpid(), "hostname": feeder.socket.gethostname(),
                     "started_at": "2026-09-19T08:50:00", "cycle": 4}, **process)
        self.write(paths.state, json.dumps(dict(feeder.new_state(), process=body)))

    def test_a_run_records_its_process_and_every_event_names_its_manifest_and_pid(self):
        self.plans = [{"2": halted(1500)}]
        self.assertEqual(self.feed(), 0)
        events = self.events()
        self.assertEqual([event["event"] for event in events],
                         ["started", "cycle_started", "cycle_result", "leaving"])
        for event in events:
            self.assertEqual((event["manifest"], event["pid"]),
                             (self.paths.manifest, os.getpid()))
        self.assertEqual(events[1]["appended"], ["1", "2", "3"])
        self.assertEqual(events[1]["cycle"], 1)
        self.assertEqual((events[2]["run_exit"], events[2]["landed"], events[2]["halted"]),
                         (2, ["1", "3"], ["2"]))
        self.assertEqual((events[3]["reason"], events[3]["exit_code"]), ("stop_file", 0))
        process = self.state()["process"]
        self.assertEqual((process["pid"], process["cycle"], process["left_reason"],
                          process["exit_code"], process["runner_tree"]),
                         (os.getpid(), 1, "stop_file", 0, feeder.runner_tree()))
        self.assertEqual(self.state()["last_cycle"]["result"], events[2])

    def test_a_wait_is_an_event_with_its_reason_and_when_it_ends(self):
        self.plans = [3, {}]
        self.feed()
        waits = [event for event in self.events() if event["event"] == "waiting"]
        self.assertEqual([(event["reason"], event["seconds"], event["until"]) for event in waits],
                         [("lease_held", 600, "2026-09-19T09:00:00")])
        results = [event for event in self.events() if event["event"] == "cycle_result"]
        self.assertEqual([event["run_exit"] for event in results], [3, 0])

    def test_a_usage_limit_wait_says_so(self):
        self.plans = [{"1": halted(20), "2": halted(20), "3": halted(20)}, {}]
        self.feed()
        self.assertIn(("waiting", "usage_limit"),
                      [(event["event"], event.get("reason")) for event in self.events()])

    def test_each_way_of_leaving_carries_its_own_reason(self):
        self.adapter.ready_cards = []
        self.assertEqual(self.feed(), 0)
        self.assertEqual(self.events()[-1]["reason"], "empty_queue")
        self.adapter.ready_cards = [card(1)]
        self.plans = [1]
        self.assertEqual(self.feed(), 1)
        self.assertEqual((self.events()[-1]["reason"], self.events()[-1]["exit_code"]),
                         ("run_refused", 1))
        os.unlink(self.paths.stop)
        self.adapter.ready_cards = [card(2)]
        self.plans = [{}]
        self.assertEqual(self.feed(once=True), 0)
        self.assertEqual(self.events()[-1]["reason"], "once")
        self.assertEqual(self.state()["process"]["left_reason"], "once")

    def test_a_crash_is_recorded_before_it_raises(self):
        def boom(manifest_path, retry_ids=()):
            raise RuntimeError("the disk went away")
        original = self.deps
        self.deps = lambda: SimpleNamespace(**dict(vars(original()), run_cycle=boom))
        with self.assertRaises(RuntimeError):
            self.feed()
        self.assertEqual((self.events()[-1]["reason"], self.events()[-1]["exit_code"]),
                         ("crashed", None))
        self.assertIn("the disk went away", self.state()["process"]["left_message"])

    def test_liveness_is_the_recorded_pid_holding_this_manifests_lock(self):
        self.assertFalse(feeder.liveness(self.paths, {})["running"])
        self.record(self.paths)
        held = feeder.acquire_lock(self.paths)
        try:
            answer = feeder.liveness(self.paths, feeder.read_state(self.paths))
        finally:
            held.close()
        self.assertEqual((answer["running"], answer["pid"]), (True, os.getpid()))
        # The same live pid without the lock is some other process, never this feeder.
        answer = feeder.liveness(self.paths, feeder.read_state(self.paths))
        self.assertFalse(answer["running"])
        self.assertIn("does not hold", answer["detail"])

    def test_a_dead_pid_or_a_recorded_leave_is_not_running(self):
        self.record(self.paths, pid=self.dead_pid())
        answer = feeder.liveness(self.paths, feeder.read_state(self.paths))
        self.assertFalse(answer["running"])
        self.assertIn("recorded no leaving", answer["detail"])
        self.record(self.paths, left_at="2026-09-19T09:10:00", exit_code=0,
                    left_reason="stop_file", left_message="stop file present, leaving")
        answer = feeder.liveness(self.paths, feeder.read_state(self.paths))
        self.assertFalse(answer["running"])
        self.assertIn("left at 2026-09-19T09:10:00 with exit 0 (stop_file)", answer["detail"])

    def test_a_lock_held_with_no_record_is_running_and_another_host_is_unknown(self):
        held = feeder.acquire_lock(self.paths)
        try:
            answer = feeder.liveness(self.paths, {})
        finally:
            held.close()
        self.assertEqual((answer["running"], answer["pid"]), (True, None))
        self.record(self.paths, hostname="elsewhere.local")
        answer = feeder.liveness(self.paths, feeder.read_state(self.paths))
        self.assertIsNone(answer["running"])
        self.assertIn("elsewhere.local", answer["detail"])

    def test_a_second_boards_live_feeder_never_answers_for_this_one(self):
        """The incident: a process match found the other board's feeder alive while this board's
        had left on its stop file, and this board sat idle for hours."""
        other = feeder.paths_for(os.path.join(self.tmp.name, "other-board.toml"))
        self.record(other)
        self.record(self.paths, left_at="2026-09-19T09:10:00", exit_code=0,
                    left_reason="stop_file", left_message="stop file present, leaving")
        held = feeder.acquire_lock(other)
        try:
            mine = feeder.liveness(self.paths, feeder.read_state(self.paths))
            theirs = feeder.liveness(other, feeder.read_state(other))
        finally:
            held.close()
        self.assertFalse(mine["running"])
        self.assertTrue(theirs["running"])

    def test_status_prints_liveness_and_the_last_cycle(self):
        self.plans = [{"2": halted(1500)}]
        self.feed()
        code, text = self.call("--status")
        self.assertEqual(code, 0, text)
        self.assertIn("feeder: not running, pid %d left at" % os.getpid(), text)
        self.assertIn("(stop_file)", text)
        self.assertIn("last cycle: 1 started 2026-09-19T08:50:00, appended [1, 2, 3]", text)
        self.assertIn("relay run exited 2 at 2026-09-19T08:50:00: landed [1, 3], halted [2]",
                      text)
        self.assertIn("last event: leaving at 2026-09-19T08:50:00 exit 0 (stop_file)", text)
        code, text = self.call("--status", "--json")
        report = json.loads(text)
        self.assertEqual((report["running"], report["manifest"], report["cycles"]),
                         (False, self.paths.manifest, 1))
        self.assertEqual(report["last_event"]["event"], "leaving")

    def test_status_before_any_feeder_and_over_a_broken_state_file(self):
        code, text = self.call("--status")
        self.assertEqual(code, 0)
        self.assertIn("feeder: not running, no feeder has recorded itself", text)
        self.assertIn("last cycle: none recorded", text)
        self.write(self.paths.state, "{not json")
        code, text = self.call("--status")
        self.assertEqual(code, 1)
        self.assertIn("could not be read", text)

    def test_events_prints_every_line_and_status_verb_adds_the_feeder_line(self):
        self.plans = [{}]
        self.feed()
        code, text = self.call("--events")
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["event"] for line in text.splitlines()],
                         ["started", "cycle_started", "cycle_result", "leaving"])
        args = cli.build_parser().parse_args(["status", self.manifest_path])
        out = io.StringIO()
        self.assertEqual(cli.cmd_status(args, self.base_env(), out), 0)
        self.assertIn("feeder: not running, pid %d left at" % os.getpid(), out.getvalue())

    def test_follow_prints_only_new_events_and_ends_on_leaving(self):
        self.plans = [{}]
        self.feed()                                         # four old events, not reprinted
        leaving = {"event": "leaving", "manifest": self.paths.manifest, "reason": "stop_file"}
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            with open(self.paths.events, "a", encoding="utf-8") as handle:
                if len(polls) == 1:
                    handle.write('{"event": "waiting", "reason": "idle"}\n{"event": "lea')
                else:
                    handle.write(json.dumps(leaving)[len('{"event": "lea'):] + "\n")
        held = feeder.acquire_lock(self.paths)
        try:
            deps = self.deps()
            deps.sleep = sleep
            code, text = self.call("--follow", deps=deps)
        finally:
            held.close()
        self.assertEqual(code, 0)
        self.assertEqual([json.loads(line)["event"] for line in text.splitlines()],
                         ["waiting", "leaving"])
        self.assertEqual(polls, [feeder.FOLLOW_POLL_SECONDS] * 2)

    def test_follow_ends_with_its_own_line_when_no_feeder_is_running(self):
        self.record(self.paths, pid=self.dead_pid())
        lines, polls = [], []
        feeder.follow_events(self.paths, lines.append, polls.append,
                             now=lambda: self.clock)
        self.assertEqual(len(polls), feeder.FOLLOW_GRACE_POLLS - 1)
        last = json.loads(lines[-1])
        self.assertEqual((last["event"], last["manifest"]), ("not_running", self.paths.manifest))
        self.assertIn("recorded no leaving", last["detail"])


class RealRunner(FeederCase):
    def test_one_cycle_through_the_real_runner_over_the_stub(self):
        """The seam the fakes cannot prove: the feeder launches `relay_cli.py run` from its own
        tree, reads that run's exit code, and finds its tasks in the real summary. The ready
        list is the real markdown adapter reading tracker.md at the remote head."""
        self.task_success("T-1")
        self.closeout_landed("T-1")
        self.task_success("T-2")
        self.closeout_landed("T-2")
        env = self.base_env()
        config = feeder.Config(batch=2, caffeinate=False, default_model="sonnet",
                               default_effort="low")
        deps = feeder.build_deps(config, env, sleep=self.sleeps.append,
                                 child_stdout=subprocess.DEVNULL)
        self.assertTrue(feeder.runner_entry().startswith(_paths.SCRIPTS_DIR))
        out = io.StringIO()
        code = feeder.Feeder(self.paths, config, deps, env, out, once=True).run()
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.listed(), ["T-1", "T-2"])
        self.assertIn("relay run exited 0", out.getvalue())
        self.assertIn("cycle result: landed ['T-1', 'T-2'], halted []", out.getvalue())
        self.assertIn("- [x] T-1", self.tracker_at_remote())
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()

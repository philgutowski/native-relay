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
from datetime import datetime
from types import SimpleNamespace

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
        self.records, self.lease = {}, []
        self.run_record = {}     # the summary's run level keys: run_status, halt_task, halt_class
        self.before_run = None

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

    def _run_cycle(self, manifest_path):
        if self.before_run:
            self.before_run()
        listed = self.listed()
        self.runs.append(listed)
        plan = self.plans.pop(0) if self.plans else {}
        if not self.plans:
            feeder.request_stop(self.paths)
        if isinstance(plan, int):
            return plan
        excluded = manifestedit.excluded_ids(self.text())
        for task_id in listed:
            if self.records.get(task_id, {}).get("status") in ("landed", "blocked"):
                continue
            outcome = plan.get(task_id, "landed")
            record = dict(outcome) if isinstance(outcome, dict) else {"status": outcome,
                                                                      "wall_seconds": 1500}
            if task_id in excluded:
                record = {"status": "excluded"}
            self.records[task_id] = dict(record, id=task_id)
        return 2 if any(r["status"] == "halted" for r in self.records.values()) else 0

    def deps(self):
        return feeder.Deps(
            sleep=self.sleeps.append, now=lambda: datetime(2026, 9, 19, 8, 50),
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

    def test_a_bad_sidecar_is_exit_1_before_anything_runs(self):
        self.write(self.paths.config, "[feeder]\nbatch = -1\n")
        code, text = self.call("--once")
        self.assertEqual(code, 1)
        self.assertIn("batch must be a positive integer", text)
        self.assertEqual(self.runs, [])


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

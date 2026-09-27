"""Dispatch: overlapping claude and grok builds, merges in pair order."""
import os
import unittest

import _paths
import _repo
from relay import contracts, gitread, manifest as mf, pair, run as runner, state
from test_run import CLOSE_SH, COMMENT_SH, HELPER, MANIFEST, RunCase, task_branch_sh

GROK_COMPLETE = os.path.join(_paths.FIXTURES_DIR, "backends", "grok",
                             "session-transcript-complete.jsonl")

MIXED_TASKS = '''
[defaults]
backend = "claude"

[[tasks]]
id = "T-1"
model = "sonnet"
effort = "low"

[[tasks]]
id = "T-2"
model = "grok-4.6"
effort = "low"
backend = "grok"
reason = "mechanical work, a good use of the grok account"

[[tasks]]
id = "T-3"
model = "sonnet"
effort = "low"

[[tasks]]
id = "T-4"
model = "grok-4.6"
effort = "low"
backend = "grok"
reason = "mechanical work, a good use of the grok account"
'''

TRACKER_FOUR = """# Tasks

- [ ] T-1 Add the brief renderer
- [ ] T-2 Wire the run loop
- [ ] T-3 Write the summary
- [ ] T-4 Add the pair split
"""


def _group_alive(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_group_quietly(group):
    """A test's own cleanup, so a group the runner failed to end never outlives the test."""
    import signal
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass


class DispatchCase(RunCase):
    def setUp(self):
        super().setUp()
        # Rebuild the repo with four tracker lines.
        self.tmp.cleanup()
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = _repo.make_repo(self.tmp.name, files={"tracker.md": TRACKER_FOUR,
                                                          "README.md": "# fixture\n"})
        self.home = os.path.join(self.tmp.name, "home")
        self.queue = os.path.join(self.tmp.name, "queue")
        os.makedirs(self.home)
        os.makedirs(self.queue)
        self.helper = os.path.join(self.tmp.name, "helper.py")
        with open(self.helper, "w") as handle:
            handle.write(__import__("textwrap").dedent(HELPER))
        self.manifest_path = os.path.join(self.tmp.name, "manifest.toml")
        head, _, _ = MANIFEST.partition("[[tasks]]")
        text = head.replace("__REPO__", self.repo) + MIXED_TASKS
        with open(self.manifest_path, "w") as handle:
            handle.write(text)
        self.manifest = mf.load(self.manifest_path)
        self.entry = 0

    def grok_success(self, task_id, sleep=0):
        self.queue_entry(GROK_COMPLETE, task_branch_sh(task_id, self.manifest.project.branch_prefix),
                         sleep=sleep, backend="grok")

    def task_success(self, task_id, sleep=0):
        self.queue_entry("success.jsonl", task_branch_sh(task_id, self.manifest.project.branch_prefix),
                         sleep=sleep, backend="claude")

    def closeout_landed(self, task_id, backend="claude"):
        self.queue_entry("closeout_skipped.jsonl", CLOSE_SH % (task_id, task_id), backend=backend)

    def grok_closeout_landed(self, task_id):
        self.closeout_landed(task_id, backend="grok")

    def closeout_halted(self, task_id, backend="claude"):
        self.queue_entry("closeout_skipped.jsonl", COMMENT_SH % (task_id, task_id), backend=backend)

    def go_dispatch(self, **kwargs):
        kwargs.setdefault("base_env", self.base_env())
        kwargs.setdefault("home", self.home)
        kwargs.setdefault("stream", lambda line: None)
        kwargs.setdefault("launch_kwargs", {"sigkill_grace_seconds": 2, "heartbeat_interval": 0})
        return runner.dispatch(self.manifest, **kwargs)

    def land_all_four(self, t1_sleep=0, t2_sleep=0):
        self.task_success("T-1", sleep=t1_sleep)
        self.closeout_landed("T-1")
        self.grok_success("T-2", sleep=t2_sleep)
        self.grok_closeout_landed("T-2")
        self.task_success("T-3")
        self.closeout_landed("T-3")
        self.grok_success("T-4")
        self.grok_closeout_landed("T-4")


class DispatchEndToEnd(DispatchCase):
    def test_four_tasks_land_in_manifest_order_even_when_grok_finishes_first(self):
        self.land_all_four(t1_sleep=2, t2_sleep=0)
        outcome = self.go_dispatch()
        self.assertEqual(outcome.exit_code, runner.EXIT_OK, outcome.message)
        records = self.store().records()
        for task_id in ("T-1", "T-2", "T-3", "T-4"):
            self.assertEqual(records[task_id]["status"], contracts.STATUS_LANDED, task_id)
        self.assertEqual(records["T-1"]["backend"], "claude")
        self.assertEqual(records["T-2"]["backend"], "grok")
        log = _repo.git(self.repo, "log", "--format=%s", "origin/main").stdout.splitlines()
        # Newest first. The four merge commits plus closeouts sit above the initial commit.
        merges = [line for line in log if line.startswith("Merge relay task")]
        self.assertEqual(merges, [
            "Merge relay task T-4 from relay/T-4",
            "Merge relay task T-3 from relay/T-3",
            "Merge relay task T-2 from relay/T-2",
            "Merge relay task T-1 from relay/T-1",
        ])
        self.assertEqual(self.relay_branches(), [])
        self.assertTrue(gitread.is_clean(self.repo))
        self.assertEqual(gitread.current_branch(self.repo), "main")

    def test_the_default_serial_policy_does_not_overlap_backends_in_wall_clock(self):
        overlap = os.path.join(self.tmp.name, "overlap.log")
        env = self.base_env()
        env["OVERLAP_LOG"] = overlap
        t1_sh = task_branch_sh("T-1") + (
            'echo "T-1 start $(python3 -c \'import time; print(time.time())\')" >> "$OVERLAP_LOG"\n'
            "sleep 2\n"
            'echo "T-1 end $(python3 -c \'import time; print(time.time())\')" >> "$OVERLAP_LOG"\n'
        )
        t2_sh = task_branch_sh("T-2") + (
            'echo "T-2 start $(python3 -c \'import time; print(time.time())\')" >> "$OVERLAP_LOG"\n'
        )
        self.queue_entry("success.jsonl", t1_sh, backend="claude")
        self.closeout_landed("T-1")
        self.queue_entry(GROK_COMPLETE, t2_sh, backend="grok")
        self.grok_closeout_landed("T-2")
        self.task_success("T-3")
        self.closeout_landed("T-3")
        self.grok_success("T-4")
        self.grok_closeout_landed("T-4")
        outcome = self.go_dispatch(base_env=env)
        self.assertEqual(outcome.exit_code, runner.EXIT_OK, outcome.message)
        with open(overlap) as handle:
            lines = handle.read()
        self.assertIn("T-1 start", lines)
        self.assertIn("T-2 start", lines)
        self.assertIn("T-1 end", lines)
        times = {}
        for line in lines.splitlines():
            name, _, stamp = line.partition(" ")
            when = line.rsplit(" ", 1)[-1]
            times[line.split(" ", 2)[0] + " " + line.split(" ", 2)[1]] = float(when)
        self.assertGreaterEqual(times["T-2 start"], times["T-1 end"])

    def test_a_halt_that_does_not_continue_past_abandons_the_sibling(self):
        # T-1 claims complete but leaves no branch, which is unclean_exit. T-2 is still
        # building. The halt must kill T-2, drop its worktree, and leave T-3 and T-4 unlaunched.
        self.queue_entry("success.jsonl", git_sh=None, sleep=0, backend="claude")
        self.closeout_halted("T-1")
        self.grok_success("T-2", sleep=3)
        self.task_success("T-3")
        self.closeout_landed("T-3")
        self.grok_success("T-4")
        self.grok_closeout_landed("T-4")
        outcome = self.go_dispatch()
        self.assertEqual(outcome.exit_code, runner.EXIT_HALTED, outcome.message)
        records = self.store().records()
        self.assertEqual(records["T-1"]["status"], contracts.STATUS_HALTED)
        # Under the serial-default schedule T-2 never launches, which is stronger than
        # abandoning an already-running sibling.
        t2 = records.get("T-2") or {}
        self.assertIn(t2.get("status"), (None, contracts.STATUS_PENDING))
        self.assertFalse(gitread.branch_exists(self.repo, "relay/T-2"))
        self.assertFalse(os.path.isdir(self.store().path("worktrees", "T-2")))
        self.assertNotEqual(records.get("T-3", {}).get("status"), contracts.STATUS_LANDED)
        self.assertFalse(gitread.branch_exists(self.repo, "relay/T-3"))

    def interrupt_once_building(self, env):
        """Dispatch with T-1 building in a real stub process that has a sleeping grandchild, and
        a keyboard interrupt on the main thread once the build's process group exists. The
        interrupt reaches the main thread only: the Task process sits in its own session and its
        launch, on a worker thread, installed no signal handler. Returns the group id."""
        from unittest import mock
        env["RELAY_STUB_CHILD"] = "1"
        self.task_success("T-1", sleep=60)
        seen = []
        real_wait = runner._wait_any_flight

        def interrupted(slots, timeout=0.1):
            for flight in slots.values():
                if flight.pgid:
                    seen.append(flight.pgid[0])
                    self.addCleanup(_kill_group_quietly, flight.pgid[0])
                    raise KeyboardInterrupt()
            return real_wait(slots, timeout)

        with mock.patch.object(runner, "_wait_any_flight", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.go_dispatch(base_env=env)
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_an_interrupted_dispatch_ends_its_task_processes_before_it_marks_anything(self):
        """Issue #71. Nothing ended a flight on the way out of an interrupted dispatch, so the
        Task process kept building and moving cards while its record read crashed and the lease
        was free for a relaunch to start beside it. Now the whole group is gone before any record
        is marked, and the build is abandoned the way a halt that does not continue past does."""
        group = self.interrupt_once_building(self.base_env())
        self.assertFalse(_group_alive(group))
        record = self.store().get("T-1")
        self.assertEqual(record["status"], contracts.STATUS_PENDING)
        self.assertNotEqual(record.get("halt_class"), contracts.HALT_RUNNER_CRASHED)
        self.assertFalse(os.path.isdir(self.store().path("worktrees", "T-1")))
        self.assertFalse(gitread.branch_exists(self.repo, "relay/T-1"))
        self.assertIsNone(self.store().lease())
        terminal = self.store().terminal()
        self.assertEqual(terminal["run_status"], contracts.RUN_CRASHED)
        self.assertNotIn("surviving_flights", terminal)

    def test_a_flight_that_will_not_die_is_named_and_never_marked_crashed(self):
        """Issue #71. A group still alive at the bound is named in the terminal record, its
        record keeps reading running because something is still driving it, and its worktree is
        left for the process using it. The lease is released regardless."""
        from unittest import mock
        with mock.patch.object(runner, "_signal_group"), \
                mock.patch.object(runner, "FLIGHT_EXIT_SECONDS", 0.5):
            group = self.interrupt_once_building(self.base_env())
        self.assertTrue(_group_alive(group))
        record = self.store().get("T-1")
        self.assertEqual(record["status"], contracts.STATUS_RUNNING)
        self.assertTrue(os.path.isdir(self.store().path("worktrees", "T-1")))
        self.assertIsNone(self.store().lease())
        terminal = self.store().terminal()
        self.assertEqual(terminal["run_status"], contracts.RUN_CRASHED)
        self.assertEqual(terminal["surviving_flights"],
                         [{"task": "T-1", "process_group": group}])

    def test_an_interrupt_after_a_halts_abort_keeps_the_survivor_the_halt_named(self):
        """Issue #71, from the code review. The halt's abort named T-2 and took it out of the
        slots. An interrupt landing before the terminal write aborts again with nothing left in
        the slots, and must not forget T-2, or the finally marks its live record crashed."""
        import threading
        import types
        from unittest import mock
        release = threading.Event()
        self.addCleanup(release.set)
        stuck = threading.Thread(target=release.wait, daemon=True)
        stuck.start()
        flight = runner._Flight(begun=types.SimpleNamespace(task=types.SimpleNamespace(id="T-2"),
                                                            branch="relay/T-2"),
                                worktree=None, thread=stuck, pgid=[], box=[])
        cfg = types.SimpleNamespace(launch_kwargs={"sigkill_grace_seconds": 0}, stream=None,
                                    surviving_flights=[])
        with mock.patch.object(runner, "_abandon_build") as abandon, \
                mock.patch.object(runner, "FLIGHT_EXIT_SECONDS", 0.2):
            runner._abort_siblings(cfg, {"T-2": flight}, {}, "T-1")
            runner._abort_siblings(cfg, {}, {}, None)
        abandon.assert_not_called()
        self.assertEqual(cfg.surviving_flights, [{"task": "T-2", "process_group": None}])

    def test_an_interrupt_leaves_a_finished_build_waiting_its_merge(self):
        """Issue #71, from the code review. A build parked behind an earlier merge has exited
        and its branch is completed work. The interrupt ends flights, not finished builds."""
        import types
        from unittest import mock
        cfg = types.SimpleNamespace(launch_kwargs={}, stream=None, surviving_flights=[])
        begun = types.SimpleNamespace(task=types.SimpleNamespace(id="T-2"), branch="relay/T-2")

        def drive(_cfg, _announce, _slots, waiting):
            waiting["T-2"] = (begun, None)
            raise KeyboardInterrupt()

        with mock.patch.object(runner, "_concurrent_drive", side_effect=drive), \
                mock.patch.object(runner, "_abandon_build") as abandon:
            with self.assertRaises(KeyboardInterrupt):
                runner._concurrent_loop(cfg, None)
        abandon.assert_not_called()
        self.assertEqual(cfg.surviving_flights, [])

    def test_dispatch_of_a_pair_file_uses_the_pair_path_for_state(self):
        self.land_all_four()
        loaded = pair.split(self.manifest, out_dir=self.tmp.name)
        combined = pair.combine(loaded)
        outcome = runner.dispatch(
            combined, home=self.home, base_env=self.base_env(),
            stream=lambda line: None,
            launch_kwargs={"sigkill_grace_seconds": 2, "heartbeat_interval": 0},
        )
        self.assertEqual(outcome.exit_code, runner.EXIT_OK, outcome.message)
        store = state.StateStore(loaded.path, self.repo, home=self.home)
        self.assertEqual(store.terminal()["run_status"], contracts.RUN_COMPLETED)
        self.assertEqual(store.get("T-2")["backend"], "grok")

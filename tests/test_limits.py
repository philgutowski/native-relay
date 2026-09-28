"""U1 of the usage limit plan: the reader that answers confirmed, refuted, or unconfirmed for one
death, and the stub entry keys that let a process die the way the CLI dies at its limit.

U2: the state machine as two pure decisions, one after a run and one at the start of a Cycle,
tested as a table of facts in and a record out, with no Feeder, no clock, and no files."""
import copy
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import _paths  # noqa: F401
import test_run
from relay import contracts, limits, summary

QUICK = 600

# The reset the CLI printed in AE9, 20:20 local, as the epoch seconds the log carries.
RESET = datetime(2026, 9, 27, 20, 20)
RESET_EPOCH = int(RESET.timestamp())

INIT = {"type": "system", "subtype": "init", "session_id": "s", "model": "claude-fable-5-1"}
WARNING = {"type": "rate_limit_event",
           "rate_limit_info": {"status": "allowed_warning", "resetsAt": RESET_EPOCH,
                               "rateLimitType": "seven_day", "utilization": 0.87}}
REJECTED = {"type": "rate_limit_event",
            "rate_limit_info": {"status": "rejected", "resetsAt": RESET_EPOCH,
                                "rateLimitType": "five_hour"}}
# `overageStatus` reads rejected on an account with overage off while the turn itself runs.
ALLOWED_OVERAGE_OFF = {"type": "rate_limit_event",
                       "rate_limit_info": {"status": "allowed", "resetsAt": RESET_EPOCH,
                                           "rateLimitType": "five_hour",
                                           "overageStatus": "rejected"}}
LIMIT_RESULT = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                "terminal_reason": "api_error", "api_error_status": 429,
                "result": "You've hit your session limit. Resets 8:20pm."}
MISSING_RESULT = dict(LIMIT_RESULT, api_error_status=404,
                      result="There's an issue with the selected model.")
DONE_RESULT = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 12,
               "result": "done"}
ASSISTANT = {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}


def log(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


# The real shape recorded under the plan's Sources: a warning above the attempt that belongs to
# no attempt, then the attempt's `init`, the rejected event, and the 429 `result`.
REAL_LIMIT_LOG = log(WARNING, INIT, REJECTED, LIMIT_RESULT)


def died(wall, status="blocked", halt_class="no_envelope", log_path=None):
    return {"status": status, "class": halt_class, "wall_seconds": wall, "log_path": log_path}


class ReadDeath(unittest.TestCase):
    def read(self, record, text):
        return limits.read_death(record, text, QUICK)

    def test_a_429_result_confirms_quick_or_slow(self):
        for wall in (8, 3000):
            with self.subTest(wall=wall):
                self.assertEqual(self.read(died(wall), log(INIT, LIMIT_RESULT)),
                                 (limits.CONFIRMED, None))

    def test_a_404_result_refutes(self):
        self.assertEqual(self.read(died(8), log(INIT, MISSING_RESULT)), (limits.REFUTED, None))

    def test_a_result_with_no_api_error_status_refutes(self):
        self.assertEqual(self.read(died(8), log(INIT, ASSISTANT, DONE_RESULT)),
                         (limits.REFUTED, None))

    def test_no_result_line_on_a_quick_death_is_unconfirmed(self):
        self.assertEqual(self.read(died(8), log(INIT, ASSISTANT)), (limits.UNCONFIRMED, None))

    def test_no_result_line_on_a_slow_death_refutes(self):
        self.assertEqual(self.read(died(3000), log(INIT, ASSISTANT)), (limits.REFUTED, None))

    def test_a_record_with_no_log_that_died_quickly_is_unconfirmed(self):
        record = died(8)
        self.assertEqual(self.read(record, limits.log_tail(record["log_path"])),
                         (limits.UNCONFIRMED, None))

    def test_a_record_with_no_wall_time_refutes_whatever_the_log_says(self):
        for text in (REAL_LIMIT_LOG, "", log(INIT, ASSISTANT)):
            with self.subTest(text=text[:40]):
                self.assertEqual(self.read(died(None), text), (limits.REFUTED, None))

    def test_the_real_shape_confirms_and_returns_the_reset_as_a_time(self):
        """Covers AE9."""
        reading, resets_at = self.read(died(8), REAL_LIMIT_LOG)
        self.assertEqual(reading, limits.CONFIRMED)
        self.assertIsInstance(resets_at, datetime)
        self.assertEqual(resets_at, RESET)

    def test_a_confirmed_log_with_no_rate_limit_event_has_no_reset(self):
        self.assertEqual(self.read(died(8), log(INIT, LIMIT_RESULT)), (limits.CONFIRMED, None))

    def test_a_warning_alone_gives_no_reset(self):
        self.assertEqual(self.read(died(8), log(INIT, WARNING, LIMIT_RESULT)),
                         (limits.CONFIRMED, None))

    def test_an_allowed_event_with_overage_rejected_gives_no_reset(self):
        self.assertEqual(self.read(died(8), log(INIT, ALLOWED_OVERAGE_OFF, LIMIT_RESULT)),
                         (limits.CONFIRMED, None))

    def test_a_rejected_event_above_the_last_init_gives_no_reset(self):
        self.assertEqual(self.read(died(8), log(REJECTED, INIT, LIMIT_RESULT)),
                         (limits.CONFIRMED, None))

    def test_two_rejected_limits_give_the_later_reset(self):
        # A week's limit spent beside a session's: the model is back only when both lift.
        week = dict(REJECTED, rate_limit_info=dict(REJECTED["rate_limit_info"],
                                                   resetsAt=RESET_EPOCH + 3 * 86400,
                                                   rateLimitType="seven_day"))
        later = datetime.fromtimestamp(RESET_EPOCH + 3 * 86400)
        for events in ((week, REJECTED), (REJECTED, week)):
            with self.subTest(first=events[0]["rate_limit_info"]["rateLimitType"]):
                self.assertEqual(self.read(died(8), log(INIT, *events, LIMIT_RESULT)),
                                 (limits.CONFIRMED, later))

    def test_an_earlier_attempts_429_does_not_confirm_the_last(self):
        text = log(INIT, REJECTED, LIMIT_RESULT) + log(INIT, ASSISTANT)
        self.assertEqual(self.read(died(8), text), (limits.UNCONFIRMED, None))

    def test_a_torn_first_line_is_read_past(self):
        tail = REAL_LIMIT_LOG[7:]
        self.assertEqual(self.read(died(8), tail), (limits.CONFIRMED, RESET))
        torn_result = json.dumps(LIMIT_RESULT)[9:] + "\n"
        self.assertEqual(self.read(died(8), torn_result + log(ASSISTANT)),
                         (limits.UNCONFIRMED, None))

    def test_status_and_class_do_not_change_the_reading(self):
        for text in (REAL_LIMIT_LOG, log(INIT, MISSING_RESULT), log(INIT, ASSISTANT)):
            readings = {self.read(died(8, status, halt_class), text)
                        for status in ("halted", "blocked")
                        for halt_class in ("no_envelope", "unclean_exit", "unexpected_error",
                                           None)}
            self.assertEqual(len(readings), 1, readings)

    def test_an_unreadable_reset_is_no_reset(self):
        for value in ("soon", None, True, 10 ** 30):
            event = {"type": "rate_limit_event",
                     "rate_limit_info": {"status": "rejected", "resetsAt": value}}
            with self.subTest(value=value):
                self.assertEqual(self.read(died(8), log(INIT, event, LIMIT_RESULT)),
                                 (limits.CONFIRMED, None))


class AttemptBoundary(unittest.TestCase):
    """Issue #94: the launcher's own line starts an attempt exactly as `init` does, so an attempt
    that printed nothing is not read as the one before it."""

    BOUNDARY = json.loads(limits.attempt_line())

    def read(self, text, wall=0.01):
        return limits.read_death(died(wall), text, QUICK)

    def test_the_line_is_a_system_object_with_the_runners_subtype_and_the_time(self):
        line = limits.attempt_line()
        self.assertTrue(line.endswith("\n"))
        event = json.loads(line)
        self.assertEqual((event["type"], event["subtype"]), ("system", limits.ATTEMPT_SUBTYPE))
        self.assertIsNotNone(datetime.fromisoformat(event["at"]).tzinfo)
        self.assertTrue(limits.is_attempt_boundary(event))
        self.assertFalse(limits.is_attempt_boundary(INIT))

    def test_an_attempt_that_printed_nothing_after_a_429_is_unconfirmed(self):
        text = log(self.BOUNDARY, INIT, REJECTED, LIMIT_RESULT) + log(self.BOUNDARY)
        self.assertEqual(self.read(text), (limits.UNCONFIRMED, None))

    def test_an_attempt_that_printed_nothing_after_a_success_is_unconfirmed(self):
        text = log(self.BOUNDARY, INIT, ASSISTANT, DONE_RESULT) + log(self.BOUNDARY)
        self.assertEqual(self.read(text), (limits.UNCONFIRMED, None))

    def test_an_attempt_that_printed_its_own_429_confirms_with_its_own_reset(self):
        later = dict(REJECTED, rate_limit_info=dict(REJECTED["rate_limit_info"],
                                                    resetsAt=RESET_EPOCH + 3600))
        text = (log(self.BOUNDARY, INIT, REJECTED, LIMIT_RESULT)
                + log(self.BOUNDARY, INIT, later, LIMIT_RESULT))
        self.assertEqual(self.read(text, wall=8),
                         (limits.CONFIRMED, datetime.fromtimestamp(RESET_EPOCH + 3600)))

    def test_an_attempt_that_printed_only_plain_text_is_unconfirmed(self):
        text = (log(self.BOUNDARY, INIT, REJECTED, LIMIT_RESULT)
                + log(self.BOUNDARY) + "error: unknown option '--bogus'\n")
        self.assertEqual(self.read(text), (limits.UNCONFIRMED, None))

    def test_the_boundary_above_an_init_does_not_move_the_attempt(self):
        # A warning between the boundary and `init` still belongs to no attempt.
        self.assertEqual(self.read(log(self.BOUNDARY, REJECTED, INIT, LIMIT_RESULT), wall=8),
                         (limits.CONFIRMED, None))
        self.assertEqual(limits.result_event(log(self.BOUNDARY, INIT, LIMIT_RESULT)),
                         LIMIT_RESULT)
        self.assertIsNone(limits.result_event(log(INIT, LIMIT_RESULT, self.BOUNDARY)))


class LogTail(unittest.TestCase):
    def test_a_missing_file_reads_empty(self):
        self.assertEqual(limits.log_tail(None), "")
        self.assertEqual(limits.log_tail("/nonexistent/relay/task.stdout.log"), "")


class ThroughTheStub(test_run.RunCase):
    """The stub's `init` and `result_lines` keys, through the real runner, end a Task the way the
    CLI ends one at its limit, and the record and log it leaves read confirmed."""

    def test_an_entry_with_the_new_keys_leaves_a_log_that_reads_confirmed(self):
        self.manifest = replace(self.manifest, tasks=self.manifest.tasks[:1])
        self.queue_entry("no_envelope.jsonl")
        path = os.path.join(self.queue, str(self.entry), "entry.json")
        with open(path) as handle:
            entry = json.load(handle)
        entry.update(init=True, result_lines=[REJECTED, LIMIT_RESULT])
        with open(path, "w") as handle:
            json.dump(entry, handle)
        self.closeout_blocked("T-1")
        self.go()
        # The record as the feeder reads it: the summary's task entry, which names the log.
        record, = summary.build(self.manifest, self.store())["tasks"]
        # The path a real limit death takes: no envelope, recorded blocked.
        self.assertEqual((record["status"], record["class"]),
                         (contracts.STATUS_BLOCKED, contracts.HALT_NO_ENVELOPE))
        text = limits.log_tail(record["log_path"])
        self.assertNotIn("stub_done", text)
        self.assertIn('"subtype": "init"', text)
        self.assertEqual(limits.read_death(record, text, QUICK), (limits.CONFIRMED, RESET))


# U2. The Cycle facts the tables below are built from. NOW is AE9's death, 20:06, fourteen minutes
# before RESET.
NOW = datetime(2026, 9, 27, 20, 6)
BEFORE_RUN = "2026-09-27T19:00:00"
THIS_RUN = "2026-09-27T20:00:00"
SETTINGS = limits.Settings(quick_death_seconds=QUICK, limit_wait_seconds=1800,
                           limit_waits_max=16, max_halts=2, fallback_hours=5, batch=3)
FIVE_HOURS = timedelta(hours=5)
MUTUAL = {"fable": "opus", "opus": "fable"}

CONFIRMED = (limits.CONFIRMED, None)
REFUTED = (limits.REFUTED, None)
UNCONFIRMED = (limits.UNCONFIRMED, None)


def record(task_id, status, model="fable", wall=8, started=THIS_RUN, halt_class="no_envelope"):
    return {"id": task_id, "status": status, "model": model, "wall_seconds": wall,
            "started_at": started, "class": halt_class if status != "landed" else None}


def landed(task_id, model="sonnet"):
    return record(task_id, "landed", model=model, wall=900)


def facts(records, readings=None, before=None, **overrides):
    """This Cycle's facts: `records` after the run, and before it each launched one had no record
    unless `before` says otherwise. The model each died on is the record's own."""
    after = {rec["id"]: rec for rec in records}
    values = dict(before=before or {}, after=after, readings=readings or {},
                  died_on={task_id: rec["model"] for task_id, rec in after.items()},
                  listed_on={task_id: rec["model"] for task_id, rec in after.items()},
                  died_at={}, fallback={}, marks={}, streak=0, halts={}, deferred=frozenset(),
                  passed_over=frozenset(), retries=frozenset(), now=NOW, settings=SETTINGS)
    values.update(overrides)
    return limits.CycleFacts(**values)


def fallback_mark(since=NOW):
    return limits.Mark(since=since, until=since + FIVE_HOURS, source=limits.MARK_FALLBACK_HOURS)


class AfterRun(unittest.TestCase):
    """`decide_after_run`: one Cycle's facts in, one decision record out."""

    def decide(self, *args, **kwargs):
        return limits.decide_after_run(facts(*args, **kwargs))

    def assertGoesRound(self, decision):
        self.assertEqual(decision.outcome, limits.GO_ROUND)

    def test_ae1_the_mutual_fallback_walk_marks_moves_holds_and_moves_back(self):
        """Covers AE1. Sonnet lands beside T every Cycle, so the streak never moves off 0."""
        # Cycle 1: T dies on fable with a 429. Fable is marked and T moves to opus.
        first = self.decide([record("T", "blocked", "fable"), landed("S1")],
                            {"T": CONFIRMED}, fallback=MUTUAL)
        self.assertEqual(first.marks, {"fable": fallback_mark()})
        self.assertEqual(first.notify, ("fable",))
        self.assertEqual(first.moves, (("T", "fable", "opus"),))
        self.assertEqual((first.holds, first.retry, first.report), ((), ("T",), ()))
        self.assertEqual(first.streak, 0)
        self.assertGoesRound(first)
        marks = dict(first.marks)

        # Cycle 2 starts an hour later: T is listed on opus, which is free, so it is retried.
        later = NOW + timedelta(hours=1)
        start = limits.plan_cycle_start(["T"], {"T"}, {"T": "opus"}, marks, MUTUAL, later,
                                        SETTINGS)
        self.assertEqual((start.moves, start.retry, start.defer), ((), ("T",), ()))
        # T dies on opus with a 429. Opus is marked, fable is still marked, so T is held.
        second = self.decide([record("T", "blocked", "opus", started="2026-09-27T21:06:00"),
                              landed("S2")], {"T": CONFIRMED},
                             before={"T": record("T", "blocked", "fable")},
                             fallback=MUTUAL, marks=marks, now=later)
        self.assertEqual(second.marks, {"opus": fallback_mark(later)})
        self.assertEqual(second.notify, ("opus",))
        self.assertEqual((second.moves, second.holds), ((), (("T", "opus"),)))
        self.assertEqual((second.retry, second.report, second.streak), (("T",), (), 0))
        self.assertGoesRound(second)
        marks.update(second.marks)

        # While both marks stand, T is held: not retried, not deferred, keeping its queue place.
        held = limits.plan_cycle_start(["T"], {"T"}, {"T": "opus"}, marks, MUTUAL,
                                       later + timedelta(hours=1), SETTINGS)
        self.assertEqual((held.moves, held.retry, held.defer, held.held),
                         ((), (), (), (("T", "opus"),)))

        # Fable's mark expires: T moves back to fable before the run and is retried there.
        back = limits.plan_cycle_start(["T"], {"T"}, {"T": "opus"}, marks, MUTUAL,
                                       NOW + FIVE_HOURS + timedelta(minutes=1), SETTINGS)
        self.assertEqual((back.moves, back.retry, back.defer),
                         ((("T", "opus", "fable"),), ("T",), ()))

    def test_ae2_a_quick_unconfirmed_blocked_death_beside_a_landing_is_reported(self):
        """Covers AE2."""
        decision = self.decide([record("T", "blocked", "fable"), landed("S")],
                               {"T": UNCONFIRMED}, fallback=MUTUAL)
        self.assertEqual((decision.marks, decision.moves, decision.holds), ({}, (), ()))
        self.assertEqual((decision.report, decision.retry), (("T",), ()))
        self.assertGoesRound(decision)

    def test_ae3_a_landing_does_not_keep_two_confirmed_deaths_off_their_model(self):
        """Covers AE3."""
        decision = self.decide([landed("1", "fable"), record("2", "blocked", "fable"),
                                record("3", "blocked", "fable")],
                               {"2": CONFIRMED, "3": CONFIRMED})
        self.assertEqual(set(decision.marks), {"fable"})
        self.assertEqual(decision.holds, (("2", "fable"), ("3", "fable")))
        self.assertEqual((decision.moves, decision.retry, decision.report), ((), ("2", "3"), ()))
        # The next Cycle routes no card to fable: it is held, and so are 2 and 3.
        self.assertTrue(limits.is_held("fable", decision.marks, {}, NOW))
        start = limits.plan_cycle_start(["2", "3"], {"2", "3"}, {"2": "fable", "3": "fable"},
                                        decision.marks, {}, NOW, SETTINGS)
        self.assertEqual((start.retry, start.defer, start.room), ((), (), 3))

    def test_ae4_a_quick_halt_with_a_404_is_an_ordinary_halt(self):
        """Covers AE4."""
        decision = self.decide([record("T", "halted", "fable", halt_class="unclean_exit")],
                               {"T": REFUTED}, fallback={"fable": "opus"})
        self.assertEqual((decision.marks, decision.moves, decision.holds), ({}, (), ()))
        self.assertEqual(decision.halts, {"T": 1})
        self.assertEqual(decision.exclude, ())

    def test_a_landing_on_the_same_model_does_not_stop_the_move_either(self):
        decision = self.decide([landed("1", "fable"), record("2", "blocked", "fable")],
                               {"2": CONFIRMED}, fallback={"fable": "opus"})
        self.assertEqual(set(decision.marks), {"fable"})
        self.assertEqual((decision.moves, decision.holds), ((("2", "fable", "opus"),), ()))
        self.assertEqual(decision.retry, ("2",))

    def test_two_models_that_fall_back_to_each_other_both_die_and_both_hold(self):
        decision = self.decide([record("A", "blocked", "fable"), record("B", "blocked", "opus"),
                                landed("S")], {"A": CONFIRMED, "B": CONFIRMED}, fallback=MUTUAL)
        self.assertEqual(set(decision.marks), {"fable", "opus"})
        self.assertEqual(decision.notify, ("fable", "opus"))
        self.assertEqual(decision.moves, ())
        self.assertEqual(decision.holds, (("A", "fable"), ("B", "opus")))

    def test_a_confirmed_halted_death_is_never_counted_moved_or_held(self):
        for table, moves, holds in (({"fable": "opus"}, (("T", "fable", "opus"),), ()),
                                    ({}, (), (("T", "fable"),))):
            with self.subTest(fallback=table):
                decision = self.decide([record("T", "halted", "fable")], {"T": CONFIRMED},
                                       fallback=table, halts={"T": 1})
                self.assertEqual((decision.moves, decision.holds), (moves, holds))
                self.assertEqual((decision.halts, decision.exclude), ({}, ()))
                # A halted Task is not a retry: the next run launches it, or it is deferred.
                self.assertEqual(decision.retry, ())

    def test_a_confirmed_death_on_a_marked_model_restamps_quietly(self):
        old = fallback_mark(NOW - timedelta(hours=1))
        decision = self.decide([record("T", "halted", "fable")], {"T": CONFIRMED},
                               marks={"fable": old})
        self.assertEqual(decision.marks, {"fable": fallback_mark()})
        self.assertEqual(decision.notify, ())

    def test_ae6_three_unconfirmed_quick_deaths_and_no_landing_wait(self):
        """Covers AE6."""
        decision = self.decide([record("A", "halted"), record("B", "halted", "opus"),
                                record("C", "blocked", "sonnet")],
                               {"A": UNCONFIRMED, "B": UNCONFIRMED, "C": UNCONFIRMED},
                               streak=0)
        self.assertEqual(decision.outcome, limits.Outcome(limits.WAIT, reason="usage_limit",
                                                          seconds=1800))
        self.assertEqual(decision.streak, 1)
        self.assertEqual((decision.halts, decision.report), ({}, ()))
        self.assertEqual(decision.retry, ("C",))
        self.assertEqual((decision.marks, decision.moves, decision.holds), ({}, (), ()))

    def test_a_confirmed_death_beside_an_unconfirmed_one_still_marks_and_waits(self):
        decision = self.decide([record("A", "blocked", "fable"), record("B", "halted", "opus")],
                               {"A": CONFIRMED, "B": UNCONFIRMED}, fallback={"fable": "sonnet"})
        self.assertEqual(set(decision.marks), {"fable"})
        self.assertEqual(decision.moves, (("A", "fable", "sonnet"),))
        self.assertEqual(decision.retry, ("A",))
        self.assertEqual(decision.outcome.kind, limits.WAIT)
        self.assertEqual(decision.outcome.reason, "usage_limit")
        self.assertEqual((decision.streak, decision.halts), (1, {}))

    def test_every_death_confirmed_and_moved_does_not_wait(self):
        decision = self.decide([record("A", "blocked", "fable"), record("B", "halted", "fable")],
                               {"A": CONFIRMED, "B": CONFIRMED}, fallback={"fable": "opus"},
                               streak=3)
        self.assertEqual(len(decision.moves), 2)
        self.assertGoesRound(decision)
        self.assertEqual(decision.streak, 3)

    def test_a_landing_beside_an_unconfirmed_quick_halt_counts_it_and_clears_the_streak(self):
        decision = self.decide([record("A", "halted"), landed("S")], {"A": UNCONFIRMED},
                               streak=3)
        self.assertEqual(decision.halts, {"A": 1})
        self.assertEqual(decision.streak, 0)
        self.assertGoesRound(decision)

    def test_a_slow_death_alone_clears_the_streak(self):
        decision = self.decide([record("A", "halted", wall=3000)], {"A": REFUTED}, streak=3)
        self.assertEqual((decision.halts, decision.streak), ({"A": 1}, 0))
        self.assertGoesRound(decision)

    def test_nothing_dying_clears_the_streak(self):
        self.assertEqual(self.decide([landed("S")], streak=3).streak, 0)
        self.assertEqual(self.decide([], streak=3).streak, 0)

    def test_one_more_waited_cycle_past_the_maximum_leaves(self):
        decision = self.decide([record("A", "halted"), record("C", "blocked", "sonnet")],
                               {"A": UNCONFIRMED, "C": UNCONFIRMED}, streak=16)
        self.assertEqual(decision.outcome, limits.Outcome(
            limits.LEAVE, reason="limit_waits_exhausted", code=limits.LEAVE_EXIT))
        self.assertEqual(limits.LEAVE_EXIT, 2)
        self.assertEqual((decision.report, decision.retry), (("C",), ()))
        self.assertEqual((decision.streak, decision.halts), (0, {}))

    def test_the_last_wait_before_the_maximum_still_waits(self):
        decision = self.decide([record("A", "halted")], {"A": UNCONFIRMED}, streak=15)
        self.assertEqual((decision.outcome.kind, decision.streak), (limits.WAIT, 16))

    def test_an_unlaunched_blocked_record_with_a_429_marks_and_moves_nothing(self):
        stale = record("T", "blocked", "fable", started=BEFORE_RUN)
        for queued in (frozenset(), frozenset({"T"})):
            with self.subTest(queued=bool(queued)):
                decision = self.decide([stale], {"T": CONFIRMED}, before={"T": stale},
                                       fallback={"fable": "opus"}, retries=queued)
                self.assertEqual((decision.marks, decision.moves, decision.holds), ({}, (), ()))
                self.assertEqual(decision.retry, ())
                # Still queued, the run never reached it: its report was made when it blocked.
                self.assertEqual(decision.report, () if queued else ("T",))

    def test_ae8_a_halt_refused_before_launch_is_counted_and_excluded_at_max_halts(self):
        """Covers AE8. Refused in two Cycles in a row."""
        stale = record("T", "halted", started=BEFORE_RUN, halt_class="unclean_exit")
        first = self.decide([stale], {"T": CONFIRMED}, before={"T": stale})
        self.assertEqual((first.halts, first.exclude, first.marks), ({"T": 1}, (), {}))
        second = self.decide([stale], {"T": CONFIRMED}, before={"T": stale}, halts=first.halts)
        self.assertEqual((second.halts, second.exclude), ({"T": 2}, ("T",)))

    def test_a_halt_deferred_or_passed_over_is_not_counted(self):
        stale = record("T", "halted", started=BEFORE_RUN)
        for key in ("deferred", "passed_over"):
            with self.subTest(key=key):
                decision = self.decide([stale], {"T": REFUTED}, before={"T": stale},
                                       **{key: frozenset({"T"})})
                self.assertEqual((decision.halts, decision.exclude, decision.report),
                                 ({}, (), ()))

    def test_ae9_the_mark_ends_at_the_cli_reset_when_it_lies_ahead(self):
        """Covers AE9."""
        ahead = self.decide([record("T", "halted")], {"T": (limits.CONFIRMED, RESET)})
        self.assertEqual(ahead.marks, {"fable": limits.Mark(
            since=NOW, until=RESET, source=limits.MARK_CLI)})
        self.assertEqual(RESET - NOW, timedelta(minutes=14))
        # Issue #96: a reset that passed after the death means the limit is over. Only a death
        # with no time of its own still reads the reset against the decision, as before.
        reset = NOW - timedelta(minutes=1)
        past = self.decide([record("T", "halted")], {"T": (limits.CONFIRMED, reset)},
                           died_at={"T": NOW - timedelta(minutes=10)})
        self.assertEqual((past.marks, past.halts), ({}, {}))
        untimed = self.decide([record("T", "halted")], {"T": (limits.CONFIRMED, reset)})
        self.assertEqual(untimed.marks, {"fable": fallback_mark()})

    def test_an_ordinary_halt_at_max_halts_minus_one_is_excluded(self):
        decision = self.decide([record("T", "halted", wall=3000)], {"T": REFUTED},
                               halts={"T": SETTINGS.max_halts - 1})
        self.assertEqual((decision.halts, decision.exclude), ({"T": 2}, ("T",)))

    def test_a_confirmed_death_on_no_known_model_is_still_never_counted(self):
        bare = dict(record("T", "halted"), model=None)
        decision = self.decide([bare], {"T": CONFIRMED}, died_on={}, listed_on={})
        self.assertEqual((decision.marks, decision.moves, decision.holds), ({}, (), ()))
        self.assertEqual((decision.halts, decision.report), ({}, ()))

    def test_a_landing_from_an_earlier_cycle_is_not_a_landing_now(self):
        old = landed("S")
        decision = self.decide([old, record("A", "halted")], {"A": UNCONFIRMED},
                               before={"S": old})
        self.assertEqual((decision.outcome.reason, decision.streak), ("usage_limit", 1))

    def test_deferred_and_passed_over_may_be_any_collection(self):
        stale = record("T", "halted", started=BEFORE_RUN)
        decision = self.decide([stale], before={"T": stale}, deferred=["T"], passed_over=[])
        self.assertEqual(decision.halts, {})

    def test_a_launched_death_with_no_reading_is_ordinary(self):
        decision = self.decide([record("T", "halted", wall=3000)], {})
        self.assertEqual((decision.halts, decision.marks), ({"T": 1}, {}))

    def test_the_facts_are_left_as_they_were(self):
        marks = {"opus": fallback_mark()}
        cycle = facts([record("T", "blocked", "fable")], {"T": CONFIRMED}, marks=marks,
                      fallback=MUTUAL, halts={"X": 1})
        limits.decide_after_run(cycle)
        self.assertEqual((marks, cycle.halts), ({"opus": fallback_mark()}, {"X": 1}))


class CycleStart(unittest.TestCase):
    """`plan_cycle_start`: the moves, retries, and deferrals before a run, and the R7 wait."""

    def plan(self, unsettled, retries, listed_on, marks, table=None, now=NOW, settings=SETTINGS):
        return limits.plan_cycle_start(unsettled, retries, listed_on, marks, table or {}, now,
                                       settings)

    def test_a_queued_retry_on_a_marked_model_with_a_free_fallback_moves_and_retries(self):
        start = self.plan(["T"], {"T"}, {"T": "fable"}, {"fable": fallback_mark()},
                          {"fable": "opus"})
        self.assertEqual((start.moves, start.retry, start.defer, start.held),
                         ((("T", "fable", "opus"),), ("T",), (), ()))
        self.assertIsNone(start.wait)

    def test_ae5_a_halted_task_on_a_held_model_is_deferred(self):
        """Covers AE5. Its halt is untouched: the start of a Cycle counts nothing."""
        start = self.plan(["T"], set(), {"T": "fable"}, {"fable": fallback_mark()})
        self.assertEqual((start.moves, start.retry, start.defer), ((), (), ("T",)))
        self.assertEqual(start.held, (("T", "fable"),))

    def test_a_task_with_no_record_on_a_held_model_is_deferred(self):
        start = self.plan(["T"], set(), {"T": "fable"}, {"fable": fallback_mark()}, MUTUAL)
        # Opus is free, so T moves rather than defers; with opus marked too it is deferred.
        self.assertEqual(start.moves, (("T", "fable", "opus"),))
        both = {"fable": fallback_mark(), "opus": fallback_mark()}
        held = self.plan(["T"], set(), {"T": "fable"}, both, MUTUAL)
        self.assertEqual((held.moves, held.defer), ((), ("T",)))

    def test_ae10_held_tasks_take_no_room_in_the_batch(self):
        """Covers AE10."""
        start = self.plan(["A", "B"], set(), {"A": "fable", "B": "fable"},
                          {"fable": fallback_mark()})
        self.assertEqual(start.defer, ("A", "B"))
        self.assertEqual(start.room, 3)
        busy = self.plan(["A", "C"], set(), {"A": "fable", "C": "sonnet"},
                         {"fable": fallback_mark()})
        self.assertEqual(busy.room, 2)

    def test_only_held_work_waits_no_longer_than_the_earliest_mark(self):
        def at(minutes):
            return limits.Mark(since=NOW, until=NOW + timedelta(minutes=minutes),
                               source=limits.MARK_CLI)
        listed = {"A": "fable", "B": "opus"}
        start = self.plan(["A", "B"], set(), listed, {"fable": at(40), "opus": at(90)})
        self.assertEqual(start.wait, limits.Outcome(limits.WAIT, reason="model_held",
                                                    seconds=1800))
        soon = self.plan(["A", "B"], set(), listed, {"fable": at(10), "opus": at(90)})
        self.assertEqual(soon.wait.seconds, 600)

    def test_a_held_task_waits_on_the_earliest_mark_along_its_chain(self):
        marks = {"fable": fallback_mark(), "opus": limits.Mark(
            since=NOW, until=NOW + timedelta(minutes=10), source=limits.MARK_CLI)}
        start = self.plan(["T"], set(), {"T": "fable"}, marks, MUTUAL)
        self.assertEqual((start.defer, start.wait.seconds), (("T",), 600))

    def test_runnable_work_beside_held_work_asks_for_no_wait(self):
        start = self.plan(["A", "C"], set(), {"A": "fable", "C": "sonnet"},
                          {"fable": fallback_mark()})
        self.assertIsNone(start.wait)
        self.assertEqual(start.defer, ("A",))

    def test_no_marks_leave_everything_as_it_was(self):
        start = self.plan(["A", "B", "C"], {"B", "C"},
                          {"A": "fable", "B": "opus", "C": "sonnet"}, {})
        self.assertEqual((start.moves, start.defer, start.held), ((), (), ()))
        self.assertEqual(start.retry, ("B", "C"))
        self.assertEqual((start.room, start.wait), (0, None))

    def test_a_task_listed_with_no_model_is_read_on_the_default(self):
        start = limits.plan_cycle_start(["T"], ["T"], {}, {"opus": fallback_mark()}, {}, NOW,
                                        SETTINGS, default_model="opus")
        self.assertEqual((start.retry, start.held), ((), (("T", "opus"),)))

    def test_retries_come_back_in_natural_order_whatever_the_container(self):
        listed = {"2": "opus", "9": "opus", "10": "opus"}
        for retries in (["9", "10"], {"10", "9", "2"}, ("10", "2", "9"), frozenset({"10", "9"})):
            with self.subTest(retries=retries):
                start = self.plan([], retries, listed, {})
                self.assertEqual(start.retry, tuple(sorted(retries, key=int)))
        # Unsettled order does not reorder the retries either.
        start = self.plan(["10", "9"], {"10", "9"}, listed, {})
        self.assertEqual(start.retry, ("9", "10"))

    def test_a_task_with_no_model_and_no_default_raises_naming_it(self):
        for unsettled, retries in ((["T-7"], set()), ([], {"T-7"})):
            with self.subTest(retried=bool(retries)):
                with self.assertRaisesRegex(ValueError, "T-7"):
                    self.plan(unsettled, retries, {}, {"fable": fallback_mark()})
        # A default covers it.
        start = limits.plan_cycle_start(["T-7"], set(), {}, {}, {}, NOW, SETTINGS,
                                        default_model="sonnet")
        self.assertEqual(start.room, 2)

    def test_no_argument_is_changed(self):
        args = (["A", "B"], {"B", "C"}, {"A": "fable", "B": "opus", "C": "fable"},
                {"fable": fallback_mark()}, dict(MUTUAL), NOW, SETTINGS)
        kept = copy.deepcopy(args)
        limits.plan_cycle_start(*args, default_model="sonnet")
        self.assertEqual(args, kept)

    def test_an_expired_mark_holds_nothing(self):
        stale = fallback_mark(NOW - FIVE_HOURS - timedelta(minutes=1))
        start = self.plan(["T"], set(), {"T": "fable"}, {"fable": stale})
        self.assertEqual((start.moves, start.defer, start.room), ((), (), 2))


LATER = datetime(2026, 9, 27, 21, 10)       # the decision, after two half hour sonnet Tasks


class DeathTime(unittest.TestCase):
    """Issue #96: a death's mark runs from the time the death ended, not from the decision, and
    a reset that passed between the two means the limit is already over."""

    def decide(self, records, readings, now=LATER, died_at=None, **overrides):
        died_at = {rec["id"]: NOW for rec in records} if died_at is None else died_at
        return limits.decide_after_run(facts(records, readings, now=now, died_at=died_at,
                                             **overrides))

    def test_a_reset_that_passed_before_the_decision_writes_no_mark(self):
        for status in ("blocked", "halted"):
            with self.subTest(status=status):
                decision = self.decide([record("T", status), landed("S1"), landed("S2")],
                                       {"T": (limits.CONFIRMED, RESET)}, fallback=MUTUAL,
                                       halts={"T": 1})
                self.assertEqual((decision.marks, decision.notify), ({}, ()))
                self.assertEqual((decision.moves, decision.holds), ((), ()))
                self.assertEqual((decision.halts, decision.exclude, decision.report),
                                 ({}, (), ()))
                self.assertEqual(decision.retry, ("T",) if status == "blocked" else ())
                self.assertEqual(decision.outcome, limits.GO_ROUND)

    def test_the_same_death_decided_before_the_reset_marks_until_it(self):
        decision = self.decide([record("T", "blocked")], {"T": (limits.CONFIRMED, RESET)},
                               now=datetime(2026, 9, 27, 20, 10))
        self.assertEqual(decision.marks, {"fable": limits.Mark(
            since=NOW, until=RESET, source=limits.MARK_CLI)})

    def test_fallback_hours_run_from_the_death(self):
        decision = self.decide([record("T", "blocked")], {"T": CONFIRMED})
        self.assertEqual(decision.marks, {"fable": fallback_mark(NOW)})
        self.assertEqual(decision.marks["fable"].until, datetime(2026, 9, 28, 1, 6))

    def test_fallback_hours_already_behind_the_decision_write_no_mark(self):
        noon = datetime(2026, 9, 27, 12, 0)
        decision = self.decide([record("T", "blocked")], {"T": CONFIRMED},
                               now=datetime(2026, 9, 27, 18, 0), died_at={"T": noon})
        self.assertEqual((decision.marks, decision.holds, decision.retry), ({}, (), ("T",)))

    def test_a_reset_earlier_than_the_death_reads_as_no_reset(self):
        decision = self.decide([record("T", "blocked")],
                               {"T": (limits.CONFIRMED, NOW - timedelta(hours=1))})
        self.assertEqual(decision.marks, {"fable": fallback_mark(NOW)})

    def test_a_death_with_no_time_is_marked_from_the_decision(self):
        decision = self.decide([record("T", "blocked")], {"T": CONFIRMED}, died_at={})
        self.assertEqual(decision.marks, {"fable": fallback_mark(LATER)})

    def test_two_deaths_on_one_model_keep_the_reset_still_ahead(self):
        ahead = LATER + timedelta(minutes=30)
        readings = {"A": (limits.CONFIRMED, RESET), "B": (limits.CONFIRMED, ahead)}
        for order in (("A", "B"), ("B", "A")):
            with self.subTest(order=order):
                decision = self.decide([record(task_id, "blocked") for task_id in order],
                                       readings)
                self.assertEqual(decision.marks, {"fable": limits.Mark(
                    since=NOW, until=ahead, source=limits.MARK_CLI)})
                # The model is marked after all, so both deaths are held on it.
                self.assertEqual(set(decision.holds), {("A", "fable"), ("B", "fable")})
                self.assertEqual(decision.retry, ("A", "B"))

    def test_a_death_whose_limit_is_over_is_held_when_a_standing_mark_covers_its_model(self):
        standing = {"fable": fallback_mark(NOW - timedelta(hours=1))}
        decision = self.decide([record("T", "blocked")], {"T": (limits.CONFIRMED, RESET)},
                               marks=standing)
        self.assertEqual((decision.marks, decision.holds), ({}, (("T", "fable"),)))

    def test_mark_for_follows_the_table(self):
        hours = 5
        cases = (
            (RESET, LATER, NOW, None),
            (RESET, datetime(2026, 9, 27, 20, 10), NOW,
             limits.Mark(since=NOW, until=RESET, source=limits.MARK_CLI)),
            (None, LATER, NOW, fallback_mark(NOW)),
            (NOW - timedelta(minutes=5), LATER, NOW, fallback_mark(NOW)),
            (None, NOW + timedelta(hours=6), NOW, None),
            (NOW - timedelta(minutes=5), NOW, None, fallback_mark(NOW)),
            (None, LATER, None, fallback_mark(LATER)),
        )
        for resets_at, now, died_at, mark in cases:
            with self.subTest(resets_at=resets_at, now=now, died_at=died_at):
                self.assertEqual(limits.mark_for(now, resets_at, hours, died_at), mark)


class DeathTimeHelper(unittest.TestCase):
    """`death_time`, the time a record's process ended, for the caller to pass as `died_at`."""

    def test_started_at_plus_wall_seconds(self):
        self.assertEqual(limits.death_time({"started_at": "2026-09-27T20:00:00",
                                            "wall_seconds": 360}), NOW)
        self.assertEqual(limits.death_time({"started_at": "2026-09-27T20:00:00",
                                            "wall_seconds": 0.5}),
                         datetime(2026, 9, 27, 20, 0, 0, 500000))

    def test_a_utc_stamp_comes_back_as_local_time_with_no_zone(self):
        # The runner stamps `started_at` in UTC with its offset; `now` and a reset are local.
        epoch = RESET_EPOCH
        stamp = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        moment = limits.death_time({"started_at": stamp, "wall_seconds": 60})
        self.assertIsNone(moment.tzinfo)
        self.assertEqual(moment, datetime.fromtimestamp(epoch + 60))
        self.assertLess(moment, RESET + timedelta(minutes=2))

    def test_a_missing_or_unreadable_field_gives_nothing(self):
        for rec in ({}, {"started_at": THIS_RUN}, {"wall_seconds": 8},
                    {"started_at": THIS_RUN, "wall_seconds": None},
                    {"started_at": None, "wall_seconds": 8},
                    {"started_at": "yesterday", "wall_seconds": 8},
                    {"started_at": 1790000000, "wall_seconds": 8},
                    {"started_at": THIS_RUN, "wall_seconds": "8"},
                    {"started_at": THIS_RUN, "wall_seconds": True},
                    {"started_at": THIS_RUN, "wall_seconds": float("nan")},
                    {"started_at": THIS_RUN, "wall_seconds": float("inf")},
                    {"started_at": THIS_RUN, "wall_seconds": 10 ** 30}):
            with self.subTest(record=rec):
                self.assertIsNone(limits.death_time(rec))


class ReviewGaps(unittest.TestCase):
    """The four small gaps the review of U2 found beside issue #96."""

    def decide(self, *args, **kwargs):
        return limits.decide_after_run(facts(*args, **kwargs))

    def test_a_confirmed_blocked_death_on_no_known_model_is_reported_not_queued(self):
        bare = dict(record("T", "blocked"), model=None)
        decision = self.decide([bare, landed("S")], {"T": CONFIRMED}, died_on={},
                               listed_on={})
        self.assertEqual((decision.marks, decision.moves, decision.holds), ({}, (), ()))
        self.assertEqual((decision.report, decision.retry, decision.halts), (("T",), (), {}))

    def test_leaving_reports_every_blocked_limit_death(self):
        decision = self.decide([record("A", "blocked", "fable"), record("C", "blocked", "sonnet"),
                                record("H", "halted", "opus")],
                               {"A": CONFIRMED, "C": UNCONFIRMED, "H": UNCONFIRMED}, streak=16)
        self.assertEqual(decision.outcome.reason, limits.LEAVE_LIMIT_WAITS)
        self.assertEqual((decision.report, decision.retry), (("A", "C"), ()))
        # The confirmed death's mark still stands for the next start.
        self.assertEqual(set(decision.marks), {"fable"})

    def test_decided_retries_come_back_in_natural_order(self):
        decision = self.decide([record(task_id, "blocked") for task_id in ("10", "9", "2")],
                               {task_id: CONFIRMED for task_id in ("10", "9", "2")})
        self.assertEqual(decision.retry, ("2", "9", "10"))

    def test_the_death_marks_its_own_model_and_the_start_moves_by_the_listed_one(self):
        # T died on opus; the Manifest lists it on fable, where a move put it.
        decision = self.decide([record("T", "blocked", "opus"), landed("S")], {"T": CONFIRMED},
                               died_on={"T": "opus"}, listed_on={"T": "fable"},
                               fallback=MUTUAL)
        self.assertEqual(set(decision.marks), {"opus"})
        start = limits.plan_cycle_start(["T"], {"T"}, {"T": "fable"}, decision.marks, MUTUAL,
                                        NOW, SETTINGS)
        self.assertEqual((start.moves, start.retry), ((), ("T",)))
        listed_on_opus = limits.plan_cycle_start(["T"], {"T"}, {"T": "opus"}, decision.marks,
                                                 MUTUAL, NOW, SETTINGS)
        self.assertEqual(listed_on_opus.moves, (("T", "opus", "fable"),))

    def test_no_argument_is_changed(self):
        cycle = facts([record("T", "blocked", "fable"), record("U", "halted", "opus"),
                       record("V", "halted", "sonnet", wall=3000)],
                      {"T": (limits.CONFIRMED, RESET), "U": UNCONFIRMED, "V": REFUTED},
                      before={"V": record("V", "halted", "sonnet", started=BEFORE_RUN)},
                      died_at={"T": NOW}, marks={"opus": fallback_mark()}, fallback=MUTUAL,
                      halts={"V": 1}, deferred=["X"], passed_over={"Y"}, retries={"T"},
                      streak=4, now=LATER)
        kept = copy.deepcopy(cycle)
        limits.decide_after_run(cycle)
        self.assertEqual(cycle, kept)


class ResolveFallback(unittest.TestCase):
    def test_the_chain_is_walked_once_and_never_loops(self):
        table = {"a": "b", "b": "c", "c": "a"}
        self.assertEqual(limits.resolve_fallback("a", table, set()), "b")
        self.assertEqual(limits.resolve_fallback("a", table, {"b"}), "c")
        self.assertIsNone(limits.resolve_fallback("a", table, {"b", "c"}))
        self.assertIsNone(limits.resolve_fallback("z", table, set()))


if __name__ == "__main__":
    unittest.main()

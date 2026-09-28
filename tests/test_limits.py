"""U1 of the usage limit plan: the reader that answers confirmed, refuted, or unconfirmed for one
death, and the stub entry keys that let a process die the way the CLI dies at its limit."""
import json
import os
import unittest
from dataclasses import replace
from datetime import datetime

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


if __name__ == "__main__":
    unittest.main()

---
title: A hold set beside a rules stop was recorded and logged, but never notified
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: medium
root_cause: early_return_skips_a_later_unconditional_side_effect
resolution_type: code_fix
related_components: [feeder, cli]
symptoms:
  - "a post cycle hook failed with post_cycle_hold on in the same cycle the run itself halted with a run scoped class (or any other cycle apply_rules already had its own exit code and message for), and the operator was told only the rules' own reason, never that a hold had also been set"
  - "every later start refused with the hold sentence and logged it, on the documented assumption that the hold itself had already notified once, which was false whenever it landed beside a rules stop, so the operator was never told at all for that hold"
  - "feed <manifest> --release still worked and the state file still carried the hold record correctly; only the notification was missing, which made the bug invisible to anything that reads state rather than watches for a push"
tags: [feeder, post-cycle-hook, hold, notify, settle, code-review-catch]
---

# A hold set beside a rules stop was recorded and logged, but never notified

## Problem

`Feeder.settle()` (`skills/relay/scripts/relay/feeder.py`) runs `apply_rules()` (rules 2 and 3,
which can themselves choose to stop the feeder, e.g. `run_scoped_halt` or
`limit_waits_exhausted`) and then `post_cycle()` (the post cycle hook, which can set a hold on a
failure with `post_cycle_hold` on). Before this fix, the two outcomes were combined like this:

```python
outcome = self.apply_rules(data, after, by_status)
held = self.post_cycle(manifest, code, by_status, merge)
if isinstance(outcome, Pending):
    return self.hold(held) if held else self.wait(outcome.seconds, outcome.reason)
if held and outcome in (None, EXIT_OK):
    return self.hold(held)
return outcome
```

`self.hold(held)` is the only place that notifies the operator about the hold: it calls
`self.stop(EXIT_HALTED, message, HOLD_WORD)`, and `stop()` both logs and notifies. But it is only
reached from two branches: a `Pending` wait the rules asked for, or an `outcome` of `None` /
`EXIT_OK` (the ordinary "cycle ended, nothing more to say" case). Whenever `apply_rules` had
already returned its own stop code for its own reason (a run scoped halt, an exclusion that could
not be written, the whole cycle's usage limit wait finally running out), neither branch matched,
and `settle()` fell through to `return outcome`, skipping `hold()` and its `notify()` call
entirely. The hold was still written to the state file inside `post_cycle()`, and `post_cycle()`'s
own log line already said "holding the feeder" as part of its ordinary bookkeeping, so the bug was
invisible to anything reading the log or the state file. Only the operator-facing notification
(desktop push, or whatever `--notify` wires up) was missing.

This made the standing assumption documented beside every later refusal false: `cycle()`'s own
comment says a start refused under an existing hold is "logged every time and not notified: the
hold itself already was, and a cron `--once` would repeat it." That assumption only holds if the
hold reliably notifies once when it is set. It did not, for exactly the cycles where the rules
also had their own reason to stop, which is precisely the case an operator most needs to hear
about both halves of: the rules' own halt, and separately, that the gate itself is now broken and
blocking every further start until `--release`.

## What did not work

Nothing was tried and discarded here; the bug was found by `/code-review` during the round 2 self
run on task #75 (which was itself only there to fix an unrelated documentation drift and two
low-severity #58 gaps), not by a failing test. No existing test exercised a hold set in the same
cycle as a rules-level stop; `test_a_hold_replaces_a_usage_limit_wait` and
`test_a_failed_hook_with_hold_stops_the_feeder_after_the_bookkeeping` both cover a hold beside the
`Pending`/`None` branches, never beside `apply_rules`'s own early-return stop codes.

## Solution

Keep the rules' own stop as the cycle's outcome (its own `stop()` call already logged and notified
its own reason, and that decision should stand), but notify the hold too, unconditionally, in the
one remaining branch that used to silently drop it:

```python
if isinstance(outcome, Pending):
    return self.hold(held) if held else self.wait(outcome.seconds, outcome.reason)
if held:
    if outcome in (None, EXIT_OK):
        return self.hold(held)
    self.notify(self._hold_message(held))
return outcome
```

`_hold_message(failure)` is `hold()`'s own message text pulled into a shared helper so the two
call sites (the exit-with-`EXIT_HALTED` path and this notify-only path) say the identical sentence
rather than drifting apart. The rules' own log line, exit code, and `leaving` reason are untouched;
only a second `notify()` call was added for the hold, beside whatever the rules already sent.

## Why this works

The root cause is an early return skipping a later, unconditional side effect that a narrower
condition check assumed would always be reached. `held and outcome in (None, EXIT_OK)` reads as
"there's a hold, and nothing else needs to happen" when what it actually tested was "there's a
hold, and the rules did not also want to return their own code." Those are different questions.
The two outcomes, "what does this cycle report as its result" and "does the hold need to tell the
operator," are independent facts about the same cycle and were coupled through one shared `if`
instead of being decided separately. Once separated, the fix is one line: notify whenever `held`
is set, in whichever branch does not already do it through `hold()`.

## Prevention

1. When two independent facts about the same event are combined into one branch (whether to
   report a rules-level outcome, and whether a hold needs its own notification), check whether the
   branch actually covers every combination, not just the ones exercised by the existing tests. A
   `Pending`-or-`None`/`EXIT_OK` check silently excludes every other value `apply_rules` can
   return, and `apply_rules` has several: `run_scoped_halt`, `exclusion_failed`,
   `limit_waits_exhausted`.
2. A side effect (`notify()`) that lives only inside a helper named for a different purpose
   (`hold()`, whose main job is to build the exit code and message) is easy to lose when a caller
   adds a new branch that does not call that helper. Naming the notification as its own concern
   (`_hold_message()` here) makes it possible to call from more than one place without duplicating
   the sentence.
3. The regression guard is `test_a_hold_beside_a_rules_stop_is_notified_too`
   (`tests/test_feeder.py`, `PostCycle`), which sets up a run scoped halt and a failing hook with
   `post_cycle_hold` on in the same cycle, and asserts both the rules' own note and the hold's
   note are present, not just the exit code.

## Update (2026-09-27, same task): a second, narrower finding in the same review round

The same review round flagged a second issue in a related fix landed alongside the one above:
`feed <manifest> --release` was changed to also remove a stray stop file `--stop` can leave behind
against a held feeder that has already exited (`--stop` never checks liveness). The first pass
removed the file whenever `--release` could take the feeder lock, on the reasoning that a free
lock proves no live feeder is waiting to read the file. That reasoning is correct but wider than
the actual guarantee: a lock being free does not distinguish "a `--stop` against a held feeder
that already left" from "an unrelated `--stop` an operator meant to stand" or, in principle, "a
live `--restart` handover's own stop file, read and removed by `wait_for_lock` once it acquires
the same lock." The fix narrowed the removal to `hold is not None` (an actual hold was cleared by
this call), which both matches the documented scenario exactly and is safe against the
`--restart` case for a structural reason: `cmd_feed` refuses a `--restart` under a hold before it
ever reaches `wait_for_lock`, so a hold and a live restart handover's stop file cannot coexist.
`test_release_with_no_hold_leaves_an_unrelated_stop_file_alone` guards the narrowed scope.

The lesson is adjacent to, not a repeat of, the root cause above: "the lock is free" and "nothing
else has a legitimate reason to touch this file right now" are different claims, and the second
one needs a positive reason (here, that a hold's own existence rules out the one other live writer
of that file), not just the absence of a contending lock holder.

## Related Issues

- `docs/solutions/logic-errors/feeder-idle-conflated-empty-queue-unreadable-source-and-all-refused-cards-into-one-exit.md`
  is a see-also: the same task also corrected `CONCEPTS.md`'s vocabulary entry to name the fourth
  `idle()` answer that file's own "Update (2026-09-27, issue #58)" section introduced, and carried
  that same `empty_queue_scanned` fact onto the `waiting` and `--once` `leaving` events for the
  `idle_waits_max` above zero path, which that file's landing had left generic. No root cause
  overlaps; both are the same area of the codebase touched in the same task.

---
title: An in review read at a relaunch is ambiguous, and only the runner's own last read of the card tells a leftover from staging
date: 2026-09-27
category: logic-errors
module: closeout
problem_type: logic_error
component: closeout
severity: medium
root_cause: logic_error
resolution_type: code_fix
related_components: [run, state, audit, cli]
tags: [baseline, relaunch, stale-cards, lease-break, interrupt, runner-crashed, triple, audit]
---

# An in review read at a relaunch is ambiguous, and only the runner's own last read of the card tells a leftover from staging

## Problem

Issue #64, found by the independent review of the #51 merge (`bbba0ce`). #51 kept a card's first
baseline across a relaunch only when there was evidence the runner had left the card in review:
the record's halt class was `runner_crashed`, or the last run end audit named the card
`card_stale_in_review`. Five ways a run can end left that evidence missing or wrong, and the
relaunch then recorded the in review status as the baseline, so the card could never go back.

1. `lease --break` cleared the lease and marked nothing.
2. An interrupt from the keyboard on a foreground `run` passed every `except Exception`. The
   launch's signal handler released the lease first, the `finally` wrote a crashed terminal
   record, and the record stayed `running` with no class and no audit.
3. A tracker failure at run end recorded the card unreadable, not stale.
4. A triple run writes no audit.
5. The audit is replaced only at a run end that reaches it, so an old one could name a card the
   operator had since restaged on purpose, and the Closeout sent the staged card back.

Paths 1 and 2 were confirmed with failing run loop tests before the fix. Both recorded
`In review` as the baseline.

## Why the suggested fix was not taken

The issue suggested keeping the first baseline on every in review read. That fixes 1 to 4 and
breaks 5 and the existing staging test. A status read alone cannot tell the runner's leftover
from the operator's staging, and "always the leftover" is as wrong as "always the staging".

## Solution

The record carries `card_in_review_by_run`. Every launch, serial and triple, sets it, because the
Task's first step moves the card. Only a read of the card that finds it out of review clears it:
the read back after a Closeout (`closeout.read_back`, which returns the finding and whether the
card read out of review), or the run end audit (`audit.build(observed=...)`, cleared in
`run._clear_seen_out_of_review`). `closeout._left_in_review` answers from the flag and uses the
old audit and crash inference only for a record written before the field existed.

Separately, records in flight are now marked `runner_crashed` the way a reclaim marks them. This
happens when the operator breaks the lease (`StateStore.break_lease`), and when a run, dispatch,
or triple coordinator leaves with no terminal record (`StateStore.mark_in_flight_crashed`). The
evidence's `cause` says which one: `lease_reclaimed`, `lease_broken`, `interrupted`, or
`exited_without_terminal`.

## Traps the code review caught

- The interrupt path reaches the `finally` with the lease already free, because
  `launch.launch`'s signal handler calls `on_release` before raising. A "still mine" check alone
  marks nothing on the real interrupt. `mark_in_flight_crashed` marks when the lease is mine or
  free, and never when another holder has it, since that holder reclaimed this runner's stale
  lease and is driving the records now. A test that raises `KeyboardInterrupt` without releasing
  first would pass on the wrong rule, so the run loop test releases the lease first, the same way
  the handler does.
- `run_triple`'s `finally` gated its crash handling on "no terminal record at all". The terminal
  is never cleared between runs, so an earlier run's terminal suppressed it. It now compares
  against the terminal read at the start of this run. It also marks only when every worker is
  known stopped, since a live worker is still driving its record.
- A flag cleared only by the Closeout read back stayed set after the operator fixed a
  `card_left_in_review` by hand. A later deliberate staging was then overridden, which the old
  audit inference had got right. The run end audit's read now clears it too.

## Review findings deliberately not fixed

- `break_lease` marks the records of a live holder too. The break is the operator saying the
  holder is gone. A holder that is not gone finds its heartbeat refused and halts its own Task as
  `runner_crashed`, so the early mark names the class the record ends on. Limiting the mark to a
  stale lease would leave the usual case, a runner that died inside its lease's lifetime, reading
  `running`.
- Nothing marks an orphaned in-flight record at `acquire` when there is no previous lease, which
  is the shape a pre-#64 interrupt or break left. The flag handles the baseline for new records.
  An old record still relaunches correctly, but its summary line reads `running` until then.
- No live run was made. The Closeout brief, the envelope, and the classify digest are unchanged.
  The new `cause` key in `runner_crashed` evidence is read only by the runner and the summary.

## The rule that is not visible in the code

Any evidence that a card was left in review has to come from a read the runner made after its own
last move of the card. It cannot come from an audit that can predate that move, or from a halt
class that only some endings write. If a future path moves the card (a new launch route, a
coordinator transition) and does not set `card_in_review_by_run`, or reads a card out of review
and does not clear it, relaunched leftovers and staged cards start to be confused again.

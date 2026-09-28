---
title: ready_queue calls select() without the same file lists, so status --queue and the real batch disagree
date: 2026-09-28
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: low
root_cause: missing_side_effect
resolution_type: deferred
related_components: [cli, feeder]
symptoms:
  - "U7 (issue #108) added AE9's same file rule to select(): two cards the browser test loop filed for one cause file never batch together, the second held until the first settles"
  - "select() takes that rule from three extra arguments, filed, unsettled, and refused, which only Feeder.plan_cycle_start collects from live state (launch.running, launch.defer, launch.queue, self.state[\"refused\"])"
  - "ready_queue(), which backs feed --status --queue, calls select(cards, listed, config, {}, 0, scanned) with none of the three, so its queue lists a held card as buildable now"
  - "code review caught this during U7 and the task left it unfixed on purpose, staying inside the unit's file list per the card's own instruction"
---

# ready_queue calls select() without the same file lists, so status --queue and the real batch disagree

## Problem

`select()` (`skills/relay/scripts/relay/feeder.py`) grew a fourth return value and three new
keyword arguments in U7: `filed`, `unsettled`, and `refused`, together implementing AE9, the rule
that two cards the browser test loop filed for the same cause file never share a batch. The rule
only works when the caller passes state describing what else is in flight.

`Feeder.plan_cycle_start` is one such caller, and it builds all three from live state before
calling `select`. `ready_queue`, the function behind `feed --status --queue`, is the other caller,
and it does not: `select(cards, listed, config, {}, 0, scanned)` leaves `filed`, `unsettled`, and
`refused` at their defaults, so `select` cannot see that two queued cards share a cause file. The
queue it returns lists both as ready to build, when the real cycle would hold the second out of
its batch until the first settles.

The two call sites look identical in shape, so nothing in the code signals that only one of them
carries the rule. A session that later touches `select`'s same file arguments and checks only the
live `plan_cycle_start` path will not notice `ready_queue` fell out of step.

## Status

Left unfixed by design. The U7 card scoped the unit to `plan_cycle_start`'s batch, cycle, and
`feed --status` (the loop block), not to `status --queue`; widening the fix meant leaving the
unit's file list, so the task stopped blocked on that line and reported the gap instead of
patching around it.

## Rule

`select`'s same file rule needs `filed`, `unsettled`, and `refused` from whichever caller invokes
it. Before trusting `feed --status --queue` to preview what the next cycle will actually batch,
check whether `ready_queue` has since been given those three arguments; as of U7 it has not, and
a queued card sharing a cause file with another queued card will print as buildable when it is
not.

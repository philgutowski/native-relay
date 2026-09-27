---
title: The card baseline was rewritten at every launch and read after the coordinator's own start move, so a card left in review could never go back
date: 2026-09-27
category: logic-errors
module: closeout
problem_type: logic_error
component: closeout
severity: medium
root_cause: logic_error
resolution_type: code_fix
related_components: [run, adapters, audit]
tags: [baseline, return-to, stale-cards, relaunch, triple, jira, closeout-instructions, prose-contract]
---

# The card baseline was rewritten at every launch and read after the coordinator's own start move, so a card left in review could never go back

## Problem

Issue #51. `baseline_tracker_status` is the status a card read before the run, and
`closeout.return_to_for` uses it to tell a blocked or halted Closeout where to return the card.
Three routes left that record wrong, and a wrong baseline is silent: the Closeout is simply told
to leave the card where it is.

1. **No baseline.** `adapter.read` succeeded and `adapter.status` came back skipped, so the
   launch recorded `None`. `return_to_for` returned `None`, and the GitHub and Jira adapters told
   the Closeout the card "keeps its current status". That sentence is false once the Task's start
   step has moved the card to in review, which is the drift
   `docs/solutions/workflow-issues/two-instructions-to-two-processes-written-weeks-apart-disagreed-about-moving-the-card-back-and-the-adapter-couldnt-see-it.md`
   was written about, reached by a narrower route. No read back ran either, because
   `confirm_card_returned` was gated on `return_to`.
2. **A relaunch.** The serial launch upsert wrote the baseline again on every attempt. A card
   a blocked attempt left in review read in review at the relaunch, and `return_to_for` then took
   the leftover for the operator's staging and refused to move it, for good.
3. **A Jira triple, found by the code review.** `_triple_jira_start` moves every card to in
   review before any worker exists, and the baseline was read from the snapshot taken after that
   move. Every Jira triple card therefore recorded in review as its baseline, and none was ever
   returned. This predates #51 and is the same failure.

## Why nothing noticed

The baseline is written in one place per route, far from the three places that read it
(`return_to_for`, `audit.build`, the triple return transition). Every writer assumed its read was
the card's pre run status, and none was: a relaunch reads the previous attempt's leftover, and
the triple read comes after the runner's own write. The run loop tests covered a single launch
from a clean card, which is the one case where the assumption holds.

## Solution

- `closeout.baseline_unknown(manifest, record)` names the first refusal on its own. The adapters
  take `baseline_unknown` in `closeout_instructions`, and `adapters.unknown_baseline_move` renders
  the true sentence: the runner could not read the status, the Task may have moved the card to
  in review, leave it rather than guess, and say it needs moving by hand. The runner then reads
  the card back, and a card still in review is a `card_left_in_review` finding naming
  `contracts.UNKNOWN_RETURN`, the same words the audit uses. An unreadable read back on this path
  is no finding, since the launch read of the same board already failed and the audit reports it.
- `closeout.launch_baseline(manifest, record, status, last_audit)` decides what a launch records.
  A first launch records what it read. On a relaunch whose earlier baseline was not in review, an
  empty read keeps the earlier baseline, and an in review read keeps it only on evidence that the
  runner left the card there: the last run end audit named it `card_stale_in_review`, or the
  record is a crashed runner's (`halt_class` is `runner_crashed`, and no Closeout ran). Any other
  read wins, so a card the operator moved or restaged between runs is respected.
- The triple route records the status from the snapshot read before the start transition.

Each state has a run loop test on the GitHub and Jira file boards in `tests/test_run.py`, and the
triple route has one in `TripleCoordinator`. Each fails on the code before this change.

## The rule that is not visible in the code

An in review read at a relaunch is ambiguous. It is either the runner's leftover or the
operator's staging, and `return_to_for` must not undo the second. The first version of the fix
kept the earlier baseline on any in review read, which the review caught as overriding a
deliberate restaging. The evidence the runner already holds, the last audit and the crash
marker, is what separates the two. If a future change stops writing the audit at run end, or
writes it without the task id, relaunched leftovers quietly become staged cards again.

## Review findings deliberately not fixed

- The three closeout sites (`_blocked_route`, `_note_halt`, `_triple_close_blocked`) each call
  `return_to_for` and `baseline_unknown` and gate the read back on either. A shared helper
  returning both would keep them in step; the next refusal added to `return_to_for` has to touch
  all three.
- `FakeAdapter` records `(closeout_instructions, outcome, return_to)` and not
  `baseline_unknown`, so the flag is checked through the rendered text only.
- `audit.py` keeps its own `_same`, identical to the one `closeout.py` gained.
- The new Closeout sentence is a change to a contract between processes, and it has only stub
  coverage. The live proof CLAUDE.md asks for, one real Closeout on a throwaway board told the
  baseline is unknown, has not been run.

## Prevention

A field recorded "before the run" has to be read before anything in the run writes to what it
describes, and a relaunch is not before the run. When a record field is written on every attempt,
ask what the second attempt reads, and whether it could be the first attempt's own write.

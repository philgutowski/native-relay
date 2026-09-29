---
title: ready_queue called select() without the same file lists, so status --queue and the real batch disagreed
date: 2026-09-28
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: low
root_cause: missing_side_effect
resolution_type: code_fix
related_components: [cli, feeder, progress]
symptoms:
  - "U7 (issue #108) added AE9's same file rule to select(): two cards the browser test loop filed for one cause file never batch together, the second held until the first settles"
  - "select() takes that rule from extra arguments, filed, unsettled, refused, and held, which only Feeder.cycle collected from live state"
  - "ready_queue(), which backs status --queue, called select(cards, listed, config, {}, 0, scanned) with none of them, so its queue listed a held card as buildable now"
  - "code review caught this during U7, the task left it unfixed to stay inside the unit's file list, and issue #119 fixed it"
---

# ready_queue called select() without the same file lists, so status --queue and the real batch disagreed

## Problem

`select()` (`skills/relay/scripts/relay/feeder.py`) grew keyword arguments in U7, `filed`,
`unsettled`, and `refused`, which together implement AE9, the rule that two cards the browser
test loop filed for the same cause file never share a batch. It already took `held`, the cards
routed to a model a usage mark holds. The rule only works when the caller passes state describing
what else is in flight.

`Feeder.cycle` built all of them from live state. `ready_queue`, the function behind
`status --queue`, called `select(cards, listed, config, {}, 0, scanned)` and left them at their
defaults, so its queue listed a card the cycle and `feed --dry-run` hold as ready to build.

The two call sites looked identical in shape, so nothing in the code signalled that only one of
them carried the rule.

## Fix (issue #119)

Both callers now take the inputs from one function, `same_file_inputs`, so they cannot drift:

- **Unsettled** is every listed Task not excluded or settled, plus every queued retry. The cycle
  used to spell this `launch.running | launch.defer | launch.queue`; that union equals the record
  reading for every state `start_cycle` produces, and reading it from the records is the form a
  process with no marks to apply can compute too.
- **Held and refused** come from `route_card`, the cycle's routing moved out of `Feeder.route`,
  with the marks read from the state file without pruning it. A refused card is judged on the
  model the cycle would route it to, a mark's fallback included.
- **Excluded and listed** are read from the manifest's text through `manifestedit`, as the cycle
  reads them. `manifest.Task.excluded` is `bool(value)`, and `excluded_ids` wants `is True`, so
  reading them two ways could disagree on a malformed line.
- **Rank** is the order file's. Of two fresh cards on one file, the first in that order holds it,
  so a queue ranked by id alone named the wrong card as the one waiting.

`select()` also takes a `room`, and `ready_queue` passes every card's worth. The queue spans
later cycles, so every card in it needs its same file answer, not only the next batch's.

Each queue entry is now `(id, model, waits_on)`. A held card stays in the queue and is priced,
since it runs once the card it waits on settles, and the `queue:` line names up to five of them.
The priced model is still `choose_model`'s, without the fallback, because a mark lasts hours and
the queue it prices lasts longer; only the same file inputs follow the mark.

## Rule

When a function previews what the cycle will do, it must get every input the cycle hands
`select` from the same function the cycle uses, not from a second derivation of the same idea.
`tests/test_feeder_testloop_routing.py` `QueueAgreement` drives `status --queue`, the dry run,
and the cycle over one state and asserts one answer; extend it when `select` grows an input.

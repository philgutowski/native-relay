---
title: A feeder manifest keeps every card it appended, so the landed sample is the queue's history, not one cycle's
date: 2026-09-27
category: logic-errors
module: progress
problem_type: logic_error
component: progress
severity: low
root_cause: misleading_documentation
resolution_type: code_fix
related_components: [feeder, cli]
tags: [feeder, status, estimate, queue, landed-sample, manifest, review-findings]
---

# A feeder manifest keeps every card it appended, so the landed sample is the queue's history, not one cycle's

## Problem

Issue #50 asked `status` to price the ready queue behind a feeder's cycle, the second half of #31.
#31 had described the feeder manifest as holding "only the current cycle's cards", and the README
repeated it. Read literally, that says the landed durations `progress.build` draws from (the
manifest's own tasks only, never a record the manifest no longer names) are one cycle's worth, a
sample of one to three. The code review of #50 read it that way and asked for the queue to be
priced from every record in the state directory instead.

## What is actually true

The feeder only ever appends. `manifestedit` has `append_tasks`, `exclude_task`, and `set_model`,
and nothing that removes a `[[tasks]]` block. A landed card stays listed for the life of the
manifest. What is "only the current cycle" is the *unsettled* part: the tasks the next `relay run`
will launch, which is what the cycle estimate multiplies the mean by.

So on a feeder manifest the in-manifest landed sample is every card the feeder has landed, and it
is the right sample for both estimates. Records outside the manifest are a different list's runs,
which `progress._entry` deliberately gives no elapsed; widening the sample to them would price the
queue from another manifest's work. The README line now says "the manifest's unsettled tasks are
only the current cycle".

## The shape that landed

- `feeder.read_ready` is the one read of the ready source, shared by the loop's `ready_cards` and
  by `status`.
- `feeder.ready_queue` applies `select` and `scanned_ids`, drops a card already refused with its
  routed model, and routes the rest with `choose_model`. It reads only: no lock, no state write,
  no pre cycle hook.
- `progress.build` carries `landed_by_model`; `queue_estimate` prices each card at its model's
  mean, or the overall mean when its model has none; `queue_line` renders it.
- `cmd_status` prints the `queue:` line after `remaining:` when a sidecar exists, and turns any
  failure into a sentence on that line. Since issue #63 it does so only under `status --queue`:
  the ready command runs in the target repository beside a live Task process, so plain `status`
  prints `queue: not read` and names the flag rather than running it, and the command's whole
  process group ends at the one minute bound.

## Review findings deliberately not fixed

- **Pricing from records outside the manifest.** Not a gap, for the reason above.
- **The exhausted fallback.** `ready_queue` prices and checks refusals against `choose_model`'s
  model, not `Feeder.route`'s. While a model is marked exhausted, a card refused on one side of
  the fallback can be counted or dropped where the loop would do the opposite. The mark lasts
  `fallback_hours`, shorter than the queue it prices, and reading it needs the expiry logic in
  `Feeder.exhausted_models`, which writes state. The docstring names the disagreement.
- **Adapter timeouts.** The one minute bound covers a ready command only. A Jira ready read keeps
  the adapter's own bounds, up to twenty pages at thirty seconds each.
- **No state yet.** `status` before any run returns early with "no state", and prints no queue
  line. With nothing landed there is no estimate to give; a bare card count is left for later.
- **A tracker read on every `status`.** Inherent to the feature. `status` under a feeder now
  launches the ready command or the adapter read each time it is asked; the bar and the follower
  do not.

## Also found

`tests/test_feeder.py` and `tests/test_progress.py` both carried test classes after their
`if __name__ == "__main__"` guard, so running either file directly skipped them while `discover`
still collected them. The guard is now last in both.

---
title: A limit death's mark was timed from the decision, so a reset that passed during the run marked the model for the full fallback_hours
date: 2026-09-28
category: logic-errors
module: limits
problem_type: logic_error
component: runner
severity: medium
root_cause: wrong_reference_time
resolution_type: code_fix
related_components: [limits, feeder, state, usage-limit-state-machine]
symptoms:
  - "a Task died on fable at 20:06 with a reset at 20:20; decided at 21:10 after two half hour sonnet Tasks, fable was marked until 02:10"
  - "every fallback_hours mark ran from the decision rather than from the death"
tags: [usage-limit, mark, died-at, reset-time, timezone, started-at, ktd5, ktd10, issue-96]
---

## Problem

`limits.mark_for` compared a death's CLI reset time with `now`, the moment `decide_after_run`
runs. That moment comes after the whole run, which can be an hour past the death. A reset that
lifted in between read as "behind `now`, no usable reset", and the model got the full
`fallback_hours` from the decision (issue #96).

## What changed

`decide_after_run` takes `died_at` {id: time}, and `mark_for` decides against the death:

- reset later than `now`: marked until the reset, source the CLI.
- reset at or after the death but not after `now`: no mark. The death is still a confirmed limit
  death: not counted, a blocked one queued, and neither moved nor held while its model is open.
- reset before the death, or none: `fallback_hours` after the death, or no mark when that has
  already passed.
- no `died_at`: `now` stands in, the old result. A `died_at` after `now` is clamped to `now`.

## Traps a later session would walk into

**The two times live in different frames.** A record's `started_at` is stamped by
`state._iso` in UTC with an offset (`...+00:00`). The Feeder's `now` is `datetime.now()`, local
with no zone, and `_rejected_reset` returns the CLI's reset through `datetime.fromtimestamp`,
also local with no zone. Adding `wall_seconds` to a parsed `started_at` and comparing it with
`now` raises TypeError, and stripping the zone without converting is off by the UTC offset.
`limits.death_time` converts an aware stamp to local time first. Any new caller that builds a
death time some other way must do the same.

**`started_at` is not the launch.** The record enters `running`, and gets `started_at`, before
pre flight, baseline, and worktree setup. `started_at + wall_seconds` is therefore early by that
setup time, a few minutes, so a `fallback_hours` mark ends that much early. The task prescribed
this derivation and it is accepted for now; a launch or end stamp on the record would remove it.

**KTD5's "a newer death restamps" assumed every mark was stamped at the decision.** Once marks
are timed from the death, a Task launched before a standing mark can die later in wall clock
yet carry an older `since`. `decide_after_run` now skips a death whose mark is older than the
mark already standing on its model, so a weekly CLI mark is not cut short by an older five hour
one.

## Related

- `a-summary-record-names-the-model-a-task-died-on-so-a-moved-retry-read-through-it-waited-on-the-dead-models-mark.md`,
  the reason `died_on` and `listed_on` stay separate facts.
- `docs/plans/2026-09-27-2247-refactor-usage-limit-state-machine-plan.md`, R4, KTD5, KTD10.

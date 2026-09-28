---
title: An early return in _begin_task does not leave a record alone, because the startup re-verify and the run end audit write every record
date: 2026-09-28
category: logic-errors
module: skills/relay/scripts/relay/run.py
problem_type: logic_error
component: runner
severity: medium
root_cause: hidden_writer
resolution_type: code_fix
related_components: [verify, audit, dispatch, scheduler]
symptoms:
  - "a halted record named in run --defer was not launched, yet its verify field was restamped with a new at time on every run"
  - "the plan placed the whole of --defer at one early return in _begin_task, and a test of that return alone passed while the record still changed"
tags: [defer, usage-limit, startup-reverify, card-audit, dispatch-schedule, record-untouched, r10]
---

## Problem

Unit U3 of `docs/plans/2026-09-27-2247-refactor-usage-limit-state-machine-plan.md` builds `run
--defer ID`. R10 says the deferred Task's record, branch, and card are left exactly as they were.
The plan puts the whole mechanism at one point: return early in `_begin_task`, after the exclusion
check, with no upsert, no branch, and no tracker read.

That return is necessary and not sufficient. Three passes touch Tasks outside `_begin_task`:

1. `verify.startup_reverify`, called by `run` and `dispatch` before any Task is begun, re-runs
   the full verdict on every halted record and upserts `verify` with a fresh `at`. It reads the
   card as it does so. A deferred halted record is therefore rewritten on every run.
2. `_audit_cards` at run end reads every Manifest Task's card. Two of its helpers,
   `_clear_seen_out_of_review` and `_retire_confirmed_item_findings`, then upsert the record
   when the card disagrees with it.
3. `dispatch` reads every Task's card before it builds its schedule, and prints and stores a
   schedule that shows the deferred Task building.

## Solution

- The startup re-verify gets `_undeferred(store, defer)`, a view that hides deferred records from
  `records()` and `get()`. `verify.py` is unchanged.
- The audit still reads and reports every card, since its findings are run level. Only the two
  record repairs are filtered by `cfg.defer`.
- `dispatch` builds its schedule from the Tasks that are not deferred. `_concurrent_drive` reads
  only the schedule's edges and walks `manifest.tasks`, so a deferred Task is still visited and
  settles through the early return.

`store.validate` and the stale lease reclaim still write a deferred record that is malformed or
left running. Both repair what a crash or corruption left behind, so they stay.

## Prevention

A test that a record is byte for byte unchanged must use a record the run's passes would
otherwise touch. A halted record does that, a record with no record does not.
`test_run.Defer.test_a_deferred_halted_record_is_not_launched_and_is_unchanged` fails when
`_undeferred` returns the plain store, and
`test_the_run_end_audit_writes_nothing_to_a_deferred_record` fails without the audit filter.

U6 returns early "at the same point `--defer` returns" for the Tasks it passes over. A passed over
Task that already holds a record is exposed to the run end audit in the same way, so its ids need
the same filter.

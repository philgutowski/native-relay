---
title: A summary record names the model a task died on, so a moved retry read through it waited on the dead model's mark
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: medium
root_cause: two_sources_for_one_fact_disagree_after_a_manifest_edit
resolution_type: code_fix
related_components: [feeder]
symptoms:
  - "seven BlockedLimit tests hung with no output after the retry queue learned to defer a retry on a held model"
  - "a blocked limit death moved from fable to opus was never retried; each cycle took a model_held wait instead"
tags: [feeder, usage-limit, fallback, retry-blocked, model, test-hang, fake-clock]
---

# A summary record names the model a task died on, so a moved retry read through it waited on the dead model's mark

## Problem

Issue #52 taught `Feeder.pending_retries` (`skills/relay/scripts/relay/feeder.py`) to defer a
queued `--retry-blocked` retry while its model is held. The first build read each retry's model
through `Feeder._models`, the helper `apply_rules` already used. Every retry that a fallback had
just moved was then deferred, and seven existing tests hung.

## Cause

`_models` answers "which model did this task run on": the summary record's own `model` field
first, the manifest's `model` line only when the record has none. After `fall_back` moves a task
with `manifestedit.set_model`, those two disagree until the next run. The record still says
fable, where it died, and the manifest says opus, where it will relaunch. fall_back has just
marked fable exhausted, so a retry read through `_models` looked held and was deferred until
fable's mark expired.

A retry relaunches where the manifest lists it, so the question there is "where will it run",
and the manifest has to win. `pending_retries` now reads `manifestedit.task_models(text)` first
and falls back to the record's model only for a task with no `model` line.

## Why the tests hung rather than failed

`FeederCase.deps` injects `sleep=self.sleeps.append`, a sleep that neither sleeps nor moves the
fake clock. The stop file is dropped only by the fake runner, when its plan list runs out. A wait
that never calls `run_cycle` therefore loops for ever, and the `model_held` wait is exactly that:
it waits for a mark to expire, and a clock that never moves never expires it. The `HeldModel`
tests override `deps` with a sleep that advances `self.clock` by its seconds.

## Prevention

- Before reading "a task's model", decide which one is meant. `_models` gives the model it ran
  on, which is right for reading a death (`apply_rules`, `limit_blocked`). The manifest gives
  the model it will run on, which is right for anything about its next launch (retries,
  routing).
- A new feeder wait that no run ends, bounded only by time, needs a test sleep that moves the
  clock. Run a new test class under a subprocess timeout first
  (`subprocess.run([... "unittest", "test_feeder.X"], timeout=...)`), because a hang in the
  foreground costs the whole command timeout and says nothing about which test.
## Left open by #52

- A halted limit death on a held model is still counted toward `max_halts`, and it relaunches
  on the held model every run, because the runner relaunches every halted task and has no flag to
  skip a listed one. Deferring it the way a blocked retry is deferred needs runner support;
  issue #67 tracks it.
- The code review asked whether a single 429 on a model with the fallback off should hold that
  model for `fallback_hours`. It does, unless a task landed on that model in the same cycle.
  That is the decision #52 asked for, and a 429 `result` line is the CLI's own limit signal.
- `ready_queue`, behind `feed --status`, still prices a held card as queued work. Its docstring
  already says it ignores marks, because reading them needs the expiry logic that writes state.

## Related

- Related: `a-fallback-move-reset-the-usage-limit-streak-so-mutual-fallback-never-reached-exit-2.md`
  is the neighbouring usage limit rule, and it is why the `model_held` wait neither strikes nor
  resets `limit_waits`.

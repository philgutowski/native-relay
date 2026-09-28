---
title: A feeder test whose sleep does not move the clock hangs for ever on a model_held wait
date: 2026-09-28
category: workflow-issues
module: feeder
problem_type: test_hang
component: tests
severity: medium
root_cause: test_double_clock
resolution_type: test_fix
related_components: [limits, feeder]
tags: [feeder, usage-limit, model-held, test-double, clock, hang, fake-runner, terminal-record]
---

# A feeder test whose sleep does not move the clock hangs for ever on a model_held wait

## Problem

Usage limit plan, U4 (task 91). Once the Feeder ran on the state machine, a confirmed limit death
with no free fallback holds its Task, and a Cycle with only held work waits, reason `model_held`,
until the earliest mark along the chain expires. In `tests/test_feeder.py` the default
`FeederCase` sleep appended to `self.sleeps` and left `self.clock` where it was. A held Task's
mark never expired, the loop never reached `run_cycle`, the fake runner never dropped the stop
file, and the case waited for ever with no output. `python3 -m unittest test_feeder` printed a
few `F` characters and then nothing.

Three pinned tests hung this way before it was clear what they had in common: mutual fallback
walks, a retry that died on the last model of its chain, and two models that fall back to each
other and both die with a 429.

## Cause

The fake runner ends a looping case only when its plan list runs out, and that happens inside
`run_cycle`. A `model_held` wait returns before `run_cycle` is called. So with a clock that
stands still, nothing in the loop can change: the mark reads the same at every Cycle start.

The `usage_limit` wait of the whole Cycle rule never had this problem, because it is taken after a
run, so the plan list still shrinks.

## Fix

`FeederCase.clock_moves` is a class attribute, False by default. When it is True, `_sleep`
advances `self.clock` by the seconds slept. `HeldModel`, `BlockedLimit`, and `LimitMachine` set it
at class level, and any test that can reach a held wait sets it on `self`. Other classes keep a
still clock, since several assert event times that a moving clock would shift.

Run a new or changed feeder test class alone under a bound before the whole module. macOS has no
`timeout`, so wrap it:

```
python3 -c "import subprocess, sys; subprocess.run([sys.executable, '-m', 'unittest', 'test_feeder.LimitMachine'], timeout=120)"
```

## Two seams found beside it

- `summary.build` carries `limit_passed_over` from whatever terminal record the state holds, not
  from this run's. The summary has no `written_at`, and `summary.py` was outside U4's files, so
  the Feeder's real `read_summary` adds `terminal_written_at` from `store.terminal()`. The Feeder
  compares it before and after the run and trusts the list only when it changed and the run is
  not `running` (`Feeder.passed_over`). The fake runner mirrors this: every run stamps
  `terminal_written_at` unless its plan carries `NO_TERMINAL`.
- A run scoped halt read as a confirmed limit no longer stops the Feeder. The run still stopped
  there, so every record it did not launch is added to the passed over set. Otherwise
  `decide_after_run` counts the old halted records the stopped run never reached, the cascade
  the run scoped stop exists to prevent.

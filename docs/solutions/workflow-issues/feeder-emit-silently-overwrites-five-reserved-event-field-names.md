---
title: The feeder's emit() silently overwrites five reserved event field names
date: 2026-09-27
category: workflow-issues
module: feeder
problem_type: contract_seam
component: post-cycle-hook
severity: medium
root_cause: undocumented_contract
resolution_type: naming_convention
related_components: [feeder, hooks, events]
symptoms:
  - "an event field the caller passes named pid, cycle, manifest, at, or event vanishes, replaced by the feeder's own value"
  - "a post cycle hook that tried to report its own process id under RELAY_HOOK pid saw the feeder's pid instead"
---

# The feeder's emit() silently overwrites five reserved event field names

## Problem

Building the post cycle hook (issue #37), the natural name for the hook process's id in the
event record was `pid`. `feeder.py:715` `emit(self, event, **fields)` builds its record as
`dict(fields, at=..., event=event, manifest=..., pid=self.pid, cycle=self.state["cycles"])`.
Because those five keys are added to the dict after the caller's `fields`, a caller-supplied
`pid` (or `at`, `event`, `manifest`, `cycle`) is silently replaced, not rejected and not merged.
There is no error, no warning, just the feeder's own value in place of what was passed. The hook
had to name its field `hook_pid` instead.

## Cause

`emit()` was written to guarantee every event line self-identifies its manifest, pid, and cycle
so a stream of two feeders' lines can't be misread (its own docstring says as much) but it does
this by construction order in a `dict()` call, not by validating the caller's `fields` against a
reserved set. Nothing in the function signature or the docstring flags these five names as
off-limits to callers; the only way to learn it is to trace the merge order.

## Next time

Any code that calls `self.emit(event, **fields)` anywhere in the feeder, including future hooks,
must not use `pid`, `cycle`, `manifest`, `at`, or `event` as a field name. If a new hook or event
needs to carry its own process id, cycle number, or timestamp, prefix or rename it (`hook_pid`,
`hook_cycle`, and so on) the way the post cycle hook's `RELAY_HOOK_PID` did.

---
title: A launch off the main thread has no interrupt forwarder, so an interrupted dispatch marked live builds crashed
date: 2026-09-27
category: logic-errors
module: runner
problem_type: logic_error
component: runner
severity: high
root_cause: concurrency
resolution_type: code_fix
related_components: [dispatch, launcher, state-store, task-process]
symptoms:
  - "an interrupt from the keyboard on a foreground dispatch returns to the shell while the Task process keeps building in its worktree and moving its card"
  - "the record reads halted, class runner_crashed, cause interrupted, with an end time, while its process is alive"
  - "the lease is free, so a relaunch can start beside the orphaned build"
tags: [dispatch, keyboard-interrupt, signal-handler, worker-thread, process-group, orphan, crash-marking, issue-71]
---

# A launch off the main thread has no interrupt forwarder, so an interrupted dispatch marked live builds crashed

## Problem

Issue #71. The only thing in Relay that passes an operator's interrupt on to a Task process
is the SIGINT and SIGTERM handler `launch.launch` installs around its wait. The Task process
sits in its own session (R49), so the terminal's Ctrl+C never reaches it directly, and the
handler is what kills the group and releases the lease.

Python installs signal handlers on the main thread only. `launch.launch` catches the
`ValueError` from `signal.signal` and runs on without a handler. Dispatch runs every build
from a worker thread (`_spawn_flight`), and so does the triple route, so neither has a
forwarder. The interrupt lands on the coordinator's main thread, dispatch's `finally` writes
the crashed terminal record, releases the lease, and exits. The Task processes keep going.

The orphaning is older than #64. What #64 added was `_mark_in_flight_crashed` in that same
`finally`, which turned a silent orphan into a record asserting the work had stopped.

## Fix

`_concurrent_loop` is now a wrapper. Any exception leaving the loop first ends every flight
through `_abort_siblings(cfg, slots, {}, None)`, before dispatch's handlers mark a record or
release the lease. `_stop_flights` sends SIGTERM to every group together, then SIGKILL after
the grace, and waits up to `FLIGHT_EXIT_SECONDS` for each group to empty. Stopped flights are
abandoned the way a halt that does not continue past abandons them. A flight still alive at
the bound keeps its worktree and its running record. It is named under `surviving_flights` in
the terminal record and passed as `spare` to `mark_in_flight_crashed`, and the lease is still
released.

Three details the code review surfaced, each easy to lose in a refactor:

- **A group that is empty counts as exited, even if its thread has not returned.** A
  descendant that left the group can hold the stdout pipe open and keep the launch's reader
  waiting past any bound. Waiting on the thread would name a dead flight as a survivor.
- **A flight can reach Popen after the stop began.** Its launch may still be probing the host
  when the interrupt lands. `_Flight.stopping` is set before the stop reads the group ids, and
  `on_started` appends its group before it checks the event, so one of the two always sees the
  other.
- **An interrupt ends flights, not finished builds.** A build parked in `waiting` behind an
  earlier merge has exited, and its branch is completed work. The halt route still abandons
  it. The interrupt route leaves it for the next pre flight to name.

## Prevention

Any new code that calls `launch.launch` from a thread other than the main thread owns the
stop itself. Nothing in `launch` will forward an interrupt for it. Any `finally` that marks
records crashed must end the processes first, or mark only what it has proven dead.

The test that reproduces this starts a real stub Task process with a sleeping grandchild,
raises the interrupt from the patched `_wait_any_flight` once the group exists, and checks the
group with `os.killpg(group, 0)`. A test that mocks the launch cannot see an orphan.

The triple route has the same missing forwarder. On an interrupt it keeps its leases and
records rather than lying about them, but it never signals its workers either.

---
title: status ran the ready command beside a live run, and a timeout without a process group waited on the grandchild's pipe
date: 2026-09-27
category: logic-errors
module: cli
problem_type: logic_error
component: runner
severity: medium
root_cause: a_read_only_promise_quietly_widened_to_running_an_operator_command
resolution_type: code_fix
related_components: [feeder, progress]
symptoms:
  - "plain status ran the feeder sidecar's ready command in the target repository on every call, beside a building Task process"
  - "a ready command that is a shell wrapper left its children running past the 60 second bound"
  - "an exception outside a named list escaped the queue read and took the rest of status down"
tags: [status, feeder, ready-command, process-group, subprocess-timeout, read-only]
---

# status ran the ready command beside a live run, and a timeout without a process group waited on the grandchild's pipe

## Problem

Issue #50 gave `status` a `queue:` line priced from the feeder's ready source. Reading that source
means running the sidecar's `[ready] command` with the working directory set to the target
repository, or reading the tracker. `status` is documented as safe beside a live run, and after
#50 it could no longer promise that: a ready command that fetches, writes a cache, or dirties the
tree does it beside the Task process, and a dirty tree changes how that task's exit is classified.

## Fix (issue #63)

- The queue estimate runs only under `status --queue`. Plain `status` prints
  `progress.QUEUE_ASK`, `queue: not read; ...`, when a sidecar exists, with or without state, and
  starts no child. `cli._queue_status_line` is the one place both paths decide the line.
- `_queue_line` catches every `Exception`, so no read failure ends `status`. An interrupt is not an
  `Exception` and still stops the command.
- `feeder.build_deps`'s real `run_command` starts the command in its own session and, at the bound
  or on any interrupt, calls `feeder.end_group`: SIGTERM to the group, up to five seconds for the
  whole group to leave (not just the leader), then SIGKILL. `run_hook` uses the same helper.

## The trap: `subprocess.run(..., capture_output=True, timeout=N)` is not bounded by N

On a timeout, `subprocess.run` kills the direct child and then calls `communicate()` again with no
timeout, to collect the output. A grandchild that inherited stdout keeps the pipe open, so that
second call waits until the grandchild exits. The 60 second bound on the ready command was really
"until whatever the wrapper started lets go", and killing only the leader never ends that. Ending
the whole group closes every writer, which is what makes the bound hold. `launch.py` meets the same
pipe for the Task process; this is the same fact at a smaller seam.

The group kill also reaches the loop's ready command and the pre cycle hook, which share
`run_command` and had the same defect at fifteen minutes.

## Why SIGTERM first

A plain `killpg(SIGKILL)` at the bound would kill a `git fetch` under a wrapper mid write and leave
`.git/*.lock` files for the live Task process to trip over. `end_group` polls `killpg(pid, 0)`
rather than waiting on the leader, because a shell leader dies at once on SIGTERM while its `git`
child still needs its moment. It calls `proc.poll()` in that loop so a zombie leader does not keep
the group looking alive.

## Review findings deliberately not fixed

- `start_new_session` takes the ready command and pre cycle hook out of the feeder's process group,
  so a feeder killed by a signal Python never sees (SIGKILL, an unhandled SIGTERM) leaves the child
  running. `run_hook` made that trade already, for the same reason: a group of its own is what lets
  the bound end the grandchildren. A command that prompts on `/dev/tty` also loses its terminal;
  every one of these runs with stdin on `/dev/null` and unattended, so that was never supported.
- Catching every `Exception` can turn a refactoring error in `ready_queue` into a routine sentence.
  The issue asked for exactly that catch, and the sentence names the exception type, so a
  `NameError` there is visible on the line. The feeder loop calls the same helpers without the
  catch and would crash on it.
- The re-raised `TimeoutExpired` carries no partial output. Nothing reads it (`read_ready` uses
  `str(exc)`), and draining the pipes after the kill would hang again on any descendant that left
  the group with its own `setsid`.

## Related

- `a-feeder-manifest-keeps-every-card-it-appended-so-the-landed-sample-is-the-queues-history-not-one-cycles.md`
  is #50, which added the queue line this changes.

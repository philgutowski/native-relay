---
title: A SIGTERM handler added beside an existing except BaseException cleanup ends the same process group twice
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: low
root_cause: signal_handler_added_without_removing_the_cleanup_it_now_duplicates
resolution_type: code_fix
related_components: [feeder, launch]
symptoms:
  - "none observed in production; caught by an independent code review agent before merge"
  - "a real SIGTERM during a blocking hook calls end_group(proc) twice: once from the new
    signal handler, once from the pre-existing except BaseException clause the handler's own
    KeyboardInterrupt unwinds through"
tags: [feeder, signal-handling, sigterm, process-group, code-review-catch]
---

# A SIGTERM handler added beside an existing except BaseException cleanup ends the same process group twice

## Problem

Issue #56. `run_hook` in `skills/relay/scripts/relay/feeder.py` already ended a blocking hook's
process group on any exception (`except BaseException: end_group(proc); raise`), which covered a
timeout and a keyboard interrupt. It did not cover SIGTERM: Python raises no exception of its own
for that signal, so ending the feeder by SIGTERM during a blocking hook left the hook (and
anything it started) running in the checkout.

The fix installed a SIGTERM handler around `proc.wait()`, modeled on `launch.py`'s existing
SIGINT/SIGTERM handling around a Task process:

```python
def handle(signum, frame):
    end_group(proc)
    if callable(previous):
        previous(signum, frame)
    else:
        raise KeyboardInterrupt()

signal.signal(signal.SIGTERM, handle)
try:
    return proc.wait(timeout=timeout)
except BaseException:
    end_group(proc)
    raise
```

This looks right by inspection and passed a test that sends SIGTERM to a real subprocess running
the hook. It is still wrong: `launch.py`'s `handle()` is the *only* place that ends its group for
that code path, with no enclosing catch-all beside it. `run_hook` already had one. In production,
no other SIGTERM handler exists anywhere in `feeder.py` or `cli.py`, so `previous` is always
`SIG_DFL`, never callable. On a real SIGTERM: `handle()` runs, ends the group, then raises
`KeyboardInterrupt`; that exception is what `proc.wait()`'s EINTR-retry surfaces, so it lands in
the pre-existing `except BaseException` clause too, which ends the same group a second time.

## Why this was not obvious from reading the diff

The two call sites read as independent: one is inside a nested function defined for the signal
handler, the other is the ordinary exception cleanup a few lines below it, and nothing in either
call site's text mentions the other. The two-call chain only appears at the moment a real SIGTERM
arrives while `proc.wait()` is blocked, which the manual read-through never modeled: it read
`handle()` and the `except BaseException` clause as covering two different failure axes, signal
versus exception, and missed that this signal *becomes* that exception in exactly this file's own
control flow. A single automated review agent focused narrowly on "does this function still
behave the way its comment claims" caught it; four other review agents examining the same diff
from different angles (reuse, cross-file tracing, altitude, simplification) did not.

## What did not need fixing

`end_group` is written to be idempotent: a second `os.killpg` on an already-ended group raises
`OSError` (`ESRCH`), caught and ignored; a second `proc.wait()` on an already-reaped process
returns the cached return code. So the double call was not a crash and not a hang. It broke an
invariant the surrounding comment implied ("the whole group is ended on a timeout, an interrupt,
or the feeder itself being told to terminate", read as *once*), and it carried a narrow hazard: if
the OS recycled `proc.pid` as a new process group leader in the brief window between the two
calls, the second `end_group` would signal an unrelated process group.

## Solution

A `nonlocal` flag makes the shared cleanup run once, called from both the handler and the
existing exception clause:

```python
ended = False

def end_once():
    nonlocal ended
    if not ended:
        ended = True
        end_group(proc)

def handle(signum, frame):
    end_once()
    ...

try:
    return proc.wait(timeout=timeout)
except BaseException:
    end_once()
    raise
```

## Prevention

- Before copying a signal-handling pattern from one function into another, check what the
  destination function already does on the exception the signal becomes (here, `KeyboardInterrupt`
  by construction, since no other handler is installed). A pattern that is safe in isolation
  (`launch.py`) is not automatically safe next to a pre-existing catch-all.
- `end_group`'s idempotency is a safety net, not a design invariant to rely on going in: it hid
  this exact bug from the new test, whose own comment says the driver's `KeyboardInterrupt`
  traceback is "expected" without asking whether the cleanup ran once or twice to get there.
- The same leaf-level SIGTERM install/restore pattern was applied only to `run_hook`, not to the
  structurally identical `run_command` (used by `pre_cycle_command` and the ready command, and hit
  every cycle, more often than the optional blocking hook). That gap is real and was left for a
  follow-up issue rather than folded into this change, since the task that produced this fix named
  `run_hook` specifically. A future fix there should re-check this same double-call hazard rather
  than assume the copied pattern is already safe.

---
title: The feeder lock file is never removed, so a waiter on its existence never fires, and feed --stop in an idle wait takes until the next wake
date: 2026-10-14
category: workflow-issues
module: feeder
problem_type: workflow_issue
component: feeder
severity: medium
root_cause: missing_workflow_step
resolution_type: documentation_update
related_components: [feeder, cli, liveness, board-lanes]
applies_when:
  - "an operator or a watcher session needs to run something that must not overlap a feeder, and has asked the feeder to leave"
  - "a script waits for a feeder to exit"
  - "feed --stop was placed while the feeder was in an idle wait or a limit wait"
symptoms:
  - "a background waiter on the lock file's existence runs until the harness kills it and never reports the feeder gone"
  - "feed --stop returns at once and the feeder stays up for up to idle_wait_seconds (1800) more"
  - "the lock file carries a date weeks before the feeder that holds it started"
tags: [feeder, stop-file, lock, flock, idle-wait, waiter, operator-procedure, issue-139]
---

# The feeder lock file is never removed, so a waiter on its existence never fires, and feed --stop in an idle wait takes until the next wake

## Context

Issue #139. Cratekit board, 2026-10-13, the operator seat. The day's run had landed and the
feeder was idle ("nothing ready and nothing unsettled, waiting (1 of 48)"). Phillip wanted a
rekordbox session that night, and rekordbox never runs beside a feeder, so the operator ran
`feed <manifest> --stop` at 20:04 and set a background shell waiter to report when the feeder
was gone:

    until [ ! -e <board>.feeder.lock ] && ! ps -p <pid> >/dev/null; do sleep 15; done

The waiter never fired. The feeder left at 20:28, and the rekordbox read back waited until
the next morning.

## What was wrong

Two separate things.

**The lock file is never unlinked.** `<board>.feeder.lock` is an empty file the feeder holds
with `flock(LOCK_EX | LOCK_NB)` for its life; the operating system releases the lock when the
process exits, and the file stays. Cratekit's is dated 2026-09-20, the first feeder on that
board. So "the lock file is gone" is a condition that is never true, and a waiter on it runs
until something else kills it. The harness ends every background shell at thirty minutes, so
the waiter died silently as well. (The companion entry, feeder liveness is the manifest's own
lock, says the lock is the liveness check; it does not say the file outlives the holder, and a
reader who has not seen the flock code assumes a lock file is removed on exit.)

**The stop file is read at the top of a cycle only.** In an idle wait the feeder sleeps
`idle_wait_seconds` (1800 by default) and reads the stop file when it wakes, so a stop placed
a minute into the wait is honoured 29 minutes later. `--stop`'s own output does not say this,
and names nothing to watch meanwhile.

## What to do instead

Wait on one of the three things that do change when the feeder leaves:

- the pid in the state file's `process` block: `ps -p <pid>`;
- the lock being free, the same probe `liveness` makes:

      python3 -c 'import fcntl,sys; h=open(sys.argv[1]); fcntl.flock(h, fcntl.LOCK_EX|fcntl.LOCK_NB); print("free")' <board>.feeder.lock

  which raises BlockingIOError while a feeder holds it;
- the "stop file present, leaving" line at the tail of `<board>.feeder.log`.

And expect the leave to take up to the idle wait interval. A waiter is re armed every 25
minutes or so, because the harness cap is the waiter's lifetime, not the feeder's.

## What the fix should be

Issue #139 asks for `feed --stop --wait`, a wake on the stop file inside idle and limit waits,
and a `--stop` message that names the lock, the pid and the log line and says the lock file
is never removed.

## Where it is recorded

Cratekit `docs/cutover.md`, sitting 40 (2026-10-14). The board-lanes skill in agent-config
carries the one line rule under "Traps paid for".

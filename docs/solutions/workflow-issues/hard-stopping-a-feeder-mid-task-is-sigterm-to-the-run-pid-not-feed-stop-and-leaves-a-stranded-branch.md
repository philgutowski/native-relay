---
title: Hard stopping a feeder mid task is SIGTERM to the run pid, not feed --stop, and it leaves a stranded branch
date: 2026-09-24
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: medium
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [feeder, launch, lease, gitwrite]
applies_when:
  - "the operator must stop a running feeder now, for a network reset, a reboot, or a machine hand off, while a Task is mid build"
  - "feed --stop was placed but a Task process is still running and will run for a long time yet"
  - "a feeder is about to be restarted after a hard stop and the checkout is on a Task branch"
symptoms:
  - "feed --stop returns at once but the feeder and its Task process keep running for the rest of the current cycle"
  - "after the run pid is killed the summary reads crashed while the Task stays running in the state file"
  - "the checkout sits on the Task branch with an uncommitted tree"
tags: [feeder, stop-file, sigterm, hard-stop, lease, crashed, task-branch, operator-procedure]
---

# Hard stopping a feeder mid task is SIGTERM to the run pid, not feed --stop, and it leaves a stranded branch

## Context

Relay has no pause between Tasks. On 2026-09-24 the Cratekit feeder, `relay feed cratekit-board.toml`, had to stop for a router reset while a Task was 18 minutes into its build. `feed --stop` only places the stop file. The feeder reads that file at the top of its next cycle (`feeder.py` logs "stop file present, leaving"), so the wait would have been the rest of the current batch. It is a graceful stop, not an interrupt.

## Guidance

The clean hard stop is SIGTERM to the pid of `relay_cli.py run <manifest>`, the child of the feeder, not the feeder itself and not the `claude -p` process. `ps -axo pid,ppid,command | grep relay_cli` shows both, and the run pid is the one whose command says `run`. The runner's handler in `launch.py` (`handle`) kills the Task process group, calls the lease release, then re-raises as an interrupt. The record reads `crashed` and the Task is left `running` in the state file.

Sequence that worked:

1. `feed --stop`, so no new cycle starts once the run dies.
2. SIGTERM to the `run` pid.
3. Clear the stranded Task branch. The checkout is on it with an uncommitted tree. Clear it, or tag it and delete it if the partial work is worth keeping. Restarting with it in place fails the `no_task_branch` preflight, see `task-branch-in-flight-from-an-earlier-run-fails-no-task-branch-preflight-and-validate-never-warns.md`.
4. Remove the stop file.
5. `feed --dry-run` to confirm the queue reads sanely.
6. `feed --detach --notify`, from a fresh pinned extract.

## What to expect after restart

The feeder rebuilds the interrupted card from scratch, because nothing of the crashed attempt is resumed. It also re-appends the ready cards it never reached. No commit was involved, this is operator procedure.

## Applicability

Use this only for a stop that cannot wait. If a Task is near its end, `feed --stop` alone is the cheaper choice and leaves no branch to clean. Only the `run` pid is signalled, so a second feeder on another manifest is untouched.

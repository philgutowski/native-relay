# Parallel builds in worktrees: up to two Task processes at a time, merges still serial

Date: 2026-09-08
Status: implemented 2026-09-17 as dual manifest dispatch (one claude build and one grok build
at a time, worktrees, merges in pair order). See docs/plans/2026-09-17-feat-dual-manifest-dispatch-plan.md.
This note is the design that was talked through first.

## The ask

Run up to two Tasks of one Manifest at a time. The gain is wall clock: a Task process spends most
of its life planning, building, and reviewing, and two of those can overlap.

## Why it is not a flag

The repo level Lease (`state.py`, R31) exists so two runners never interleave merges into one
repository. The run loop does everything, checkout, branch, merge, push, verify, on the single
working directory at `manifest.project.repo`, and the Task brief tells the process to create its
own branch as its first step, inside that directory. Two Task processes in one working directory
would step on each other's checkout constantly. So "two at a time" needs a second working
directory, and it needs the merge to stay one at a time.

## The lighter shape that was agreed in outline

Only the build phase runs in parallel. The merge, push, verify, and closeout stay one at a time,
in Manifest order, on the main repo directory exactly as today.

1. A git worktree per concurrent build. A worktree is a second working folder checked out from
   the same repository, sharing its history and branches, so two branches can be edited at once.
   The Task process launches with `cwd` set to its worktree (`launch.py`, the one `popen` call)
   instead of the repo. Location: `~/.relay/<hash>/worktrees/<task-id>/`.
2. A new Manifest key, `project.max_concurrent_builds`, default 1 (today's behaviour), cap 2, and
   `validate` refuses anything above 2. Manifest rather than a CLI flag, because the Manifest
   carries every project specific fact.
3. The loop keeps a window of up to two builds in flight. Task N launches; while it builds, if a
   slot is free and Task N+1 passes the same eligibility checks a Task passes today (not landed,
   not excluded, card readable, card not terminal), it launches in its own worktree.
4. Merges strictly in Manifest order. When Task N's process exits, the runner classifies it and
   runs the existing merge route. Task N+1 may have finished building first; it waits. This keeps
   the resume rule (the next run resumes at the first record that did not land) and the cursor
   honest.
5. On a halt that does not continue past, the runner kills the sibling build, deletes its
   worktree and its half built branch, and leaves its record as pending so the next run starts it
   fresh. Anything else strands a branch that blocks pre flight.

What stays untouched: both Leases, the halt class set, `local_merge_tail`, closeout, verify. The
Lease still means one Runner per Manifest and one merger per repository.

## Two traps found while reading the seams

- Git refuses to check out a branch that is currently checked out in another worktree. The merge
  tail does exactly that (`gitwrite.local_merge_tail` checks the Task branch out), so the
  worktree must be removed after the Task process exits and before the merge tail starts.
- The Claude CLI files each session transcript under a folder named after the process's working
  directory, and the classifier finds it by `cwd` (`launch.find_transcript`). Launching in a
  worktree changes that path, so the classifier has to be handed the worktree path. That is a
  contract between two processes, so by this repo's own rule it needs one live run against a
  throwaway target before it counts as done.

## Open questions for the plan

- Heartbeat coverage across two concurrent launches. `launch.launch` runs its own `_Heartbeat`;
  two of them renewing the same lease is harmless, but the merge tail's own heartbeat and the
  concurrent launch's must both be alive during the window.
- Whether a Closeout for Task N may overlap Task N+1's build. Probably yes, since it writes to a
  different card and its own allowed paths, but it commits on the default branch, which the
  worktree's branch will later be merged onto.
- How the Follower and `progress` present two in flight Tasks. Both assume one today.

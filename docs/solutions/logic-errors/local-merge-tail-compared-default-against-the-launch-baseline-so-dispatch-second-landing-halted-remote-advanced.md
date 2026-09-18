---
title: local_merge_tail compared default against the launch baseline, so Dispatch's second landing halted remote_advanced
date: 2026-09-17
category: logic-errors
module: runner
problem_type: logic_error
component: runner
severity: high
root_cause: missing_validation
resolution_type: code_fix
related_components: [gitwrite, run-loop, worktree, dispatch]
symptoms:
  - "Dispatch launches a claude Task and a grok Task from the same baseline, so the first landing always moves the default branch under the second"
  - "comparing the second merge against the launch baseline halted every later Task with remote_advanced even when the only mover was this coordinator's own previous landing"
  - "serial run stayed green because each Task's baseline_sha is captured after the previous landing, so remote_sha equals baseline_sha"
  - "a finished build waiting its merge turn still occupied that backend's slot; freeing the slot at process exit started the next same-backend Task too early"
tags: [dispatch, merge-tail, remote-advanced, expected-default, launch-baseline, worktree, slot-occupancy]
---

# local_merge_tail compared default against the launch baseline, so Dispatch's second landing halted remote_advanced

## Problem

`gitwrite.local_merge_tail` refuses to merge when the default branch moved during the Task. That refusal is Halt class `remote_advanced`, a run scoped class. The Cause line names both movers: the local default branch, or the remote. Serial `run()` is correct under that rule, because each Task's `baseline_sha` is captured at launch after the previous landing. Dispatch launches a claude Task process and a grok Task process from the same baseline, then merges in Pair order, so the first landing always moves default under the second. Comparing that later merge against the launch baseline halted every later Task with `remote_advanced`, even when the only mover was this coordinator's own previous landing plus its Closeout commit.

## Symptoms

- A Dispatch of a mixed Pair lands the first Task and then halts the next one with Halt class `remote_advanced`. The evidence names the SHA of this coordinator's own merge as if a concurrent session had advanced the landing base.
- Serial `run()` does not show it. Each Task recaptures `baseline_sha` at launch after the previous landing.
- The four Task Dispatch end to end (`tests/test_dispatch.py`, `test_four_tasks_land_in_manifest_order_even_when_grok_finishes_first`) is the shape that would stop on T-2: T-1 and T-2 launch together from the same main, T-1 merges first, T-2's tail then sees main past T-2's launch baseline.
- Under `shipping.push = false` the halt fires at stage `baseline` from `_unpushed_base_refusal`. Under push true it fires at stage `fetch`. The class is the same either way.
- The operator repair for a real `remote_advanced` is rebase or redo the Task branch by hand. That repair is the wrong action when the mover was this Dispatch's previous landing.

## What Didn't Work

- Leaving `local_merge_tail` untouched, as `docs/ideation/2026-09-08-parallel-builds-in-worktrees.md` proposed. Merges stay serial and the Lease stays exclusive. The ideation did not split "the default moved" into "a foreign session moved it" versus "this coordinator already landed the previous Task".
- Recapturing `baseline_sha` at merge time, the way serial `run()` recaptures it at the next launch. That would make the mover check pass and would also move `claude_dir_backstop` and the Task path bound onto the new default. Those diffs must stay against what the Task started from.
- Dropping the mover check. A real concurrent session committing to the default branch is the collision KTD3 of the no push plan exists to stop. `remote_advanced` stays run scoped because every later baseline is then suspect.
- A new Halt class for sibling landing. Halt classes are a closed set. The sibling case is the existing check comparing against the wrong SHA.
- Treating Dispatch as two Runners, each recapturing baseline the serial way. Two Runners cannot merge into one repository. The repo Lease is exclusive for the whole Dispatch.
- Updating the compare SHA only after the merge, and not after Closeout. The Closeout commit would then look like a foreign mover for the next Task.
- (session history) A 2026-09-17 live serial sweep with push off halted IW-289 as `remote_advanced` because main moved while that Task was running. That was a real foreign mover and correctly stopped the run. Serial Relay recaptures baseline between Tasks, so a later Task in the same serial run can still land. Dispatch's later merge is the case that still used a stale comparison.

## Solution

`local_merge_tail` takes `expected_default=None` (`gitwrite.py:405`). When set, it is the SHA the coordinator believes the default branch sits at after its own earlier landings in this Dispatch. The `remote_advanced` check compares against that SHA rather than the Task's launch baseline. Serial `run` leaves it unset and behaviour is unchanged.

```python
compare_sha = expected_default if expected_default is not None else baseline_sha
```

That line is `gitwrite.py:457`. On the push true path, fetch then `remote_sha != compare_sha` refuses. Evidence keeps the Task launch SHA under `baseline_sha` and records `expected_default` as `compare_sha`. On the no push path, `_unpushed_base_refusal` is called with `compare_sha` in the slot it used to call `baseline_sha` (`gitwrite.py:468`).

`claude_dir_backstop` still receives `baseline_sha`. The Task path bound still receives `ctx.baseline_sha`. Only the mover check uses `compare_sha`.

`_Run.expected_default` defaults to `None` (`run.py:64`). Serial `run()` never assigns it. `_merge_route` always passes `expected_default=ctx.expected_default` (`run.py:1228`), which is `None` on the serial path.

Dispatch writes the field in `_concurrent_loop`. After each completed merge and Closeout, drain does `cfg.expected_default = gitread.rev_parse(cfg.repo, cfg.default)` (`run.py:1056`). A halt that continues past does the same read (`run.py:1066`). The first Task of a Dispatch still merges with `expected_default is None`, because no sibling has landed yet.

`tests/test_gitwrite.py` class `DispatchExpectedDefault` pins the seam. `test_a_sibling_landing_is_not_a_foreign_mover_when_expected_default_matches` builds T-2 from original main, lands a sibling commit on main, pushes it, and calls `run_tail(expected_default=sibling)`. It asserts `result.ok`. `test_a_real_foreign_mover_still_refuses_when_expected_default_is_stale` does the same setup but passes `expected_default=self.baseline` and asserts `halt_class == contracts.HALT_REMOTE_ADVANCED`.

## Why This Works

KTD3 of the no push plan asked whether the landing base moved under this Task in a way the operator must reconcile. Serial `run()` answers that by capturing `baseline_sha` after the previous landing, so any movement during this Task is foreign. Dispatch cannot recapture at launch: two Task processes start together, from one SHA, in worktrees. The question for the later merge is whether the landing base moved past what this coordinator already wrote. `expected_default` is that SHA.

`None` versus a SHA is the serial versus Dispatch split. Serial never writes the field. Dispatch writes it only after a completed `_complete_task` (or a continued past halt), from `gitread.rev_parse` of the default branch as it sits then.

Keeping `baseline_sha` on the backstop and the path bound preserves the Task's own diff. The mover check is not a scope check.

The Halt class stays `remote_advanced`. A stale `expected_default` still refuses. A matching `expected_default` after this coordinator's own merge still lands. The operator repair for a true foreign mover is unchanged.

## Prevention

- Keep `DispatchExpectedDefault.test_a_sibling_landing_is_not_a_foreign_mover_when_expected_default_matches` (`tests/test_gitwrite.py:266`) and its stale counterpart (`tests/test_gitwrite.py:278`). Those two prove the mover check honors a matching versus stale `expected_default`. They do not prove the backstop or the Task path bound are unchanged.
- Serial `run` must keep leaving `expected_default` unset. `_Run.expected_default` defaults to `None`. `run()` never assigns the field. `RemoteMoved.test_a_remote_past_the_baseline_halts_before_any_merge` and `NoPushTail.test_a_local_default_that_moved_during_the_task_refuses_before_the_merge` call `run_tail()` with no `expected_default`. A future edit that writes `expected_default` inside `run()` would make serial treat this run's own previous landing as expected even when a concurrent session could have been the mover.
- Do not reuse `baseline_sha` for the mover check in Dispatch. Do not point `claude_dir_backstop` or `task_scope_offenders` at `expected_default`. Do not add a Halt class.
- The four Task Dispatch test (`tests/test_dispatch.py:111`) landing T-1 through T-4 in Manifest order, including when grok finishes first, is the integration check that drain actually stores `expected_default` after Closeout. A green unit test on `local_merge_tail` with a hand passed SHA does not prove that wiring.
- Neighbouring trap, same note, not a second learning. A finished build waiting its merge turn still occupies that backend's slot. `backend_busy` (`run.py:1014`) returns true when the backend is in `slots` and also when any waiting pair has that backend. The wait loop deletes the slot when the process exits (`run.py:1087`) and only then records the result under `waiting`. If `backend_busy` looked only at `slots`, the next `fill` would launch the next same backend Task while the finished one still waited to merge. That collides two grok (or two claude) branches, and in tests the stub FIFO queue is stolen so a Closeout can run another Task's `git.sh`. Keep the waiting check. Do not free the backend at process exit.

## Related Issues

- `docs/plans/2026-09-10-feat-no-push-shipping-plan.md` KTD3: any default branch movement during the Task is `remote_advanced` against the launch baseline. Serial run still means that. Dispatch is the first caller for whom the coordinator itself is the mover.
- `docs/ideation/2026-09-08-parallel-builds-in-worktrees.md`: merges stay serial; the worktree must be removed before the merge tail. It also said `local_merge_tail` stays untouched, which `expected_default` reversed, and it treated a slot as free once the build process ends.
- `docs/plans/2026-09-17-feat-dual-manifest-dispatch-plan.md` KTD4: `expected_default` is the SHA of the default branch after this coordinator's last landing, including the Closeout commit.
- `docs/solutions/logic-errors/continue-past-halt-checked-general-state-blind-to-the-branch-its-own-skip-left.md`: same family. A general safety check treats the coordinator's own leftover as a foreign blocker.
- `docs/solutions/workflow-issues/foreign-untracked-file-from-another-session-fails-tree-clean-preflight-and-unclean-exit-blames-the-task.md`: provenance gap on a preflight. `tree_clean` cannot tell own dirt from foreign dirt. `remote_advanced` had the same gap for default branch SHA movement until `expected_default` named this coordinator's own landings.
- GitHub issue 15: cited as the no push product source (KTD3). The same issue number also names continue past task halt in the runner.

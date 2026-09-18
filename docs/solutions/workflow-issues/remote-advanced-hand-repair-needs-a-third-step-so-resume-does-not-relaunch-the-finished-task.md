---
title: "The remote_advanced repair has a third step, move the card, and a resume without it re-baselines the record so the landing can never verify"
date: 2026-09-17
category: workflow-issues
module: runner
problem_type: workflow_issue
component: development_workflow
severity: high
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components:
  - verify
  - run-loop
  - gitwrite
  - adapters
  - audit
applies_when:
  - a run halts with Halt class remote_advanced because the default branch moved during the Task
  - repairing a remote_advanced halt by hand, rebasing the Task branch onto the default branch and merging it
  - resuming a run after a by-hand repair, expecting Verify-landed to recognise the merge as landed
  - reading the halt table's remote_advanced row as a two step repair, rebase the branch then resume
  - deciding whether a hand repaired Task's card moves forward to a done status or back to its todo status
symptoms:
  - resume relaunches a Task whose work is already merged to the default branch, running the whole pipeline again from an empty context
  - new_commit_since_baseline fails with baseline_sha equal to head_sha and a commit count of zero, so the verdict reads not landed although the code is on the default branch
  - the run summary and the audit both tell the operator to move the card back to its todo status, which is the wrong direction once the branch has been hand landed
  - a record that should read landed reads skipped instead, permanently
tags:
  - remote-advanced
  - hand-repair
  - verify-landed
  - resume
  - tracker-card
  - halt-table
  - re-baseline
  - skipped
---

# The remote_advanced repair has a third step, move the card, and a resume without it re-baselines the record so the landing can never verify

## Context

Halt class `remote_advanced` fires when the default branch moves under a Task while that Task is running, which on a live repository means a peer session merged something mid build. The halt table in `skills/relay/SKILL.md:310` reads:

| `remote_advanced` | the default branch moved during the task, locally or at the remote (the evidence's `reason` says which), or the merge conflicted | rebase or redo the task branch by hand, resume |

That row names two steps, rebase and resume, and it is the whole instruction an operator gets. It is incomplete. The Task branch carries finished work, and the repair merges it, so after the repair the Task has landed by every measure except the one Verify-landed actually reads. The card is still sitting at the in review status, because the Closeout process never ran for a Task whose merge tail refused. Resuming on that state relaunches a Task whose work is already on the default branch, and the relaunch is what makes the damage permanent.

Two live sweeps on 2026-09-17 against the support-workbench repository, both under the `local_merge` shipping mode with `shipping.push` off, ran the same halt to two different endings. The only difference between them was whether the operator moved the card before typing `run` again.

## Guidance

Treat the repair as three steps, not two, and run them in this order.

**1. Land the branch by hand.** From the target repository, rebase the Task branch onto the new default head, run the project's own gate, and merge. For a project whose default branch is `main` and whose Task branch is the card key:

```bash
git checkout IW-310
git rebase main
./scripts/test.sh
git checkout main
git merge --no-ff IW-310
```

Push only if the Manifest's shipping mode pushes. Under `shipping.push = false` the local default branch is the landing, and `verify` says so itself rather than consulting the remote (`verify.py:157` guards on the push setting, `verify.py:160` records the skip).

**2. Move the card forward to a status the Manifest calls done.** This is the step the halt table omits and the one that decides everything downstream. The status names come from the Manifest's `tracker.done_statuses` (`manifest.py:71`, parsed at `manifest.py:247`, required for the jira adapter at `manifest.py:514`). The Jira adapter lowercases that list once (`jira.py:87`) and reports a card terminal when its status name is in it (`jira.py:183`). Comment the merge commit on the card while you are there, so the board carries the evidence the Closeout process would have written.

**3. Clean the checkout, then resume.** Pre flight runs before the Runner reads any card, and its checks are ordered tree clean, on default, in sync, no task branch (`gitwrite.py:296`). A leftover Task branch or a checkout sitting on one halts the next run with class `unclean_exit` before the terminal card check is ever reached (`run.py:733`). So delete the merged branch and return to the default branch first:

```bash
git branch -d IW-310
python3 skills/relay/scripts/relay_cli.py run <manifest>
```

The `verify` verb is worth running between steps two and three to see what the record thinks, but do not treat a `not landed` verdict there as a reason to redo the work. Read the next section for what that verdict is actually telling you.

## Why This Matters

The Runner decides landing from git and the Tracker alone, and the git half is a comparison against the record's stored `baseline_sha`. `new_commit_since_baseline` passes only when at least one commit sits between that baseline and the current default head (`verify.py:168` reads the stored baseline, and the check is decided at `verify.py:176`, which fails on a commit count of zero). A halted Task's baseline is the default head captured at its launch, so the operator's own merge is a commit after it and the check passes.

A relaunch destroys that. `_begin_task` takes a fresh baseline from the current default head (`run.py:747`) and writes it over the record on the running upsert (`run.py:789`). After a relaunch, `baseline_sha` is the operator's merge commit itself, which means the merge is no longer after the baseline, it is the baseline, and `new_commit_since_baseline` can never pass again for that Task. No later repair recovers it, because nothing recomputes a baseline backwards.

That is exactly what the IW-310 record showed once the card had been moved:

```
  new_commit_since_baseline  fail  {"baseline_sha": "a42b9b430d5d91eff39eed64a6f56e0ee2cb2679", "commits": 0, "head_sha": "a42b9b430d5d91eff39eed64a6f56e0ee2cb2679"}
```

Baseline and head are the same SHA, the operator's own merge, and that SHA is a commit in the support-workbench repository rather than in this one. `on_default` failed beside it because the killed relaunch had left the checkout on a re-created Task branch.

What the card move buys is a decision made earlier in the sequence than any of this. `_begin_task` reads the card's status immediately after taking the baseline and before the record is written, and a terminal card returns early with a Runner decided skip (`run.py:749` guards it, `run.py:753` calls `_skip`, defined at `run.py:646`, which writes `contracts.STATUS_SKIPPED` and a `skip_reason`, and `run.py:755` is the return itself). The early return sits between the baseline read and the upsert, so a terminal card is also what stops the re-baseline from happening at all. Move the card first and the record is never clobbered; resume first and it is clobbered before the card can help.

`skipped` and `excluded` are different records here, and the difference is why this repair works at all. `STATUS_EXCLUDED` is the Manifest's own `excluded = true`, and `STATUS_SKIPPED` is the Runner's launch time decision, checked again on every run (`contracts.py:381` states the rule, with the two constants at `contracts.py:383` and `contracts.py:384`). A skip is not a permanent verdict on the Task, it is a report of what the card read at that moment.

Startup re-verify is the mechanism that would have promoted the record cleanly had the card been moved before the resume. It runs at the top of `run` (`run.py:304`) over halted records only, at full scope, and promotes the ones that now pass (`verify.py:342`). With the original baseline intact and the card terminal, both halves of Landed hold: `new_commit_since_baseline` sees the operator's merge, `card_terminal` passes, and `hand_landing` (`verify.py:283`) finds a default branch commit naming the Task, matching a non numeric id as a whole word (`verify.py:275`). The record then reads `landed` rather than `skipped`.

The cost of missing the step is a full task rebuild. The IW-310 relaunch started a fresh Task process against work that was already merged and was killed roughly six minutes in. A Task process is a headless agent invocation with a plan, build, review and verify pipeline in front of it, so letting one run to completion would have spent the whole build budget rewriting a landed change, and the merge tail would then have had its own collision to resolve. Six minutes was the cheap outcome.

### The runner tells you to do the opposite, and it is not wrong, it is uninformed

Expect the tooling to argue with this. The audit raises `card_stale_in_review` for any card that reads the in review status with no process working on it, and the sentence it writes sends the card **backwards**, to `baseline_tracker_status` or, when that is unknown, to "its todo status" (`audit.py:77` through `audit.py:85`). The run summary carries the same finding. The audit has no way to know whether the branch was hand landed, so it gives one answer for two opposite situations:

- **The branch is finished and you merged it.** Move the card forward to a done status. The audit's advice is wrong here, and following it re-arms the relaunch the whole learning is about.
- **The branch is unfinished, or you threw it away to redo the work.** Move the card back, exactly as the audit says, and delete the branch so pre flight does not refuse the next run.

A session on 2026-09-17 recommended the backwards move for a `remote_advanced` halt and was correct, because that Task's branch was being abandoned rather than landed (session history). The two pieces of advice only look contradictory until you notice they answer different questions. The deciding question is not the halt class, it is whether the work is on the default branch now.

One further wart to expect at the end. A record that reaches `skipped` is listed in the run summary's check by hand section as "was skipped by the runner: the card already reads Done, which is terminal; nothing to run. Fix the card and run again, or run it attended." (`summary.py:124` guards it, the sentence template is at `summary.py:127`). That sentence is written for the ordinary skip causes, an unreadable card or one caught by the brief scan, and it asks for a repair that is already done. Read it as a notice, not as an instruction.

## When to Apply

- Halt class `remote_advanced`, which is run scoped and always stops the run, whenever the Task branch holds finished work and only the merge was refused. If the branch is incomplete, this is a rerun rather than a repair, the card goes back rather than forward, and the branch is deleted.
- Shipping mode `local_merge`, both with `shipping.push` on and off. Under no push, `head_equals_remote` skips rather than fails (`verify.py:157` and `verify.py:160`) and the local default branch is the landing, so step one ends at the merge.
- The same three step shape applies to any halt whose repair is a hand landing, which is why the halt table's `partial_landing` and `tracker_write_denied` rows already name moving the card. Those two rows carry the step; the `remote_advanced` row is the one that does not.
- It does not apply to a `blocked` outcome. Blocked is a deliberate stop with the repository left as it was found, and `--retry-blocked` is the lever there.
- It does not apply to Dispatch treating its own earlier landing as a foreign mover. That was a code defect with its own fix, and the learning beside this one covers it.

## Examples

**IW-289, 2026-09-17, the repair done right.** A serial sweep against support-workbench halted at the merge step with `remote_advanced` because a peer session merged to `main` mid build. The operator rebased the Task branch onto the new `main`, ran the project gate green, merged, commented the merge commit on the card, and transitioned the card to its done status. Resuming the run worked. The card move happened before the resume, so startup re-verify read a record whose baseline still predated the merge and promoted it.

**IW-310, 2026-09-17, the same repair minus step two.** The same halt class from the same cause, a docs only commit reaching `main` one minute after launch. The operator rebased, gated green, merged as `a42b9b4` in that repository, and resumed without moving the card. The Runner relaunched a completed Task from scratch and it was killed roughly six minutes in. Moving the card afterwards and running `verify` produced the verdict above: every relevant check either skipped for the shipping mode or failing on a baseline that now equals head, with `card_terminal` the one thing passing. The record could not be promoted by any means. What resolved it was the next `run`, where the terminal card check fired at launch and recorded the Task as skipped with the reason "the card already reads Done, which is terminal; nothing to run", after which the sweep continued and completed with 15 landed.

The two cases differ by one board transition and by about six minutes of build time, and the second one also leaves a record that reads `skipped` forever where the first reads `landed`.

## Related

- `docs/solutions/workflow-issues/hand-landing-repair-lands-only-when-a-commit-names-the-issue-number-as-a-word.md`, the sibling operator procedure for a tracker write halt. It documents the other half of the same recognition machinery, the commit message pattern `hand_landing` matches, and its step five is this doc's step two under a different halt class.
- `docs/solutions/logic-errors/verify-checked-only-one-direction-of-the-landing-tracker-link.md`, the code side ancestor that taught `verify` to derive a landing from the commit graph. Its own record already lists `remote_advanced` among the classes whose documented recovery path is repair by hand then resume.
- `docs/solutions/logic-errors/local-merge-tail-compared-default-against-the-launch-baseline-so-dispatch-second-landing-halted-remote-advanced.md`, the case where the mover was Dispatch itself rather than a peer session. That one was fixed in code with `expected_default`; this one is a gap in the operator instruction and has no code fix implied. The two share a halt class name and nothing else, so read the title before assuming a search hit is the one you want.
- `docs/solutions/workflow-issues/change-spanning-a-live-template-and-a-frozen-module-breaks-the-landing-run.md`, the earlier one step version of the same repair shape, move the card and run again.
- `skills/relay/SKILL.md:310`, the halt table row this learning says is short a step.

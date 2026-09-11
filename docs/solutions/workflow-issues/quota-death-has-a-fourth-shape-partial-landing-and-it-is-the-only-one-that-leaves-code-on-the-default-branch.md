---
title: A quota death has a fourth shape, partial_landing, and it is the only one that leaves code on the default branch, sometimes unreviewed
date: 2026-09-10
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: high
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [run-loop, verify, closeout, classify, adapters, summary]
applies_when:
  - "reading a run that died on a usage limit and deciding which repair each burned Task needs"
  - "a Task record reads partial_landing, or a summary line says landed at <sha> alongside did not verify as landed"
  - "repairing a Task whose record already carries a landing_ref, rather than one whose branch never merged"
  - "authoring or relaunching a Manifest after a limit that named one model rather than the account"
  - "deciding whether the default branch is safe to build on after an unattended run was cut off"
symptoms:
  - "the summary reads landed at <sha> and did not verify as landed: card_terminal, closing_reference on the same Task"
  - "the default branch carries a merged unit whose issue is still open and whose review, mutation table and record never ran"
  - "relay verify reports not landed on a hand repaired Task even though a default branch commit names the issue as #N"
  - "two different limit messages in one run's logs, one naming the account session and one naming a single model"
  - "a relaunch on the same model halts within seconds while the same Manifest on another model runs to completion"
tags:
  - usage-quota
  - model-limit
  - partial-landing
  - halt-classification
  - hand-repair
  - closing-reference
  - unattended-run
  - review-gap
---

# A quota death has a fourth shape, partial_landing, and it is the only one that leaves code on the default branch, sometimes unreviewed

## Context

Two sibling docs already own most of this ground and this one does not repeat them.
`quota-exhaustion-reads-as-no-envelope-and-the-rate-limit-telemetry-is-already-discarded.md` owns
what exhaustion looks like from inside the Runner, the measured cost table, the discarded
`rate_limit_event` telemetry, and pre launch sizing.
`on-halt-continue-past-task-halt-is-not-the-quota-switch-and-the-path-a-quota-death-takes-decides-whether-the-manifest-votes.md`
owns the authoring correction, and carries a three row table of the shapes a quota killed Task can
leave: no transcript, a transcript with no envelope, and an envelope claiming complete or a dirty
tree.

That table is right about every Task the Runner had not yet merged. It has no row for the Task the
Runner had already merged and pushed.

On 2026-09-08 a six Task Cratekit run died on an account session limit. Task 72 had reached the
Runner's merge, so the code was on `main` and on the remote before the window closed. On
2026-09-09 the relaunch died again on Task 75, this time on a limit naming a single model rather
than the account, and that one had not merged. Two deaths, two shapes, and two repairs that share
almost nothing. The relaunch on 2026-09-10 with one Task's `model` changed landed all six.

The shape that had already merged is the dangerous one, and it is the one with no row.

## Guidance

**Add the fourth row before you read a burned run.**

| What the dying process left | Halt class | Default branch | Repair |
|---|---|---|---|
| No transcript at all | `unexpected_error` | untouched | relaunch the Task |
| A transcript, no envelope, nothing merged | `no_envelope` | untouched | relaunch the Task |
| An envelope claiming complete, or a dirty tree | `unclean_exit` | untouched | finish or discard the branch, then relaunch |
| **A completed merge and push, and a Closeout that died** | **`partial_landing`** | **changed and pushed** | **finish the unit by hand on the merged code, then comment the sha on the card, then `verify`** |

The first three leave the default branch exactly as the run found it, so the cost of a wrong
diagnosis is wasted time. The fourth has already changed the branch every later Task branches
from, so a wrong diagnosis compounds into the rest of the run.

**Know why the merge survives a death that kills everything after it.** The landing sequence writes
the reference to the record before it launches anything else: `run.py:811` upserts `landing_ref`
from the merge sha, `run.py:812` runs a code scope verify, and only then does `run.py:825` launch
the Closeout as a fresh `claude -p`. On an exhausted account that Closeout dies in seconds. The
card never moves and no comment naming the sha is ever written, so the full verdict fails exactly
the two tracker checks and passes every code check. `_finish` reads that combination at
`verify.py:326` and assigns `partial_landing` at `verify.py:328`: "the code is on the remote and the card is not". The
class is not a guess about a dead process. It is a measurement of a split that really happened.

**Assume the review did not run, because the record cannot tell you it did.** `review_skipped` is a
finding rather than a halt, and it is raised from a transcript scan. A Task that merged and then
lost its window can be missing the review for a different reason: on the 2026-09-08 run the process
died partway through its own review step, after the Runner had merged. The record says landed. The
default branch carries a unit whose review, mutation table and record never happened, and nothing
in the state file distinguishes that from a unit that went through all three. **Read the diff of
the landing commit by hand before you build on it.**

**The repair is not the hand landing repair, and reaching for that one will not work.**
`hand-landing-repair-lands-only-when-a-commit-names-the-issue-number-as-a-word.md` covers a record
with no `landing_ref`, where `verify` derives the landing from a default branch commit naming the
task as a word. This shape is the mirror image. The record already carries a `landing_ref`, so
`verify.py:240` never attempts that derivation, because its guard opens `if not landing_ref`; control reaches the `else` at `verify.py:253`, and
the check becomes `adapter.closing_reference(task_id, landing_ref)`, which on GitHub scans the
card's **comments** for a body naming that sha (`adapters/github.py:147`). No commit message can
satisfy it. Adding an empty commit saying `Closes #N`, the fix the sibling doc prescribes, changes
nothing here.

So the order matters:

1. Finish the unit by hand on the merged code, on the default branch, to whatever bar the project
   requires. The Runner will not do it and `verify` will not ask.
2. Close the card if it is still open.
3. Comment on the card with the landing sha, as a comment, not a commit message.
4. `python3 <runner> verify <manifest> <task-id>` to confirm the landing. The verb reports
   and writes nothing. A halted record is promoted at the next run's startup, by
   `startup_reverify`, which re-runs the full verdict on every halted record and promotes
   the ones that now pass. On a finished manifest that next run never comes, so a repaired
   Task keeps a halted record. That is cosmetic, not a second repair to chase.

Step 3 is the one people skip, and skipping it leaves a Task that is genuinely finished reading
`partial_landing` forever.

**A limit naming one model is a different event from a limit naming the account, and only one of
them the Manifest can answer.** Both appear as the same halt classes and both read in the summary
as `exited without a return envelope`, so the class does not separate them. The last assistant
message does:

```
You've hit your session limit · resets 12:10pm (America/New_York)
You've reached your Fable limit. Switch to another model, or manage usage credits at claude.ai/...
```

Both of those are in one Task's log from the 2026-09-09 death. The second names its own remedy, and
that remedy is a Manifest field. The sibling doc's conclusion that "for the exhaustion question the
Manifest offers nothing" holds for an account window and does not hold here: a per model limit is
answered by changing that Task's `model`, and the change costs one line.

**Do not relaunch on the same model to find out.** A Manifest whose Tasks all name the exhausted
model halts within seconds of launch, which looks like a new failure and is the old one.

## Why This Matters

The three shapes in the sibling table are all failures to produce work. This one is a success that
lost its receipt, and the two need opposite instincts.

For the first three the safe move is to relaunch and let the Task redo everything, because nothing
it did survived. Doing that here would branch a fresh Task from a default branch that already
carries the merge, against a card that may already be closed, to redo work that is already shipped.

The reverse mistake is worse and quieter. The record reads `partial_landing`, the summary says
`landed at <sha>`, and both are true, so an operator skimming for halts sees a Task that mostly
worked. The card is open, so the tracker says the work is not done, and the code is merged and
pushed, so the repository says it is. Every Task after it in the Manifest branches from that
default branch. On the 2026-09-08 run this meant `main` carried a unit whose review had not run
while its issue was still open, and the next Task in the queue would have built on it without
anybody having looked.

**The telemetry that would have prevented the launch is on disk, and it also carries the dimension
that separates the two limits.** The sibling doc established that `rate_limit_event` is streamed
and skipped. Decoding one burned Task's log adds the shape: 69 `allowed_warning` events with
`utilization` climbing from 0.78 through 0.83, then two `rejected` events. The last one reads:

```json
{"status": "rejected", "rateLimitType": "seven_day_overage_included",
 "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
 "unifiedWindows": {"five_hour":  {"utilization": 0.8},
                    "seven_day":  {"utilization": 0.58},
                    "seven_day_overage_included": {"utilization": 1}}}
```

Three windows, and the two an operator would think to check were fine. The seven day window was at
0.58 and the five hour at 0.8. What bound was the overage included window at 1.0, with overage
disabled at the organisation level. A pre launch check of "how much of my week is left" would have
read 58 percent and launched. The event that says otherwise went past the Follower as a skipped
stream type, tens of times, with the number climbing.

The deeper point is that the Runner already receives a graded warning and treats it as noise, and
that the split it cannot see is the same split that decides the repair. A merge that lands and a
Closeout that dies are one process boundary apart. Everything the operator needs to tell those
apart is written down. None of it is joined up.

## When to Apply

- Reading any run that died on a usage limit. Sort the burned Tasks by whether they merged before
  you decide anything else, because that single fact selects the repair.
- Seeing `landed at <sha>` and `did not verify as landed` on the same summary Task. That pair is
  the signature, and it means the code shipped and the card did not.
- Repairing a Task whose record carries a `landing_ref`. Comment the sha on the card. Do not reach
  for the empty commit trick, which belongs to the opposite case.
- Before building on a default branch an unattended run was cut off against. Read the landing
  commit's diff; a landed record is not evidence that a review ran.
- Relaunching after a limit. Read the last assistant message first. If it names a model, change
  that Task's `model` and relaunch. If it names the account session, wait for the reset.
- Not applicable to metered API key runs, where exhaustion presents as a billing error.
- Not applicable to a Task that halted before its merge. Those are the sibling doc's three rows and
  its guidance is unchanged.

## Examples

**The 2026-09-08 death, Task 72.** The Task process reached the Runner's merge; `main` moved to
`0285aae` and was pushed. The account window then closed. The Closeout launched and died, so the
card never moved and no comment named the sha. The full verdict failed `card_terminal` and
`closing_reference`, passed every code check, and `_finish` assigned `partial_landing`. The summary
recorded both halves at once:

```
72  landed at 0285aaedcfc5520a91263ba5e59c009a1dfb27f0
    72 did not verify as landed: card_terminal, closing_reference
    finding: exited without a return envelope; last message: You've hit your session limit
```

The repair, in the order that works: the unit's review, mutation table and record were run by hand
on the merged code, the card was closed, then a comment naming `0285aae` was posted on the card, and
only then did `verify` return a landed verdict, with `closing_reference pass` and the comment id
as its evidence. The attempt before that comment failed with every git check green,
because a `landing_ref` on the record routes past the commit message derivation entirely.

**The 2026-09-09 death, Task 75, a different shape from the same cause family.** No merge had
happened. The tree was left dirty on `relay/75`, so the class was `unclean_exit`, the run stopped
there whatever `continue_past_task_halt` said, and the branch held five commits and a complete
mutation table. The repair was the sibling doc's: commit what the tree held, run the gate, merge,
`verify`. Nothing about `main` had to be audited, because `main` had not moved.

**The relaunch, and the one line that mattered.** The dead Task's last message named a model rather
than the account, and the remaining Tasks all named that same model. Changing one Task's `model`
was the whole fix:

```toml
[[tasks]]
id = "71"
model = "opus"
effort = "high"
# Moved from fable to opus 2026-09-10: task 75 exhausted the fable limit mid record and the
# quota had not returned, so a relaunch on fable would have halted in seconds.
```

The run then completed all six Tasks with no further halts. Relaunching unchanged would have burned
the next Task within seconds of launch and looked like a fresh failure.

## Related

- `docs/solutions/workflow-issues/quota-exhaustion-reads-as-no-envelope-and-the-rate-limit-telemetry-is-already-discarded.md`
  is the parent. It owns classification, the cost table, the telemetry argument, and sizing. This
  doc adds the decoded shape of a `rejected` event and the observation that the window an operator
  would check is not the window that binds.
- `docs/solutions/workflow-issues/on-halt-continue-past-task-halt-is-not-the-quota-switch-and-the-path-a-quota-death-takes-decides-whether-the-manifest-votes.md`
  carries the three row table this doc extends by one row. Its conclusion that the Manifest offers
  nothing against exhaustion holds for an account window; a per model limit is the exception, and
  the field is the Task's `model` rather than anything under `on_halt`.
- `docs/solutions/workflow-issues/hand-landing-repair-lands-only-when-a-commit-names-the-issue-number-as-a-word.md`
  is the opposite repair, for a record with no `landing_ref`. Read the two together and note which
  branch of `verify.py:240` you are on before choosing, because each doc's fix is inert in the
  other's case.
- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  is the standing rule this doc pays. Every claim above came from a live run's state file and logs
  rather than from reading the source alone.

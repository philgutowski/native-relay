---
title: continue_past_task_halt is not the quota switch, and which of three paths a quota death takes decides whether the Manifest gets a vote at all
date: 2026-09-10
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: high
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [manifest, run-loop, classify, closeout, gitwrite, audit]
applies_when:
  - "answering on_halt.continue_past_task_halt in the /relay authoring interview, on any Manifest"
  - "authoring a Manifest long enough that account exhaustion partway through is plausible"
  - "reaching for a Manifest field as the control that stops one dead Task process from becoming a whole run burn"
  - "reading a run that exited 0 with several consecutive blocked Tasks and card_left_in_review findings"
  - "planning recovery after a run died mid Manifest, and choosing between a plain resume and --retry-blocked"
symptoms:
  - "an operator believes continue_past_task_halt decides how far a dead account can spread, and for the most common quota death it decides nothing"
  - "several consecutive Tasks record status blocked with halt class no_envelope, and the run still writes RUN_COMPLETED and exits 0"
  - "card_left_in_review findings accumulate against Task after Task while every card stays at the in review status"
  - "a plain resume silently skips every burned Task instead of retrying it, because a blocked record early returns"
  - "--retry-blocked then halts on the first stranded branch carrying commits past its baseline"
tags:
  - manifest-authoring
  - continue-past-halt
  - blocked-route
  - halt-routing
  - quota-exhaustion
  - stranded-branch
  - resume
  - card-audit
---

# continue_past_task_halt is not the quota switch, and which of three paths a quota death takes decides whether the Manifest gets a vote at all

## Context

On 2026-09-10, while authoring a thirteen Task Manifest against a Jira board, the question came up
of what to answer for the third degraded path field, `on_halt.continue_past_task_halt`. The
authoring interview asks for it as a throughput trade (`skills/relay/SKILL.md:114` to `:120`,
`docs/manifest-authoring.md:155` to `:158`): off, the first halt of any class stops the run; on, a
halt contained to one Task pauses that Task and the later independent Tasks keep running.

Thirteen Tasks is long enough that account exhaustion partway through is a live possibility rather
than a tail risk.
`docs/solutions/workflow-issues/quota-exhaustion-reads-as-no-envelope-and-the-rate-limit-telemetry-is-already-discarded.md`
already owns what exhaustion looks like from inside the Runner, the measured cost table, and pre
launch sizing as the remedy. This doc does not repeat any of that.

What that doc does not cover is the authoring question actually in front of the operator. If a long
run is going to meet the wall, which Manifest field, if any, decides whether one dead Task process
becomes thirteen? The reading was made from source rather than from a live run, and it produced a
plausible and wrong answer: that `continue_past_task_halt = true` is the right default against
ordinary halts and what lets the run step forward into the wall.

Tracing it properly inverted the conclusion. The switch is not wired to the most common quota
death at all, its default is `false` rather than `true`, and on the one quota death shape where it
does have authority, `true` is the setting that would cause the cascade rather than the setting
that permits it.

## Guidance

**Answer `on_halt.continue_past_task_halt` on the halt question alone. It is not a quota control,
a cascade control, or a blast radius control.**

The field is read at one line in the whole package, `run.py:389`, inside `_continue_past`, and
`_continue_past` is called from one place, `run.py:291`, on the run loop's exception path. It
governs exactly one thing: whether a `_Halt` raised out of `_one_task` stops the run or is stepped
over. A Task that ends blocked never raises, so the switch never sees it.

**Know the three shapes a quota killed Task can leave, because only one of them consults the
Manifest.** All three start the same way, with a process killed mid Task, and they differ only by
what the dying process left behind.

| What the process left | Halt class | Path | Does the switch vote |
|---|---|---|---|
| No transcript file at all | `unexpected_error` | `_blocked_route` (`run.py:699`) | No, always continues |
| A transcript, no envelope, nothing merged | `no_envelope` | `_blocked_route` (`run.py:699`) | No, always continues |
| An envelope claiming complete with nothing merged, or a dirty tree | `unclean_exit` | `_Halt` to `_continue_past` | Yes |

The first row is worth reading twice, because it is the one that looks like a stop and is not.
`unexpected_error` is a member of `RUN_SCOPED_HALT_CLASSES` (`contracts.py:452`), and membership
does not stop a run. That tuple is consulted at exactly two places, `run.py:391` inside
`_continue_past` and `run.py:999` in the halt comment gate, and both sit on the raised path. A
class recorded through the blocked route never reaches either one. On the claude backend, the only
backend native mode runs, a missing transcript never raises: the guard that would raise it is
`if not capability.enforces_at_launch and digest.get("findings_unavailable")` (`run.py:677`), and
claude's capability record sets `enforces_at_launch` to `True` (`contracts.py:106`), so that
condition is always false. Control falls through to `run.py:699` like any other unroutable exit.
A passing test pins the behaviour: `tests/test_run.py:541` queues a Task that leaves no transcript,
asserts the record carries `unexpected_error`, and asserts the two Tasks behind it still land with
an exit code of OK.

So membership of the run scoped set is not what decides whether a run stops. Raising is. On claude,
no quota death classified from a transcript raises at all, which leaves the `unclean_exit` row as
the only one the Manifest can speak to.

The middle row is the cascade. `_blocked_route` (`run.py:853`) strands the branch, runs a Closeout,
writes `STATUS_BLOCKED`, and returns normally, and its own docstring says so: "the run continues. A
blocked task is a normal outcome (R23)" (`run.py:854`). A normal return lands on the run loop's
`else` branch, which advances the cursor unconditionally (`run.py:286`). Setting the switch to
`false` does not stop that. There is no stop on that path to set.

The bottom row is where the switch matters, and it matters in the opposite direction to the one
assumed. **A live run on 2026-09-08 hit exactly this: a Task process was killed by a session limit,
left a dirty tree on its branch, and the halt was classified `unclean_exit` rather than
`no_envelope`. The run did not continue. The four queued Tasks behind it were left untouched
(session history).** The default `false` is what stopped it. Had the switch been `true`, the run
would have stepped over that Task and launched the next one into the same closed window.

**So write the field explicitly, and write `false` unless you have a specific reason.** The default
is already `false` (`manifest.py:268`), but an omitted key reaches the operator only as one line in
`validate`'s applied defaults list (`cli.py:123`), and the field changes what exit 0 means
(`SKILL.md:95` to `:97`). A value nobody chose is a value nobody will remember choosing.

```toml
[on_halt]
# Off. A halt is a stop and a decision point. Every stepped over halt strands its own Task
# branch, the runner never deletes it, and the next run refuses that Task on no_task_branch
# (run.py:393, gitwrite.py:278).
continue_past_task_halt = false
```

Choose `true` only when the Tasks are genuinely independent, so a halt in one says nothing about
the rest, and you accept that every stepped over halt leaves a branch you will delete by hand
before that Task can run again. `resume_disposition` never deletes, resets, or stashes
(`gitwrite.py:437` to `gitwrite.py:439`), and `_continue_past` refuses unconditionally on the
resulting `no_task_branch` preflight refusal (`run.py:393`). Under `true`, exit 0 means the run
reached the end of the Manifest, not that every Task landed.

**For the exhaustion question the Manifest offers nothing, so the control sits outside it.** In
order:

1. Size the run before launching. That procedure belongs to the quota doc named above. Read it
   before writing a Manifest of this size.
2. Stage the Manifest. Thirteen Tasks in one file is thirteen chances at the wall, and nothing in
   the run loop counts consecutive failures. Two files of six and seven, launched separately, put
   an operator decision between them, which is the only breaker that exists today.
3. Order the Tasks so the ones you most want landed run first. The blocked route continues
   unconditionally, so position in the Task list is the only priority the Runner honours.
4. After any run, read the card audit lines and the summary's check by hand list rather than the
   exit code.

**Know which recovery you are in before you resume.** A burned Task's record reads `blocked`, and a
plain resume skips it (`run.py:532`), so the resume exits 0 having done nothing. Retrying it needs
`--retry-blocked`, and that path refuses any stranded branch carrying commits past its baseline
(`run.py:725` to `run.py:729`), so those branches are yours to keep or discard first. A Task
stepped over under `true` reads `halted` instead, and its recovery is deleting `relay/<id>` before
the next run, or the run halts at it on `no_task_branch`.

## Why This Matters

The wrong answer is easy to reach because the two paths out of `_one_task` look identical in the
summary and are opposite in the code.

Trace the quota death that cascades. The Task process is killed. `launched.timed_out` is false, so
the timeout branch does not fire. The transcript exists, so the `unexpected_error` branch does not
fire. There is no envelope, so `classify` assigns `no_envelope`. Back in `_one_task`, nothing is
routable, control reaches `run.py:699`, and `_blocked_route` runs. It strands the branch, launches
a Closeout that dies the same way and comes back unfinished, reads the card back over HTTP and
appends `card_left_in_review` when it still sits at the in review status, writes `STATUS_BLOCKED`,
and returns. No exception. The cursor advances. Task two launches into the same wall.

Now trace an ordinary Task scoped halt, a failed gate. `_merge_route` raises `_Halt`, the
`except _Halt` handler catches it, and `_continue_past` decides. Here, and only here, the Manifest
gets a vote.

Both produce a record carrying a halt class, both write evidence, and both appear in the summary as
a Task that did not land. The field name `on_halt` reads as though it covers both. It covers one.
An operator who sets `false` believing they bought a circuit breaker has bought a stop on a class
of failure that is not the one about to happen, and will read exit 0 at the end of a run where
twelve of thirteen Tasks were blamed for not writing an envelope.

The opposite mistake is quieter and compounds. Every stepped over halt leaves the Task's own branch
in the checkout, deliberately (`gitwrite.py:439`, "The task branch is left in place either way").
The next run's preflight sees that branch and refuses before launching anything
(`gitwrite.py:278`), and `_continue_past` refuses to step over that particular refusal by name
(`run.py:393`). That refusal is correct and is itself a fix, recorded in
`docs/solutions/logic-errors/continue-past-halt-checked-general-state-blind-to-the-branch-its-own-skip-left.md`;
stepping over it would produce a forever loop where the record reads `continued_past` and the run
reads `completed`. So `true` trades a mid run stop for a stop on the next run plus manual git work.
Across a cascade that is not one branch but one per burned Task, cleared one at a time.

**What did get better recently is worth crediting rather than assuming.** The run end card audit and
the Closeout card return merged at `6cf67f4`. The Closeout is now told where each card came from,
the Runner reads the card back and files `card_left_in_review` when the move did not happen, and
`_audit_cards` reads every card once at run end and names each stale one with the status to move it
to. Those reads are direct HTTP through the adapter rather than model processes
(`skills/relay/scripts/relay/adapters/jira.py:80` to `:95`), so they keep working when every Claude process in the run is
dead. That is the part of the pipeline that stays honest during a burn. What it cannot do is move a
card: the Runner never writes to a Tracker, so the audit is a checklist, not a repair.

The deeper point is about where a control can live. The switch reads a halt class, which is a
verdict on evidence the dead process left behind. Whether a quota kill leaves a dirty tree, a clean
tree, or no transcript is not a property of the account running out; it is an accident of where the
process happened to be. A control keyed on that accident cannot be aimed at the cause. That is why
the answer to exhaustion is sizing and staging before the run, and why `on_halt` should be answered
on the question it actually asks.

## When to Apply

- Answering the third degraded path question in the `/relay` authoring interview, on any Manifest.
  Decide it on Task independence and on your willingness to clear branches by hand, and write the
  value rather than omitting it.
- Authoring any Manifest long enough that account exhaustion partway through is plausible, roughly
  the shapes the quota doc names. Reach for staging and sizing there, not for `on_halt`.
- Diagnosing a run that exited 0 with a run of consecutive blocked Tasks and `card_left_in_review`
  findings. Do not look for a Manifest setting that failed to stop it. There is none, by
  construction.
- Planning recovery after a run died mid Manifest. Check whether the burned Tasks read `blocked` or
  `halted` first, because a plain resume skips the former and stops on the latter.
- Not applicable to deciding whether a run should launch at all. That is the sizing question, and it
  belongs to the quota doc.
- Not applicable to metered API key runs, where exhaustion presents as a billing error rather than a
  silent kill.

## Examples

**Before, the field answered as a quota hedge.** A thirteen Task Manifest authored on the belief
that the switch decides how far a dead account can spread, with `true` chosen so ordinary halts do
not cost the whole run:

```toml
[on_halt]
# thirteen tasks, keep going past a single bad one
continue_past_task_halt = true
```

The comment states an intent the code cannot honour. For a `no_envelope` death the loop keeps going
for nine more Tasks without the switch ever being read. For the `unclean_exit` death actually
observed on 2026-09-08, this setting is what converts a safe stop into a cascade.

**After, the field answered on its own question, with the exhaustion risk handled where it can be.**
Two staged Manifests, six Tasks and seven, each carrying:

```toml
[on_halt]
# Off. These tasks are independent, but a stepped over halt strands its own branch and the next
# run refuses on no_task_branch (run.py:393), so a halt is a stop and a decision point.
continue_past_task_halt = false
```

with the launch preceded by a sizing read per the quota doc, and stage two launched only after
stage one's summary and card audit have been read.

**The cascade traced through the code.** Thirteen Tasks, the weekly window closing during Task
four, and the process leaving a transcript with no envelope.

`classify` writes `no_envelope` plus a matching finding. `_one_task` reaches `run.py:699` and
returns `_blocked_route`. `gitwrite.blocked_path` records the branch and head and checks the
repository back out to the default branch. `closeout.return_to_for` reads the record's baseline
tracker status and returns the status the card held before the run. `_run_closeout` launches a
fresh `claude -p`, which dies in seconds, so `closeout_unfinished` is appended.
`confirm_blocked_comment` reads the card over HTTP, finds no new comment, appends
`blocked_unrecorded`. `confirm_card_returned` reads it again, sees the in review status, appends
`card_left_in_review`. The record is written `STATUS_BLOCKED` with halt class `no_envelope`, and
`_blocked_route` returns.

The run loop's `else` at `run.py:286` runs. Cursor advances. Tasks five through thirteen repeat the
sequence. `_continue_past` is not called once. At the end `_audit_cards` reads all thirteen cards
over HTTP and names each stale one, `RUN_COMPLETED` is written, and the process exits 0.

Recovery: a plain resume skips every blocked Task at `run.py:532` and exits 0 again, having done
nothing. `--retry-blocked` reaches `_clear_blocked_branch`, which deletes a stranded branch carrying
nothing past its baseline and raises `unclean_exit` on the first one that carries commits. Nine
branches, cleared by hand, one refusal at a time.

**What the switch would have changed: nothing.** Set to `true`, every step above is identical, and
so is the run where the CLI dies before creating a transcript at all. Both shapes reach
`_blocked_route` and return normally. The switch changes the outcome only when a dying process
leaves a dirty tree or an envelope claiming complete, and there `true` is what converts a stop into
a cascade rather than what permits one.

## Related

- `docs/solutions/workflow-issues/quota-exhaustion-reads-as-no-envelope-and-the-rate-limit-telemetry-is-already-discarded.md`
  is the parent. It owns the classification path, the measured cost table, the `rate_limit_event`
  telemetry argument, and pre launch sizing. This doc adds only the authoring side correction: the
  Manifest field an operator reaches for is not connected to the common failure, and on the one
  shape where it is connected it votes the other way.
- `docs/solutions/logic-errors/continue-past-halt-checked-general-state-blind-to-the-branch-its-own-skip-left.md`
  is why `_continue_past` refuses on `no_task_branch`. Read it before concluding that refusal is a
  gap. It is the fix.
- `docs/solutions/workflow-issues/task-branch-in-flight-from-an-earlier-run-fails-no-task-branch-preflight-and-validate-never-warns.md`
  is the same preflight refusal reached from the other direction, a branch left by an earlier run
  rather than by this run's own stepped over halt. Its point that `validate` exiting 0 does not mean
  no Task will be refused at launch applies here unchanged.
- `docs/solutions/workflow-issues/two-instructions-to-two-processes-written-weeks-apart-disagreed-about-moving-the-card-back-and-the-adapter-couldnt-see-it.md`
  is the card return seam from the other side. It documents why the Closeout returns a blocked or
  halted card at all; this doc documents what happens to that return when the Closeout is itself a
  process the account can no longer run.
- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  is the standing rule this doc paid. Every claim here came from reading source, and the one claim
  that mattered most was wrong until a live run recorded in session history contradicted it.

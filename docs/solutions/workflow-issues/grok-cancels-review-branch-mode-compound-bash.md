---
title: Grok auto mode cancels the /review skill's compound git setup, so a complete Task can still die with no envelope
date: 2026-09-11
category: workflow-issues
module: runner
problem_type: workflow_issue
component: grok_backend
severity: high
root_cause: missing_workflow_step
resolution_type: config_change
related_components:
  - contracts
  - backend-pins
  - brief
  - review-step
  - task-process
applies_when:
  - a grok native Task is blocked with cancelled_tool_call and no_envelope after it already committed on its branch
  - the last message is about reviewing a branch or setting up the review harness
  - the cancelled Bash is a multi-line if/elif or a `$(git merge-base ...)` from bundled `/review`, not a heredoc git commit
tags:
  - grok
  - review
  - cancelled-tool-call
  - no-envelope
  - first-live-run
  - unattended-run
  - backend-pins
---

# Grok auto mode cancels the /review skill's compound git setup, so a complete Task can still die with no envelope

## Context

The grok native review lift (`docs/plans/2026-09-11-feat-grok-native-review-step-plan.md`) names
`/review` as the built in skill a headless Task actually reaches. The 1.0.13 pin already said this
CLI cancels a `run_terminal_command` whose argument uses command substitution or a heredoc, and the
brief told the Task to use plain `git commit -m` forms only.

Live T-72 on 2026-09-11 against the proof target showed the cancel is wider than that commit form.
The Task implemented `double()`, committed it on `relay/T-72` as `42fab3e`, then ran `/review` in
branch mode. The bundled skill's setup is a single compound script: a multi-line if/elif that
assigns `BASE` from `origin/main` or `origin/master`, then `MERGE_BASE=$(git merge-base ...)`, then
redirected `git diff` into a scratch file. Grok cancelled that call
(`cancellationCategory: PermissionCancelled`, body `User cancelled the execution for tool
\`run_terminal_command\``), the turn ended, and classify recorded `no_envelope` plus
`cancelled_tool_call`. Closeout completed. Nothing landed. Session
`420a4122-7b75-4e00-8127-7aa17a18eeec`, grok 1.0.25, 84 seconds wall.

A Phase 0 probe the same day had run `/review` to completion on a scratch repo under `--effort low`.
The stub cannot produce this. The live multi-turn Task with the operator catalogue loaded is the
observation that counts.

The existing heredoc-commit constraint did not name this script, so the process had no reason not to
paste it.

## Guidance

Treat a grok `cancelled_tool_call` during review as a harness failure, not as evidence the Task
never ran. Read `git log main..relay/<id>` before rewriting the helper.

The grok `commit_message_constraint` has to name every cancelled shape the brief is expected to
prevent, not only `git commit`. `/review` is a skill the process is told to run, and that skill
shows a compound git script. If the constraint does not name that script, the skill wins.

Do not:
- Diagnose a blocked grok Task with a stranded feature commit as "the helper never landed."
- Assume only the heredoc commit form is cancelled on this backend.
- Teach `classify.review_ran` a Skill event grok does not emit. Skip stays undetectable.

Do:
- Keep the constraint as the only enforcement layer. Broaden it when a live run shows a new
  cancelled shape.
- Tell the Task to run each `git rev-parse` and `git merge-base` as its own one-line command when
  `/review` shows the compound setup.
- Expect no envelope after a cancelled call. That is session-fatal on this CLI.

## Why This Matters

The tracker line looks empty: unchecked box, no landing sha, no envelope. The work is on the
stranded branch. A later session that trusts the blocked outcome will duplicate finished work,
while the real defect is that `/review` branch mode is not a safe unattended script on grok auto
mode until the process splits that git setup.

## When to Apply

- Outcome `blocked` on a grok Task, finding `cancelled_tool_call`, last message in a review or
  harness-setup sentence.
- Timing long enough that a commit could already exist (tens of seconds, not a two second launch
  miss).
- A new grok pin or brief change that names `/review`.

## Related

- `docs/solutions/workflow-issues/grok-accepts-dontask-then-cancels-every-tool-call.md` (the
  dontAsk cancel, every tool, no work)
- Issue #57 (the 1.0.13 heredoc-commit cancel)
- Live T-72, branch `relay/T-72`, commit `42fab3e`, session
  `420a4122-7b75-4e00-8127-7aa17a18eeec`

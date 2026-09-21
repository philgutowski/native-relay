---
title: A blocked row from a Jira run is not a rebuild list, and the Atlassian failures sat inside complete envelopes rather than suppressing them
date: 2026-09-21
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: high
root_cause: missing_validation
resolution_type: documentation_update
related_components: [classify, closeout, verify, feeder, summary, adapters]
applies_when:
  - "a run's status or summary reports Tasks blocked with halt_class unexpected_error and the digest reads transcript_present false"
  - "a Jira run's logs carry CONNECT_TIMEOUT, an MCP server still connecting at session start, or a security policy refusal from Atlassian"
  - "deciding which blocked or excluded Tasks need a relaunch after an unattended run on a Jira board"
  - "reading a state directory written by a Runner older than the stdout log fallback in classify"
symptoms:
  - "4 blocked on the status screen for a run whose default branch already carries all four merges"
  - "halt_evidence error_type unreadable evidence, naming a transcript path that could not be read, with last_message (no final message)"
  - "a closeout_unfinished finding on a Closeout process whose own stdout log ends on the terminal line"
  - "an envelope blocker saying the closing tracker comment or the In Progress transition was not made because the Atlassian MCP timed out"
tags:
  - atlassian-mcp
  - jira
  - unexpected-error
  - transcript-fallback
  - hand-repair
  - blocked
  - operator-diagnosis
  - stored-verdict
---

# A blocked row from a Jira run is not a rebuild list, and the Atlassian failures sat inside complete envelopes rather than suppressing them

## Context

The IW board run of 2026-09-20 to 2026-09-21 (Manifest `iw-board.toml`, 50 Tasks, target
`support-workbench`) ended `completed` with 45 landed, 4 blocked and 1 excluded. Every one of the
five non landed Tasks had its code on `main` by the time anyone asked `relay status`:

| Task | Record | Merge commit on support-workbench main |
|---|---|---|
| IW-182 | blocked, `unexpected_error` | `82326bc` |
| IW-322 | blocked, `unexpected_error` | `e0e3be0` |
| IW-330 | blocked, `unexpected_error` | `e0140e2` |
| IW-277 | blocked, `no_envelope` | `b134826` |
| IW-347 | excluded after two halts, last `unclean_exit` | `dc04b8a` |

Each merge was confirmed with `git merge-base --is-ancestor <commit> main` in `support-workbench`.
The first three merges share one timestamp, 22 seconds after IW-182's record ended, so they are a
repair of stranded branches rather than a Runner landing. IW-277's merge came an hour after its
halt.

The first diagnosis, made while answering `relay status`, was that the Atlassian tracker writes
failed, that the Task process emits its envelope only after those writes, and so the failure
suppressed the envelope and left the Runner with nothing to route. That story fit the symptoms and
the logs did carry Atlassian errors. It was wrong for every Task it was applied to, and this doc
exists mostly to stop the next reader from repeating it.

## Guidance

**Read the stdout log before believing the halt class.** For each of IW-182, IW-322 and IW-330 the
Task process's own stdout log, `logs/<id>.stdout.log` in the state directory, holds a fenced
`relay-envelope` block. Replaying the classifier at the current tree against those logs gives a
complete, routable envelope for all three:

```python
lr = types.SimpleNamespace(exit_code=0, timed_out=False,
                           log_path=f"{state_dir}/logs/{task}.stdout.log")
classify.classify("/nonexistent/<session>.jsonl", lr)
# IW-182: halt_class None, routable True, status complete
# IW-322: halt_class None, routable True, status complete
# IW-330: halt_class None, routable True, status complete
```

The real cause was the transcript path. The CLI wrote those transcripts under a project slug that
neither the Runner's prediction nor its glob matched, so the transcript never opened, and
`classify` correctly refused to call an absent source empty: `not result["transcript_present"]`
routes to `HALT_UNEXPECTED_ERROR` with findings unavailable
(`skills/relay/scripts/relay/classify.py:514` to `:520`). The fix is the commit titled
"fix(classify): the run's own stdout log stands in for a lost transcript" (`58bde99`, merged
locally to `main` at `b39fe37` on 2026-09-20). It lets the Runner's own stdout log stand in when
the transcript does not open (`skills/relay/scripts/relay/backends/claude.py:19` to `:49`). Those three Tasks ran
between 16:46 and 17:24 UTC and the fix merged at 21:47 UTC, so their records were written by the
old code.

**A stored record keeps the verdict it was written with.** The fix changes how `classify` reads,
not what `state.json` already says. As of this writing the three records still read `blocked` and
`unexpected_error`, so the status screen after the fix still shows the pre fix answer. The
correction lives in the replay, not the screen.

**Atlassian failures appear as blockers inside a complete envelope, never as a missing one.** The
envelopes recovered from the logs say so in their own words. IW-182: "Atlassian MCP server failed
to connect (CONNECT_TIMEOUT), so the In Progress transition on the card and the head commit
comment on the card were not made." IW-330: the Atlassian MCP returned "You can't access this site
because a security policy restricts access to it." IW-347's digest, which the Runner did read,
carries a complete envelope whose single blocker is the closing comment that timed out three
times. In each case the Task process finished its work, wrote the envelope with the tracker
failure listed as a blocker, and exited 0. The Closeout process logs for all four show the
Atlassian MCP still connecting at session start, and IW-330 and IW-347 show `CONNECT_TIMEOUT`
there too.

**The closeout findings on those three were the same transcript miss.** Each Closeout process's
stdout log ends on the terminal line (`Documentation skipped`), yet each record carries
`closeout_unfinished` with `(no final message)`. `closeout.py` classifies its own process through
the same `classify` call (`skills/relay/scripts/relay/closeout.py:285` to `:299`), so an unread
transcript there also reads as no ending. The fallback covers that call as well, since
`launch_result` carries the log path.

**The per card check is git, then the log, then the board.**

1. Find the Task's merge on the default branch (`git log --merges --grep <id>` or the branch head)
   and confirm it with `git merge-base --is-ancestor <commit> main`.
2. If it merged, read the envelope out of `logs/<id>.stdout.log`, or replay `classify` as above,
   for any blocker still owed to the tracker.
3. Only then read the card, and settle what it owes by hand. The board is not the authority in
   either direction: IW-330's card was left In Progress, and IW-347's card read In Progress after
   its code merged.

A blocked Task is settled and a later run does not attempt it again unless the operator asks at
launch (CONCEPTS.md, Blocked). Asking for that on these four would have relaunched finished work
against a `main` that already holds it.

## Why This Matters

The wrong diagnosis points at the wrong repair. "Atlassian suppressed the envelope" implies the
envelope contract is fragile and the tracker write should move after the envelope, or that the
Task needs a rerun once Atlassian is healthy. Neither is true. The envelope contract held on every
Task the Atlassian errors touched, and the halt came from the Runner losing its own evidence. A fix built on the
wrong story would reorder a brief that works and leave the real reader bug unnamed.

It also inflates the rebuild count. An operator reading "4 blocked, 1 excluded" plans five
rebuilds; the true count here was zero rebuilds and a handful of card writes.

The two failure shapes are easy to conflate because they arrive together. A Jira run on a day when
Atlassian is flaky will show Atlassian errors in nearly every log, including the Tasks that halted
for an unrelated reason, so "the logs mention Atlassian" is not evidence that Atlassian caused a
halt.

## When to Apply

- Any `unexpected_error` with `transcript_present: false`. Before anything else, check whether the
  stdout log holds assistant records and an envelope.
- Any run whose state directory predates `58bde99`. Its `unexpected_error` records may be complete
  Tasks the old reader could not see.
- Any Jira run where the logs show the Atlassian MCP still connecting, `CONNECT_TIMEOUT`, or a
  security policy refusal. Expect envelope blockers, `closeout_unfinished`, `blocked_unrecorded`
  and `card_left_in_review` findings, and cards stale in either direction; do not expect those
  errors to explain a halt class.
- Before relaunching blocked or feeder excluded Tasks, run the git check above for each one.

## Examples

**IW-182, as the status screen showed it.** `status: blocked`, `halt_class: unexpected_error`,
`halt_evidence.error: the transcript at .../18f00abe....jsonl could not be read`, digest
`transcript_present: false`, `line_count: 0`, `exit_code: 0`, finding `closeout_unfinished`.

**IW-182, as the evidence shows it.** The stdout log holds a complete envelope whose blocker is the
Atlassian `CONNECT_TIMEOUT` on the In Progress move and the head commit comment, naming head
`d9903ce`. The merge is on `main` at `82326bc`. What the card still owes is the closing comment and
the move to its terminal status.

**IW-277, the genuine one.** `no_envelope` with `transcript_present: true` and the last message
"The model's tool call could not be parsed (retry also failed)." The transcript opened and the
envelope really is absent, so the class is honest. Its branch still merged by hand at `b134826`,
which is the one card of the five whose build a person should check, because no envelope ever
reported what it did.

**IW-347, the partial landing.** The digest is complete and routable, the landing is on `main` at
`dc04b8a`, and verify failed only `card_terminal` and `closing_reference` because the closing
comment never posted. The Feeder then excluded it after two halts, the last an `unclean_exit` on
the `no_task_branch` preflight. The repair was moving the card to Done by hand, not a rebuild.

## Related

- `docs/solutions/workflow-issues/quota-death-has-a-fourth-shape-partial-landing-and-it-is-the-only-one-that-leaves-code-on-the-default-branch.md`:
  the other shape where a record says less landed than the default branch holds. IW-347 is its
  tracker side twin, a merged unit whose card never reached a terminal status.
- `docs/solutions/logic-errors/verify-checked-only-one-direction-of-the-landing-tracker-link.md`:
  why verify can accept a hand landing like these once the card and the closing reference agree.
- `docs/solutions/workflow-issues/a-refused-claude-dir-edit-on-a-complete-envelope-is-a-finding-not-a-halt-so-the-task-lands-with-its-skill-file-stale.md`:
  the same lesson from another side, a problem outside the build that must not be read as a worse
  outcome than it is.
- `docs/solutions/workflow-issues/plan-premise-about-evidence-shape-was-wrong-existing-incident-transcripts-caught-it.md`:
  an earlier case where reading the real evidence overturned a plausible story about it.
- `docs/solutions/workflow-issues/remote-advanced-hand-repair-needs-a-third-step-so-resume-does-not-relaunch-the-finished-task.md`:
  the hand repair steps that keep a resume from relaunching a Task that is already finished.

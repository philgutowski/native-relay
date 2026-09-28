---
title: A usage limit death recorded blocked needs the 429, not terminal_reason, and the stdout log holds every attempt
date: 2026-09-26
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: medium
root_cause: missing_validation
resolution_type: code_fix
related_components: [feeder, summary, cli]
symptoms:
  - "a task routed to a model whose account limit was spent printed only the CLI's limit message, exited in seconds, and was recorded blocked with class no_envelope, so the per model fallback, which reads only halted tasks, never moved it"
  - "the feeder logged that the task was blocked and would not be retried without --retry-blocked, and the only way back was a bare run --retry-blocked that also retried every older blocked record in the manifest"
tags: [feeder, usage-limit, model-fallback, retry-blocked, stream-json, api-error, code-review-catch]
---

# A usage limit death recorded blocked needs the 429, not terminal_reason, and the stdout log holds every attempt

## Problem

Issue #39. The per model fallback from #33 read a usage limit only from halted tasks. A process
that dies on its model's limit before writing anything prints the CLI's limit text as its only
turn and exits. That is no envelope, and a process with no envelope and no commits takes the
blocked route, not a halt. So the most common shape of a model limit death was the one shape the
fallback never saw.

The fix reads a blocked `no_envelope` record that died inside `quick_death_seconds` as a limit
death, moves it like a halt, and relaunches it with `--retry-blocked ID` for that id alone.
Three things about the evidence were not where they looked.

## What the evidence actually says

**`terminal_reason: api_error` is not a usage limit.** The issue suggested matching either
`api_error_status: 429` or the `terminal_reason: api_error` result line. The logs on this machine
disagree with the second. A limit death (`IW-307` in a Jira run's state directory) ends:

```json
{"type": "result", "is_error": true, "num_turns": 1, "terminal_reason": "api_error",
 "api_error_status": 429, "result": "You've reached your Fable limit. ..."}
```

preceded by an assistant line carrying `"error": "rate_limit"` and
`"api_error": "model_requires_usage_credits"`. But `T-35`, a task launched on a model the account
could not reach, ends with the same `terminal_reason: api_error` and `api_error_status: 404`
(`"error": "model_not_found"`). Matching on the reason would move a task off a model that does
not exist and mark that model exhausted. The status is the signal. A `result` line with any other
outcome rules a limit out; no `result` line leaves the time rule to decide, as it does for halts.

**The stdout log holds every attempt.** `launch.py` opens `logs/<id>.stdout.log` in append mode,
and it is truncated only on a backend reassignment. A retried task's log therefore ends with
its newest attempt but also holds the older ones. A retry that died without printing a `result`
line of its own would have the previous attempt's successful `result` read as its own, and the
limit ruled out. `limits.result_event` (in `feeder.py` until the usage limit plan's U1 moved it)
walks back from the end and stops at the last attempt's `{"type": "system", "subtype": "init"}`
line.

**A retry refused before launch keeps the old attempt's timings.** Pre flight and the R48
stranded branch refusal both raise before the launch upsert, so the record goes halted with the
blocked attempt's `wall_seconds` and `model` still on it. Read naively, four seconds on fable is
another quick death, moved and re-marked every cycle and never counted toward `max_halts`. The
feeder queues each retry with the blocked record's `started_at` (now in the summary's task entry,
restamped at every launch) and reads a record that left blocked with the same stamp as one that
never launched, so its wall time is treated as absent. The same stamp tells a retry the run never
reached, because it halted first, from one that ran.

## Guidance

- Read limit evidence from the `result` event's `api_error_status`, never from
  `terminal_reason` alone.
- Anything that reads a task's stdout log after the fact must bound the read to the last
  attempt. The log is per task, not per attempt.
- A record's timing fields describe the last launch, not the last run. Before reading
  `wall_seconds` as this run's measurement, check `started_at` moved.
- `run --retry-blocked ID` retries one task. The bare flag still retries every blocked record,
  and the feeder never passes it bare.

## Not done

The runner's classify step could attach a usage limit finding to the record itself, so the
summary and `run` would show it too. That changes the classify digest, a contract between
processes that needs a live run, so it was left out of #39.

## Follow up: issue #45, the whole cycle rule read halts only

#39 taught the per model rule to see a blocked limit death, but `settle` still handed only the
halted list to `looks_like_usage_limit`. A cycle whose deaths were all blocked, with the fallback
already marked or two models that fall back to each other both dying, took no move and no wait:
each task fell through to an ordinary blocked report, the model stayed unmarked, and the next
cycle appended fresh cards on it. The fix passes `halted + limited` to the whole cycle rule and
waits when any of them has no move, queuing each blocked one for a `--retry-blocked` relaunch.

The lesson is the shape of the gap. A quick death has two records, halted and blocked
`no_envelope`, and the feeder has two rules that read quick deaths. Any rule that reads one
shape alone reopens the quota burn of #12 through the other. When a new rule reads quick
deaths, give it `dead`, never `halted`.

Two things stay as they were. A blocked limit death with no free fallback in a cycle where
something landed is reported blocked, and one on a model with no `models.fallback` entry is not
read as a limit at all; both are follow up work. A queued retry holds its room in the batch,
but the dead model is never marked on the wait path, so fresh cards still fill any room the
retries leave.

## Follow up: the usage limit plan's U1, the shared reader

`limits.read_death` now answers confirmed, refuted, or unconfirmed for a death, and returns the
CLI's reset time beside a confirmed reading. Three things about that reset were not where they
looked.

- The reset comes from a `rate_limit_event` line whose `rate_limit_info.status` is `rejected`.
  Key on that field alone. The fixtures from real runs carry `"overageStatus": "rejected"` beside
  `"status": "allowed"` on an account with overage off, on a turn that ran normally, so a match
  on the word rejected anywhere in the event reads every healthy run as a limit.
- A `rate_limit_event` with status `allowed_warning` can sit above the last attempt's `init`
  line. It belongs to no attempt, and the same `init` bound that guards the `result` line
  guards it.
- Two limits can be rejected in one attempt, a session's and a week's. The reader takes the
  later reset, since the model is back only when both have lifted.

The `init` bound has one hole the reader cannot close from the log. An attempt that prints
nothing, a CLI that fails to start for one, leaves no `init` line of its own, so the walk runs
into the attempt before it and reads that attempt's `result` as this one's.

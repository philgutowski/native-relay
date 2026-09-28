---
title: Consolidating item lag checks into a second pass reordered pending_checks even on the unchanged, no adapter path
date: 2026-09-27
category: logic-errors
module: summary
problem_type: logic_error
component: summary
severity: low
root_cause: refactor_side_effect
resolution_type: code_fix
related_components: [audit, closeout, run]
tags: [summary, pending_checks, ordering, dedupe, code-review, print-time-check]
---

# Consolidating item lag checks into a second pass reordered pending_checks even on the unchanged, no adapter path

## Problem

Issue #80. `_pending_checks` in `summary.py` prints one line per class of thing an operator has
to do by hand, built while walking each task's own record and, separately, the run end audit's
findings. Fixing #80's two gaps (prefer the audit's later `board_item_not_terminal` reading over
the record's, and let `summary` retire it at print time with one more live read) needed to look
at a task's record finding and the audit's finding for the same task together, so the first draft
pulled both into two dicts (`item_lag_from_record`, `item_lag_from_audit`), resolved them after
both loops had run, and appended the result to `checks` last, after every other check.

That silently changed the order of every other pending check relative to a lagging item's own
check, on every run, including with no adapter passed in at all (the exact "old behaviour" the
docstring claimed was unchanged). A run landing task T-1 with a lagging item and separately
leaving task T-2 blocked with a stranded branch used to print `T-1`'s item check before `T-2`'s
stranded branch check, matching manifest order; the first draft printed `T-2`'s first, since T-1's
check now waited for the second pass.

## Why nothing caught it locally

Every test written alongside the change filtered `pending_checks` by `kind` or `task` before
asserting, the way most of this module's existing tests already do, so a check reaching the list
in the wrong position still satisfied every assertion. Nothing in the suite pins the relative
order of two different kinds of check for two different tasks; `lines()` renders the list
in whatever order `pending_checks` holds it, so a real operator would have seen T-2's line above
T-1's with no indication anything had moved.

`/code-review`'s line-by-line diff scan agent found it by generating the two dicts' `main` and
`relay/80` outputs from identical hand-built inputs and diffing the resulting key order, not by
reading the code for a bug shape.

## Solution

Resolve the audit's line against the record's line inline, at the record finding's own place in
the per task loop, rather than batching both into a second pass:

- Index the run end audit's own `card_item_not_terminal` findings by task id once, before the per
  task loop starts, since `card_audit` is already fully available at that point.
- When the per task loop meets a task's own `board_item_not_terminal` finding, look up that
  index immediately: an audit entry for the same task wins outright (issue #80's own fix), and
  the print time live confirmation (`_item_confirmed_terminal_now`) runs right there too, in the
  same position the record's line always printed at.
- The card audit's own loop, unchanged in position, only still needs to print an item lag finding
  that had no matching record finding at all (a Closeout that landed cleanly, whose item lagged
  again later, so nothing before this ever wrote a record finding to check the audit's finding
  against) and only for tasks the per task loop did not already resolve.

## Review findings deliberately not fixed

- Two mechanisms now confirm a board item's terminal status independently:
  `run._retire_confirmed_item_findings`, which persists the retirement under the Runner's Lease
  at run end, and `summary._item_confirmed_terminal_now`, which never persists anything and
  exists only so `summary` can read cleaner than the state file between runs. A future change to
  what counts as confirmed has to be applied in both places; generalizing them into one shared
  entry point, parameterized by whether the caller may persist, is a plan of its own.
- `cmd_summary`'s new adapter is scoped to a GitHub-tracked manifest, since Jira and Markdown
  adapters have no `_item_confirmed_terminal` method for the print time check to call; a manifest
  whose tracker later grows that capability under a different adapter name needs this gate
  revisited.

## Prevention

`summary` reads state only and never takes the Runner's Lease, the same reason `audit`'s own
verb prints without writing (`audit.py`'s own docstring: "a reader beside a live run would race
the Runner for the state file"). A print time confirmation that clears a check for the operator
therefore can never write the record it confirmed away; the JSON `pending_checks` and
`store.records()`/`store.audit()` are allowed to disagree on this one class of finding by design,
and only another run's own end of run audit retires the record for good. Document that
divergence next to the function that produces it, since R46's own promise ("the JSON is the
summary") reads, at a glance, like it should not be able to happen.

When a fix needs two related findings resolved together, resolve them at the earlier finding's
own place in whatever loop already visits it, rather than deferring both into a second pass over
newly built collections; a second pass is where relative order among the untouched checks quietly
stops being what it was, and a test asserting only kind and task will not notice.

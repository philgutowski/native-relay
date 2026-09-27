---
title: A closed GitHub issue is terminal on its own, so an instruction to move the item had no check, and the test board held only one truth
date: 2026-09-27
category: logic-errors
module: adapters
problem_type: logic_error
component: adapters
severity: medium
root_cause: missing_validation
resolution_type: code_fix
related_components: [audit, closeout, run, summary]
tags: [github-adapter, project-board, two-truths, closeout-instructions, prose-contract, test-double, audit, finding]
---

# A closed GitHub issue is terminal on its own, so an instruction to move the item had no check, and the test board held only one truth

## Problem

Issue #43. On 2026-09-26, during the round 2 self run, issue #39 landed at `2c68d07`. The issue
was closed, but its item on project 6 still read `In review`, and the run end card audit said
every card agreed with its record. #28 had already added "move the item to `status_field`" to
the landed Closeout's instruction. That change edited the instruction and added no check, so a
Closeout that closed the issue and then skipped or failed the item edit still produced a clean
landing.

## Why nothing noticed

GitHub keeps two truths about one card: the issue's state and its project item's status. Every
reader looked at the first one only.

- `GitHubAdapter.status()` returns terminal as soon as the issue reads `CLOSED` and never reads
  the board after that. The comment in `closeout_instructions` says so ("a CLOSED issue is
  terminal on its own").
- `audit.build` reads the status `status()` returned. For a closed issue that is `CLOSED`, so
  the stale in review check can never fire for a closed issue whose item is still in review.
- `capture_closeout_delta` (the triple path) accepts a closed issue on its own.
- `classify` flags a *refused* `gh project item-edit`. It does not flag one that ran and failed,
  or one that was never attempted.

The test double had the same blind spot. `GitHubBoard` in `tests/test_run.py` modelled a GitHub
card as a single `<id>.status` file and treated it as terminal when it equalled `status_field`.
Under that double, "issue closed, item in review" could not be expressed at all, which is why
#28 shipped with a green suite.

## Solution

The finding and the read, with no new halt class and no new public adapter method:

- `GitHubAdapter._board_lag(task_id)` reads the item and returns `{card_status, terminal_status}`
  when the item is on the declared project and does not read `status_field`. An issue the
  project does not carry returns nothing. So does a manifest with no `status_field`.
- `adapters.board_lag(adapter, task_id)` calls that method when the adapter has it and returns
  `(None, None)` when it does not. Jira and markdown have one status per card, so verify has
  already read it. A raise becomes a reason.
- `closeout.confirm_board_terminal` runs after the final verify on the serial and triple landed
  routes and attaches `board_item_not_terminal`. The summary lists it under check by hand.
- `audit.build` makes the same read for a landed record on a terminal card and reports
  `card_item_not_terminal`. A failed read there is `card_unreadable`.
- `GitHubBoard` gained an `<id>.issue` file, so the issue's state and the item's status are now
  two separate facts in tests.

Code review then found two ways the new read could itself go silently clean, and both are fixed.
First, `_project_item` now matches `content.repository` as well as the issue number, because a
project that carries two repositories can put another repository's `#12` ahead of this one.
Second, a board whose `totalCount` exceeds what item-list returned is reported as a reason, not
as "not on the project", because the item may sit past the 500 item limit.

A live read against project 6 reproduced the incident exactly: #39 read `CLOSED` from
`status()` and `In review` from the lag read.

## Review findings deliberately not fixed

- `verify.startup_reverify` and the hand landing route promote a record to landed without the
  item read. Only the run end audit reports a lag on those records.
- The audit makes one item-list call for each landed GitHub record. That matches what `status()`
  already did for every open issue, but a manifest with many landed tasks now pays it at every
  run end.
- One lag shows twice in the summary, once as the record's finding and once as the audit's.
  `card_left_in_review` and `card_stale_in_review` already set that precedent.
- The triple landed route's new call has no end to end test. No test drives
  `_triple_integrate` to a landing today.

## Noticed, outside this task

`_project_item_for_issue`, the triple GraphQL read, passes `status_field` as the *field name* to
`fieldValueByName`. Everywhere else `status_field` is the terminal *option value* (`"Done"`), so
on a manifest shaped like this repo's, that read looks up a field called `Done`. It has not been
checked live.

## Prevention

An instruction added to a process brief is not a check. When a brief tells a process to make a
write the runner depends on, pair it with a runner side read of that write, the way
`confirm_card_returned` pairs with the return sentence. When a tracker holds more than one truth
per card, the test double has to hold each truth separately, or the disagreement cannot be
written as a test.

---
title: An empty GitHub project item read the issue state OPEN, and once it read None every consumer of status() took that None as a read that saw nothing
date: 2026-09-27
category: logic-errors
module: adapters
problem_type: logic_error
component: adapters/github
severity: medium
root_cause: vocabulary_substitution
resolution_type: code_fix
related_components: [closeout, audit, run, verify]
symptoms:
  - "A blocked Closeout brief says to move the project item back to `OPEN`, a column the board does not have"
  - "The run end audit reports every card agreeing with its record after the Closeout guessed a column"
tags: [github-projects, baseline, unknown-baseline, status-vocabulary, first-live-run, two-truths]
---

# An empty GitHub project item read the issue state OPEN, and once it read None every consumer of status() took that None as a read that saw nothing

## Problem

Issue #78, found live on `philgutowski/relay-proof`, Project 7. A card on the project with its
Status cleared was launched, reported blocked, and its Closeout brief said "Move its project item
back to `OPEN`". The board's columns were Todo, In Progress, and Done. The Closeout guessed Todo,
and nothing reported it.

## Cause

`GitHubAdapter.status()` ended `return {"status": board or state, ...}`. With `status_field`
declared, an open issue whose item had no Status, or that the project did not carry, got the
issue's state in place of a board column. So a GitHub baseline was never unknown. It was `OPEN`,
a word from the issue vocabulary, and `closeout.return_to_for` passed it through as a column to
return to. The unknown baseline sentence #51 added (`adapters.unknown_baseline_move`) could not
render on this route at all.

The stub suite could not produce it. `GitHubBoard` in `tests/test_run.py` overrides `status()`
entirely, so the real fallback never ran under a run loop test.

## Solution

`status()` now answers the item's Status alone for an open issue once `status_field` is declared,
and None when there is none. The issue state stays the status only with no `status_field`, and a
closed issue still reads `CLOSED` and terminal.

## The trap the fix walked into

None had always meant "never read" to the status consumers, because no production adapter
returned None from a read that worked. After the fix, GitHub does, and it means "read, no
column", which is out of review. Code review found four consumers still reading it the old way:

- `closeout.read_back` returned `out_of_review = bool(status)`, so an item cleared of its column
  never lost `card_in_review_by_run`, and a later deliberate staging read as the runner's own
  leftover (the #64 case).
- `run._clear_seen_out_of_review` skipped a falsy status for the same effect after the audit.
- `verify.card_status_of` rendered a None as "unreadable" in the partial landing cause line.
- The unknown baseline sentence said the read failed, which is false for an empty item. It now
  names both causes.

The rule to keep: in a `status()` result, a read that failed is marked by `skipped`, never by a
None `status`. A None `status` on an unskipped result is an answer. Any new consumer that reads
`status` must branch on `skipped` first and treat a None after that as a card with no status.

## Not fixed here

`status()` still reads the board through `_project_status`, which neither matches the repository
nor treats a board past `PROJECT_ITEM_LIMIT` as a reason. `_project_item` does both. A card past
the limit now reads as an unknown baseline, which is honest, where it used to read `OPEN`. A
foreign repository's item with the same number on a shared project can still answer for this one,
as it could before. `launch_baseline` also keeps the earlier baseline on a relaunch that reads
None, which on GitHub now overrides an operator who cleared the column between runs.

Related: `a-closed-github-issue-is-terminal-alone-so-an-instruction-to-move-the-item-had-no-check-and-the-test-board-held-one-truth.md`
(the other half of GitHub's two truths) and
`an-in-review-read-at-a-relaunch-is-ambiguous-and-only-the-runners-own-last-read-of-the-card-tells-leftover-from-staging.md`
(the #64 mark this fix had to keep clearing).

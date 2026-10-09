---
title: A repo's own board sync that does not know in_review_status reads every in flight card as drift, and a mid cycle sync pulls the card out from under its build
date: 2026-10-09
last_updated: 2026-10-09
category: workflow-issues
module: run (in_review_status), the host repo's board tooling
problem_type: workflow_issue
component: tracker
severity: medium
applies_when:
  - "a repo with its own board tool (a derive-and-sync script over the GitHub project) is run by a Relay feeder whose manifest sets in_review_status"
  - "a watcher or operator session runs that repo's board audit while a cycle is in flight"
  - "a watcher record or a skill says 'audit exit 1 means run sync'"
  - "setting the board lanes shape up on a new repo"
root_cause: two_writers_one_field
resolution_type: documentation_update
related_components: [run, audit, closeout, github-adapter, feeder, board-lanes, feeder-watch]
tags: [in_review_status, project-board, two-truths, drift, sync, board.py, cratekit, watcher, derive_status]
---

# A repo's own board sync that does not know in_review_status reads every in flight card as drift, and a mid cycle sync pulls the card out from under its build

## Problem

Cratekit keeps its GitHub project true with `scripts/board.py`. `audit` derives what every
card's Status should be from closure state and the dependency graph, reports anything that
differs as drift, and `sync` writes the derived value. Its `derive_status` keeps one status it
cannot derive, "In Progress", on the reasoning that "a unit on a branch stays on a branch".

The Cratekit feeder manifest sets `in_review_status = "In review"`. The Task's first step moves
the card there, thirty seconds into the cycle (#270 at 2026-10-09 15:24:56, cycle 48 started
15:24:24). So on the push watcher's first wake, `audit` said:

```
DRIFT, 1 item(s). Derived values that fell out of date:
  #270 status is In review, should be Ready
Fix it with: .venv/bin/python scripts/board.py sync
```

The watcher's record said exit 1 means run `sync`. The feeder's own output file held the same
line 154 times across every run since the status was configured. Each one was false drift: the
runner's in flight marker, read by a tool that only knows its own vocabulary.

## Why it matters more than a noisy audit

The runner reads that status back. `run.py` routes a no envelope landing on "commits on the
branch plus the card in In review", and the run end audit compares every card against
`in_review_status`. A `sync` run mid cycle sets the card to Ready, and the runner's own evidence
that the card was staged is gone: the no envelope route stops applying, the audit reads the card
as never moved, and the card can be offered again as Ready. The neighbour on relaunch reads
(`an-in-review-read-at-a-relaunch-is-ambiguous...`) is the same field read from the other side:
the runner cannot tell its leftover from an operator's staging, and here the operator's tool
cannot tell the runner's staging from drift.

## Solution

Two writers to one Status field need one vocabulary. The repo's deriver has to treat the
runner's `in_review_status` exactly as it treats its own "In Progress": a unit card holding it
derives it unchanged, its queue listing counts the card as busy, and closure still wins. On
Cratekit that is unit card U122 (#280). Until a repo has that:

- Never run its `sync` while a cycle is in flight. Run it only between cycles, when no card is
  in review, because `sync` applies every drift line at once.
- A watcher record that says "exit 1 means sync" is wrong for this line. The Cratekit record
  now carries the exception and the timing rule.
- Setting the lanes shape up on a new repo includes checking that the repo's board tool knows
  the manifest's `in_review_status`, before the first cycle, in the same step that sets
  `push = false` and the lane labels in the sidecar's deny list.

CI could not do this damage on Cratekit: `board.yml` runs `sync --labels-only` without a
project scoped token, so it never moves cards. A repo whose CI has that token moves cards on
every push, and there the deriver fix is a precondition of running the feeder at all.

## How to tell the two apart

The runner's mark lands at `cycle_started` plus seconds, on exactly the cards the cycle names,
with the project item's `updatedAt` matching, and the `creator` is the account the runner
authenticates as (on a personal board, the owner). Read it with:

```
gh api graphql -f query='{ repository(owner:"<owner>", name:"<repo>") { issue(number:N) {
  projectItems(first:3) { nodes { updatedAt fieldValueByName(name:"Status") {
    ... on ProjectV2ItemFieldSingleSelectValue { name updatedAt creator { login } } } } } } } }'
```

Drift that is real sits on cards no cycle holds.

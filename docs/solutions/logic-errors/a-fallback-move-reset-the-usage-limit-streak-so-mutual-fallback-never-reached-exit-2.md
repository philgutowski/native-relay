---
title: A fallback move reset the usage limit streak, so two models that fall back to each other never reached exit 2
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: high
root_cause: streak_reset_on_a_cycle_that_carried_no_evidence
resolution_type: code_fix
related_components: [feeder]
symptoms:
  - "with fallback = { fable = \"opus\", opus = \"fable\" }, a task that died quickly on both models was relaunched every half hour for ever, and no blocked report was ever made"
  - "the feeder never left with exit 2, although limit_waits_max is meant to bound a usage limit wait at eight hours"
  - "the state file's limit_waits climbed to about ten, dropped to 0 after a move, and climbed again"
tags: [feeder, usage-limit, fallback, streak, exit-2, code-review-catch, altitude]
---

# A fallback move reset the usage limit streak, so two models that fall back to each other never reached exit 2

## Problem

Issue #54. `Feeder.apply_rules` (`skills/relay/scripts/relay/feeder.py`) waited out a cycle of
quick deaths and struck `limit_waits` each time, and past `limit_waits_max` left with exit 2. Every
other cycle ran `self.state["limit_waits"] = 0`. Under a mutual fallback that reset is reached by a
cycle that proves nothing:

1. A task dies quickly on fable. It moves to opus and fable is marked exhausted for
   `fallback_hours` (5).
2. It dies quickly on opus. fable is marked, so no fallback is free, and the cycle waits.
3. About ten waits later fable's mark expires. The next death on opus finds fable free and moves.
4. That cycle did not wait, so it reset `limit_waits` to 0, and the count started again on fable.

## What did not work

The issue suggested a per task counter kept in the state file and never reset by a move, giving up
the one task past a bound. It was built and reviewed twice, and both reviews found real faults.

- Counting the move and the wait put the per task bound one wait ahead of the streak, so a one way
  fallback on a real long limit excluded a card where the feeder used to stop with exit 2 for a
  person.
- The counter survived a restart, which resets the streak, so a card that really was usage limited
  could be excluded after a restart.
- A cycle whose deaths were all given up went round without a wait and appended fresh cards onto
  the model that had just died.
- It had to be cleared in five places (landing, exclusion, exit 2, `--retry-blocked`, pruning), and
  each one was somewhere for the two counters to disagree.

## Solution

Fix the reset, not the bound. A cycle whose quick deaths all moved, with nothing landed, neither
waits nor resets:

```python
quick = looks_like_usage_limit(dead, landed, config)
if quick and len(moves) < len(dead):
    ...strike, wait, or exit 2...
if not quick:
    self.state["limit_waits"] = 0
```

Every other non waiting cycle resets as before: something landed, a death was slow, a halt never
launched a process, or nothing died. The existing exit 2 then bounds the mutual case, and it already
reports each blocked limit death blocked. The exit 2 message says the waits had "only fallback moves
between them", and SKILL.md's exit code sentence quotes that.

## Why this works

The streak measures "nothing has told us the limit is over." A landing or a slow death tells us
that. A move does not; it only says the task found another model to die on. Resetting on "did not
wait" treated the absence of a wait as evidence, and a move is exactly the cycle that is neither.

## Prevention

- Before adding a second counter beside a bound that fails to trip, find what resets the first one.
  A bound that never trips is usually a reset that fires on a cycle carrying no evidence.
- A per entity counter that persists while the streak it shadows resets on restart will disagree
  with that streak at every restart. That disagreement lands on the destructive side (exclusion).
- Left as found, not fixed: under `--once` the streak is never cleared at start and an idle cycle
  never resets it, so a partial count can outlive its episode by days. That was true before this
  change for plain waits; an all moved cycle now carries it too.
- Tests: `BlockedLimit.test_under_mutual_fallback_*` run the mutual case past the first mark's
  expiry with the clock moving ninety minutes a run, and fail on the old reset.

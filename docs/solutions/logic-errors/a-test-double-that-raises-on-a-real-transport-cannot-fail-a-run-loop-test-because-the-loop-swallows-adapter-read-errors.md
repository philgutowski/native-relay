---
title: A test double that raises on a real transport cannot fail a run loop test, because the loop swallows adapter read errors
date: 2026-09-27
category: logic-errors
module: runner
problem_type: logic_error
component: run-loop
severity: medium
root_cause: missing_validation
resolution_type: test_fix
related_components: [run, audit, closeout, tests, adapters]
symptoms:
  - "a double built to raise if a read reaches the real GitHub or Jira transport never turned a test red"
  - "a leaked read showed up only as an empty card or an unreadable finding in the record"
---

# A test double that raises on a real transport cannot fail a run loop test

## Problem

While adding board routes for issue #13, the natural guard for a fake adapter was a transport
that raises if anything reaches it. The run loop never let that raise fail a test. A read that
leaked past the fake board looked like a card with no data, or an unreadable card, which are
both states the loop handles on purpose.

## Cause

`run.py`, `audit.py` and `closeout.py` each wrap adapter reads in `except Exception` and turn
the failure into an empty card or an unreadable finding. That is correct for production, where a
tracker read can fail. It means an exception raised inside a double is absorbed before the test
sees it. `tests/_nonet.py` raises the same way, so it has the same blind spot on these paths.

## What to do next time

Have the double record every leak in a list and assert the list is empty in `tearDown`. The
file board in `tests/test_run.py` does this: `_leak` appends, and `tearDown` asserts
`leaks == []`. Any new adapter double driven through the run loop needs the same record and
assert shape. Raising alone is not a guard on these paths.

Related: `two-instructions-to-two-processes-written-weeks-apart-disagreed-about-moving-the-card-back-and-the-adapter-couldnt-see-it.md`.

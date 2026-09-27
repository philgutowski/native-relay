---
title: Fixing a test that passed by construction removed the only real call to host.snapshot, and nothing but a missing host line on a live run would notice
date: 2026-09-27
category: logic-errors
module: host
problem_type: coverage_gap
component: runner
severity: low
root_cause: mock_replaced_the_only_integration_call
resolution_type: accepted_tradeoff
related_components: [test_host]
tags: [host, test-coverage, mock, integration-test, silent-failure, card-32]
---

# Fixing a test that passed by construction removed the only real call to host.snapshot, and nothing but a missing host line on a live run would notice

## Problem

Issue #32's review found `test_this_host_answers_with_every_field_present` in `tests/test_host.py`
passed by construction: `snapshot()` starts from `empty()`, so the key set equals `FIELDS` even
when every real read fails. The card asked to replace it with a test that feeds the parser a
recorded reading and asserts the numbers, and #59 did that.

Doing it that way removed the only test in the suite that ever called `host.snapshot()` with its
real `subprocess` and `os.getloadavg` defaults. Every other `Snapshot` test already fed it a
mocked reading, so once the trivial test was gone, nothing in the suite exercises the real
`vm_stat` call path at all.

## Why nothing caught it

`host.py` is deliberately silent on failure: `snapshot()` never raises, and a field it cannot
read on the platform is `None` rather than a reason to skip a launch. That design is correct for
production, where a bad `vm_stat` parse should degrade one line of telemetry, not fail a task.
But it means the same property that makes the module safe to run also makes a coverage loss
invisible in CI: a future `vm_stat` output format change would degrade `host_at_start` and
`host_at_end` to mostly-`None` fields, the suite would stay green, and the only visible symptom
would be a real run's `host:` summary line quietly losing its numbers.

## What to do next time

Before removing a test that happens to be the only one calling a real subprocess or OS API,
check whether any other test in the file covers that real path. If none does, keep or add one
real-call smoke test alongside the mocked ones, even a loose one that only asserts the return
type and key set, rather than trading the last integration check for a stronger unit assertion.

This one was left unfixed on purpose in #59, noted in the tracker rather than folded into that
task; a follow-up card should add a smoke test that calls `host.snapshot()` unmocked and asserts
only that it returns a dict with `FIELDS` as its keys, so a parser regression fails a test again
instead of a live run.

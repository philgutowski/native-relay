---
title: "Adding an adapter method is asserted in two files, not one, so a unit's file list that names only its own module fails the full suite on a line its own tests never reach"
date: 2026-09-28
category: workflow-issues
module: runner
problem_type: workflow_issue
component: development_workflow
severity: medium
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [adapters, filing, test_adapters, test_board_item]
applies_when:
  - "a plan unit adds a new public method to every adapter (github, jira, markdown)"
  - "the unit's file list names the adapters and its own new module's tests, but not tests/test_board_item.py or adapters/__init__.py"
  - "the full suite is run at the end of the unit, not just the unit's own test module"
symptoms:
  - "tests/test_adapters.py and the unit's own new test module pass in isolation"
  - "the full suite fails on a single assertion in tests/test_board_item.py (test_the_read_adds_no_public_method) that the unit's plan never mentions and its own tests never exercise"
  - "the failing assertion compares dir(adapter) against adapters.INTERFACE alone, so any new adapter method not yet accounted for breaks it"
tags: [adapter-interface, hidden-coupling, file-list, plan-unit, shared-contract, filing, u4]
---

# Adding an adapter method is asserted in two files, not one, so a unit's file list that names only its own module fails the full suite on a line its own tests never reach

## Context

Unit U4 of the browser test loop plan (`docs/plans/2026-09-28-0935-feat-browser-test-loop-plan.md`)
added `filing_instructions(labels, design_note)` and `filing_allowed_tools(backend)` to every
adapter (github, jira, markdown). Its file list, correctly by the plan's own scope, named the
adapters, `skills/relay/templates/brief-filing.md`, `skills/relay/scripts/relay/filing.py`,
`tests/test_filing.py`, and `tests/test_adapters.py`. Running the full suite at the end of the
unit failed on one assertion in `tests/test_board_item.py`, a file the unit's plan never named and
whose own tests never touch filing at all.

## The trap: two files assert the same adapter surface

`skills/relay/scripts/relay/adapters/__init__.py` defines `INTERFACE`, the tuple of every public
method every adapter must expose. `tests/test_adapters.py`'s `SharedContract` asserts each adapter
matches `INTERFACE`, plus, since U4, `filing.ADAPTER_METHODS`
(`skills/relay/scripts/relay/filing.py:35`, `("filing_instructions", "filing_allowed_tools")`),
the pair deliberately kept out of `INTERFACE` itself rather than folded into it. That much is in
the unit's own file list and its own tests cover it.

The second assertion is not. `tests/test_board_item.py::test_the_read_adds_no_public_method`
(line 198) independently computes `dir(adapter)` and compares it against
`set(adapters.INTERFACE) | set(filing.ADAPTER_METHODS)`. It is a different file, testing a
different module (`board_lag`), and reads `filing.ADAPTER_METHODS` only because that name already
existed when the assertion was written for an earlier surface change; it has no reason to appear
on a plan unit's radar unless someone already knows this duplication exists. A unit that adds a
new adapter method and does not know about this line will pass its own tests and `test_adapters.py`
clean, then fail the one line in `test_board_item.py` that nothing in the unit's spec pointed at.

## What U4 did about it

Blocking the whole chain over one duplicate assertion was worse than widening it: U4 updated
`test_board_item.py:204` to the same union `test_adapters.py` checks, left
`adapters/__init__.py` untouched, and reported it as a deviation from the unit's file list rather
than hiding it. Issue #112 is the follow-up: fold the Filing pair into `INTERFACE` itself and give
both test files one shared assertion instead of two copies of it, so a future adapter method
change has one place to touch, not two.

## Why This Matters

The plan's file list is supposed to be a complete map of what a unit must touch. Here it wasn't,
because the coupling lives in a third file's test, not in the adapters module or the new module
being added. A unit that treated its file list as final would stop at "my tests pass" and ship a
red full suite, or would discover the failure late and have to guess, mid-task, whether widening
an assertion outside its file list is a deviation worth taking or a sign the unit is scoped wrong.

## When to Apply

Before writing a plan unit that adds, removes, or renames a public method on every adapter: search
for every place that reads `adapters.INTERFACE` or `filing.ADAPTER_METHODS`, not just the modules
the change conceptually touches, and put every hit on the unit's file list. Until issue #112 lands,
that search finds two files: `tests/test_adapters.py` and `tests/test_board_item.py`.  After #112
folds the pair into `INTERFACE`, re-check whether the duplication this doc describes still exists
before relying on it.

## Related

- Issue #112, the fold-in follow-up that this trap motivated.
- `docs/plans/2026-09-28-0935-feat-browser-test-loop-plan.md`, unit U4.

---
title: A mixin ahead of the real adapter in MRO means super() in an override calls the mixin, not the adapter
date: 2026-09-27
category: logic-errors
module: tests
problem_type: logic_error
component: test-fixtures
severity: low
root_cause: mro_assumption
resolution_type: test_fix
related_components: [test_run, adapters/github, audit]
symptoms:
  - "TypeError: BoardReads.status() got an unexpected keyword argument 'cache'"
  - "the error names the mixin's method, not the real adapter's, even though the override calls super().status(task_id, cache=cache) expecting the real adapter"
---

# A mixin ahead of the real adapter in MRO means super() in an override calls the mixin, not the adapter

## Problem

Task 61 threaded an optional `cache` keyword through `GitHubAdapter.status` so a run end audit
could share one board read across many cards. `tests/test_run.py`'s `GitHubBoard` fake overrides
`status` to special case a closed issue, then falls through with `super().status(task_id,
cache=cache)` for everything else. That raised `TypeError: BoardReads.status() got an unexpected
keyword argument 'cache'` the moment the audit ran through the fake, even though `GitHubAdapter`
(the class the override clearly means to reach) had already been given a `cache=None` parameter.

## Cause

`class GitHubBoard(BoardReads, github_adapter.GitHubAdapter)`. `BoardReads` is a shared mixin
that answers every one of the nine adapter methods from flat files, mixed in ahead of the real
adapter class so "every method it does not name is that adapter's own" (its own docstring). It
does name `status`, so Python's MRO puts `BoardReads.status` before `GitHubAdapter.status`, and
`super()` inside `GitHubBoard.status` resolves to whichever ancestor is next in that order:
`BoardReads`, not `GitHubAdapter`. `BoardReads.status(self, task_id)` takes no `cache`, so the
call raised.

Nothing about the override's own code suggested this. `super()` reads like "the adapter this
class extends," and `GitHubBoard`'s docstring and its own `_project_item` override both talk
about "the real GitHubAdapter" as if it were the only other class in the chain. The mixin's
position in the class statement is the only place the true resolution order is visible.

## What to do next time

Before changing an interface method's signature and forwarding the new argument through
`super()` in a class with more than one base, check the MRO with
`ClassName.__mro__` or `ClassName.mro()` rather than trusting the class statement's reading
order to mean "extends." Any `BoardReads` subclass overriding a method `BoardReads` itself also
defines has the same shape: `super()` there means `BoardReads`, whatever the real adapter is
called in the subclass's name.

The concrete fix here was to stop forwarding the keyword at all: `GitHubBoard._project_item`
already reads a status straight from a file rather than a shared board payload, so the fake has
nothing to actually cache, and `GitHubBoard.status` accepts `cache=None` (so `adapters.status`'s
capability probe still succeeds) but calls `super().status(task_id)` without it.

Related: `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
is the general form of a fixture built by the same hands as its consumer agreeing by
construction; this is the same shape one level down, inside the fixture's own class hierarchy.

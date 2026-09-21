---
title: The halt fields are cleared by hand at three launch sites with no shared list, so a field added later leaks a previous attempt into a task that lands
date: 2026-09-21
category: logic-errors
module: runner
problem_type: logic_error
component: runner
severity: medium
root_cause: missing_validation
resolution_type: code_fix
related_components: [state-store, summary, halt-evidence, triple]
symptoms:
  - "a task that halted, was relaunched, and then landed printed a halt sentence in the run summary"
  - "a captured issue named halt_message as the uncleared field, but the main launch upsert already cleared it"
  - "the triple launch upsert cleared only halt_class, so a relaunched triple worker kept halt_stage, halt_message and halt_evidence"
tags: [halt-fields, halt-evidence, relaunch, upsert, stale-record, run-summary, triple, stale-issue-text]
---

# The halt fields are cleared by hand at three launch sites with no shared list, so a field added later leaks a previous attempt into a task that lands

## Problem

A task's record holds four halt fields: `halt_class`, `halt_stage`, `halt_message` and
`halt_evidence`. When a task launches again after a halt, the record must forget all four, or the
run summary describes a failure that no longer happened on a task that landed. The issue that
started this work said `halt_message` was the field left behind. In current source it was not:
commit 57b6a02 had already cleared it at the main launch upsert in `_begin_task`. The real gaps
were different.

- `halt_evidence` was cleared nowhere. It feeds the Cause line last and beats the fresh record, so
  a leftover key could put a previous attempt's sha or branch inside a well formed sentence.
- The triple launch upsert in `run_triple` cleared only `halt_class`.
- `_abandon_build`, the sibling reset that puts a task back to pending, cleared three of four.

## Cause

There is no single definition of "the halt fields." Each of the three sites spells out its own
keyword list to `store.upsert`, so adding a field to the halt record means remembering every reset
by hand, and a missed site raises nothing. A relaunch test that asserts on `halt_class` sees the
cleared value at every site, so the suite stays green while another field is stale. Nothing
asserted on a halt field other than the class across a relaunch boundary.

The second trap is the issue text itself. A captured backlog line carried a mechanism, with line
numbers, that had already been partly fixed. Acting on it would have added a `halt_message` clear
that was already there.

## What to do next time

- When a field joins the halt record, grep for `halt_stage=None` and update every site that
  resets it. As of this fix that is `_begin_task`, `_abandon_build`, and the launch upsert in
  `run_triple`. If a fifth field arrives, prefer one shared reset dict over a fourth copy of the
  list.
- Test the relaunch by landing, not by halting again. A second halt overwrites `halt_evidence`
  wholesale, so a second halt test cannot show a stale evidence bug. Only a landing, or a halt path
  that writes no evidence such as the triple halt, exposes it. The landing test is the one that
  fails with the reset reverted, so prove the guard bites by reverting the reset and watching it
  fail.
- Read the current source before trusting a mechanism copied into an issue or a backlog line.
  Check `git log -S` on the field name for a fix that landed after the capture.

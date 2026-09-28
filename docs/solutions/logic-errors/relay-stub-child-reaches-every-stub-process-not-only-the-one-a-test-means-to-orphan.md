---
title: RELAY_STUB_CHILD reaches every stub process a test launches, not only the one it means to orphan, and hid a false hang behind the manifest timeout
date: 2026-09-27
category: logic-errors
module: tests
problem_type: coverage_gap
component: tests
severity: medium
root_cause: test_env_var_scoped_to_process_not_to_intent
resolution_type: workflow_improvement
related_components: [dispatch, launch, stub-claude]
symptoms:
  - "a dispatch test that needs one build to finish normally so its Closeout can start instead hangs until the manifest's eleven minute task timeout"
  - "the failure looks like a deadlock in dispatch or launch, not in the test's own fixture"
  - "the test only fails at full suite scale or under a schedule that mixes a finishing build with an orphaned one"
tags: [dispatch, stub-claude, sigint, keyboard-interrupt, test-fixture, issue-79]
---

# RELAY_STUB_CHILD reaches every stub process a test launches, not only the one it means to orphan

## Problem

Issue #79's end to end test needed two things in the same dispatch run: task A finishing
normally on the main thread so its Closeout starts, and task B still building in a worktree
when the interrupt lands. `RELAY_STUB_CHILD` makes the stub spawn a sleeping grandchild so a
test can assert a process group is still alive after the parent exits.

Setting that knob for the test process reaches every stub the test launches, not just the one
meant to outlive its parent. Task A's own stub then leaves a sleeping grandchild behind too.
That grandchild inherits A's stdout pipe, so `launch` never sees EOF on it, and A's launch call
blocks past its normal exit. There is no crash and no assertion failure, only a wait. The test
runs to the manifest's eleven minute task timeout before anything reports failure, so the fault
reads as a hang in dispatch rather than as a fixture mistake.

## Fix

Leave `RELAY_STUB_CHILD` off whenever any build in the test must finish, since a live process
group is already what a build looks like while it is running; only turn it on for the flight
that is meant to still be alive at interrupt time. Pass `timeout_overrides` on tests built this
way so a regression fails fast instead of riding out the full manifest timeout.

To inject an interrupt inside a main thread launch specifically (as opposed to while waiting on
a flight), send SIGINT from the stream callback only once
`signal.getsignal(SIGINT).__qualname__` contains `launch.<locals>.handle`. That is the proof
`launch`'s own handler is installed on the thread the test is driving, rather than a guess based
on timing.

## Prevention

Treat `RELAY_STUB_CHILD` as global to the test process, not scoped to one stub invocation.
Before adding it to any fixture that also expects a normal exit elsewhere in the same test, ask
whether that other stub's grandchild will hold a pipe open. A test that hangs to the timeout
instead of failing fast is the symptom to distrust first, before assuming the bug is in
production code.

A second interrupt landing during dispatch's stop of already-stopping flights is a related gap,
opened as issue #84, and is not what this note is about.

---
title: closeout_scope_check bounds by resetting, so a caller outside the run loop inherits the Runner's clean tree preflight
date: 2026-09-28
category: logic-errors
module: testpass
problem_type: logic_error
component: testpass
severity: high
root_cause: missing_precondition
resolution_type: code_fix
related_components: [gitwrite, closeout, run-loop, feeder, filing]
symptoms:
  - "a hand run of relay test on a checkout with an uncommitted edit would have hard reset that edit away after the Filing process, and the pass record would have blamed the Filing process for the change"
  - "the pass compared the checkout before and after the Test process and passed, because both reads were equally dirty, so nothing before the filing step said the tree was unsafe to reset"
tags: [scope-check, reset-hard, preflight, dirty-tree, test-pass, browser-test-loop, reuse-trap]
---

# closeout_scope_check bounds by resetting, so a caller outside the run loop inherits the Runner's clean tree preflight

## Problem

`gitwrite.closeout_scope_check` is how the Runner bounds what a Closeout process committed:
it diffs the pre closeout head against HEAD, adds the working tree's changed paths, and on any
path outside the allowed set, or any in scope path left uncommitted, it runs `git reset
--hard` to the pre closeout head. That is the right shape inside `run.py`, where `preflight`
has already refused a dirty tree and a checkout off the default branch before any process
launched, so every path the check sees is the Closeout's own.

The browser test loop's pass (`skills/relay/scripts/relay/testpass.py`, plan U5) reuses the
same check to bound the Filing process's commit to the tracker file, as the plan says to. The
first version had no preflight of its own. It recorded the checkout's HEAD and status before
the Test process and compared them after, which catches a Test process that touched the
checkout, but a tree that was already dirty passes that comparison, since both reads agree.
The scope check then ran with the operator's pre existing uncommitted edit among the changed
paths, treated it as the Filing process's doing, and reset it away. The note on the pass
record named the Filing process as the culprit.

## Cause

The check's contract carries an unstated precondition: the tree was clean when the process it
bounds started. Inside the run loop that precondition is enforced two functions away, in
`preflight`, and nothing on `closeout_scope_check` itself says so. A caller reading the
function alone sees a bound and a reset and reuses it as a bound.

## Fix

The pass refuses, before it takes the Lease, when `feeder.checkout_problem` names a dirty
tree or a branch other than the default, with the config exit and nothing written. That is the
Feeder's own preflight, and it is the same clean tree rule the Runner enforces, so the reset
inside the scope check can only ever take the Filing process's own change. The pass's before
and after comparison of the checkout stays, for the Test process, which runs in a worktree
and must not have touched the checkout at all.

## Rule

A function that repairs by resetting has a precondition equal to whatever the reset would
destroy. Before reusing `closeout_scope_check`, or anything else that ends in `reset_hard`,
outside `run.py`, put the Runner's clean tree preflight in front of it. The comparison of a
before and after snapshot is not a substitute: two dirty reads compare equal.

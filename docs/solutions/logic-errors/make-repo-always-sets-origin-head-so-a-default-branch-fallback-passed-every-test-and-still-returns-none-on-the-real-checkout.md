---
title: tests/_repo.make_repo always sets origin/HEAD, so a default branch fallback passed every test and still returns None on the real native-relay checkout
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: feeder
severity: medium
root_cause: fixture_fidelity
resolution_type: code_fix
related_components: [gitread, verify, tests/_repo, feed---pin]
symptoms:
  - "feed --pin refuses on the operator's own native-relay checkout with \"refs/remotes/origin/HEAD is not set\", even though the suite is fully green"
  - "gitread.default_branch returns None on a real checkout that was never made with git clone"
tags: [stub-cli, fixture-fidelity, origin-head, default-branch, first-live-run, self-hosted-checkout]
---

# tests/_repo.make_repo always sets origin/HEAD, so a default branch fallback passed every test and still returns None on the real native-relay checkout

## Problem

Task 48 (`docs/plans` issue: feed --pin extracts HEAD) fixed `pin_plan` so `feed --pin`
extracts the manifest's default branch instead of whatever HEAD happens to be when the
checkout is mid task. The fallback for a manifest that does not name
`project.default_branch`, or a `--pin` run against a repository other than the manifest's own,
is `gitread.default_branch(tree)`, which reads `refs/remotes/origin/HEAD`.

That ref does not exist on the operator's real `native-relay` checkout, because it was never
made with `git clone`. Cloning is what sets it; adding a remote by hand with `git remote add`
does not, and neither does any other way of ending up with a working tree that has an `origin`
remote. `gitread.default_branch`'s own docstring already says this: "A bare origin added with
`remote add` never gets this ref." Running `--pin` from that checkout against a manifest
pointed at a different repository hits the fallback, gets `None`, and refuses with the sentence
`pin_plan` was written to raise for exactly this case, naming the fix:
`git remote set-head origin <branch>`.

## What Didn't Work

**The full suite, 1505 tests, green.** `tests/_repo.make_repo` is the one fixture every test
in the suite uses to build a working repository, and it always runs
`git remote set-head origin main` (`tests/_repo.py:50`) as part of building the bare origin and
pushing to it. So every test that exercises `gitread.default_branch`, or anything that falls
back to it, sees the ref present and resolvable. No test in the suite can produce the state the
operator's own checkout is actually in, because the fixture always does the one step a real
checkout may never have done.

## Solution

Not fixed by task 48, and not a defect in what it landed. `pin_plan`'s refusal is correct
behavior: it names the exact command that resolves the gap, and a self hosted manifest that
names `project.default_branch` is unaffected, since that path never reaches
`gitread.default_branch` at all. The task's own closing comment recorded the same operator
note. This doc exists so the next session that touches a default branch lookup does not read
1505 green tests as proof the fallback works on a real checkout.

## Why This Works

This is the same shape as
`docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`,
one layer down: not a stubbed CLI agreeing with its own parser, but a fixture repository
agreeing with its own consumer. `tests/_repo.make_repo` was written to give every test a
repository that behaves the way the code expects a repository to behave, and
`git remote set-head origin main` is a completely reasonable thing to include in "a repository
that behaves normally." It just means the fixture can never exercise the one gap that made
task 48 refuse on the operator's own machine, because the fixture closes that gap every time it
runs.

## Prevention

**A new default branch lookup, or any new code path that calls `gitread.default_branch` or
falls back to it, needs a test that deletes `refs/remotes/origin/HEAD` first**, with
`git remote set-head origin -d`, not just a test built on the standard fixture. `feeder.py` and
`verify.py` both already have their own `default_branch_of`; either one gaining a new fallback
branch should get this test alongside it.

**Treat `tests/_repo.make_repo` itself as a smell whenever a test is about a git precondition.**
It is the right fixture for almost everything, but any assertion that a certain git state is
present or absent should ask whether `make_repo` already forces that state true, the same
question this doc's sibling document asks about a hand written fixture agreeing with its own
parser.

## Related Issues

- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  is the general form: a fixture and its consumer written by the same hands agree by
  construction, and only a real target proves anything. This doc is the case where the fixture
  is `tests/_repo.make_repo` itself and the gap is one git ref.

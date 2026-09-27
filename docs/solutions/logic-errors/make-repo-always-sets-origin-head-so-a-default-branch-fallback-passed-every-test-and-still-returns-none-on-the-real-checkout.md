---
title: tests/_repo.make_repo always sets origin/HEAD, but three tests in test_feeder_pin.py already delete it to cover the real checkout's fallback
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

# tests/_repo.make_repo always sets origin/HEAD, but three tests in test_feeder_pin.py already delete it to cover the real checkout's fallback

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

**A first read of the full suite, 1505 tests, green, taken as proof the fallback was
untested.** `tests/_repo.make_repo` is the one fixture nearly every test in the suite uses to
build a working repository, and it always runs `git remote set-head origin main`
(`tests/_repo.py:50`) as part of building the bare origin and pushing to it, so most tests that
touch `gitread.default_branch` see the ref present and resolvable. Read alone, that looked like
no test in the suite could produce the state the operator's own checkout was actually in.

That read overstates it. `tests/test_feeder_pin.py` already deletes the ref, with
`git remote set-head origin -d`, in three places: `Unresolvable.test_no_default_branch_anywhere_is_refused_with_a_sentence`,
`Unresolvable.test_another_repo_and_no_origin_head_is_refused_with_the_fix`, and
`Verb.test_pin_refuses_when_the_default_branch_cannot_be_resolved`. Each builds a repository with
`make_repo` and then undoes the one step that closes the gap, so `pin_plan`'s fallback path and
its refusal sentence are exercised on exactly the state the operator's checkout was in, and the
suite already covers them.

## Solution

Not fixed by task 48, and not a defect in what it landed. `pin_plan`'s refusal is correct
behavior: it names the exact command that resolves the gap, and a self hosted manifest that
names `project.default_branch` is unaffected, since that path never reaches
`gitread.default_branch` at all. The task's own closing comment recorded the same operator
note. This doc exists so the next session that touches a default branch lookup checks
`test_feeder_pin.py`'s pattern of deleting the ref by hand, rather than assuming a green suite
alone means the fallback is untested.

## Why This Works

This is the same shape as
`docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`,
one layer down: not a stubbed CLI agreeing with its own parser, but a fixture repository
agreeing with its own consumer, everywhere a test builds one and stops there.
`tests/_repo.make_repo` was written to give every test a repository that behaves the way the
code expects a repository to behave, and `git remote set-head origin main` is a completely
reasonable thing to include in "a repository that behaves normally." A test that wants the gap
`make_repo` closes has to reopen it by hand afterward, the way `test_feeder_pin.py` already
does; a test built on the fixture alone, with no such step, cannot exercise it.

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

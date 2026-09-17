---
title: Dual Manifest Dispatch
type: feat
date: 2026-09-17
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: conversation 2026-09-17, docs/ideation/2026-09-08-parallel-builds-in-worktrees.md
execution: code
---

# Dual Manifest Dispatch

## Goal Capsule

- **Objective:** an operator can split a list of independent tasks onto Claude Code and Grok
  Build at once, without the two colliding in the working tree, and still merge in the order
  that made sense when they ranked the list.
- **Means:** a Pair of sibling Manifests, `pair split` / `pair validate`, and `dispatch`, which
  overlaps one claude Task process with one grok Task process in git worktrees and merges in
  Pair order on the primary checkout.
- **Authority:** this plan. Halt classes stay closed. The runner still never writes the tracker.

## Product Contract

### Requirements

- R1. `pair split` writes two member Manifests and a pair file from a mixed Manifest. The
  original Task order is the merge order. Split is by the backend each Task already names.
- R2. A pair needs at least one claude Task and one grok Task. Codex is refused. Members share
  repo identity, tracker, shipping, gate, and branch prefix, and their Task ids are disjoint.
- R3. `dispatch` runs both backends at once, at most one in flight process per backend. Each
  build uses a detached git worktree under the state directory. The primary checkout stays on
  the default branch and stays clean during the build.
- R4. Merges stay in Pair order. A Task that finishes out of order waits. The merge tail treats
  a default branch SHA this coordinator already landed as expected, not as a foreign mover.
- R5. A halt that does not continue past kills the sibling, removes its worktree, deletes its
  half built branch, and leaves that record pending.
- R6. `run` is unchanged: one Task process at a time, cwd is the repo.
- R7. No new halt class.

## Key Technical Decisions

- KTD1. Dual files are the authoring artifact. Execution is one coordinator, not two runners.
  The repo lease stays exclusive for the whole dispatch.
- KTD2. The rubric stays judgment in the skill. The runner never assigns a backend.
- KTD3. Worktree removed after the process exits, before the merge tail, because git refuses to
  check out a branch another worktree holds.
- KTD4. `expected_default` is the SHA of the default branch after this coordinator's last
  landing, including the closeout commit. Serial `run` leaves it unset.

## Units

- U1 Worktree add/remove, repo identity for the lease, launch `cwd`.
- U2 Pair load, split, validate, combine.
- U3 Dispatch loop, expected_default, sibling abort.
- U4 CLI verbs, skill, authoring, CONCEPTS, examples.
- U5 Tests against the stub.

## Out of scope

More than one process per backend. Auto assignment of backends. Changing either brief template.
A live run against a throwaway target, required by CLAUDE.md before calling the worktree cwd
contract done, because the classifier finds the transcript by cwd.

---
title: A Task branch still in flight from an earlier run fails the no_task_branch preflight, and validate never warns
date: 2026-09-07
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: medium
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [gitwrite, run-loop, manifest, skill, brief]
applies_when:
  - "authoring a Manifest for a repository an earlier Relay run already worked in, under any prefix"
  - "a Task on the new Manifest was in flight when the earlier run halted, timed out, or was killed, and its branch still exists in the checkout"
  - "the tracker card still reads In Progress from that earlier attempt rather than blocked or done"
  - "the stranded commits are worth keeping and the Task should continue from them rather than start over"
  - "validate exits 0 and the operator is about to read that as ready to launch"
symptoms:
  - "validate reports the Manifest valid and lists the Task among the candidates with no warning"
  - "the run halts at that Task before any process launches, class unclean_exit, evidence check no_task_branch"
  - "the halt repeats identically on every later run, and continue_past_task_halt does not step over it"
  - "the local branch relay/<id> exists and carries commits from a run whose summary reads timed out or halted"
tags: [task-branch, preflight, no-task-branch, validate, in-flight-branch, manifest-authoring, resume, issue-as-brief]
---

# A Task branch still in flight from an earlier run fails the no_task_branch preflight, and validate never warns

## Context

On 2026-09-07 the Cratekit finishing Manifest, `~/.relay/manifests/cratekit-finish.toml`, was authored against a board whose issue 70 was still In Progress. An earlier run under the previous Relay, `cratekit-workers.toml` on 2026-09-01, had launched that Task, timed out at its 240 minute bound, and left `relay/70` in the checkout with eleven commits: a plan, a seam, both verbs queueing real items, a worker binding, five review fixes, and a measured mutation table. That run's own summary already carried the shape this doc is about, `pre flight refused before launching 70 on check no_task_branch`, from a resume attempt made after the timeout.

`validate --list` on the new Manifest exited 0, named the six Tasks, and said nothing about the branch. Had the run been launched on the strength of that, its first act would have been the same refusal, and every later run would have repeated it.

## Guidance

**What the runner checks, and where.** Preflight runs before every Task launch. Its four checks are the tuple `PREFLIGHT_CHECKS` in `skills/relay/scripts/relay/gitwrite.py` (line 241 at the time of writing): `tree_clean`, `on_default`, `head_equals_remote`, `no_task_branch`. The last one is `gitread.branch_exists(repo, task_branch)` inside `preflight` (line 263), and it reads the local branch list. A branch of that name existing at all is the refusal; the check does not ask whether the branch has commits, who made them, or which run made them.

**Why continuation does not step over it.** `run._continue_past_task_halt` returns `False` for a `no_task_branch` refusal by name, before the lease heartbeat and before any checkout. The docstring on that function (around line 332 of `run.py`) says why: the branch in the way is this same Task's own, nothing in the runner deletes it, so continuing would repeat the identical refusal on every later run while the record read `continued_past` and the run read `completed`. The runner is right to stop. The repair is the operator's.

**Why validate says nothing.** `manifest.validate` checks the Manifest and, with `check_repo`, the repository's shape. It has no reference to `branch_exists`, `task_branch_for`, or `preflight`. Preflight is a per Task, launch time check, and validate never composes a Task branch name. So a Manifest naming a Task whose branch is already in flight is valid by every rule validate applies. Do not read exit 0 as "no Task will be refused at launch".

**Update, issue 10.** `validate` now warns for this case. Under the repository checks it composes each listed, non excluded Task's branch from the Manifest's resolved prefix, reads the local branch list with the same `branch_exists` preflight uses, and reads origin with `gitread.remote_heads`, and it warns once per Task with a hit, naming the branch, where it was found, and the repair below. It is a warning and not an error, and the exit code stays 0, so exit 0 still does not mean ready to launch while a warning stands. A branch found only on origin is warned about too, although preflight would not refuse it, because the fresh Task process would not know the earlier work is there. The by hand check below remains the way to see the same thing without running validate.

**Check by hand before launch.** For every Task id on the Manifest, with the prefix the Manifest resolves to, which is the default relay prefix followed by a slash when `project.branch_prefix` is absent:

```bash
git -C <repo> branch --list 'relay/*'
git -C <repo> ls-remote --heads origin 'relay/*'
```

A hit on a Task id that is not landed is this doc. Also read `relay_cli.py status` and `summary` for the earlier Manifest that ran against the same repository; the halted or timed out Task there names the branch it left in place.

**The repair that keeps the work.** Three moves, in this order, and only the first touches the checkout.

1. Rename the local branch out of the prefix so `branch_exists` stops matching, and keep every commit:

   ```bash
   git -C <repo> branch -m relay/70 u52/pre-relay-70
   ```

   Leave the remote copy where it is. The merge tail pushes only the default branch, `push(repo, ["origin", default_branch], ...)` in `local_merge_tail` (line 400), and it never pushes or deletes a Task branch on the remote, so `origin/relay/70` is a free backup and collides with nothing. Do not delete either copy: the Task process needs one of them to merge from.

2. Put the resume instruction where the Task process will read it. The issue is the brief. Nothing else reaches a fresh headless process, not the earlier run's state directory, not its transcript, not this doc. Append a dated paragraph to the issue body that names the branch to merge first (the remote name, which the Task process can always reach), the conflicts to expect, and how the project's own rules say to resolve them. In the Cratekit case that paragraph named `origin/relay/70`, said the branch was 27 commits behind main, told the Task to take the incoming restart file and rewrite it, take main's method file and session contract, renumber incoming residuals above main's highest, and continue the unit rather than start over.

3. Launch. The new Task process creates `relay/70` fresh from the default branch, merges the old branch as its first act, and the runner's landing is an ordinary local merge of a branch that now carries the old commits plus the new ones.

**When to start over instead.** If the stranded commits are not worth keeping, the rename still applies, since preflight cares about the name and not the contents, but the issue edit does not. Say so in the Manifest's comment, because a later operator reading `origin/relay/70` beside a landed 70 will otherwise wonder which of the two was the real work.

**What this is not.** It is not the stranded branch refusal on `--retry-blocked`. That one is for a Task whose record reads blocked: the runner knows the branch, judges it against the name and baseline stored on the record, and refuses the retry while the branch still carries commits, which the Resume section of `SKILL.md` covers. A Task that halted or timed out has no such record on a new Manifest, so the runner meets its branch cold, at preflight, and has nothing to judge it against.

## Why This Matters

The runner is deliberately unable to delete a Task branch, and deliberately unable to tell its own stranded branch from anyone else's. Both are the right call for an unattended process that merges and pushes to the operator's repository. The cost is that a Manifest authored after an interrupted run carries a refusal validate cannot see, and the operator pays for it at launch rather than at authoring time.

The second half matters as much as the first. A rename alone gets the Task past preflight and throws the earlier work away in effect, because the fresh process starts from the default branch and does not know the old branch exists. Only the issue body reaches it. Writing the resume instruction there is what turns a relaunch into a continuation.

## When to Apply

- Before launching any Manifest against a repository that has had a Relay run before, whichever Relay ran it and whatever the prefix was.
- When a run's summary names `branch left in place: <name>` for a Task the next Manifest lists.
- When a tracker card reads In Progress and no runner is currently holding the repository's lease.
- When diagnosing a halt whose evidence is `check: no_task_branch` and whose Task never launched.

## Examples

The Cratekit case, 2026-09-07. Before the launch:

```text
$ git branch --list 'relay/*'
  relay/70
$ git ls-remote --heads origin 'relay/*'
<same commit as the local branch>  refs/heads/relay/70
$ python3 relay_cli.py validate ~/.relay/manifests/cratekit-finish.toml
~/.relay/manifests/cratekit-finish.toml is valid: 6 task(s), github adapter, local_merge mode
```

The repair, then the launch:

```text
$ git branch -m relay/70 u52/pre-relay-70
$ gh issue edit 70 --body-file issue70-after.md     # body plus the start-here paragraph
$ python3 relay_cli.py run ~/.relay/manifests/cratekit-finish.toml --follow --phases --notify --for 540
== 70 task ==
70 is now running
```

Preflight passed and the Task process was launched. Its log showed steady tool activity through the nine minute follow window; whether it merged the old branch first was not read at the time, so that half rests on the issue paragraph rather than on an observed transcript.

## Related

- `docs/solutions/workflow-issues/task-branch-namer-empty-is-not-omit-retry-reads-the-stored-name.md`, the retry blocked side of the same branch name: the runner refuses against the stored name, and this doc is the case where no stored name exists.
- `docs/solutions/workflow-issues/foreign-untracked-file-from-another-session-fails-tree-clean-preflight-and-unclean-exit-blames-the-task.md`, the sibling preflight check, `tree_clean`, refusing on something the Task did not do.
- `CONCEPTS.md`, Task process and Backend, which carry the stranded branch rule for a blocked Task.
- `skills/relay/SKILL.md`, "Author a manifest", "Validate before anything else", and "Resume", which named no Task whose branch was already in flight from a previous run when this doc was written. That gap was why this doc existed, and issue 10 closed it with the subsection "A Task branch left by an earlier run".

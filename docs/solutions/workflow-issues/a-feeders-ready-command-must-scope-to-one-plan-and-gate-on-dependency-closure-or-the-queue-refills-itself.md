---
title: A feeder's ready command must scope to one plan and gate on dependency closure, or the queue refills itself
date: 2026-09-28
category: workflow-issues
module: feeder
problem_type: workflow_issue
component: feeder
severity: high
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [feeder, ready-command, sidecar, order-file, models-file, tracker-adapter]
applies_when:
  - "a Relay feeder (`relay feed <manifest>`) is being set up against a multi unit plan whose units depend on each other"
  - "the plan's tracker cards live on the same board as other, unrelated open work"
  - "code review findings on a landed unit will themselves become tracker cards"
  - "the run is meant to be unattended, or attended only at intervals, for hours"
symptoms:
  - "a feeder authored against 'whatever the board says is ready' keeps finding new cards to build, because review findings for each landing get filed onto the same board the ready source scans"
  - "a run that started with 9 cards has landed 35 and still has roughly a dozen queued a day later, and only a manual `feed <manifest> --stop` ends it"
  - "a plan that should be ten units, in a known order, behaves instead like an open ended backlog with no end condition"
tags:
  - relay-feeder
  - ready-command
  - dependency-gate
  - queue-scoping
  - interleaved-review
  - stop-rule
  - unattended-run
  - github-project
---

# A feeder's ready command must scope to one plan and gate on dependency closure, or the queue refills itself

## Context

`relay feed <manifest>` (documented at `docs/operating-loop.md:57` to `:86`, "When the queue is
long or its cards depend on each other") exists because listing a queue by hand fails as soon as
one card depends on another: a card for unit B sits in the manifest next to unit A, and a plain
`relay run` would launch both without waiting for A to land. The feeder's answer is to ask a
ready source what is ready, once per cycle, and append only that. What "ready" means is entirely
up to whatever the sidecar's `[ready]` section points at. `docs/manifest-authoring.md:424` to
`:427` shows the three choices, labels, a JQL query, or `command`, the last one marked "the
escape hatch" in a comment right there at `:427`, a plain script the feeder trusts completely,
printing a JSON array of cards in the same shape `gh issue list --json number,title,body,labels`
already prints. There is no dependency graph inside Relay itself. If a plan's units depend on
each other, whatever encodes that dependency has to live in the ready script, because nothing
else in the feeder knows it.

This repo ran the same feeder mechanism against its own GitHub Project board twice, a day apart,
and the two runs show what happens with and without that scoping.

The first run, self hosted round two, used `~/.relay/manifests/native-relay-round2-ready.py`.
Its logic (confirmed on disk) pulls every open issue in the whole repository with no label
filter narrowing it to one plan (`native-relay-round2-ready.py:30` to `:31`, `gh issue list
--repo philgutowski/native-relay --state open --limit 400`), and checks only that a card is
open, owner written, owner commented only, on project 6 with Status Todo, and not labelled
`attended`. Nothing in it reads what a card depends on. The run's independent code reviewer
filed each landing's findings as new GitHub issues onto that same board, project 6, and the
ready script picked every one of them up on its very next cycle with no gate at all. What was
meant to be a bounded set of cards grew instead: the run landed 35 cards against an initial list
of 9, still had roughly a dozen queued a day later, and had to be stopped by hand with
`feed native-relay-round2.toml --stop`.

The second run, the usage limit plan (2026-09-27 23:02 to 2026-09-28 05:26, ten cards over ten
feeder cycles, 6h21m, zero halts, zero blocked tasks), used a differently shaped ready script
against the same board and did not have this problem.

## Guidance

Give the ready command two properties, both visible in
`~/.relay/manifests/usage-limits-ready.py`, which is the actual script the usage limit
plan's sidecar pointed at (`usage-limits.feeder.toml:30`, `[ready] command =
["python3", "/Users/pgutowski/.relay/manifests/usage-limits-ready.py"]`).

**First, scope the ready source to the plan's own cards, and nothing else on the board.**
`usage-limits-ready.py:43` to `:48` filters with `--label usage-limit-plan`, then `:55` iterates
only over the fixed set of issue numbers named in its own `NEEDS` dict, ignoring every other open
issue on project 6 regardless of status. Contrast this with `native-relay-round2-ready.py:30`
to `:31`, which has no label argument at all and reads the whole open backlog.

**Second, gate each card on its dependencies being closed.** `usage-limits-ready.py:28` to `:39`
declares:

```python
NEEDS = {
    87: [],
    88: [],
    89: [87],
    94: [87],
    96: [89],   # the mark length fix the review of U2 found
    90: [87, 88, 94],
    91: [89, 88, 90, 94, 96],
    92: [91],
    98: [91, 92],   # the unreached record guard the review of U4 found; after U5, same file
    93: [91, 92, 90, 98],
}
```

The script computes the set of currently CLOSED issue numbers (`:53`), then for each card in
`NEEDS`, includes it in the ready output only when the card is OPEN, authored by the repo owner,
carries no comment from anyone else, has no `attended` label, sits in the tracker's Todo column,
and every number in its own `NEEDS` entry is already closed (`:55` to `:70`). This dependency
closure check is not a Relay feature. It is ordinary Python living entirely inside this one
plan's ready script. Relay itself has no concept of one card waiting on another; the ready
command is where that concept has to be built, per `docs/manifest-authoring.md:458`, "Use it
when ready is a rule labels cannot say, such as a card that waits for other cards to close."

Both properties matter together. A dependency gate with no scoping still lets an unrelated card
into the queue the moment it happens to have no dependencies. A label scope with no dependency
gate offers every one of the plan's own cards on cycle one, including units whose prerequisites
have not landed yet.

**Then interleave review with the queue instead of running it after the queue empties.** While
unit N+1 was already building, an independent reviewing session with only read only diff access
(`git show`, `git diff`, no write access to the repo) reviewed the just landed merge for unit N.
When it found a real defect, the fix did not wait for a refill. A new GitHub issue was opened for
it, added as a key in `NEEDS` naming the unit it actually depended on, and its number was
inserted into `usage-limits.order` immediately ahead of the unit that needed it. Three real
instances of this happened in the usage limit run:

- Issue #94, "The limit reader reads through an attempt that printed no init line," found
  reviewing U1 (merge `c4db90d`, #87), entered `NEEDS` as `94: [87]` and placed in
  `usage-limits.order` ahead of U6 (#90) and U4 (#91), the two units that act on a confirmed
  reading.
- Issue #96, "A reset time that passed during the run marks the model for the full
  fallback_hours (usage limit plan)," found reviewing U2 (merge `c6255c0`, #89), entered as
  `96: [89]` and placed ahead of U6.
- Issue #98, "A halted limit death that stops the run counts a halt against every task the run
  never reached (usage limit plan)," found reviewing U4 (merge `3c87a21`, #91), entered as
  `98: [91, 92]` and placed after U5 (#92), since both units touch the same file.

The next feeder cycle offered each fix card because the ready command reads `NEEDS` and
`usage-limits.order` fresh from disk every time it runs, and because the routing file is
documented as "read fresh each cycle" with no restart needed (`docs/manifest-authoring.md:392`,
and confirmed in the feeder's own code, where `read_order` and `read_routing` are called inside
`cycle()` at `feeder.py:1166` and `:1171`, never cached at start); the fix cards' routing lines
were added to `usage-limits.models` the same way. No feeder restart was needed for any of the
three insertions.

**Apply a stop rule alongside this, or the same mechanism regresses into round two's problem.**
When a second review of the same area, a fix that had already been built once, still found a
gap, the fix was not built a third time inside the run. It was filed as issue #97, "A confirmed
limit death whose reset has already passed is relaunched every cycle with no bound (needs a
decision)," carrying the repo's own `attended` label. `usage-limits-ready.py:63` to `:64`
excludes any card carrying that label from its output, so #97 could never be offered to this or
any other unattended feeder cycle, and it was left for a person to decide instead. Filing every
review finding as a new card ahead of the queue is only safe because there is a label that takes
a card out of the unattended pool entirely; without that exit, a plan with a defect that keeps
resurfacing has no way to stop feeding itself the same fix.

A related trap worth naming, because it shows the same scoping principle failing from the
opposite direction: a generic, once run housekeeping step at the end of the same session, meant
to add any open issue not yet on the project board, briefly re added issues #25 and #27 that
`native-relay-round2.feeder.toml:41`'s own `[deny] ids = [33, 25, 34, 12, 27, 38]` had
deliberately excluded from that other feeder's ready output. It was caught and reversed within a
minute. No script matching that generic sweep's description remains on disk to cite by file and
line; the closest script present, `native-relay-round2-board-sync.py`, is itself narrowly scoped
by a `SINCE` timestamp (`:22`) and would not have touched issues that old, so this was a separate,
ad hoc step. The lesson stands regardless of which script ran it: any board wide housekeeping
that adds cards to a project has to either respect every feeder's own deny list or be scoped as
narrowly as that feeder's own ready command is. A deny list is not self enforcing against code
that never reads it.

## Why This Matters

An unattended feeder run has no natural end condition of its own. `docs/operating-loop.md:59`
to `:63` describes the feeder's basic loop: it appends only the cards the tracker reports ready,
then asks again next cycle. Nothing in that loop asks whether the plan is done; it asks only
whether the ready source currently returns anything. That makes the ready source the entire
control surface for when the run stops. Scope it to the whole board and the run's size is
whatever the board happens to contain at each cycle, which grows every time anyone, including
the run's own reviewer, files a new issue onto that board. That is exactly what happened in round
two: a run meant to land roughly nine cards landed 35, because its own review output looked, to
the ready script, indistinguishable from the original queue.

The two part fix removes both routes to that failure without giving up the benefit review
findings provide. Label scoping means a finding filed anywhere else on the board, or by anyone
else's work, cannot enter this run's queue no matter how it is labelled or filed. The `NEEDS`
gate means a finding that is filed can enter the queue, but only once its own prerequisite is
actually closed, so a fix for unit N never races ahead of unit N landing. Interleaving the review
itself, rather than running it after the whole plan completes, is what lets a real defect become
a card the very next cycle can pick up rather than something discovered only after every unit
has already built on top of it. The usage limit run's zero halts and zero blocked tasks over ten
cycles is the direct result: every card the feeder ever offered was either an original unit
whose dependencies had genuinely closed, or a fix whose dependency had genuinely closed, never a
card racing ahead of what it needed.

The stop rule closes the one gap the two part fix does not: a defect that keeps recurring. Without
it, "file every review finding as a new card ahead of the queue" is just round two's failure mode
with slower plumbing, since a run that unconditionally re queues its own findings has, again, no
natural end condition. The `attended` label is what actually enforces the boundary between "the
feeder can fix this on its own" and "a person has to look at this," and it only works because the
ready script checks for it in code, not because the convention exists.

## When to Apply

Apply this whenever setting up a `relay feed <manifest>` run against a plan with more than one
unit where later units depend on earlier ones landing, especially when:

- The plan's tracker cards live on a board that also carries unrelated open work, so any
  ready source without a label or equivalent filter would see more than the plan.
- The run will have its own review step, human or agent, that produces findings worth turning
  into tracker cards while the run is still going.
- The run is expected to proceed for multiple cycles without someone re authoring the manifest
  or restarting the feeder between them.

It does not apply to a single unit run, or a Manifest whose tasks are already independent by
`qualifying.independence`'s ordinary meaning, since `docs/operating-loop.md:69` to `:71` notes
that under a feeder that sentence has to say something different anyway, that cards are listed
only once the tracker derives them ready and the runner merges one at a time, which presumes
exactly this scoping and gating already exist.

It also depends on the feeder having no run budget of its own. As of this writing that is issue
#81, still open, so a supervising session using this pattern still has to hold the overall run's
wall clock or cycle budget itself, the way `usage-limits.feeder.toml:7` to `:8` records it doing,
by comment, not by any enforced setting: "The feeder has no budget of its own yet (issue #81).
The session that launched it holds the budget: eight cycles or twelve hours, then
`feed usage-limits.toml --stop`." A future run relying on this pattern for a long or unattended
stretch should check whether #81 has landed before assuming the feeder will stop itself. The
`long-running-task` skill states the general form of this rule for any long running or
unattended task, not only a Relay feeder: give it a stop condition it can test, and a budget as
the backstop when the stop condition never trips.

## Examples

**Before, round two's ready script, no scoping and no gate**
(`native-relay-round2-ready.py:30` to `:31`, `:40` to `:52`): reads every open issue on the whole
repository with `--state open --limit 400` and no label argument, and includes a card once it is
owner written, owner commented only, on project 6 at Todo, and not `attended`. There is no
concept of one card depending on another anywhere in the file. A code reviewer filing a finding
as a plain new issue on project 6 is, to this script, indistinguishable from an original unit of
the plan.

**After, the usage limit plan's ready script, both properties present**
(`usage-limits-ready.py:28` to `:70`): the `gh issue list` call carries `--label
usage-limit-plan`, restricting the source to this plan's cards only, and the loop only considers
numbers present in the `NEEDS` dict, then requires every dependency listed for that number to
already be in the `closed` set before including the card. Three fix cards, #94, #96, and #98,
were added to `NEEDS` and to `usage-limits.order` mid run, each entered ahead of the specific
unit that needed it, and each was offered by the very next cycle with no feeder restart.

**The stop rule in the same run**: a second review of a limit death handling gap did not become a
fourth `NEEDS` entry. It became issue #97, labelled `attended`, which
`usage-limits-ready.py:63` to `:64` explicitly excludes from its own output, so it stayed off
every future cycle's queue until a person picked it up by hand.

**The scoping trap in miniature**: a board wide sweep with no concept of any feeder's deny list
briefly re added issues #25 and #27, which `native-relay-round2.feeder.toml:41`'s `[deny] ids`
had deliberately excluded from that feeder's own ready output. The fix in both directions is the
same rule: anything that reads from, or writes to, a shared board on a feeder's behalf has to
carry that feeder's own scoping, whether the scoping is a label filter, a dependency gate, or a
deny list, because the feeder trusts whatever that script hands it without any check of its own.

## Related

- `docs/operating-loop.md:57` to `:86`, "When the queue is long or its cards depend on each
  other," the parent doc for `relay feed` itself, and the source of the independence sentence
  language this doc's When to Apply section quotes. That section describes feeding at a higher
  level of generality than this doc; it does not name the `NEEDS` dependency dict, the label
  scope, or the interleaved review insertion technique, all specific to the run this doc
  documents.
- `docs/manifest-authoring.md:424` to `:459`, the `[ready]` and `[deny]` sidecar reference,
  including the "read fresh each cycle" behavior of the routing file that also applies to the
  order file and made the mid run `NEEDS` and `.order` edits take effect with no restart.
- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`,
  a sibling lesson from the same repo's self hosted history, that a stub cannot produce what a
  real process produces and a contract change needs one live run; the interleaved review rule in
  this doc is one more reason a live run, not a stub, is where a defect like #94, #96, or #98
  actually surfaces.
- `docs/solutions/logic-errors/a-fallback-move-reset-the-usage-limit-streak-so-mutual-fallback-never-reached-exit-2.md`,
  a defect found and fixed inside the same usage limit plan this doc's example run built, about
  the feeder's usage limit streak rather than its ready command, filed and built the ordinary way
  before the plan run this doc describes began.
- `docs/solutions/workflow-issues/self-hosted-run-cannot-observe-the-code-its-own-tasks-land.md`,
  a sibling self hosting trap from the same class of run, about the Runner process rather than
  the ready source, and a reminder that self hosted Relay runs against this repo's own board keep
  surfacing seams that a run against someone else's tracker would not.
- Issue #81, the feeder's own missing run budget, the caveat named in When to Apply.

---
title: The Closeout's do not transition instruction outlived the earlier plan that started moving cards into review before launch, and the suite's own adapter cannot produce the state that would catch it
date: 2026-09-08
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: high
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [closeout, run-loop, adapters, contracts, state, cli, summary, audit]
applies_when:
  - "a Relay Task brief moves a tracker card to an in progress or in review status before any other work, so the board shows a task as started the moment its process launches"
  - "a separate Closeout brief, written by an earlier or later plan, tells the process not to transition the card for a given outcome because the card's status at authoring time was still the pre run status"
  - "no single test exercises both instructions end to end against a tracker adapter that actually models the in review status, since the suite's default markdown adapter has only an open checkbox and a closed one"
  - "diagnosing why a blocked, halted, timed out, or crashed Task leaves its tracker card sitting in progress with nobody assigned"
symptoms:
  - "every blocked, halted, timed out, or crashed Task left its tracker card sitting In Progress with nobody on it, on every github and jira board Relay ran against"
  - "the Closeout brief for a blocked or halted outcome told the process not to transition the card because it keeps its current status, a sentence true only before an earlier plan made the Task brief move the card to in review as its first step"
  - "the earlier plan that introduced the early in review move had itself named this exact gap as an accepted residual rather than deciding against it"
  - "tests/test_run.py's run loop tests all drive the markdown adapter, which cannot represent an in review status at all, so a card stuck at in review was a state the suite structurally could not produce or catch"
tags: [closeout-instructions, task-brief, in-review-status, stale-card, cross-plan-drift, markdown-adapter-blind-spot, board-audit, card-return]
---

# The Closeout's do not transition instruction outlived the earlier plan that started moving cards into review before launch, and the suite's own adapter cannot produce the state that would catch it

## Context

Every blocked, halted, timed out, or crashed Relay Task left its tracker card sitting In Progress
with nobody working on it, on every GitHub and Jira board Relay ran against. An operator watching
a board had no way to tell a card that was genuinely stuck from one whose owning process had
simply stopped.

The root cause was two instructions written weeks apart that stopped agreeing without either one
changing to say so.

The Task brief's `start_step`, rendered by `task_tracker_steps` in
`skills/relay/scripts/relay/adapters/__init__.py:109-111`, tells a launched Task process to
"Move the tracker card to `<in_review_status>` now, before anything else in this session." That
line was added by `docs/plans/2026-08-31-1409-feat-relay-visible-halt-and-early-in-progress-plan.md`
so a board would show a task as in progress the moment its process launched, rather than only near
the end of the run (see the docstring at `skills/relay/scripts/relay/adapters/__init__.py:88-91`: "The start step exists so
the board shows a task as in progress the moment its process launches").

At that same moment, the Closeout brief's instructions for a blocked or halted outcome still read
(GitHub, before this fix, `git show 4e3c8f6^1:skills/relay/scripts/relay/adapters/github.py:170-171`):

```
Do not close the issue and do not move its project item: a blocked task stays open.
```

and Jira's equivalent (`git show 4e3c8f6^1:...jira.py:182-183`):

```
Do not transition the card: a blocked task keeps its current status so the board still shows it
as open.
```

Both sentences were correct on the day they were written, when a blocked or halted task's
"current status" was still Todo. The 2026-08-31 plan even named the exact seam this would strain,
in its own Scope Boundaries section
(`docs/plans/2026-08-31-1409-feat-relay-visible-halt-and-early-in-progress-plan.md:114-116`):
"the card can read `in_review_status` while the retry itself halted at pre-flight with nothing
running... an accepted residual gap, not a hidden one." The same plan's KTD2 section is very
likely the literal origin of the stale sentence: "Every adapter's `closeout_instructions(outcome)`
gains a `OUTCOME_HALTED` case: comment only, never transition or close." The plan chose to defer
the gap rather than decide against it, and nothing tracked that deferral forward into the Closeout
instructions, so once the early-move step landed, "keeps its current status" silently started
meaning "keeps reading In Progress forever."

The bug survived for weeks because the one adapter every test in `tests/test_run.py` exercises by
default cannot produce the state the bug lived in. The module-level manifest fixture in
`tests/test_run.py:39` sets `adapter = "markdown"`, and `RunCase.setUp` (`tests/test_run.py:167`)
builds every case from it. The markdown adapter's tracker line has exactly two states, an
unchecked box and a checked one (`skills/relay/scripts/relay/adapters/markdown.py:153-166`, whose
own docstring says the point plainly: "a markdown tracker has only an open box and a checked one,
and the task process never moves it"). There is no third, in-review state for a markdown card to
get stuck in. So however many blocked, halted, or crashed scenarios the suite ran, its own default
fixture adapter was structurally incapable of ever showing the failure. The tests were green
because the double could not represent the world where the bug lived, not because the bug was
absent.

## Guidance

**When a plan changes the timing of when one process writes to shared state, audit every other
instruction set that assumed the old timing. Don't just add the new step and move on.** A tracker
card is shared, mutable state written by two independently launched processes, the Task and the
Closeout, at two different times. A sentence like "don't move it, it's already where it should be"
is only true as long as "where it should be" hasn't changed underneath it. The 2026-08-31 plan
changed when the Task writes the card; it did not re-derive what "current status" meant for the
Closeout's own writes, and the drift sat live for a full plan cycle.

The concrete fix, and the shape worth reusing whenever this class of drift shows up:

1. **Widen the seam that already carries the instruction, rather than inventing a new one.**
   `closeout_instructions(self, outcome, return_to=None)` gained one optional keyword across all
   three adapters (`skills/relay/scripts/relay/adapters/__init__.py`, `github.py:162`, `jira.py:174`, `markdown.py:153`). The
   eight-method adapter interface (`skills/relay/scripts/relay/adapters/__init__.py:40-49`) did not grow; each adapter still
   owns the exact wording of its own move sentence, so the brief and the adapter cannot disagree
   about how a card moves.

2. **Make the decision of "should we even try" a single, separately named function with reasoned
   refusals, not a boolean.** `return_to_for(manifest, record)`
   (`skills/relay/scripts/relay/closeout.py:308-322`) returns `None` in three distinct cases, each
   a reason rather than a gap:
   - no `baseline_tracker_status` recorded at all, nowhere known to send the card back;
   - the baseline already equals `manifest.tracker.in_review_status`, the operator deliberately
     staged the card there before the run started, and the runner must not overwrite a human's
     placement;
   - the record carries a `landing_ref`, the halt happened after a landing whose own Closeout
     already closed the card (a mirror push failure or a failing final verify after the merge
     already exists), and moving a closed card back would undo a real landing.

   Naming these as three refusals rather than one `if not ok: return` makes each one legible on
   its own and auditable independently the next time the timing of a write changes again.

3. **Confirm the instruction actually happened, and treat "it didn't" as data, not as a halt.**
   `confirm_card_returned(adapter, manifest, task_id, return_to)`
   (`closeout.py:325-346`) reads the card back with `adapter.status()` after the Closeout process
   exits and appends a `contracts.CARD_LEFT_IN_REVIEW` finding if the card still reads the
   in-review status, or if the read itself failed. It never raises and never halts the run,
   because a launched Claude process ignoring or mis-executing an instruction is a possibility the
   runner can only observe from outside, per this repo's own rule that the runner never writes to
   a tracker.

4. **Add a structural safety net that catches the whole class of drift, not just this one
   instance.** `audit.build` (`skills/relay/scripts/relay/audit.py:32-101`) runs once at the end of
   every run via `_audit_cards`, independent of whether any single Closeout ran correctly, and
   compares every card against its record and against git. It reports four disagreements, not
   just the one this bug produced, so a future timing change that breaks a different assumption
   still surfaces as a stale-card finding rather than as silence.

## Why This Matters

The Task brief and the Closeout brief are two separate instruction sets, rendered by different
code paths (`task_tracker_steps` in `skills/relay/scripts/relay/adapters/__init__.py` versus
`closeout_instructions` on each
adapter), for two processes launched at different times with no shared runtime state between
them. Nothing in the architecture forces the two to agree about what state a card is in when each
one starts writing. That decoupling is deliberate and correct for what it buys: the guarantee that
the runner itself never writes to a tracker, and the guarantee that a Closeout never sees the
Task's own transcript. But the same decoupling means every implicit assumption one brief makes
about the other's writes, "the card is still at Todo" is an assumption, not a contract, can go
stale the moment either brief's timing changes, with no compiler, type system, or test failure to
catch it. This is not a one-time bug; it is a standing property of the design, and it will recur
any time a future plan changes when either process writes to the card, a comment, or any other
shared tracker state.

The second risk is independent and just as durable: `tests/test_run.py`'s default fixture manifest
uses the markdown adapter, whose state space, an open box or a checked one and nothing else, is
narrower than either real adapter's. Every test built on `RunCase` inherits that narrower state
space unless it explicitly swaps in a different adapter. A suite passing every test is not
evidence that a bug involving a tracker state the default fixture cannot represent has been
caught; it is evidence that no test in the suite was capable of representing it. This is worth
naming as a standing property of the test suite, not a fact that dies with this bug: any future
scenario that depends on GitHub's or Jira's richer status vocabulary, an in-review state, a
reopened state, an unreadable card, needs a test that deliberately exercises `FakeAdapter` or an
equivalent with that richer shape, not one that trusts the markdown-backed default to have
covered it.

## When to Apply

- Editing `task_tracker_steps` (`skills/relay/scripts/relay/adapters/__init__.py:77-117`) or any other Task-brief step that
  writes tracker state: check every Closeout instruction, in every adapter, that assumes what
  state the card was in before the Closeout runs.
- Editing any `closeout_instructions` implementation in `github.py`, `jira.py`, or `markdown.py`:
  check whether its wording still matches what the Task brief promises to have already done to
  the card by the time the Closeout runs.
- Adding a new run-loop route that can end a Task without a landing, a new halt path alongside
  `_blocked_route` and `_note_halt` in `run.py`: call `closeout.return_to_for` and
  `closeout.confirm_card_returned` from it, the same way both existing routes do, rather than
  assuming the card is fine because some other route handles the return.
- Writing a test for any scenario that involves a tracker status transition, in review, reopened,
  closed then reopened, unreadable: use `FakeAdapter` or a manifest with `adapter = "github"` or
  `"jira"` rather than relying on the suite's markdown-backed default, because the default cannot
  represent those states at all.
- Any time a plan changes when a process writes to shared tracker state: treat "audit every other
  instruction set that assumed the old timing" as a required step of that plan, not an optional
  follow-up, and if it is deliberately deferred, as the 2026-08-31 plan did, write down the
  deferral somewhere it will be found later, the way that plan's Scope Boundaries section did.

## Examples

**GitHub's blocked-outcome sentence, before and after.**

Before (`git show 4e3c8f6^1:skills/relay/scripts/relay/adapters/github.py:170-171`):

```
Add one comment carrying the blocker digest below with `gh issue comment`. Do not close the
issue and do not move its project item: a blocked task stays open.
```

After (`skills/relay/scripts/relay/adapters/github.py:170-178`):

```python
move = ("Do not close the issue and do not move its project item" if not return_to else
        "Do not close the issue. Move its project item back to `%s`, the status it read "
        "before this run, since no process is working on it now; use `gh project "
        "item-edit` with the board's Status field" % return_to)
...
return ("Add one comment carrying the blocker digest below with `gh issue comment`. %s: a "
        "blocked task stays open." % move)
```

**The three `return_to_for` refusals, as concrete scenarios:**

1. *Operator-placed card.* An operator manually moves T-7's card to "In Review" before starting a
   run, intending to hand it to the runner mid-review. `return_to_for` sees
   `baseline_tracker_status == manifest.tracker.in_review_status` and returns `None`: the Closeout
   is not told to move anything, because the runner did not put the card there and must not assume
   it knows better than the human who did.

2. *Halt after a landing.* T-12 lands, its own Closeout closes the card, and then the run's final
   verify step fails, or a mirror push is refused. The record now carries a `landing_ref`.
   `return_to_for` sees the `landing_ref` and returns `None`: sending this card "back" would
   reopen a card that closed over a real, merged commit, which is worse than leaving it closed.

3. *No baseline at all.* T-3's very first status read failed, a transient API error or a
   misconfigured board, before the run recorded any `baseline_tracker_status`. `return_to_for`
   returns `None` because there is no known prior status to send the card to; the run-end audit's
   `card_stale_in_review` finding (`audit.py:74-82`) is what eventually surfaces this card to the
   operator instead.

**Verification status, stated precisely.** The full suite passes, but no live run against a real
GitHub or Jira board has yet exercised a real Closeout process actually returning a card. This
repo's own CLAUDE.md states the standing rule directly: a contract change between two launched
processes needs one live run against a throwaway target before it counts as done, and that live
run has not happened for this change (the plan's own Verification section says the same: "the
suite, then one live run against a target with a real board... a stub that agrees by construction
is not evidence that a real process returns the card"). The `card_left_in_review` finding and the
run-end audit exist precisely to catch the case where that live run would have found a problem: if
a real Closeout process ignores or misreads the new instruction, the failure surfaces in the
summary as a finding rather than silently leaving the board wrong.

## Related

- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  is the direct ancestor of this bug's own root cause: the markdown adapter's narrower state
  space already caused five contract defects in an earlier live run, one of which was the same
  family, a brief step telling a markdown-tracked task to move a card to a status markdown has no
  concept of. That fix resolved the Task brief's own tracker-step text per adapter; it could not
  have touched the Closeout side documented here, because the stale "do not transition" sentence
  did not exist yet, it was introduced four days later by the 2026-08-31 plan.
- `docs/solutions/logic-errors/verify-checked-only-one-direction-of-the-landing-tracker-link.md`
  names the same shape one level up: a fact about the world, there a landing, here a card's pre
  run status, that only one code path was ever responsible for restoring, so any other real event
  that changes the same fact goes unrecognized. Its own Prevention rule, ask what else outside the
  codebase could make the represented fact true, is exactly what `audit.py` operationalizes here,
  one plan later: compare the represented fact against ground truth from both directions rather
  than trusting the one path that is supposed to maintain it.
- `docs/plans/2026-08-31-1409-feat-relay-visible-halt-and-early-in-progress-plan.md` names, in its
  own Scope Boundaries section, the accepted residual gap that this fix closes: a card that reads
  `in_review_status` while a retry halts at pre-flight with nothing running. The run-end audit
  covers that case too, since it compares every card against ground truth regardless of which
  code path caused the drift.

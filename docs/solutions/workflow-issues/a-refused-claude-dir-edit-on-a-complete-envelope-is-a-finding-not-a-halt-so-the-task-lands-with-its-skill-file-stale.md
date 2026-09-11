---
title: A denied .claude/ edit on a complete envelope is a finding, not a halt, so the Task lands and the skill file it could not edit goes stale
date: 2026-09-11
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: high
root_cause: missing_workflow_step
resolution_type: documentation_update
related_components: [classify, gitwrite, summary, verify, permission-mode, skill-docs, manifest, task-process]
applies_when:
  - "a Task's change touches a file under .claude/ in the target repo and the rest of the change lives outside .claude/"
  - "a Task process is refused an Edit or Write under .claude/ and still prints an envelope reading status: complete"
  - "reading a run summary's check by hand list, where a path_gate_denial line names a Task whose record reads landed"
  - "deciding whether a run that reported completed with zero halts needs any follow up at all"
  - "reading the path_gate row of the halt class table in skills/relay/SKILL.md and taking No stage as the unfinished work case"
symptoms:
  - "the terminal record reads run_status completed with halt_class null, and 18 of 23 Tasks landed with zero halts"
  - "IW-219 and IW-222 each record status landed, halt_stage None, and verify landed true, while each carries a path_gate finding on .claude/skills/itg-brief/SKILL.md"
  - "the only trace of the denial is a check by hand line reading <task>: in the transcript, <line>"
  - "the merged commits dfbdff6 and 310e68b ship behaviour the skill file the agent reads at runtime no longer describes"
  - "the Manifest comment predicted a path_gate halt for IW-222 and ordered it last to make that halt cheap, and no halt occurred"
tags: [claude-directory-gate, halt-class-vs-finding, dontask-permission-mode, unattended-run, halt-classification, stale-skill-file, summary-check-by-hand, headless-claude]
---

# A denied `.claude/` edit does not halt a Task, it lands a half of one

## Context

On 2026-09-11 a Runner drove `/Users/pgutowski/Documents/PhilAI/relay-runs/iw-workbench-3.toml`
against the IW board, from an extract of native-relay at `c5185d1`, backend `claude`, model opus,
effort high. Twenty three Tasks: eighteen landed, five excluded for reasons unrelated to `.claude/`
(IW-182, IW-216, IW-217, IW-218, IW-229). Zero halts. The terminal record reads
`run_status: completed`, `halt_class: null`, `halt_task: null`.

Two of the eighteen landings are not what they look like. IW-219 and IW-222 each attempted an Edit
on `support-workbench/.claude/skills/itg-brief/SKILL.md` and were refused by the headless `dontAsk`
path gate. Each Task process kept working, finished the part of its change that lives outside
`.claude/`, passed the project gate, merged, and was recorded `landed` with every Verify-landed
check passing. The refusals surface in exactly one place, as `path_gate` lines on the summary's
check by hand list.

The parent doc,
`docs/solutions/workflow-issues/headless-dontask-blocks-claude-dir-edits.md`, owns the gate itself,
the pre flight scan, the merge tail backstop, and the two raiser split from issue #8. Its framing
throughout is that the gate stops a run: "the run halts asking for an approval nobody can give",
and "A write denial means the work is unfinished. The process asked for something the harness would
not give and stopped short of doing it. An attended session has to do the work." That describes one
of two outcomes. This run produced the other, and the parent has no entry for it.

This case had never been considered. Across the four prior sessions that built the gate's tooling
between 2026-09-07 and 2026-09-11, the two walls are always discussed as two raisers filling one
sentence, so the failure under examination is always that both fire and the operator cannot tell
which. The case where neither fires, because the Envelope came back complete, appears nowhere. The
nearest neighbour is issue #14's item 4, which decided the backstop should not try to tell a
permitted `.claude/` change from a refused one, and that decision presumes the change is in the
diff. This is the case where it is not. (session history)

The manifest author expected the first outcome. The comment above the IW-222 Task block reads:

> # Report an errored completeness readout as its own state rather than not_computed. Last on
> # purpose: the agent instruction it has to change also lives in .claude/skills/itg-brief/SKILL.md,
> # which a task process is refused, so this card may halt at the path_gate wall. Last, that halt
> # costs nothing else, and the Python half may still land.

The prediction was a halt, and the mitigation was ordering the card last so the halt would be cheap.
It did not halt. The prediction was wrong in the direction that costs more: a halt leaves the
tracker card open and the operator with a loud line to act on, while a landing closes the card,
reports green, and leaves a merged commit whose agent facing half never shipped. IW-219's manifest
comment names no `.claude/` risk at all, and that card sat mid list.

## Guidance

### The mechanism: three gates, and a `.claude/` denial walks through all of them

**Gate one, classify's promotion.** `contracts.CLAUDE_DIR_PATH_REGEX`
(`skills/relay/scripts/relay/contracts.py:56`) is `re.compile(r"(^|/)\.claude/")`. In the transcript
scan, a denied Edit or Write whose tool input `file_path` matches is promoted from a `denied_tool`
finding to a `path_gate` one:

```python
# classify.py:407
if contracts.CLAUDE_DIR_PATH_REGEX.search(file_path):
    finding["class"] = contracts.HALT_PATH_GATE
    finding["detail"] = contracts.PATH_GATE_CLAUDE_DIR
```

Note what this produces. A finding on the record, carrying a Halt class name. Not a Halt class.

**Gate two, classify's precedence.** The next gate decides whether that finding becomes the record's
class, and for a Task that carried on working it never does. The block at `classify.py:453` to
`classify.py:492` reads the Envelope first:

```python
# classify.py:457
has_path_gate = any(f["class"] == contracts.HALT_PATH_GATE for f in result["findings"])
...
# classify.py:471
elif envelope and envelope["status"] == contracts.ENVELOPE_STATUS_COMPLETE:
    result["routable"] = True
...
# classify.py:484
elif envelope:
    result["halt_class"] = contracts.HALT_PATH_GATE if has_path_gate else contracts.HALT_BLOCKED_ENVELOPE
else:
    result["halt_class"] = contracts.HALT_PATH_GATE if has_path_gate else contracts.HALT_NO_ENVELOPE
```

`has_path_gate` is computed for every Task and read on two of the four branches. The complete
Envelope branch assigns no Halt class at all, sets `routable = True`, and sends the Task to
Verify-landed. So the finding only becomes a class when the Envelope is blocked or absent. A Task
process that swallowed the refusal and printed `status: complete` is routed exactly like a Task that
was never refused anything.

This precedence has no recorded rationale. Its only trace in the sessions that built native mode is
a bulk edit to `classify.py` on 2026-09-07 that rewrote the block during the fork's pipeline
rewrite, with no reasoning captured and no later session revisiting the ordering. On the evidence
available it fell out of that rewrite rather than being chosen with a denial on a complete Envelope
in view. Treat that as absence of evidence rather than proof. (session history)

**Gate three, the merge tail backstop.** `gitwrite.claude_dir_backstop`
(`skills/relay/scripts/relay/gitwrite.py:319`) is the other wall. It reads the same regex against
the Task branch's diff versus the R17 baseline:

```python
paths = gitread.diff_name_only(repo, baseline_sha, branch)
return [path for path in paths if contracts.CLAUDE_DIR_PATH_REGEX.search("/" + path)]
```

It cannot fire here, and the reason is structural rather than incidental. The write was denied, so
nothing under `.claude/` ever landed on the branch, so no `.claude/` path is in the diff. The
backstop exists to refuse a branch that *did* change `.claude/`. A branch that *failed* to change
`.claude/` is invisible to it.

**Why both walls miss the same Task.** They are aimed at opposite halves of one event. The
transcript raiser sees the attempt and hands the class to the Envelope's verdict, which withholds
it. The branch raiser sees only landings and there is no landing to see. Between them sits the exact
case of a refusal the Task process absorbed and worked around, and neither one is watching it.

The last stop is the summary, which reports it and asks for nothing. `summary._pending_checks`
(`summary.py:109`) iterates every entry with no status filter on the findings loop, so a landed
record's `path_gate` finding still emits a check line at `summary.py:166`,
`{"kind": "path_gate_denial", ...}`, rendered as "`<task>: in the transcript, <line>`". The louder
sibling, `path_gate_backstop` at `summary.py:144`, is gated on
`entry["class"] == contracts.HALT_PATH_GATE and entry["halt_stage"] == contracts.TAIL_STAGE_BACKSTOP`
(`summary.py:138`), so it does not fire either. One quiet line on a checklist, under eighteen green
landings, is the entire signal.

### What a manifest author checks before listing such a card

The check is on the card's **scope**, not on its named file list. A card can reach a skill file
without naming one, which is exactly what IW-219 did. Run it mechanically, in this order:

1. Read the card's acceptance criteria and ask whether the change alters an agent facing contract:
   an instruction an agent reads at runtime, a brief, a skill's stated behaviour, a status vocabulary
   the agent acts on. If yes, continue. If the change is purely internal to the code, stop, the card
   is safe.
2. `grep -rn "<the contract's name or the status string>" <repo>/.claude/` for the terms the card
   changes. A hit means the card's real scope spans repo code and the `.claude/` skill package,
   whatever its file list says.
3. On a hit, take one of two routes, and record which:
   - **Exclude.** Mark the Task excluded from unattended runs with the reason naming the skill file
     and the gate. The summary lists the skip and why, so the operator sees a decision instead of a
     mystery.
   - **Split.** Send the source and tests half unattended, and carry the `.claude/` edit in its own
     attended follow up card. The unattended half lands and closes its own card; the attended half
     is minutes of human time on the one file the harness will not let a headless process write.

**Ordering the card last buys nothing.** That mitigation assumes the failure is a halt, and a halt
ordered last costs only the Tasks it would otherwise have stopped. This failure is a landing. A
landing costs the same whether it happens first or twenty third, because nothing downstream of it
changes. The author's ordering was protecting against the wrong outcome: it insured the run, and the
thing at risk was the repo.

### Yes, the halt class documentation needs correcting

`skills/relay/SKILL.md:273`, the `path_gate` row of the halt class table, reads:

> | `path_gate` | one of two walls around `.claude/`, and the record's `halt_stage` says which. No
> stage: the task asked for an edit there and its permission posture refused it whatever the
> allowlist says, so the work is unfinished. Stage `backstop`: the task finished and the merge tail
> refused a branch whose diff touches `.claude/` | read the cause line, which names the repair its
> own raiser implies. Unfinished work needs an attended session to do it, then a resume. A refused
> branch needs an attended gate and merge, then `verify` for that task, never a rerun |

Two things it implies are false. First, "No stage" reads as the shape a refused edit always takes,
when in fact a record reaches "No stage" with class `path_gate` only through `classify.py:485` or
`:487`, that is, only on a blocked or absent Envelope. On a complete Envelope the same refusal never
becomes a class at all, and the row has no entry for that case. Second, the repair advice is wrong
for the landed case. There is nothing to resume, the card is Done, and the missing piece is a follow
up edit on a merged commit.

The row's history makes the gap sharper rather than excusing it. This row is not an old one nobody
revisited. It was written by issue #8 itself, in commit `762e5c9`, which replaced a single sentence
saying the posture refuses the edit with the two raiser text quoted above. The session that
understood the two walls best is the one that wrote a row with no third case, because at that moment
the third case had not been observed: every incident in hand, IW-83, IW-179 and T-70, was a Task
that stopped. A table built from the incidents available will have exactly the branches those
incidents produced, and no more. This is the first run to produce the branch where neither wall
fires.

Proposed replacement, liftable as written:

> | `path_gate` | one of two walls around `.claude/`, and the record's `halt_stage` says which. The
> class is not the only outcome of a refusal: where the Task's Envelope reads complete, the same
> refused edit stays a finding, the Task lands, and only the summary's check by hand list reports
> it. No stage: the Task asked for an edit there, its Envelope was blocked or absent, and its
> permission posture refused the edit whatever the allowlist says, so the work is unfinished. Stage
> `backstop`: the Task finished and the merge tail refused a branch whose diff touches `.claude/` |
> read the cause line, which names the repair its own raiser implies. Unfinished work needs an
> attended session to do it, then a resume. A refused branch needs an attended gate and merge, then
> `verify` for that Task, never a rerun. A landed Task carrying a `path_gate` finding needs neither:
> the edit that never landed is a follow up on a merged commit |

**This is a documentation fix, not a new Halt class.** KTD6 keeps the set closed, and this outcome
is already a finding attached to a record, which is precisely the form CONCEPTS.md says a cause
outside the closed set takes. The code is behaving as designed at all three gates. What was missing
is a sentence saying so.

## Why This Matters

A halt is loud and a finding is quiet, and the operator's attention is the resource being spent.
Eighteen green landings, a `run_status` of `completed`, and one `path_gate_denial` line on a check
by hand list is a signal to noise problem before it is anything else. The summary's own shape works
against the reader here: the line sits in a list of housekeeping items beside stranded branches and
skipped reviews, in a run with no halt to draw the eye.

Set that against the parent doc's failure. IW-83 spent roughly an hour of a high effort Opus
process, produced eight good commits and three green gate runs, and landed none of it. That is
expensive and visible. It announced itself. A human paid an hour and then did the two paragraph edit
by hand, and the repo ended consistent.

This one is cheap and invisible, and it is worse. The run cost nothing extra. The card closed. The
board is clean. What was left behind is a repo whose runtime agent instructions disagreed with its
own merged code: `support-workbench`'s `.claude/skills/itg-brief/SKILL.md` was stale against both
`dfbdff6` and `310e68b`, so the instruction the agent read at runtime no longer matched the server
behaviour those commits shipped. Nothing in git, on the board, or in the summary's headline said so.
An agent reading that skill file would have acted on it and been wrong, with nothing flagging it.

That gap was closed by hand about an hour after the run, in support-workbench `8aa772e`, merged at
`b1a2e08`, which taught the packaged skill the new error status and added two tests pinning the
package to the server's instructions so the pair cannot drift apart again. It was filed as its own
pair of cards, IW-233 and IW-232, which is the split route this doc's Guidance prescribes, and its
commit message names the cause independently: a relay task process cannot edit files under
`.claude/`. So the repair is evidenced rather than hypothetical. What is worth holding onto is that
it happened because a human read the check by hand list, not because anything stopped.

Relay's premise is that a Task is either Landed or halted with a stated reason, and never silently
half done. A Landed Task with an unlanded half is a third state the premise did not name. Verify-landed
did its job exactly as specified: it read git and the Tracker, found a merge commit and a closed
card, and said Landed. It is right. Landing is simply not the same claim as completeness, and this
is the case that separates them.

One methodological note that belongs with the finding. Halt class routing in this runner has now
been mis-predicted from reading `classify.py` twice, in two separate sittings, and both times a real
run corrected the code read. This learning is on the right side of that pattern: it is written from
a live run's recorded state rather than from the precedence block alone, and the branch point in the
Examples below is the observation, not the inference. Any future claim about what classify does or
does not promote deserves the same treatment. (session history)

## When to Apply

Expect this whenever a card's change **spans repo code and the `.claude/` skill package**, which is
not the same as a card that names a `.claude/` path. The naming case is what the pre flight scan
already catches. The spanning case is the one that gets through, because the plan can be entirely
about source paths while the work is not.

The tell is a property of the project, not of the card: **the project keeps agent facing contract
text in a skill file**. Where that is true, a contract change and a skill edit arrive together as a
matter of course, and the agent doing the work will find the skill file on its own even when the
card never mentions it. support-workbench is such a project. `itg-brief/SKILL.md` carries the
instruction the support agent acts on, so any card that changes what the agent should do reaches it.
Treat the skill file as in scope by default on those cards and route accordingly.

Three further conditions that put a card in range:

- The card changes a status vocabulary, a state name, or a readout the agent is told to interpret.
  The instruction naming those values lives in the skill file by construction.
- The card's acceptance criteria include "the agent should now do X instead of Y". That is a skill
  edit wearing a code change's clothes.
- The repo has an existing learning saying an instruction is duplicated between code and a skill
  file. That duplication is the mechanism, and a card touching either copy touches both.

**For the run that already happened.** The check by hand line is real work, not noise to dismiss.
The repair is a follow up edit on a merged commit, applied attended. It is not a resume, because
nothing halted and there is no state to resume from. It is not a rerun, because the Task's own work
landed correctly and rerunning it would rebuild what already exists. Read the summary's
`path_gate_denial` lines after every run, open the named file, and diff it against what the landing
commit actually shipped.

## Examples

Every commit named in this section is a support-workbench commit, not a native-relay one. The
Runner's state for the run is in
`/Users/pgutowski/.relay/8a76fcc68f4487a547ed17a4c551b7e02d0556834a64bb39d8fd3f0db2a82870`, and
every field below is read from its `state.json` or from the Task's own session transcript.

### IW-219, unpredicted, mid list

The card's manifest comment names no `.claude/` risk. The Task's change reached
`.claude/skills/itg-brief/SKILL.md` anyway, through the agent facing half of its own scope. The
transcript holds **one** denied Edit on that path, at transcript line 209.

| Field | Value |
|---|---|
| status | `landed` |
| halt_class | `landed` |
| halt_stage | None |
| closeout | `complete` |
| landing_ref | `dfbdff6503379b126b16f6368b65dbda4b0ffa62` |
| commits since baseline | 4 |
| verify | `landed: true`, with `card_terminal`, `closing_reference`, `new_commit_since_baseline`, `on_default`, `tree_clean` all `pass` |
| findings | one `path_gate`, tool Edit, target `.../support-workbench/.claude/skills/itg-brief/SKILL.md` |

Left stale in support-workbench: the skill file did not carry the instruction change that
`dfbdff6`'s code half implies. Repaired attended in `8aa772e`.

### IW-222, predicted to halt, ordered last, landed anyway

The card asked for an errored completeness readout to be reported as its own state rather than as
`not_computed`. The agent instruction for reading that state lives in the same skill file. The
transcript holds **two** denied Edits on it, at lines 235 and 237.

| Field | Value |
|---|---|
| status | `landed` |
| halt_class | `landed` |
| halt_stage | None |
| closeout | `skipped` |
| landing_ref | `310e68bfac0cd14719e2bb50489d897dab0dfb93` |
| commits since baseline | 3 |
| verify | `landed: true`, same five checks `pass` |
| findings | two `path_gate`, both Edit, same target file |

Left stale in support-workbench: until the follow up landed, the agent still read the old
instruction and would treat an errored readout as `not_computed`, which is the exact behaviour
`310e68b` shipped code to end. Repaired attended in `8aa772e`.

Worth noting on both records: the verify evidence block carries `"findings": []` even though the
record's own findings list is not empty. Verify does not restate findings, so reading the verify
block alone tells you nothing happened.

### The same denial on a blocked Envelope, which does become the class

The parent doc's IW-83 is the branch point. Same posture, same gate, same file. There the Task
process obeyed the denial's instruction to stop and explain, asked twice for an approval nobody
could give, and never printed a complete Envelope. With the Envelope blocked, `classify.py:485`
reads `has_path_gate` and assigns `HALT_PATH_GATE`. The record halts, the Cause line states the
repair, the summary puts it at the top, and the branch sits unmerged until a human acts.

One refusal, two records, and the only thing that differs is what the Task process printed at the
end. A process that stops gets a Halt class and an operator's attention. A process that shrugs and
carries on gets a checklist line. Nothing in the Runner distinguishes conscientious from
accommodating, so the outcome is decided by the Task process's own disposition toward a refusal.

## Related

In this repo, native-relay, which is the **Runner** side of the line:

- `docs/solutions/workflow-issues/headless-dontask-blocks-claude-dir-edits.md`, the parent. It owns
  the gate itself, the pre flight scan, the backstop, and the two raiser split from issue #8. This
  doc corrects its framing only: the write denial it documents has a second outcome, a landing
  rather than a stop. Its repair bullet and its disambiguation rule, which reads a complete envelope
  as proof of a backstop refusal, both need a third branch.
- `docs/solutions/workflow-issues/on-halt-continue-past-task-halt-is-not-the-quota-switch-and-the-path-a-quota-death-takes-decides-whether-the-manifest-votes.md`,
  which owns the general case of a class reaching a record by two routes and the route deciding the
  repair. This doc is the case where one route assigns no class at all.
- `docs/solutions/workflow-issues/quota-death-has-a-fourth-shape-partial-landing-and-it-is-the-only-one-that-leaves-code-on-the-default-branch.md`,
  the other member of the family where a landing overstates itself. There the record's own checks
  disagree and the summary says so. Here every signal reads clean.
- `docs/solutions/workflow-issues/task-card-blocking-condition-is-a-snapshot-not-a-live-fact.md`,
  which owns manifest authoring time claims going stale against the tree. This doc is a prediction
  that was wrong when it was written rather than stale, and about the run's behaviour rather than
  the repo's state.

In support-workbench, which is the **product** repo and not this one, under
`docs/solutions/architecture-patterns/`:

- `one-instruction-duplicated-into-a-skill-file-goes-stale-outside-relay-reach.md`, written by a
  Task on this very run. It owns the product side statement of the duplication: why the instruction
  lives in two places and what that costs the product.
- `a-status-the-agent-acts-on-must-separate-never-ran-from-failed.md`, written by IW-222 itself. It
  owns the status vocabulary change whose skill half never landed.
- `a-headless-relay-session-cannot-edit-its-own-permission-gates.md`, the IW-179 learning at
  support-workbench `6563bc4`. It owns the wall from the Task's side, including the case where a
  `.claude/` write succeeds and is denied later in the same run.

Those three are about the product repo and its skill package. This one is about the Runner and what
it records. The distinction matters when deciding where a repair goes: a stale skill file is fixed
in support-workbench, and a summary that fails to raise the alarm is fixed here.

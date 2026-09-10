---
title: No Push Shipping - Plan
type: feat
date: 2026-09-10
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: conversation 2026-09-10, issue #15
execution: code
---

# No Push Shipping - Plan

## Goal Capsule

- **Objective:** a manifest can ask for every task to merge to the default branch locally and
  never push, so an unattended run stays on the machine and the operator decides later, by hand,
  what reaches the remote.
- **Means:** one boolean, `[shipping] push`, default true (KTD1). Every site that pushes, fetches,
  or compares against `origin` reads it through one of three named seams (KTD2), and the read
  site table below is the enumeration that stops a site being missed.
- **Authority:** the R-IDs win on behavior, the KTDs on mechanism.
- **Execution profile:** seven units on one feature branch, the full suite, then one live task
  against a throwaway target with a local bare origin, because the markdown adapter's closeout
  instruction and the tail's ending change.
- **Stop conditions:** stop if the feature needs a new halt class (it must not, KTD6 of the outer
  loop plan), or if a no push run can reach any `git push` by any route.

## Product Contract

### Problem Frame

On 2026-09-10 a fifteen task sweep against the Integra Workbench was held back because the
operator asked for a run that pushed nothing. `local_merge` is merge and push welded together in
`gitwrite.local_merge_tail`, and `pr_terminal` is refused by validate. The workaround available
on the day was pointing the target's `origin` at a bare clone on disk for the length of the run,
which mutates repo config to get a behaviour the manifest should be able to ask for.

Pushing is not the only thing that assumes a remote. Pre flight, the resume disposition, and the
halt comment guard all require the local default branch to equal `origin/<default>`, so under no
push every task after the first is refused, because local main legitimately runs ahead. The final
verify fetches and compares against the remote. The markdown adapter reads the tracker file at
`origin/<default>`, which a closeout commit never reaches without a push. And the Task process's
disallow list refuses force pushes but not a plain push of its own branch.

### Requirements

- R1. `[shipping] push` is a boolean, default true, and a defaulted value is named in
  `defaults_applied` like every other default. Any other type is refused by validate.
- R2. With push false, no process Relay launches and no call the runner makes pushes anything:
  not the merge, not the closeout commit, not a mirror, and not a Task process pushing its own
  branch.
- R3. With push false, validate does not require an `origin` remote, and a repo with no remote
  configured runs end to end. The existing rule that an unset `default_branch` needs
  `refs/remotes/origin/HEAD` still applies, so such a repo names its default branch.
- R4. With push false, pre flight, the resume disposition, and the halt comment guard accept a
  local default branch that is equal to or ahead of `origin/<default>`, and refuse one that is
  behind it or has diverged from it. With no `origin`, or no `origin/<default>` ref, they accept.
- R5. With push false, the tail refuses to merge when the local default branch moved during the
  task, and when a best effort fetch shows `origin/<default>` has diverged from the baseline.
  Both refusals are `remote_advanced` with evidence saying which. No new halt class.
- R6. With push false, Verify-landed decides from local git and the tracker. `head_equals_remote`
  is a non blocking skip carrying its reason and both shas, the way `new_commit_since_baseline`
  records its skip under `pr_terminal`, and no verify fetches.
- R7. With push false, the markdown adapter reads the tracker file at the local default branch
  head, never the working tree.
- R8. A manifest with push false and a non empty `project.mirror` is refused by validate.
- R9. The summary of a push false run says plainly, as its last check by hand, that nothing was
  pushed, and names the single command that would ship the default branch. Its JSON carries the
  same facts under `shipping`.
- R10. With push true, behaviour is unchanged. Every existing test passes unedited except where a
  test fake grows the new attribute.
- R11. The operator facing docs, `skills/relay/SKILL.md`, `docs/manifest-authoring.md`, the three
  examples, and `CONCEPTS.md`, say what push does, and the skill asks about it when it authors a
  manifest.

### Success Criteria

- A three task stub run with push false lands two and blocks one, the bare origin's refs are
  byte for byte what they were before the run, the store's git op log holds no `push` or
  `mirror_push`, and the summary ends on the unpushed line.
- The same run against a repo with no remote at all completes.
- One live task on `sonnet` against a throwaway target with a local bare origin and push false
  lands, closes its tracker line on local main, and leaves the bare origin untouched.

## Key Technical Decisions

### KTD1. A boolean under `[shipping]`, not a third mode

A third value in `SHIPPING_MODES` reads well, but every existing `shipping_mode == "local_merge"`
comparison would become a place a new value is silently missed, which is the shape
`docs/solutions/logic-errors/continue-past-halt-checked-general-state-blind-to-the-branch-its-own-skip-left.md`
records. With a boolean those comparisons stay correct as written: a no push run is still a local
merge, the in review status is still required, the markdown warning still holds, and the brief is
still the local merge brief. The cost moves to the other side: a site that pushes or reads
`origin` and forgets the flag. The read site table below is the enumeration of those, and the end
to end test asserts on the git op log, so a forgotten push fails a test rather than a run.

`Manifest.shipping_push` is the field, appended after the existing defaulted fields so no
positional constructor moves.

### KTD2. Three seams carry the flag

- `gitwrite.default_in_sync(repo, default, push, evidence)` returns `(ok, check_name)`. Push true
  is today's `head_equals_remote`, unchanged, and names that check. Push false is
  `remote_is_ancestor`: pass when `origin/<default>` does not resolve (no remote, or never
  fetched, so there is no known remote state to have diverged from), else pass when it is an
  ancestor of the local default through `git merge-base --is-ancestor`. Pre flight, the resume
  disposition, and `run._note_halt` all call it, so the three cannot disagree.
- `gitwrite.local_merge_tail(..., push=True)` branches once where the fetch sits today, and ends
  at the merge under push false with stage `merged` and `pushed: false` in its evidence.
- `verify.verify` reads `manifest.shipping_push` itself, so every caller, the run loop, startup
  re-verify, and the `verify` verb, gets the same verdict without passing anything.

### KTD3. The tail under push false still looks at the remote, best effort

T-5 made the final verify fetch so a landing is judged against the remote's real state. With no
push the landing is the local default branch, so the remote cannot decide it, but a remote that
diverged under the run is still worth stopping for: every later task would pile onto a base the
operator must reconcile before the push they plan to make. So the tail fetches when an `origin`
exists and ignores a fetch that fails, since an offline laptop is a legitimate place for a no push
run. The ancestor check then reads whatever `origin/<default>` the repo knows. A fetch writes only
remote tracking refs; nothing leaves the machine.

The tail also refuses when the local default branch no longer sits at the baseline, which is the
direct analog of the remote moving: under no push the local default is the landing target, and a
concurrent session committing to it is exactly what the working tree collision of 2026-09-10
looked like.

### KTD4. Mirror and push false are refused together

A mirror is a push by another name. Validate refuses the pair (R8) rather than skipping the mirror
silently, and the run loop also gates the mirror push on the flag, so a manifest built by hand
past validate still cannot push.

### KTD5. The Task process gets the closeout's push refusal under push false

`contracts.CLOSEOUT_DISALLOWED_EXTRA` refuses every push spelling for the closeout. Under push
false `manifest.resolved_disallowed` adds the same two patterns to the Task process's list, with
validate's usual "was missing; added" warning, so R2 holds at the permission layer and not only in
the brief's instructions.

### KTD6. The `remote_advanced` Cause line names both movers

The template read "remote moved during the task". Under push false the mover can be the local
default branch, and `run._merge_route` raises with the Cause line as its message, so the raw
sentence would never print beside it. The template becomes "the default branch moved during the
task, locally or at the remote; merge aborted at {sha}", which stays true under both settings,
and the evidence carries a `reason` naming which one it was.

### KTD7. Brief templates do not change

Both templates tell the process not to push, which stays true. The Task brief's "the runner owns
the gate, the merge, and the push" overstates the runner under push false and harms nothing, and
leaving the templates alone keeps this change out of the envelope and terminal line contracts.
The markdown adapter's closeout instruction does change (its "the runner pushes it under the gate"
would be false), which is the reason for the live run.

## Read Site Table

Every read of the shipping mode and every site that pushes, fetches, or compares against
`origin`, at HEAD `069d808`, with what each does under both settings.

| Site | Push true | Push false |
| --- | --- | --- |
| `manifest.py:417` mode membership | unchanged | unchanged |
| `manifest.py:419` `pr_terminal` refusal | unchanged | unchanged |
| `manifest.py:439` in review status required under `local_merge` | unchanged | unchanged, a no push run is a local merge |
| `manifest.py:441` markdown warning under `local_merge` | unchanged | unchanged; wording made remote neutral |
| `manifest.py:537` origin required | required | not required (R3) |
| `manifest.py` mirror rule | unchanged | refused with a mirror (R8) |
| `manifest.resolved_disallowed` | unchanged | adds the two push spellings (KTD5) |
| `run.py:761` unimplemented mode backstop | unchanged | unchanged |
| `run.py:554` pre flight | `head_equals_remote` | `remote_is_ancestor` (KTD2) |
| `run.py:400` resume disposition | `head_equals_remote` | `remote_is_ancestor` |
| `run.py:1009` halt comment guard | `head_equals_remote` | `remote_is_ancestor` |
| `gitwrite.py:376` tail fetch | fetch, raise on failure | fetch when origin exists, ignore failure (KTD3) |
| `gitwrite.py:377` tail remote compare | `origin/<default>` equals baseline | local default equals baseline, `origin/<default>` ancestor of it |
| `gitwrite.py:400` tail push | pushes | never reached, stage `merged` |
| `run.py:825` mirror push | when a mirror is set | never |
| `run.py:837` final verify fetch | fetches | verify does not fetch |
| `run.py:948` closeout push | when the closeout committed | never |
| `cli.py:406` `verify` verb fetch | fetches | verify does not fetch |
| `cli.py:133` validate line | unchanged | says push off |
| `verify.py:150` `head_equals_remote` | pass or fail | non blocking skip with reason (R6) |
| `verify.py:159`, `:177` `pr_terminal` branches | unchanged | unchanged |
| `verify.py:201` `mirror_equals_head` | unchanged | unchanged, no mirror under push false |
| `brief.py:42`, `:235` template by mode | unchanged | unchanged (KTD7) |
| `adapters/markdown.py:81` tracker read | `origin/<default>` | `<default>` (R7) |
| `adapters/markdown.py:159` closeout instruction | unchanged | drops "the runner pushes it" |
| `contracts.py:315` closeout push refusal | unchanged | unchanged, and joins the Task list |
| `audit.py:54` local head read | unchanged | unchanged |
| `verify.default_branch_of` origin HEAD read | unchanged | unchanged; no origin needs `default_branch` set |
| `summary.build` | unchanged | `shipping` block and the unpushed check (R9) |

## Implementation Units

### U1 Manifest

`Manifest.shipping_push`, loaded through `pick` so the default is named. Validate: push must be a
bool; the origin rule applies only under push true; a mirror with push false is refused;
`resolved_disallowed` carries the push spellings under push false; the markdown warning's wording
drops "remote". Tests in `test_manifest.py`: the default and its name, false loads, a string is
refused, a mirror with false is refused, false validates against a repo with no origin while true
does not, and the disallow list gains the patterns only under false.

### U2 Git seams

`gitread.is_ancestor`. `gitwrite.default_in_sync`, `preflight(..., push=True)`,
`resume_disposition(..., push=True)`, `fetch(..., check=True)`, and the tail's push false branch.
Tests in `test_gitwrite.py`: pre flight under push false across no origin, a remote behind the
local default, an equal remote, a diverged remote, and a local default behind the remote; the tail
merging without a push op and leaving the bare origin untouched; the tail refusing a moved local
default and a diverged remote; the tail landing with no origin and with an unreachable one; the
resume disposition accepting a default ahead of the remote.

### U3 Verify and the markdown adapter

Verify's push false skip and no fetch; the adapter's ref and instruction. Tests in
`test_verify.py` and `test_adapters.py`.

### U4 Run loop

Pass the flag to pre flight, the resume disposition, the tail, and the halt comment guard; skip the
closeout push and the mirror under push false. Tests in `test_run.py`: the three task end to end
with the op log and origin assertions, the same with no remote, a halted task continued past under
push false, and the Task process's argv carrying the push refusal.

### U5 Summary and CLI

`summary.build` adds `shipping` and, under push false, the last pending check. `cli` validate says
push off. Tests in `test_summary.py` and `test_cli.py`.

### U6 Documentation

`SKILL.md` asks about push in the interview and says what launching will do; `manifest-authoring.md`
section 1 and 3; the three examples carry `push = true` with a comment; `CONCEPTS.md` Shipping mode
and the continue past sentence; `README.md` where it says the runner pushes; `contracts.py`
comments; the `remote_advanced` Cause line and the SKILL halt table row.

### U7 Live run

A throwaway repo under the scratchpad with one module, a unittest gate, a local bare origin, and
`tasks.md` holding one task; a markdown manifest with push false and one `sonnet` task. Run in the
foreground, then `summary`. Confirm the merge landed on local main, the closeout closed the line
on local main, the bare origin's `main` is the sha it held before the run, and the summary ends on
the unpushed line.

## Verification

- `python3 -m unittest discover -s tests` green from the repo root after each unit.
- U7's summary output goes in the handoff.

## Out of scope

`pr_terminal`, which stays refused. Changing either brief template. Pushing on the operator's
behalf at the end of a run. A no push variant of the mirror.

## Amendments made while executing, 2026-09-10

- The gitwrite keyword is `pushes`, not `push`. A `push` parameter on `local_merge_tail`
  shadowed the module's own `push()` function, and every push true tail call raised
  `TypeError: 'bool' object is not callable`. `manifest.pushes()` is the one reader of the field;
  `preflight`, `resume_disposition`, `default_in_sync`, and `local_merge_tail` take `pushes=`.
- Built in a separate worktree off `069d808` because a concurrent session held uncommitted work
  in the same modules. Issue #8 landed on main meanwhile (`674accb`) and merged into this branch
  without conflicts. Its `halt_stage` now carries this plan's two new tail stages, so a no push
  refusal prints "refused at the baseline step" or "refused at the fetch step" under its Cause
  line.
- R9 narrowed. The unpushed line names the command only when a task has landed; with nothing
  landed it says there is nothing to ship. The first live attempt halted before its merge and the
  line still told the operator to ship "every landing above".
- The first live attempt halted `unclean_exit` at the gate step because the throwaway target had
  no `.gitignore` and the runner's own gate wrote `__pycache__/`. A target defect, not this
  feature's: the bare origin was untouched and the refusal was the right one.
- Two existing `ContinuePastGuards` fakes of `resume_disposition` grew the `pushes` keyword, and
  the summary test fake grew `shipping_push`. No other existing test changed.
- The live run (U7), after the target gained a `.gitignore`, landed in 37 seconds on `sonnet`
  with local main already one commit ahead of origin: plan, build, `/code-review` through the
  Skill tool, the target's own suite, gate, merge `6fec1c1`, closeout closing the `tasks.md` line
  on local main. The bare origin's refs matched the pre run snapshot byte for byte, the git op
  log held `checkout`, `delete_branch`, `fetch`, and `merge` and no push, and the summary ended on
  the unpushed line naming `git -C <target> push origin main`.
- The same run showed a landed record still printing the first attempt's halt message ("left the
  tree dirty on relay/T-1"): the running upsert resets `halt_class` and `halt_stage` but not
  `halt_message`. Present on main before this work and unrelated to pushing, so it is captured
  in `docs/backlog.md` rather than fixed here.

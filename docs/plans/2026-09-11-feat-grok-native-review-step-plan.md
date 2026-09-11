---
title: Grok Native Review Step - Plan
type: feat
date: 2026-09-11
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: conversation 2026-09-11
execution: code
---

# Grok Native Review Step - Plan

## Goal Capsule

- **Objective:** an operator can name `grok` as a Task backend in native mode, and a headless
  grok Task process can follow the native brief's Review step on a built in skill this CLI
  actually reaches. The record for that Task says something true about whether review ran:
  either a real detection, or `review_skipped` listed as undetectable, never a false skip and
  never a silent claim that it ran.
- **Means:** re-pin grok against 1.0.25, set `review_skill` to `review` (KTD1), leave
  `REVIEW_SKIPPED` undetectable (KTD2), lift the validate refusal for grok only, and prove it
  with one live Task against the proof target (KTD6).
- **Authority:** the R-IDs win on behavior, the KTDs on mechanism. The native mode plan of
  2026-09-07 still governs the closed halt class set and the Claude pin. This plan is the lift
  that plan named, for grok only.
- **Execution profile:** one feature branch `native/grok-native-review-step`, units in order,
  full suite, then the live grok Task as a gate on done.
- **Stop conditions:** stop if the lift needs a new halt class, if Codex's pin or refusal
  would have to move, if detection would have to invent a Skill event grok does not emit, or
  if the live run cannot land through plan, build, review, verify, record, closeout.

## Product Contract

### Problem Frame

`manifest.py` refuses any Task whose backend has `review_skill` of `None`, and
`contracts.BACKEND_PINS["grok"]` still sets that to `None`. The pin was observed on grok 1.0.5
and re-confirmed on 1.0.13. The installed binary on 2026-09-11 is grok 1.0.25
(`grok 1.0.25 (f7e67d6988e2) [stable]`). `grok inspect` lists a bundled `code-review` skill
with no name collision, plus a bundled `review` skill, plus the operator's global `CLAUDE.md`
and 79 user skills. The stated reason for the refusal has expired. What replaced it is not
"grok now has Claude's Skill tool". It is a different built in review, reached a different way,
and the record has to tell the truth about that.

### Requirements

- R1. `contracts.BACKEND_PINS["grok"]` `version_tested` is `1.0.25`, with
  `version_output_sample` taken from `grok --version` on this machine. Every historical finding
  stays, attributed to the version it was seen on, and every re-observation is dated.
- R2. `review_skill` on grok is `"review"`. The brief's review step therefore names `/review`.
  It is not `"code-review"`. P1 showed a headless `grok -p` under `--permission-mode auto`
  whose prompt named `/code-review` did not read the bundled `code-review` skill. That skill
  carries `disable-model-invocation: true` and was not injected. The process read
  `~/.grok/bundled/skills/review/SKILL.md` and ran that skill to completion.
- R3. `REVIEW_SKIPPED` stays in grok's `_UNDETECTABLE`. P2 found no structured skill event.
  The `read_file` of the skill directory proves a read, not a run. The `subagent_spawned`
  description `[reviewer] branch feat/probe` is a convention inside `/review`. Neither is
  sound enough to teach `classify.review_ran`. The digest lists `review_skipped` as not
  checked, which is the true statement.
- R4. `classify` does not attach a `review_skipped` finding when that class is in
  `evidence.undetectable`. Today the finding fires on `review_skill and not reviewed`, and
  grok is saved only because `review_skill` is `None`. Setting R2 without this gate would
  mark every complete grok Task as skipped.
- R5. The grok brief names `/review` in the rule and in the numbered step. It does not
  promise that a missing call is reported to the operator, because R3 makes that promise
  false. Claude's brief is unchanged and still promises the report.
- R6. `validate` accepts a Task whose backend is `grok` and whose model is one grok serves.
  It still refuses `codex` for the missing review step. The error sentence no longer says
  native mode runs on claude only.
- R7. `known_models` on grok is refreshed to the names `grok models` printed on 2026-09-11:
  `grok-4.6` and `grok-4.5`. The check stays negative.
- R8. Pins that still hold on 1.0.25 stay, with a dated re-observation: `permission_mode`
  remains `auto`; `dontAsk` remains forbidden; `--deny` with a `Bash(glob)` rule still
  refuses and the marker still contains `Denied by permission policy`; `-s` still chooses
  the session id; evidence is still
  `~/.grok/sessions/<url-encoded-realpath-cwd>/<session-id>/updates.jsonl`; the final
  message still reads from `agent_message_chunk`.
- R9. `--allow` with Claude tool vocabulary (`Bash`, `Read`, `Edit`, `Write`, `Skill`) is
  accepted at launch and does not grant grok's real tools. `--deny run_terminal_command` is
  accepted at launch and does not refuse `run_terminal_command`. The working deny form
  remains `Bash(glob)`. The pin comment says this, not only that the flag is accepted.
- R10. `commit_message_constraint` stays on grok. A 1.0.25 single turn probe executed the
  heredoc commit form, so the 1.0.13 cancellation is no longer the whole story. The
  constraint stays because the original finding was a multi turn Task, and because grok now
  loads the operator's skill catalogue, including `ce-commit-push-pr`, which still teaches
  that form. The wording is strengthened to say that any skill or guide the process reads
  showing a worked heredoc commit defeats the brief.
- R11. Docs that say native mode runs on claude only are rewritten: `CLAUDE.md`,
  `CONCEPTS.md`, `README.md` Backends, `docs/manifest-authoring.md`, `skills/relay/SKILL.md`,
  `skills/relay/references/backend-rubric.md`. Codex remains refused. The runner still never
  writes to a tracker.
- R12. One live grok Task lands through the real pipeline against
  `~/Documents/PhilAI/relay-proof/` before this is done: plan, build, review, verify,
  record, closeout, landing. A sibling manifest names grok as the backend with a real grok
  model and a real effort. The stub cannot produce what a real process produces.

### Success Criteria

- `validate` of a grok Task with `model = "grok-4.6"` exits 0 against a real temp repo.
- `validate` of a codex Task still exits 1 naming the missing review step, and does not say
  claude only.
- A complete grok fixture digest lists `review_skipped` under `undetectable` and does not
  attach a `review_skipped` finding.
- The grok brief contains `/review` and does not contain "reported to the operator".
- The live grok Task's state directory exists, its halt class is empty or names a landing,
  and its digest's `undetectable` list includes `review_skipped`.
- `python3 -m unittest discover -s tests` is green.

### Scope Boundaries

- Codex stays refused. Its pin, its `review_skill`, and its docs stay except where a shared
  sentence currently says claude only and has to name grok as well.
- No new halt class.
- No change to `pr_terminal`, either lease, merge, or push ownership.
- No parallel builds, no worktrees, no `max_concurrent_builds`.
- No GitHub issue, comment, or pull request. Phillip files those.
- Probe captures under `/tmp` and `~/.grok/sessions/` are evidence for this plan. They are
  not committed. A new fixture is committed only if it can be scrubbed with confidence.

## Planning Contract

### Probe record, grok 1.0.25, 2026-09-11

All probes ran as nested `grok -p` with `GROK_AGENT` and `GROK_SESSION_ID` stripped, unique
`-s` uuid4 values, `--permission-mode` as named, `--model grok-4.6`, `--effort low`,
`--output-format streaming-json`, `--verbatim`, `--no-plan`. Scratch repos lived under
`/tmp/relay-grok-probe-20260911/`. Session files landed at
`~/.grok/sessions/%2Fprivate%2Ftmp%2Frelay-grok-probe-20260911%2F<p>/<uuid>/updates.jsonl`
because macOS `/tmp` realpaths to `/private/tmp`. The runner already realpaths cwd, so the
evidence pin still holds for a real repo.

P1, session `c82ed10e-44a8-415e-b7cb-bfb059b25815`. Prompt named `/code-review`. First tool
call was `read_file` of `/Users/pgutowski/.grok/bundled/skills/review/SKILL.md`. Never read
`.../bundled/skills/code-review/SKILL.md`. Spawned a subagent described
`[reviewer] branch feat/probe`. Wrote a review file. Final sentence claimed it reached
"the bundled code-review skill" at the `/review` path. `grok inspect` in that cwd listed
`code-review` as bundled with no collision, and `review` as bundled separately.
`/imagine` and `/relay` still collide and get namespaced. `code-review` has
`disable-model-invocation: true`. Headless grok does not inject a named slash skill into
the prompt. The model picks a skill and reads its `SKILL.md`.

P2, same capture. Distinct `sessionUpdate` values: `hook_execution`, `user_message_chunk`,
`agent_thought_chunk`, `agent_message_chunk`, `tool_call`, `tool_call_update`,
`subagent_spawned`, `subagent_finished`, `turn_completed`. No skill event. Route 1 is
absent. Route 2 exists for `/review` as a `read_file` of that skill's directory. Route 3
exists as `subagent_spawned` with description `[reviewer] ...`. Fourth answer: none of
these is a sound run signal. Take the undetectable list.

P3, session `89074da0-d3d0-47cd-94b9-0578e8e4ada5`, `--permission-mode dontAsk`. Tool
`write` failed with `User cancelled the execution for tool \`write\``. `p3-wrote.txt` was
not created. The 1.0.5 finding holds on 1.0.25, and it is not limited to
`run_terminal_command`.

P4, session `1a928b01-26c5-4f6d-865a-d28ae79f014f`. The exact command
`git commit -m "$(cat <<'EOF'\nprobe p4\nEOF\n)"` completed with exit 0. Commit `a69fdfe`
landed. The 1.0.13 pin said a trivial `-p` probe does not reproduce the cancellation. This
probe is that trivial case, and it ran. Keep the constraint (R10). Do not drop it on the
strength of this probe.

P5 deny, session `57572297-a629-41fe-8b5c-ae184c951bb2`, `--deny 'Bash(rm -rf*)'`. Body:
`Tool \`run_terminal_command\` was not executed: Denied by permission policy: deny rule on
bash matching "rm -rf*"`. Canary file remained. `_DENIAL_MARKER` still matches as a
substring.

P5 allow. `--allow Bash --allow Read --allow Edit --allow Write --allow Skill` accepted at
launch. `--deny run_terminal_command` accepted at launch. A later probe with only
`--deny run_terminal_command` still ran `echo hello > p5c-wrote.txt` to completion. Claude
tool vocabulary does not grant grok tools, and a bare deny of the real tool name does not
refuse it. `Bash(glob)` still does.

P6. `-s <uuid4>` chose the session. Evidence path still holds with url encoded realpath cwd
and `updates.jsonl` inside it.

P7. Final message still assembled from `agent_message_chunk` events. Stdout still carries
`text` tokens that `normalize_stream` already knows. The return envelope path is intact.

P8. `grok models` printed `grok-4.6` (default) and `grok-4.5`. The pin's
`grok-4`, `grok-4-fast`, `grok-code-fast-1` are gone from the listing.

Operator context. `grok inspect` loaded `/Users/pgutowski/.claude/Claude.md` (global,
about 4401 tokens) and 79 user skills, plus the compound-engineering plugin.
`ce-commit-push-pr/references/commit-and-push.md` still shows
`git commit -m "$(cat <<'EOF' ... EOF)"`. The 1.0.13 warning that any skill showing that
form defeats the brief is now the common case, not an edge.

The 1.0.5 fixture `tests/fixtures/backends/grok/session-transcript-complete.jsonl` still
shows skill use as `read_file` of a `SKILL.md` plus `subagent_spawned`. That shape did not
grow a Skill tool on 1.0.25.

### Key Technical Decisions

- KTD1. **`review_skill` is `review`, not `code-review`.** The brief names the skill a
  headless process actually reaches. `/code-review` on this CLI is a maintainability audit
  with `disable-model-invocation: true`. `/review` is the bundled reviewer that P1 ran.
  Governs R2, R5.
- KTD2. **Leave `REVIEW_SKIPPED` undetectable.** Do not synthesize a `Skill` tool_use from
  a `read_file` of `SKILL.md`, and do not key on `[reviewer]` subagent descriptions. Governs
  R3, R4.
- KTD3. **`classify` skips attaching `review_skipped` when that class is in
  `evidence.undetectable`.** The finding's current gate is `review_skill and not reviewed`.
  R2 would trip it on every complete grok Task. The undetectable list is the honest
  affordance the native mode plan already built. Governs R4.
- KTD4. **A backend with a review skill and an undetectable skip gets its own brief rule.**
  `REVIEW_RULE` promises a report to the operator. `REVIEW_RULE_FALLBACK` is for no skill.
  Neither fits grok after R2. Add a third sentence that names `/review` and says this CLI
  does not emit a structured skill call Relay can key on. Claude keeps `REVIEW_RULE`.
  Codex keeps the fallback. Governs R5.
- KTD5. **The validate error names the missing step, not "claude only".** After grok is
  admitted, the remaining refusal is Codex. A sentence that says native mode runs on
  claude only would be false. Governs R6, R11.
- KTD6. **The live grok Task is a gate on done, not a follow up.** The review contract
  between processes changes: the brief names `/review`, the digest carries `undetectable`,
  and the closeout on grok uses the Task's own model (closeout.py already substitutes
  away from claude vocabulary). The stub cannot produce that. Governs R12.

### Assumptions

- Nested `grok -p` from this session, with nesting env stripped, is the same CLI a Runner
  would launch. If a later live run disagrees with a probe, the live run wins and the pin
  is amended in the same unit.
- `/review` remains the bundled reviewer through the live run. If 1.0.25's next patch
  injects `/code-review` as a Skill-shaped event, that is a new plan, not a silent flip
  of KTD1.
- The proof target at `~/Documents/PhilAI/relay-proof/` is still the throwaway markdown
  harness, local bare origin, nothing leaving the machine.

### Sequencing

U1 pins and capability. U2 classify gate and brief rule. U3 validate lift and tests. U4
docs. U5 live run. U1 through U4 stay on one branch and the suite is green before U5.
U5 is the gate. A live defect returns to the unit it belongs to.

## Implementation Units

### U1. Re-pin grok

**Goal:** the grok pin and the capability record say what 1.0.25 actually does.

**Requirements:** R1, R2, R3, R7, R8, R9, R10.

**Files:** `skills/relay/scripts/relay/contracts.py` (the grok entry, lines 200 to 268),
`skills/relay/scripts/relay/backends/grok.py` (`CAPABILITY.known_models`, `_UNDETECTABLE`
comment), `tests/test_backends.py`.

**Approach:** bump `version_tested` to `1.0.25` and `version_output_sample` to
`grok 1.0.25 (f7e67d6988e2) [stable]`. Keep the 1.0.5 `dontAsk` finding and the 1.0.13
heredoc cancellation finding, each named with version and date, then add the 2026-09-11
re-observations. Set `review_skill` to `"review"` with a comment that P1 reached `/review`
and not bundled `code-review`. Keep `_UNDETECTABLE = frozenset((REVIEW_SKIPPED,))` and
rewrite its comment: skill invocation is still a `read_file` of `SKILL.md` plus optional
`subagent_spawned`, no Skill tool, skip is undetectable. Refresh `known_models` to
`("grok-4.6", "grok-4.5")`. Strengthen `commit_message_constraint` per R10. Date every
comment this unit touches.

**Test scenarios:** `test_only_claude_names_a_native_review_skill` becomes a test that
claude names `code-review`, grok names `review`, codex names `None`.
`test_review_command_is_the_slash_form_or_none` expects `/review` on grok.

**Verification:** `python3 -m unittest test_backends test_contracts` from `tests/`.

### U2. Classifier gate and brief rule

**Goal:** a grok Task with a named review skill does not get a false skip finding, and its
brief does not promise a report the runner cannot make.

**Requirements:** R3, R4, R5.

**Files:** `skills/relay/scripts/relay/classify.py`, `skills/relay/scripts/relay/brief.py`,
`tests/test_classify.py`, `tests/test_brief.py`.

**Approach:** in `classify.classify`, attach `REVIEW_SKIPPED` only when `review_skill` is
set, the class is not in `evidence.undetectable`, and no review call was found. In
`brief.py`, add a third rule string for a named skill on an undetectable backend, selected
when `review_command(capability)` is set and `REVIEW_SKIPPED` is in that backend's
undetectable set. Do not teach `review_ran` a grok event shape.

**Test scenarios:** grok `session-transcript-complete.jsonl` still has
`undetectable == [review_skipped]` and no `review_skipped` finding. A claude complete
envelope with no Skill call still gets the finding. The grok brief contains `/review` and
does not contain "reported to the operator". The claude brief still does. Codex still
renders the fallback.

**Verification:** `python3 -m unittest test_classify test_brief` from `tests/`.

### U3. Lift the grok refusal

**Goal:** `validate` accepts grok and still refuses Codex.

**Requirements:** R6.

**Files:** `skills/relay/scripts/relay/manifest.py` (BACKENDS comment, the refusal at
line 509), `tests/test_manifest.py`, `tests/test_cli.py` (the Codex case stays).

**Approach:** keep the `review_skill is None` check. Change the error so it names the
missing step and does not say native mode runs on claude only. Rewrite the BACKENDS
comment: native mode runs a Task on a backend whose capability record names a verified
review skill, today `claude` and `grok`. Flip `test_native_mode_refuses_a_task_on_a_backend_with_no_review_skill`
so grok is accepted (with a grok model) and Codex is still refused, including excluded
and `[defaults]` cases. A grok Task whose model is a claude name still fails the
cross backend model check.

**Test scenarios:** grok Task with `grok-4.6` validates. Codex Task still matches
`NATIVE_REFUSAL`. The remaining refusal text does not contain `claude only`.

**Verification:** `python3 -m unittest test_manifest test_cli` from `tests/`.

### U4. Docs

**Goal:** nothing that ships still says native mode runs on claude only.

**Requirements:** R11.

**Files:** `CLAUDE.md` Working here, `CONCEPTS.md` Backend and Review step, `README.md`
Backends, `docs/manifest-authoring.md`, `skills/relay/SKILL.md` (authoring step, halt
table `review_skipped` row, backend readiness table, resume paragraph),
`skills/relay/references/backend-rubric.md`. Codex sentences stay where they describe
Codex.

**Approach:** rewrite each claude only claim so grok is admitted and Codex is not. The
`review_skipped` row in the skill's halt table must not imply grok emits a `/code-review`
Skill call. Say grok's skip is undetectable and the digest lists it as not checked.

**Test scenarios:** `test_examples.Skill` and `test_examples.Readme` stay green. Grep the
shipped set for `claude only` and for `native mode runs on claude`. Hits remaining must
be Codex refusals or history under `docs/plans`.

**Verification:** `python3 -m unittest test_examples` from `tests/`, then the grep.

### U5. Live grok Task against the proof target

**Goal:** one real grok Task lands. The stub is not evidence.

**Requirements:** R12.

**Files:** a sibling manifest next to `~/Documents/PhilAI/relay-proof/manifest.toml`, one
new line in the proof target's `tasks.md`. No change inside this repository unless the
run finds a contract defect. A new grok fixture is added under
`tests/fixtures/backends/grok/` only if it can be scrubbed with
`tests/fixtures/backends/_scrub.py` after filling `PRIVATE_TERMS` locally, and then a
provenance line is added to `tests/fixtures/backends/README.md`.

**Approach:** write a markdown adapter manifest naming `backend = "grok"`,
`model = "grok-4.6"`, a real effort, `reason` if it differs from the default, and
closeout model left as the manifest's claude vocabulary (the closeout launcher already
substitutes the Task model on a non claude backend). Add one trivial Task to `tasks.md`.
`validate`, then `run` in the foreground with the harness timeout at its maximum. Confirm
the plan appeared as a message, `/review` was reached as a `read_file` of that skill
(the record will not claim this, because of KTD2), the gate ran, the merge landed, the
closeout printed a terminal line. The digest lists `review_skipped` under `undetectable`
and does not attach the finding. If the run finds a contract defect, fix it in the unit
it belongs to and file a `docs/solutions/` entry if the suite could not have seen it.

**Test scenarios:** the live run itself. Halt classes, findings, state directory path, and
whether review detection is real or declared undetectable go in the handoff.

**Verification:** `python3 skills/relay/scripts/relay_cli.py validate <sibling-manifest>`,
then `run`, then `summary`. Then `python3 -m unittest discover -s tests` from the repo
root.

## Verification Contract

- While iterating a unit: `python3 -m unittest test_<module>` from `tests/`.
- Before U5: `python3 -m unittest discover -s tests` from the repo root, green, 1071
  tests plus whatever U1 to U4 add.
- U5 is required. A green suite without it is not done.
- Git: feature branch `native/grok-native-review-step`, commit, merge to `main` locally,
  delete the branch. Do not push. Do not open a pull request. Do not add a co author
  trailer. Commit messages use `git commit -m "Subject" -m "Body paragraph."` only.

## Definition of Done

- Suite green.
- One live grok Task landed through the real pipeline against the proof target.
- Every pin this work touched names the version and the date it was observed on.
- Docs no longer say native mode runs on claude only.
- The live Task's record lists `review_skipped` as undetectable, which is the true
  statement, and does not attach a skip finding.
- Codex still refused at validate.
- Report the live run's state directory path, its halt classes or findings, and state
  plainly that review detection is declared undetectable.

## Appendix

Probe session ids, all 2026-09-11, grok 1.0.25:

- P1 review: `c82ed10e-44a8-415e-b7cb-bfb059b25815`
- P3 dontAsk: `89074da0-d3d0-47cd-94b9-0578e8e4ada5`
- P4 heredoc commit: `1a928b01-26c5-4f6d-865a-d28ae79f014f`
- P5 deny `Bash(rm -rf*)`: `57572297-a629-41fe-8b5c-ae184c951bb2`
- P5 allow Claude names: `43a20722-9b4f-4051-846b-ca46f3aa671e`
- P5 deny real tool name: `d4e5f607-89ab-4cde-9012-3456789abcde`

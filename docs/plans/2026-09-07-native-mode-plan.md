# Native Relay: replace the compound-engineering pipeline with a native one

## Context

Native Relay is a hard fork of compound-relay taken 2026-09-07 (commit `3e715b9` on `main`, pushed).
Today every Task process runs the compound-engineering plugin's chain (`ce-plan`, `ce-work`,
`ce-simplify-code`, `ce-code-review`) and every Closeout process runs `ce-compound`. The fork exists
so the runner can drive a task with no plugin in the loop: the Task process plans in a message,
builds, runs the built in `/code-review`, runs the project's own verification, records what the
project's method says to record, prints the envelope, and exits for the runner's gate and merge.
The first consumer is Cratekit, whose merge bar is its three command battery.

The plugin coupling was verified by reading the code, and it is wider than the brief: `contracts.py`
pins plugin strings and skill names, `classify.py` halts on a Skill call outside the plugin form,
`brief.py` and all three backends carry `skill_form` and `qualify_skill`, `manifest.py` probes each
backend for the plugin at validate and reads `.compound-engineering/config.yaml` for the docs root,
`closeout.py` pins the `ce-compound` invocation and its depth flags, and the stubs answer
`plugin list`. The README installs `relay@relay` and requires compound-engineering 3.23.4.

Note on the consumer: `Cratekit/docs/METHOD.md` has no section titled "The pipeline, one unit per
session" on `relay/74` (its current branch) or in any committed doc; grep finds neither that title
nor the word Relay. The battery section exists (ruff, pytest, scan_repo). The native pipeline below
follows the wording in the request, plan, build, review, verify, record, and treats Cratekit only as
the example that verification is project defined. Nothing Cratekit specific enters a template, a
contract, or the runner.

## Decisions, stated out loud

1. **Native is the only mode. The compound path is deleted outright.** compound-relay remains the
   original for anyone who wants the plugin chain. Keeping two briefs would keep the plugin pins,
   the readiness probe, the skill form machinery, and the substitution classifier alive for a mode
   this repository does not run. Deleting is what the fork is for.
2. **Native mode is Claude only for now.** Plan-in-a-message works on any CLI, but the review step
   is the built in `/code-review` skill, which is a Claude Code feature. Codex exposes no verified
   equivalent reachable from `codex exec`, and Grok has none observed. Rather than ship an
   instruction-only "review your own diff" on two backends nobody has run live, `validate` refuses
   any Task whose backend is `codex` or `grok` with an error naming the missing verified review
   step. The backends package, its capability records, normalizers, stubs, and captured fixtures
   stay: they are the launch seam and the evidence readers, still correct, and they are what a
   later plan needs to lift the refusal one backend at a time. New backends stay out of scope.
3. **The envelope loses `plan_path`.** The plan is a message in the transcript, not a file.
   Grammar becomes `status`, `blockers`, `changed_files`, `learnings`.
4. **The halt class set is amended, once, by this plan (KTD6 allows it here):**
   `skill_substitution` is removed from `HALT_CLASSES`, `FINDING_CLASSES`, `LINE_CLASSES`, and
   `HALT_LINES`. It has no meaning without a plugin to substitute for. In its place a finding, not a
   class, `review_skipped`: on Claude, a Task whose envelope reads `complete` and whose transcript
   holds no `Skill` tool_use naming the backend's `review_skill` gets that finding. Codex and Grok
   declare it undetectable exactly as they declared the old one. The summary lists it as a check by
   hand. This follows the precedent of `waiting_last_message` and `cancelled_tool_call`.
5. **The Closeout keeps both duties and the plugin leaves duty two.** Duty one (the tracker write)
   is untouched. Duty two becomes a native learning judgment: the brief tells the process to judge,
   and if warranted to write one markdown file under `<docs_root>/solutions/` by hand and commit it
   inside the allowed paths. The two terminal lines keep their exact text (`Documentation complete`,
   `Documentation skipped`) so the ending contract is unchanged; only the constants are renamed
   from `COMPOUND_*` to `CLOSEOUT_*`. The depth flags (`lightweight`/`full`) were `ce-compound`
   arguments and go. `CONCEPTS.md` renames the "Compound process" to the "Learning judgment".
6. **The docs root is a manifest key.** `[closeout] docs_root = "docs"` (defaulted, named in
   `defaults_applied`) replaces reading `.compound-engineering/config.yaml`. The Closeout's allowed
   paths are that root plus `CONCEPTS.md`, the markdown tracker file, and manifest extras, as now.
7. **The pr_terminal brief is deleted.** It ran `lfg`. `pr_terminal` stays in `SHIPPING_MODES` and
   in `UNIMPLEMENTED_SHIPPING_MODES`, so `validate` refuses it with the same message. `brief.TEMPLATES`
   keeps one entry.
8. **Backend readiness is binary presence only.** The plugin probe, `plugin_query`,
   `plugin_version`, and `plugin_version_pattern` leave the pins and `Capability`. `qualify_skill`
   leaves `INTERFACE`. A new pin `review_skill` (`"code-review"` on claude, `None` on the others)
   is the one place the review step's name lives; the brief and the classifier read it from there.
9. **Installability closes every listed gap:** MIT `LICENSE` (Phillip Gutowski, 2026); README
   rewritten around the native story and `native-relay@native-relay`; a host agnostic
   `docs/manifest-authoring.md` so a Codex or Grok Build user can author a manifest and run the
   CLI by hand with nothing that lives only in `SKILL.md`; platform facts stated (Python 3.11 or
   later for `tomllib`, macOS and Linux only for `fcntl`, the CLI versions pinned in
   `contracts.BACKEND_PINS`); personal paths and names replaced in tests and fixtures with neutral
   values that exercise the same edges; the scrubber made generic.
10. **Project tracking:** one GitHub issue per unit below on `philgutowski/native-relay`, created
    after this plan is approved, body pointing at the plan file, closed on each unit's merge. Never
    Project 4.

## Baseline

`python3 -m unittest discover -s tests` on `3e715b9`: 941 tests, OK, 521 seconds on this machine
with other work running beside it.

Installed here today: Python 3.14.6, claude 2.1.263 (pin says 2.1.250), codex-cli 0.151.0 (pin
0.149.0), grok 1.0.13 (pin 1.0.13). The README states the pins, not today's numbers.

## Units, each a branch merged to main locally, suite green before merge

### U1 Hygiene: LICENSE, plugin metadata, personal names

- Add `LICENSE` (MIT, Phillip Gutowski, 2026).
- `.claude-plugin/plugin.json` and `marketplace.json`: description "Run a manifest of independent
  tasks through a native plan, build, review, verify, record pipeline, one fresh headless Claude
  Code process per task, serially and unattended." Keywords/tags drop `compound-engineering`, add
  `outer-loop`. Version `0.3.0`.
- `tests/test_contracts.py` SlugRule: neutral cases keeping the same edges (dotfile directory
  giving a doubled dash, underscore, dot and space in a segment), e.g.
  `/Users/example/code/relay-target`, `/Users/example/.config/tool/commands`.
- `tests/test_classify.py:21` transcript path: a neutral slug. `tests/test_launch.py:83`: keep the
  dotted user edge with a neutral tail (`/Users/p.g/code/example_tool`).
- `tests/fixtures/backends/_scrub.py`: generic rules. One home directory rule
  `/Users/<any>/…` and `/home/<any>/…` outside the proof target, plus the three encodings the
  fixtures carry for the proof target itself (plain, Claude's slug `-Users-…`, Grok's percent
  encoded `Users%2F…`), mapped to a neutral `/Users/operator/relay-proof/target` in the same
  encoding. Private term list becomes an empty `PRIVATE_TERMS` tuple with a comment saying to fill
  it locally before committing a new capture. Re-run it over `tests/fixtures/backends/`; the
  decodable line count guard is the safety check, then the suite.
- `tests/fixtures/backends/README.md`: proof target named as "a throwaway target repository".
- `prototype/run-sweep.sh` header origin line and `docs/operating-loop.md` line 23: neutral wording.
  `docs/plans`, `docs/brainstorms`, `docs/ideation`, `docs/solutions` untouched (history).
- `tests/test_examples.py` `LEAK_PATTERNS`: add `pgutowski` and `PhilAI`, and widen `SHIPPED` to
  include `tests`, `CONCEPTS.md`, `CLAUDE.md`, `.claude-plugin` so the guard covers what a stranger
  clones.

### U2 Contracts and backends: remove the plugin, name the review skill

`skills/relay/scripts/relay/contracts.py`:
- Delete `PLUGIN_NAME`, `PLUGIN_MIN_VERSION`, `PLUGIN_PINS`, `LFG_TERMINAL_TOKEN`,
  `CE_WORK_RETURN_MODE`, `ENVELOPE_PLAN_PATH_KEY`, `CE_PLAN_RUNS_DOC_REVIEW`, `CODE_REVIEW_AGENT_MODE`,
  `CODE_REVIEW_VERDICT*`, `COMPOUND_NON_INTERACTIVE`, `COMPOUND_DEPTH_*`, `REQUIRED_SKILLS`.
- Rename `COMPOUND_COMPLETE_LINE`/`COMPOUND_SKIPPED_LINE`/`COMPOUND_TERMINAL_LINES` to
  `CLOSEOUT_COMPLETE_LINE`/`CLOSEOUT_SKIPPED_LINE`/`CLOSEOUT_TERMINAL_LINES`, text unchanged.
- `BACKEND_PINS`: drop `plugin_version`, `plugin_query`, `plugin_version_pattern`, `skill_form`
  from all three; add `review_skill` (`"code-review"` claude, `None` codex and grok). Rewrite the
  header comment (pins are launch facts observed against a throwaway target on 2026-08-28).
- Halt classes: remove `HALT_SKILL_SUBSTITUTION` everywhere; add `REVIEW_SKIPPED = "review_skipped"`
  to `FINDING_CLASSES`, `LINE_CLASSES`, `HALT_LINES` (`"completed without running {review}"`).
- Module docstring: a contract here is a fact about the CLI or its transcript.

`backends/__init__.py`, `claude.py`, `codex.py`, `grok.py`: `Capability` loses the three plugin
fields and `skill_form`, gains `review_skill`; `qualify_skill` removed from every module and from
`INTERFACE`; `_UNDETECTABLE` sets swap `HALT_SKILL_SUBSTITUTION` for `REVIEW_SKIPPED`.

`tests/stub-claude/{claude,codex,grok,_stub.py}`: drop the `plugin list` branches.

Tests: delete `PinsTraceToSource`; update `OwnVocabulary`, `test_backends.py` (interface tuple,
pins completeness, plugin pattern tests go), `test_summary.py` (the substitution row becomes a
`review_skipped` row), `test_closeout.py` `COMPOUND_FORMS` block and depth class (goes in U5, but
the constants rename lands here so the suite stays green: mechanical rename in that file).

### U3 Manifest: native mode is Claude only, docs root is a key, readiness is the binary

`skills/relay/scripts/relay/manifest.py`:
- New rule in `validate`: any Task (excluded ones too) whose backend's `review_skill` is `None` is
  an error: `tasks[i] (T-1) names backend codex, which has no verified native review step; native
  mode runs on claude only, see README`. `BACKENDS` stays the closed set of three so a manifest
  written for compound-relay is refused with that sentence rather than "unknown backend".
- `Closeout` dataclass gains `docs_root` (default `contracts.DEFAULT_DOCS_ROOT` via `pick`, so it
  lands in `defaults_applied`). `docs_root_for(repo)` is deleted; `completed_allowed_paths(manifest)`
  reads `manifest.closeout.docs_root`. `run.py:209` and the two `test_run.py` call sites follow.
  `validate` rejects a `docs_root` that starts with `/` or contains `..`, same rule as
  `task_allowed_paths`.
- `_backend_readiness_errors`: `shutil.which` only. `_plugin_version`, `_run_plugin_query`,
  `_version_parts` go. The `check_environment` flag and the CLI wiring stay.
- Module comment at line 25 rewritten.

Tests: `test_manifest.py` readiness class shrinks to binary present/missing, `test_docs_root_from_target_config_yaml`
becomes `test_docs_root_from_the_manifest_key`; `test_cli.py:153` and `:165` follow; new tests for
the backend refusal on an ordinary task, an excluded task, and a `[defaults] backend = "codex"`.
`docs/examples/manifest-github-projects.toml` currently mixes backends with a `reason`; U6 rewrites
it so the example still validates (the `reason` field discipline is kept for excluded tasks and
shown in a comment for a future non default backend).

### U4 Brief, classifier, run loop: the native task process

`skills/relay/templates/brief-local-merge.md`, steps become:
1. `$tracker_start_step`
2. Create `$branch` from `$default_branch`.
3. **Plan.** Read the project's own instructions (`CLAUDE.md` or equivalent at the repository
   root) and the files the task names. Then write the plan as a message before editing anything:
   what will change, which files, how it will be verified, what is out of scope. No plan file;
   the message is the plan.
4. **Build.** Implement the plan on the branch, committing as you go, subject and body only.
5. **Review.** Run `$review_command` on the branch's diff against `$default_branch`, fix what it
   finds, commit the fixes. (`$review_command` renders `/code-review` from `review_skill`.)
6. **Verify.** Run the project's own verification, whatever its instructions define as the bar
   for a unit of work, and make it pass in the foreground. Then the runner's gate:
   `$gate_description`.
7. **Record.** Record what the project's method says a unit records (findings it chose not to fix,
   a learning worth keeping, a changelog line), in the places it names, on the branch. Record
   nothing where the project names nothing.
8. `$tracker_review_step`
9. The envelope, four keys, no `plan_path`.
The `$skill_form_rule` paragraph becomes a review rule: the review step is the named built in
skill; skipping it or substituting a self review is recorded as a finding.

`brief.py`: delete `SKILL_FORM_RULE`, `LEAD_SKILL`, the `ce_*`, `return_mode`, `review_mode`,
`lfg_token` values; add `review_command`. `TEMPLATES` keeps `local_merge` only; delete
`templates/brief-pr-terminal.md`. Docstring rewritten (three load bearing things, not four).

`classify.py`: delete `PLAN_PATH_RE`, `required_skill_for`, the substitution branch; envelope dict
drops `plan_path`; new `review_skipped` finding computed after the transcript pass on a backend whose
`review_skill` is set: envelope status `complete` and no `Skill` tool_use whose `input.skill`, bare
or namespaced (`code-review`, `plugin:code-review`), equals `review_skill`. Docstring paragraph on
the two joins rewritten.

`run.py`: drop `plan_path=envelope.get("plan_path")` at `:862`; `summary.py:135` check becomes
`review_skipped` ("T-1 completed without running /code-review; review the diff by hand").

Fixtures: `tests/fixtures/transcripts/_make.py` rewrites `PROMPT`, the success/blocked fixtures'
tool uses (a `code-review` Skill call replaces the `ce-plan`/`ce-work` calls), and turns
`skill_substitution()` into `review_skipped()` (a complete envelope with no review call);
regenerate. `tests/fixtures/stdout/_make.py` text and skill name follow; regenerate.

Tests: `test_brief.py` (SkillPinning class replaced by a ReviewCommand class; step order test
checks branch, plan, review, verify, record order; plan_path test removed), `test_classify.py`
(required_skill tests removed, review_skipped tests added, envelope tests drop plan_path),
`test_closeout.py:169`, `test_tail.py` and `test_run.py` string updates.

### U5 Closeout: native learning judgment

`skills/relay/templates/brief-closeout.md`: drop the `Plan:` line; duty two rewritten: judge, then
if there is a learning write one markdown file under `$learnings_dir` (`<docs_root>/solutions/`,
or wherever the project's own instructions say learnings live if that lies inside the allowed
paths) with a title, the problem, the cause, and what to do next time, and commit it in one commit
inside `$allowed_paths`. Terminal lines unchanged.

`closeout.py`: delete `depth_for`, `FULL_DEPTH_FINDINGS`, `compound_command`, the `plan_path`
parameter on `render`/`run`; add `learnings_dir` value; constants renamed. Docstring rewritten.

Tests: `CompoundDepth` deleted; the four `compound_command`/`COMPOUND_FORMS` tests replaced by one
asserting the brief names the learnings directory and both terminal lines on every backend; the
plan_path test removed.

### U6 Documentation and the shell path

- `README.md`: native story; install as plugin (`claude plugin marketplace add`,
  `claude plugin install native-relay@native-relay`) and install by clone for a Codex or Grok Build
  host (clone, `validate`, `run`, `status`, `summary`); platform facts; the backends paragraph
  saying claude only in native mode and why the other two pins remain; keep the strings
  `test_examples.Readme` asserts (`install`, `relay_cli.py`, `docs/examples/`, the plan path,
  `CONCEPTS.md`). The "Why this exists" section stops naming the plugin.
- `docs/manifest-authoring.md`: the authoring procedure from `SKILL.md` as a plain document (repo,
  tracker, branch prefix, tasks, the four qualifying sentences, gate as an argument list, closeout
  docs root, the degraded path answers), with the shell commands. `SKILL.md` points at it rather
  than duplicating.
- `skills/relay/SKILL.md`: description and body on the native pipeline; backend readiness table
  loses the plugin rows; the backend proposal step says native mode writes `claude` and refuses the
  others; halt table drops `skill_substitution` and the readiness remediation for plugins.
  `test_examples.Skill` assertions kept.
- `skills/relay/references/backend-rubric.md`: leading paragraph stating the native mode restriction;
  the rest kept as the rubric that applies once another backend has a verified review step.
- `CONCEPTS.md`: Relationships, Backend, Brief, Envelope, Closeout process, Compound process
  (renamed Learning judgment), Halt class (set amended), plus a `Review step` entry.
- `CLAUDE.md`: first paragraph, layout (one template), working here (the closed set as amended,
  the live run rule unchanged).
- `docs/examples/*.toml`: comments and the github example's backend mix.

### U7 Live run against a throwaway target

Required by `CLAUDE.md` because the envelope grammar, the task brief, and the closeout brief all
changed. Build a throwaway repo under the scratchpad (one module, a unittest suite as the gate, a
local bare origin, `tasks.md` with one task "add a function and its test"), write a markdown
manifest with one claude task, `validate`, `run` in the foreground with the harness timeout at
its maximum, then `summary`. Confirm: the plan appeared as a message, `/code-review` was invoked
(no `review_skipped` finding), the gate ran, the merge landed, the closeout closed the line and
printed a terminal line. Fix what the run finds, in the unit the defect belongs to, and file a
`docs/solutions/` entry if the run teaches something the suite could not.

## Verification

- Per unit: `python3 -m unittest discover -s tests` green from the repo root; single modules from
  `tests/` while iterating.
- After U1: `grep -rn "pgutowski\|PhilAI\|Integrel\|Electric Passage"` over everything outside
  `docs/plans`, `docs/brainstorms`, `docs/ideation`, `docs/solutions`, `.git` returns nothing.
- After U6: `grep -rln "compound-engineering\|ce-plan\|ce-work\|ce-compound\|lfg"` over `skills`,
  `tests`, `README.md`, `CLAUDE.md`, `CONCEPTS.md`, `.claude-plugin`, `docs/examples` returns
  nothing; `python3 skills/relay/scripts/relay_cli.py validate docs/examples/manifest-markdown.toml`
  refuses only on `project.repo`.
- U7 is the end to end proof; its summary output goes in the handoff.

## Out of scope

`pr_terminal` (still refused), new backends, any change to Cratekit, lifting the Claude only
restriction, the GitHub Project board.

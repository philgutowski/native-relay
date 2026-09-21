# Authoring a manifest by hand

This is the procedure the `/relay` skill follows, written as a plain document so an operator on
any host, a shell, a Codex session, a Grok Build session, can follow it without Claude Code.
Everything the skill does is a runner subcommand; nothing lives only inside the skill.

Resolve `<runner>` once:

```text
<runner> = <this checkout>/skills/relay/scripts/relay_cli.py
```

Write the manifest to a path outside the target repository. Relay adds nothing to a project it
runs against. Start from the example that matches your tracker in `docs/examples/`, then work
through the tables below in order.

## 1. The project

```toml
[project]
repo = "~/code/example-project"
default_branch = "main"
mirror = []
# branch_prefix = "relay/"
```

- `repo` is the checkout the runner merges into. It must have an `origin` remote, unless
  `shipping.push` is false, and a git identity (`user.name`, `user.email`), because the runner's
  merge authors a commit.
- `default_branch` is where tasks land. Leave it unset only when `refs/remotes/origin/HEAD` is set
  in the checkout, so a repo with no remote always names it.
- `mirror` is an optional argument list for `git push`, run after the closeout, for a project that
  keeps a second remote. Empty means none. An argument list, never a shell string. It must be
  empty under `shipping.push = false`, since a mirror is a push.
- `branch_prefix` names task branches: the prefix plus the task id. The default is `relay/`. An
  empty string is the task id alone, which suits Jira keys that already read `ABC-12`. Write the
  key only when you want something other than the default.

## 2. The tracker

One of three adapters. The runner only reads the tracker; every write goes through a launched
process with the adapter's instructions.

```toml
[tracker]
adapter = "markdown"          # or "jira" or "github"
file = "tasks.md"             # markdown: the file in the repo
done_statuses = ["closed"]
in_review_status = "in review"
```

- `markdown`: a checklist file in the repository. `- [ ] T-1 Title` is open, `- [x] T-1 Title
  (abc1234)` is closed with its landing reference. The closeout edits the line; the runner reads
  it at the remote's default branch, or at the local one under `shipping.push = false`.
- `jira`: `site`, `project_key`, `done_statuses`, and the two environment variables the runner
  reads credentials from (`token_env`, `email_env`, default `JIRA_API_TOKEN` and `JIRA_EMAIL`).
  Those tokens are for the runner's reads. Writes go through Atlassian MCP on `claude` and
  `grok`. A grok Jira Task also needs grok's own Atlassian login; `validate` probes
  `grok mcp doctor --json` and refuses until that handshake is healthy. Codex on Jira is refused.
- `github`: `owner`, `project_number`, and `status_field` for a GitHub Project board, read
  through a logged in `gh`.
- `in_review_status` is the status the task process moves a card to at its start. It is required
  under `local_merge`; the markdown adapter has no such state and `validate` says so as a warning.

## 3. Shipping mode

```toml
[shipping]
mode = "local_merge"
push = true
```

`local_merge` is the one mode that runs: the runner runs the gate on the task branch, merges to
the default branch, pushes, and verifies the landing from git and the tracker. `pr_terminal` is
named in the schema and refused.

`push` defaults to true. Set it false to merge every task to the default branch locally and push
nothing at all, not the merge, not the closeout's commit, and not a task's own branch, so the
whole run stays on the machine and you decide afterwards what reaches the remote. Under false:

- the repo needs no `origin`, and `mirror` must be empty;
- the local default branch may run ahead of `origin`, but a remote that has diverged from it, or
  a local default branch that moves while a task runs, still stops the run;
- the landing is verified from local git and the tracker, and a markdown tracker is read at the
  local default branch;
- the summary ends by saying nothing was pushed and naming the one `git push` that ships it.

## Normal dispatch policy and conservative scheduling

For a normal manifest, `dispatch` asks an attached operator to choose `serial` or `parallel`; `serial`
is the default. Supply `--policy serial` or `--policy parallel` when the choice must be explicit.
A noninteractive or detached launch that receives no explicit policy stays serial.

`parallel` does not trust an independence claim by itself. Before workers start, Relay inspects the
repository and every task's declared scope and prints the schedule. Add a narrow
`declared_paths` list to a task when its expected write set is known:

```toml
[[tasks]]
id = "T-1"
model = "sonnet"
effort = "medium"
declared_paths = ["src/relay/schedule.py", "tests/test_schedule.py"]
```

Paths are repository-relative files or directory prefixes. They are scheduling evidence, not a
permission grant or a promise that Relay will overlap the task. Relay serializes a pair unless it
has high-confidence, deterministic evidence of disjoint bounded work. It also serializes any
task with no or broad scope and any task affecting shared configuration, dependency manifests,
migrations, CI, root documentation, generated output, or another global surface. Read-only
semantic analysis may explain an edge but cannot remove one. The printed schedule names each
serialized edge and why it exists; after that pre-launch display, Relay starts the computed run
without seeking another authorization. Permitted parallel workers use isolated worktrees, while
gates, hooks, verification, and landing remain serial in manifest order.

## Triple execution mode

For one simultaneous Claude, Grok, and Codex run from a GitHub Projects or Jira board, add:

```toml
[execution]
mode = "triple"
```

This is an exact three-card profile, not a general concurrency setting. It requires GitHub Projects
or Jira,
`local_merge`, `push = true`, three independent nonexcluded tasks, and explicit assignments of
`claude`, `grok`, and `codex` exactly once. Relay claims all three cards and an integration fence
in one atomic remote Git operation before starting any worker. Each worker receives a private,
disconnected clone; landing remains serial and follows the usual gate, verification, and Closeout
sequence. If a worker, board snapshot, or lease diverges, Relay retains the evidence rather than
starting a replacement or deleting a claim automatically. Jira triples use the coordinator's
Jira REST credential for the claimed cards' In Review, outcome-comment, and terminal/return
transitions; workers never receive Jira credentials or write tools. The credential must be
permitted to make those writes, and `tracker.coordinator_rest_writes_authorized = true` is
required. Set `tracker.in_review_transition` to the workflow label and
`tracker.in_review_status` to its exact expected destination status; Relay refuses a missing,
ambiguous, or mismatched transition. Supply `tracker.transition_labels` for that status, every
terminal status, and every possible blocked-return status; Relay does not infer a label from a
status. Custom claim-ref pushes preserve repository hooks; a failed
release retains exact-token claims until an explicit guarded recovery.

## 4. Permissions

```toml
[permissions]
allowed = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "Skill", "TodoWrite"]
disallowed = []
# task_allowed_paths = ["src/", "docs/"]
```

- `allowed` is the tool allowlist the task process launches with. Keep `Skill` in it: the review
  step is a built in skill.
- `disallowed` is yours to extend; `validate` adds every force push, hard reset, recursive delete,
  and kill spelling the runner refuses by default and names each one it added.
- `task_allowed_paths` is optional and bounds what a task's own commit may touch, as repository
  relative directory prefixes or exact files. Unset means the whole repository.
- Do not write a permission mode. The posture is fixed per backend in the runner and is never a
  manifest choice.

## 5. The gate

```toml
[gate]
command = ["python3", "-m", "unittest", "discover", "-s", "tests"]
description = "the unittest suite, run locally before the merge and again by the pre push hook"
```

One command, as an argument list, never a shell string. The runner runs it on the task branch
before the merge and refuses the merge when it fails. When a project's merge bar is several
commands, name the one the runner runs here and let the project's own instructions carry the
rest: the task process runs the project's own verification itself, from those instructions,
before it exits. Do not write a wrapper script to bundle them unless you want one anyway.

## 6. The four qualifying sentences

```toml
[qualifying]
gate = "A pre push hook runs the unittest suite and refuses the push when it fails."
durable_state = "Merge commits on main and the lines in tasks.md are the only state carried between tasks."
independence = "Each listed task touches a separate module and none depends on another's outcome."
editors = "Only the operator's own account edits tasks.md, and it is reviewed on every push."
```

Each is a sentence in your own words, and `validate` refuses a manifest missing any of them. They
are data, not configuration: they record that you checked the property. `editors` matters most,
because card text is fed verbatim to an unattended process, so it names the accounts whose text
is trusted to instruct one. The task brief carries the card's title, its description, and every
comment on it at launch, oldest first and capped at the newest 20, all inside the same fenced data
block. Scope or prerequisites written as a comment reach the task, and so does a comment an earlier
Relay attempt left there.

## 7. Timeouts and the closeout

```toml
[timeouts]
task_minutes = 90
closeout_minutes = 15

[closeout]
model = "sonnet"
effort = "medium"
allowed_tools = []
allowed_paths = []
docs_root = "docs"
```

- `task_minutes` bounds one task process, whole process group included. It must exceed the lease
  TTL, which `validate` checks.
- The closeout runs on its own model and effort: two bounded jobs that need judgement, not depth.
- `docs_root` is the directory the closeout may write a learning under, as `<docs_root>/solutions/`
  by default; the closeout also reads the project's own instructions for a better place inside
  its allowed paths. Those paths are `docs_root`, `CONCEPTS.md`, the markdown tracker file, and
  whatever `allowed_paths` adds. A closeout commit outside them is reset by the runner.

## 8. The degraded paths

```toml
[on_blocked]
merge_partial = false
open_followup = false

[on_halt]
continue_past_task_halt = false
```

- `merge_partial`: may a task commit the part it finished when one piece is blocked, provided the
  gate passes on what it commits.
- `open_followup`: may a task open one follow up card for a piece it could not finish.
- `continue_past_task_halt`: off, the first halt of any class stops the run; on, a halt contained
  to one task pauses that task and the later independent tasks keep running, so several halts in
  a row surface only in the summary.

## 9. The tasks

```toml
[defaults]
backend = "claude"

[[tasks]]
id = "T-1"
model = "sonnet"
effort = "medium"
# Optional scheduling evidence for `dispatch --policy parallel`.
declared_paths = ["src/relay/", "tests/test_relay.py"]

[[tasks]]
id = "T-2"
model = "opus"
effort = "high"
excluded = true
reason = "needs a design answer nobody can give unattended"
```

- Every task carries `id`, `model`, and `effort`. Ids are the tracker's own.
- `declared_paths` is optional, narrow repository-relative scheduling evidence for a parallel
  run. It may name files or directory prefixes. Omit it when the expected write scope is not
  known; Relay treats uncertainty as a reason to serialize, never as a reason to overlap.
- `excluded = true` keeps a task out of the run; it needs a `reason` in your words.
- `backend` is `claude` or `grok`. Naming `codex` is refused by `validate` with a sentence that
  names the missing review step. A task whose backend differs from the `[defaults]` value
  carries a `reason` string, the same field as above.
- Tasks must be independent of each other. A task that depends on another belongs in a later run.
- A card whose text contains a `.claude/` path is skipped at launch, because an unattended edit
  there is refused. A mention alone trips it, including a sentence that forbids the path, such as
  "never edit .claude/skills". The match is deliberately plain rather than a guess at what the
  sentence means: a wrong guess spends a whole launch, and a false hit costs one rewording. When
  the task does not edit there, describe the location without the literal segment, for example
  "the skills directory under the Claude config". `validate` reports every hit before launch.

A card whose scope spans repo code and the `.claude/` skill package is a different case. The
scan only trips on a mention in the card text, so a card that never names `.claude/` can still
reach a skill file. Exclude that card, or split the source half from an attended follow up for
the skill edit. Ordering it last buys nothing: a complete Envelope lands the Task, and the
finding is a check by hand, not a halt.

## 10. Validate, then run

```bash
python3 <runner> validate <manifest>          # the rules above, the checkout, the backend binary, and every card
python3 <runner> validate <manifest> --list   # the same, plus the tracker's cards not in a done status
python3 <runner> run <manifest>               # to completion or to a halt
python3 <runner> dispatch <manifest> --policy parallel  # inspect, print, and execute a conservative parallel schedule
python3 <runner> dispatch <manifest> --policy serial    # explicit serial choice
python3 <runner> run <manifest> --detach --notify
python3 <runner> run <manifest> --detach --wait-for-lease   # queue behind a live runner, then run
python3 <runner> pair split <manifest>        # write claude and grok members plus a pair file
python3 <runner> pair validate <pair>
python3 <runner> dispatch <pair>              # both backends at once, merges in the listed order
python3 <runner> status <manifest>
python3 <runner> summary <manifest>
python3 <runner> feed <manifest> --dry-run    # a manifest that grows, section 11
```

`validate` exits 0 with a warning when a listed task's branch already exists, locally or on
origin, left by an earlier run. Exit 0 does not mean ready to launch while that warning stands. A
local hit is refused at launch, before any process starts, on the `no_task_branch` preflight check,
and the refusal repeats on every later run. To keep the earlier commits, rename the local branch
out of the prefix with `git branch -m`, leave any remote copy as a backup, and put the instruction
to merge the old branch first in the card's body, since a fresh headless process reads nothing
else. A branch only on origin is not refused, but the fresh process will not know the work is
there, so the same card edit applies when it should continue. See
`docs/solutions/workflow-issues/task-branch-in-flight-from-an-earlier-run-fails-no-task-branch-preflight-and-validate-never-warns.md`.

An attached `dispatch` with no policy presents the two choices, `serial` and `parallel`, with serial
selected by default. A noninteractive or detached run with no explicit policy remains serial.
When parallel is selected, inspect Relay's pre-launch schedule; it is informational, not a second
authorization step, so the runner starts its scheduled workers immediately after showing it.
Workers that Relay permits to overlap receive separate worktrees; their landings still occur in
manifest order.

When the task list names both `claude` and `grok`, split it into a pair and dispatch that. Keep
the order you already chose as the merge order. Prefer claude for high judgment work and grok for
mechanical, bounded work, using the rubric. `run` on the mixed file still goes one task at a time.
Dispatch overlaps one claude build with one grok build, each in a worktree of the target repo, and
merges strictly in that order so a grok task that finishes first still waits its turn.

## 11. A manifest that grows: the feeder sidecar

Skip this unless the queue is too long to list or its cards depend on each other. A manifest
meant for `relay feed` is written exactly as above with two differences.

**The task list may start empty.** Leave out `[[tasks]]` entirely and the feeder appends the
first ones. With `queue.feeder.toml` beside it, `validate` runs every other check and warns
that the list is absent rather than refusing, and `status` and `lease` load it and answer as they
would for any manifest, so you can ask whether anything holds the lease before the first cycle.
`run` still refuses an empty list, and without the sidecar so do the other three. Check the
manifest with `feed <manifest> --dry-run`, which loads it, reads the tracker, and prints what it
would append.
Each block the feeder appends carries a comment line with the time and the card's title, and a
task it excludes gains `excluded = true` and a `reason` naming the halt class and cause line.
Do not reorder or renumber what it wrote; order lives in the order file.

**`qualifying.independence` says something different.** It cannot claim no task depends on
another. Write what is true, in your own words: a task is listed only once the tracker derives
it ready, so everything it depends on has landed, and the runner merges one at a time.

The feeder's settings are not a manifest table, because the feeder rewrites the manifest and an
older pinned runner must still load it. They live in files beside the manifest, named from its
stem. For `queue.toml`:

| File | Who writes it | What it is |
|---|---|---|
| `queue.feeder.toml` | you | the sidecar: settings, all optional |
| `queue.order` | you | priority, one card id per line, highest first, `#` comments |
| `queue.models` | you | model routing, `id model  # why` per line, read fresh each cycle |
| `queue.feeder.stop` | you, or `feed --stop` | its presence makes the feeder leave after the cycle |
| `queue.feeder.state.json` | the feeder | halt counts and wait counts |
| `queue.feeder.log` | the feeder | one line per decision |
| `queue.feeder.lock` | the feeder | held while it runs; one feeder per manifest |
| `queue.feeder.out` | `feed --detach` | the detached feeder's output and every run's |

The sidecar, with its defaults:

```toml
[feeder]
batch = 3
max_halts = 2
caffeinate = true

[waits]
quick_death_seconds = 600
limit_wait_seconds = 1800
limit_waits_max = 16
idle_wait_seconds = 1800
idle_waits_max = 48
lease_wait_seconds = 600

[models]
default = "opus"
effort = "high"
allowed = ["fable", "opus", "sonnet"]

[ready]
labels = ["ready"]            # github: open issues carrying every label
# jql = "project = EX AND status = \"Ready\""     # jira
# command = ["python3", "scripts/ready_cards.py"]   # the escape hatch

[deny]
ids = []
labels = []

[hooks]
# pre_cycle = ["python3", "scripts/board.py", "sync"]
```

- A key the feeder does not know is an error. A typo that was ignored would run a default for a
  day.
- `ready.command` and `hooks.pre_cycle` are argument lists, never shell strings, the same rule as
  `gate.command`. Both run in the target repository. The ready command prints a JSON array of
  cards, each with `id` or `number`, `title`, `body` or `description`, and `labels`, which is the
  shape `gh issue list --json number,title,body,labels` already prints. Use it when ready is a
  rule labels cannot say, such as a card that waits for other cards to close.
- With GitHub and no `ready.labels`, or Jira and no `ready.jql`, the feeder offers nothing. It
  never reads a whole backlog as ready.
- Markdown needs no `[ready]` at all: every unchecked box is ready, since the file has no way to
  say one task waits on another.
- `models.default` must be in `models.allowed`. A routing line or a card's `**Model:** name`
  body line outside the allowed set is ignored and logged. Whatever is chosen still passes
  `validate` before it reaches the manifest, so a model that belongs to another backend is
  refused there and that one card is left out. The closeout keeps `[closeout] model`.
- Settings are read when the feeder starts. After editing the sidecar, `feed <manifest>
  --restart`. The order and routing files are read at every cycle and need no restart.

Exit codes of `feed`: 0 it left on its own terms (the stop file, a day with nothing ready,
`--once`, `--dry-run`), 1 the manifest, the sidecar, or the checkout needs a person, 2 every
task died quickly for the whole usage limit allowance, 3 another feeder holds this manifest.

## 12. Exit codes of a run

Exit codes: 0 the run reached the end of the manifest, 1 the manifest or environment is wrong,
2 the run halted, 3 another runner holds the lease.

`validate` reads each listed task's card and makes the runner's launch checks on it. A card whose
text trips the `.claude/` scan is an error, since the runner would skip that task; a card that
cannot be read or already reads done is a warning.

`validate` names the property that failed rather than the exit code, and it never invents a
sentence on your behalf. Read the summary after a halt: it carries the halt class, the cause
line, and the checks a human still has to make, so a transcript is never the place to start.
`skills/relay/SKILL.md` carries the halt class table with what each class means and what to do.

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
- `continue_past_task_halt`: decide it on task independence alone. Off, the first halt of any
  class stops the run. On, a halt contained to one task pauses that task and the later tasks keep
  running, which is safe only when a halt in one task says nothing about the rest. The default is
  `false`. Write it explicitly rather than omit it, because an omitted key reaches you only as one
  line in `validate`'s applied defaults list while the field changes what exit 0 means: under
  `true`, exit 0 means the run reached the end of the manifest, not that every task landed.
  The cost of `true` is that every stepped over halt strands its own task branch. The runner never
  deletes it, and the next run refuses that task on `no_task_branch` until you delete the branch
  by hand. This field is halt routing only. It is not a quota, cascade, or blast radius control:
  a task that ends blocked never reaches it, and it does not limit how far a dead account
  spreads. For that, size the run before launching and stage a long task list into shorter
  manifests launched one after another, so a decision of yours sits between them.

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
python3 <runner> run <manifest> --defer T-4   # leave that one listed task alone for this run; repeatable
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

Every launched task's record also carries a snapshot of the host, its load average, free and
inactive memory, memory held by the macOS compressor, and cumulative swap counters, read just
before the Task process starts and just after it exits. `summary` prints them as one `host:` line
under the timing, with swap as the difference between the two reads. When a task ran slower than
its neighbours, read that line before blaming the card: rising load, vanishing free and inactive
memory, or a compressor that grew by gigabytes mean the host was starved.

## 11. A manifest that grows: the feeder sidecar

Skip this unless the queue is too long to list or its cards depend on each other. A manifest
meant for `relay feed` is written exactly as above with two differences.

A feeder launches the runner from its own tree at every cycle, so start it pinned to a commit,
`feed <manifest> --pin`, rather than from a checkout you may still edit: an edit made there can
reach the next task the runner launches, even one in the same batch, since the runner reads brief
templates while a batch is in flight. From a git work tree, `feed <manifest> --pin` extracts the
default branch's commit, never HEAD, to `~/.relay/extracts/native-relay-<first 12 sha characters>`,
reusing an extract whose sha is unchanged rather than remaking it, and starts the feeder from it,
taking over a live one with restart semantics; from an existing extract or a plugin install copy
`--pin` is a plain restart and extracts nothing. From a git work tree, `--pin --dry-run` says what
would be extracted, then also runs the ordinary dry run of the next cycle with this checkout's
code, noted, and writes nothing itself; from an existing extract or a plugin install copy, `--pin`
combined with `--dry-run` says nothing about pinning and is silently a no-op. Adding `--detach` to
`--dry-run`, alone or paired with `--pin`, is refused: a dry run never detaches, so nothing runs
and nothing is written. A feeder started
from a checkout instead prints the checkout
warning, naming the tree it runs from, at launch and in its log: relaunch it with `--pin` before
trusting the next cycle.

**The task list may start empty.** Leave out `[[tasks]]` entirely and the feeder appends the
first ones. With `queue.feeder.toml` beside it, `validate` runs every other check and warns
that the list is absent rather than refusing, and `status` and `lease` load it and answer as they
would for any manifest, so you can ask whether anything holds the lease before the first cycle.
`run` still refuses an empty list, and without the sidecar so do the other three. Check the
manifest with `feed <manifest> --dry-run`, which loads it, reads the tracker, and prints one line
per ready card: `would offer` for one the next cycle would append, `would hold` for one routed to
a model held back by a usage limit, and `would skip` for one the runner's own launch time scan
(the `.claude/` path scan) would refuse. A card the dry run offers is not a guarantee: the ready
source it reads may carry less than the runner's launch scan later sees. The markdown adapter's
ready read, for one, always returns an empty description, since the grammar has no body for a
task line, only a title and indented comments; a `.claude/` mention sitting in a comment is
invisible to the dry run and only trips at launch, where the runner rereads the full card and the
rendered brief. The launch scan is the one that decides; the dry run only previews it. Outside
`--dry-run`, a scanned card the feeder leaves out of the batch is logged once in
`<stem>.feeder.log`, `<id> would be skipped at launch and is left out of the batch: <reason>`,
named again at every feeder start and again if it scans clean and later trips the scan a second
time. A card that drops off the ready list entirely and comes back still naming the same path
keeps its old key instead: it is not named again until the feeder's next start.
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
| `queue.feeder.state.json` | the feeder | halt counts, wait counts, models marked exhausted, a post cycle hold, and a process record |
| `queue.feeder.log` | the feeder | one line per decision |
| `queue.feeder.events.jsonl` | the feeder | one JSON line per moment, for `feed --follow` |
| `queue.feeder.lock` | the feeder | held while it runs; one feeder per manifest |
| `queue.feeder.out` | `feed --detach` | the detached feeder's output and every run's |
| `queue.feeder.hook.out` | the feeder | the post cycle hook's output, a header line per cycle |

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
idle_waits_max = 0
lease_wait_seconds = 600

[models]
default = "opus"
effort = "high"
allowed = ["fable", "opus", "sonnet"]
fallback = {}                 # e.g. { fable = "opus" }; empty means no per model fallback
fallback_hours = 5

[ready]
labels = ["ready"]            # github: open issues carrying every label
# jql = "project = EX AND status = \"Ready\""     # jira
# command = ["python3", "scripts/ready_cards.py"]   # the escape hatch

[deny]
ids = []
labels = []

[hooks]
# pre_cycle = ["python3", "scripts/board.py", "sync"]
# post_cycle = ["python3", "scripts/after_cycle.py"]
post_cycle_mode = "blocking"  # or "detached"
post_cycle_hold = false       # blocking only: a nonzero exit stops the feeder
post_cycle_timeout_seconds = 3600
```

- A key the feeder does not know is an error. A typo that was ignored would run a default for a
  day.
- That refusal reaches the sidecar and not the manifest. The manifest stays loadable by an older
  pinned runner because project facts live in the sidecar instead of a manifest table; the
  sidecar itself gets no such protection, so a key it carries that an older extract's runner does
  not know, `hooks.post_cycle` before issue #37 for one, refuses that extract's `feed` with exit
  1 and refuses `validate` run from the same extract the same way. Pin an extract at least as new
  as every key the sidecar names.
- An extract older than issue #53 carries no such protection either, but silently rather than
  with a refusal: it has no hold check at all, so a feeder started from one, by hand or by
  `--pin`, never refuses a start under a hold and runs a cycle over whatever the default branch
  holds instead of stopping for a person to repair it. Pin an extract new enough to carry the
  hold check too.
- `ready.command`, `hooks.pre_cycle`, and `hooks.post_cycle` are argument lists, never shell
  strings, the same rule as `gate.command`. All three run in the target repository. The ready
  command prints a JSON array of cards, each with `id` or `number`, `title`, `body` or
  `description`, and `labels`, which is the shape `gh issue list --json number,title,body,labels`
  already prints. Use it when ready is a rule labels cannot say, such as a card that waits for
  other cards to close.
- `hooks.post_cycle` runs after every `relay run` the feeder settles. A run that another runner's lease or a
  refused manifest stopped before it ran settles nothing and runs no hook. The hook learns the
  cycle from its environment: `RELAY_CYCLE`, `RELAY_RUN_EXIT`, `RELAY_LANDED`, `RELAY_HALTED`,
  `RELAY_BLOCKED`, and `RELAY_SKIPPED` (that cycle's ids, space separated, empty for none),
  `RELAY_MANIFEST`, `RELAY_REPO`, `RELAY_DEFAULT_BRANCH`, `RELAY_MERGE_BASE` and
  `RELAY_MERGE_HEAD` (the default branch's sha before and after the run), `RELAY_MERGE_MOVED`
  (`true`, `false`, or empty when either sha could not be read), `RELAY_MERGE_RANGE`
  (`base..head` when the branch moved, else empty), and `RELAY_CYCLE_JSON`, all of it as one
  JSON object. Read `RELAY_MERGE_MOVED` before trusting an empty range: empty there means
  unknown, not that nothing merged. Its output is appended to `<stem>.feeder.hook.out`; the
  feeder log and a `post_cycle` event record the result. It runs after the halt and usage
  limit rules have saved their counts, but before any wait they ask for, so a limit wait never
  delays it and a cycle that ends in a stop still runs it.
- `post_cycle_mode = "blocking"`, the default, waits for the hook, up to
  `post_cycle_timeout_seconds`, and logs its exit code. A nonzero exit, a timeout, or a command
  that cannot start is logged and the feeder goes on, unless `post_cycle_hold = true`: then the
  feeder stops with exit 2 and reason `post_cycle_held` instead of starting the next cycle or
  waiting. When the cycle's own rules already stopped the feeder, their exit and reason stand
  and the hold shows as `held` in the `post_cycle` event. A timeout kills the hook's whole
  process group, so a gate it started does not outlive it. Use it for work the next cycle
  should wait on: the full gate on the merged default branch, a push on a cadence. A blocking
  hook must leave the checkout on its default branch and clean, or the next cycle stops there.
- A hold outlives the feeder. It is written to the state file, with the hook's failure, the
  cycle, the time, and the merge range, even when the cycle's own rules already stopped the
  feeder, and it is notified once when it is set, whatever else that cycle decided, so a rules
  stop beside it (a run scoped halt, say) does not swallow the hold's own notification (issue
  #53). While it is set, every start is refused with exit 2 before it acts: by hand, by
  `--restart` or `--pin` (the live feeder is never asked to leave), by `--detach` (no child
  starts), by `--dry-run`, and by a cron line running `--once`, which logs each refusal and
  notifies only the hold itself. `feed <manifest> --status` shows it. Repair the default
  branch, then `feed <manifest> --release`, which clears the hold and starts nothing, and also
  removes a stop file left over from a `--stop` issued against the held feeder (which does not
  check whether that feeder is still alive to read it), saying so; start the feeder after it.
  Turning `post_cycle_hold` off in the sidecar does not release a hold already set. `--release`
  is refused beside a live feeder (exit 3) and beside any other flag.
- `post_cycle_mode = "detached"` starts the hook in its own session and does not wait; the log
  records its pid, and `post_cycle_hold` is refused beside it. The feeder polls each hook it
  started at the start of every later cycle and logs the exit code of one that has finished; a
  hook still running when the feeder leaves goes on. Use it for work that takes a
  person or a browser. It runs beside the next cycle, so it must not touch the checkout the
  runner merges into; give it a worktree of its own.
- With GitHub and no `ready.labels`, or Jira and no `ready.jql`, the feeder offers nothing. It
  never reads a whole backlog as ready.
- Markdown needs no `[ready]` at all: every unchecked box is ready, since the file has no way to
  say one task waits on another.
- `models.default` must be in `models.allowed`. A routing line or a card's `**Model:** name`
  body line outside the allowed set is ignored and logged. Whatever is chosen still passes
  `validate` before it reaches the manifest, so a model that belongs to another backend is
  refused there and that one card is left out. The closeout keeps `[closeout] model`.
- `models.fallback` maps a model to the model its tasks move to when it looks limited, and
  both sides must be in `models.allowed`. Off by default; with nothing configured a limited
  model only ever holds the whole cycle wait's attention. `fallback_hours` is how long a mark
  lasts when the CLI's own log gives no reset time. `limit_wait_seconds` and `limit_waits_max`
  size and bound the whole cycle wait; `quick_death_seconds` is the line between a quick death
  and a slow one. `CONCEPTS.md`'s Usage limit entry names the whole state machine these settings
  feed, confirmed, refuted, unconfirmed, marked, held, moved, deferred, and its Reason words
  entry lists every `waiting` and `leaving` word a feeder prints, including `model_held` and
  `usage_limit`. `feed <manifest> --retry-blocked <id>` queues one blocked task moved or held by
  the machine, by hand, the same way a queued retry runs on its own.
- `feed <manifest> --clear-limits` clears every mark and the whole cycle wait's streak in one
  step, for an operator who knows the limit is over; the retry queue stays, since a queued retry
  runs on its own once the mark it waited on is gone. With no feeder alive it clears and leaves;
  paired with `--restart` it clears as the new feeder takes over; against a live feeder with
  neither it refuses and says why.
- Settings are read when the feeder starts. After editing the sidecar, `feed <manifest>
  --restart` reloads them on whatever tree the feeder already runs from; a restart with no
  `--pin` keeps running from a checkout when it was never pinned. Add `--pin`, run from a git work
  tree, to also re-extract the current default branch commit (reusing an extract whose sha is
  unchanged rather than remaking it) into a pinned tree and hand the feeder over to it; from an
  existing extract or a plugin install copy `--pin` is a plain restart and extracts nothing.
  The order and routing files are read at every cycle and need no restart.

Exit codes of `feed`: 0 it left on its own terms (the stop file, an empty queue, a queue held
entirely by cards the launch scan refuses, `--once`, `--dry-run`, `--release`), 1 the manifest,
the sidecar, the ready source, or the checkout needs a person, including a ready source that
could not be read three cycles in a row with nothing left to run, 2 the whole cycle wait's streak
passed `limit_waits_max` (reason `limit_waits_exhausted`), or a blocking post cycle hook failed
with `post_cycle_hold` on, or its hold is still set, 3 another feeder holds this manifest. A queue of only scanned out
cards leaves with its own reason, `empty_queue_scanned`, naming the cards, not the plain
`empty_queue` a truly empty board leaves with. While `idle_waits_max` is above zero, the same
cards are named on every wait along the way there too: its `waiting` event reads `idle_scanned`
rather than the bare `idle` a genuinely empty queue waits under, and a `--once` run that meets
the same cycle carries the cards on its `leaving` event instead of losing them to the generic
`once`.

## 12. The browser test loop

Skip this unless the target is a web app and you want the feeder to test it after each landing.
The loop is a feeder feature, off unless the sidecar's `[test_loop]` table switches it on. It
runs a Test pass after each cycle that landed cards and a full tour at its start and each time
the queue drains, files what it finds as cards on the manifest's own tracker, builds them like
any other card, retests the fixes, and stops on a rule the feeder checks in code.
`docs/examples/browser-test-loop/` has a sidecar, a tour document template, a driver, and a
walk through to copy.

A loop needs four things the rest of this document does not ask for. The manifest runs on the
`claude` backend, since the Test and Filing processes run on `claude` only in this build. A
markdown tracker pairs only with `[shipping] push = false`, because the adapter reads the tracker
at the remote when the manifest pushes and the loop never pushes, so every filing would read as
unconfirmed. The checkout is clean and on its default branch when a pass starts, as it is for a
run. And the app has a tour document, a `prepare` command, and a browser driver of its own, all
in its repository.

The table, with its defaults:

```toml
[test_loop]
enabled = false               # nothing below is read until this is true
report_only = false           # findings to <stem>.findings.md, one tour, then stop
tour = ""                     # required: the tour document, relative to the target repo
url = ""                      # required: where prepare serves the app, http or https
prepare = []                  # required: an argument list, never a shell string
prepare_timeout_seconds = 600
model = ""                    # empty: the [models] default
effort = ""                   # empty: the [models] effort
timeout_minutes = 60          # one Test process
max_rounds = 6
max_hours = 24
max_cards_per_pass = 10
max_patches_per_area = 3
max_cards_total = 30
labels = []                   # every filed card carries these
allowed_tools = ["Bash", "Read", "Grep", "Glob"]
design_model = ""             # empty: design cards route like any other
design_note = ""
issue_type = ""               # empty: the tracker's default card type (Jira: Task)
```

- **`enabled`** switches the loop on. A sidecar without the table, or with `enabled = false`,
  runs exactly as it did before the loop existed: no pass, no `test_loop` state key, no loop
  events. `enabled = true` without `tour`, `url`, or `prepare` is refused, naming the key.
- **`report_only`** writes every finding, whatever its severity, to `<stem>.findings.md` beside
  the manifest and launches no Filing process, so the tracker is untouched. A report only loop
  runs one full tour and stops with the reason `report_only`. `relay test --report-only` is the
  same switch for one hand run.
- **`tour`** is the tour document, a path inside the target repository. Every markdown heading
  in it is an area, and a finding names its area exactly as the heading spells it; a finding
  naming anything else is recorded as invalid and never filed. Open it with one title heading
  over the areas, which is the one heading a report is not asked to cover, as the paragraph on
  areas a pass could not reach says below; then keep the rest of the opening text free of
  headings, and name there the driver, the signed in session's storage state file, and how to
  tell the sign in page. List each area's approval steps under its heading.
- **`url`** is where the app under test is served. The Test process is told the app is there
  and already serves the tested commit, and it never starts, stops, or moves it.
- **`prepare`** moves the app to the commit a pass tests and confirms it. It runs in its own
  process group, from a directory under the state directory rather than the target repository,
  with `RELAY_TEST_COMMIT` (the default branch's sha), `RELAY_TEST_URL`, and `RELAY_TEST_REPO`
  (the checkout's path) in its environment, under the lease heartbeat. Name any script in the
  argument list by its absolute path. Exit 0 means the app at `url` now serves that commit;
  anything else, or running past `prepare_timeout_seconds`, which ends its whole group, records
  the pass as `not_run` with the command's last output line, and nothing is filed. A checkout
  `prepare` leaves changed, a server log or a pid file written there, is `not_run` too, naming
  the first changed path, before any process launches; the file stays until someone removes
  it. So is a checkout it moved off the default branch or off that commit. Serve the app from a
  worktree of its own, outside the checkout the runner merges into, and have `prepare` compare
  the commit the app reports with `RELAY_TEST_COMMIT` before it exits 0, since a server that
  looks current can be serving code from before the last merge. A launched process cannot stop
  a server, so moving and restarting it lives only here.
- **`prepare` serves the app with its outbound integrations stubbed.** Mail, payments,
  webhooks, and every other call that leaves the app go to a local stub while the loop runs. The
  Test brief already tells the Test process never to approve, send, submit, post, or confirm
  anything that writes outside the app, and to test a feature that ends in an external write up
  to its approval step and no further. The stub is the second line of defense, so the brief's
  rule is never the only thing between a tour and a real customer.
- **`model`** and **`effort`** are the Test process's, the `[models]` values when empty. The
  model must be in `models.allowed`. While it is marked by a usage limit a pass runs on its
  `[models] fallback`, and when every model on that chain is held the pass waits for the next
  pass point. The Filing process runs on the manifest's `[closeout] model` under its closeout
  timeout. A Filing process that could not launch, timed out, lost the lease, left no readable
  `relay-filed` block, or had its commit reset by the scope check fails the pass with that
  sentence as its reason: a pass that had something to file is `ran` only when a readable
  block was confirmed, so a filing that did not complete is never read as findings that
  produced no card. A pass with nothing to file, or a report only pass, launches no Filing
  process and is `ran` on its report alone.
- **`timeout_minutes`** bounds one Test process; past it the pass is `failed`.
- **`max_rounds`** caps full tours that ran, **`max_hours`** caps the loop's clock from its
  first pass, and **`max_cards_total`** caps the cards the whole loop files, the planning card
  of `max_patches_per_area` not counted. Each stops the loop with its own reason, below.
- **`max_cards_per_pass`** caps the cards one pass files. The high and medium findings past it,
  or past what is left of `max_cards_total`, are named in the pass record as over the cap or
  over the budget, and never filed.
- **`max_patches_per_area`** is how many times an area's loop cards may land and have their
  check file in the same area again. At the cap the loop stops testing that area, and the next
  pass files one planning card for it labelled `attended`, outside the per pass cap, for a
  person to plan. The planning card is a person's: it counts toward neither the loop's card
  budget nor a pass's new cards, and it is not a finding to the stop rules, so a tour with
  only lows beside it stops clean and a tour whose other findings all went to open cards or
  stopped areas stops on open findings.
- **`labels`** are put on every card the loop files. The ready source must admit a card carrying
  them, or the loop files cards nothing builds: on GitHub, without a ready command, every
  `[ready] labels` value must also be in `test_loop.labels`, and `relay test` refuses the sidecar
  when one is missing. Under a Jira query or a ready command, check it by hand; after each pass
  the feeder makes one ready read and notifies once, naming every confirmed card the ready source
  did not return, and does not tour again for it. Keep `attended` in `[deny] labels`, so a
  planning card is never appended.
- **`allowed_tools`** is the Test process's allow list, and must name at least one tool. Its
  deny list is the manifest's, the runner's own, and `Bash(gh *)`, so a tour cannot reach the
  tracker by any route. The Test process holds no tracker write tool; only the Filing process
  files.
- **`design_model`** and **`design_note`**. A finding that changes what a user sees is filed as
  a design card, with `design_note` added to its body. The feeder routes a design card the loop
  filed to `design_model`, which must be in `models.allowed`: after the routing file and before a
  `**Model:** name` body line, so it holds where the ready source returns empty bodies.
- **`issue_type`** is the type a filed card is created as, on a tracker whose create call needs
  one. On Jira that call requires an issue type name, and the Filing process is told to read the
  project's issue types first and file nothing under a type the project lacks, so set this to a
  type the project has; empty means `Task`, the one type every Jira project template ships
  with. The loop's labels ride in the create call's additional fields. A GitHub issue and a
  markdown line have no type, so those trackers ignore the key.

A key the feeder does not know is refused, as everywhere in the sidecar. So are a `prepare`
written as a string, an integer below one, `labels` or `allowed_tools` that is not an array of
strings, a `tour` that is absolute or leaves the repository, and a `url` that is not http or
https with a host.

**When a pass runs.** At the first cycle of a loop with no tour that ran, a full tour before the
ready read. After a cycle that landed cards, once it has settled and its `post_cycle` hook has
run, a check of the landed cards that are not the last generation. In an idle cycle, before
leaving on a true empty queue, a full tour; when it filed a card the ready source returns, the
feeder goes round to build it instead of leaving. A check that is `not_run`, or that never
started because the loop model was held, the post cycle hook held, or the cycle's own rules
stopped the feeder, keeps its cards for the next check. A check that `failed` does not: its
cards are never checked, since a card the pass refuses to read would otherwise fail every check
after it. A pass that is `not_run` or `failed` counts as no round, is logged, and is notified
once per kind and status until a pass of that kind runs.

**Generations.** A card filed by a tour, or by checking a card the loop did not file, is
generation 1. A card filed by checking a generation 1 card is generation 2, the last: its fix
lands on the gate alone and is never checked for new cards. That is what stops a check from
feeding itself.

**Two filed cards touching one file never share a batch.** The feeder holds a filed card out of
a batch while another card the loop filed with the same cause file is in that batch or listed
and unsettled, and logs it once. Cards the loop did not file are batched as before.

**How the loop stops.** After each pass the feeder asks these in this order, and the first
that holds is the stop, with its own reason word, written to the state file's stop record, the
`test_loop_stopped` event, one notice, and `feed --status`. So a tour that both reaches
`max_rounds` and brings the loop to `max_cards_total` stops on `budget`, not `round_cap`.

- `report_only`: a report only loop ran its one full tour.
- `clean`: a full tour found nothing above low, an attended planning card not counted, and
  reached every area. A tour that names areas it could not reach is never clean, however few
  findings it carries, since those areas were not looked at: with nothing serious and nothing
  filed it stops nothing, the feeder notifies once naming the areas, and the loop goes on. A
  check pass with only lows never stops the loop.
- `budget`: the loop has filed `max_cards_total` cards, attended planning cards not counted.
  This is also asked before a pass starts.
- `open_findings`: a full tour's high and medium findings produced no new card, each going to an
  open card it was commented onto, a stopped area, or a claim the tracker never confirmed, and
  an attended planning card filed beside them is no new card. This is not a clean stop. A tour
  whose Filing step did not complete is `failed`, not a tour that produced no card, and never
  stops the loop this way.
- `round_cap`: `max_rounds` full tours ran.
- `clock_cap`: `max_hours` have passed since the loop started. This is also asked before a pass
  starts, so it stops the loop at the next pass point.

A stopped loop starts no further pass, and the feeder goes on building the cards already filed
under its ordinary rules. The loop's stop does not end the feeder.

**The sign in.** The Test process never types a credential. The operator signs the app in once,
in a headed browser the driver opens, and the driver writes a browser storage state file that
every headless visit loads. Keep that file outside the repository: the Test process runs in a
fresh detached worktree that would not carry an ignored file, and a session never belongs in
git. The tour document names its path. A missing file, or a visit that lands on the sign in
page in front of every area, is a `not_run` report, and signing in again is the operator's step.

**Areas a pass could not reach.** When some areas answer and others do not, for a sign in, an
error page, or a feature that never loads, the Test process tests what it can reach and lists
the rest in the report's `untoured` key, each by its heading, with the why in `reason`. The pass
checks each name against the tour document's headings, as it checks a finding's area, and a
name that is not a heading fails the pass rather than being dropped, since dropping it would
read the tour as more complete than the process said. A stopped area is skipped, not
unreached, and is left off the list. The pass record and, in report only mode, the findings
file carry the list. A report that names every area there is to reach is recorded `not_run`
with its reason, and files nothing. The document's title, the first heading when it is the
only heading of its level, is not an area a report has to cover, and neither is a stopped
area.

**What a pass writes**, all beside the manifest:

| File | What it is |
|---|---|
| `<stem>.test/pass-<n>.json` | the pass record: kind, commit, cards checked, status and reason, every finding with its outcome, the confirmed filed and commented ids with each one's area, design flag, and cause file, the over cap, over budget, dropped, low, invalid, and planning findings, the approval steps reached, the areas the process could not reach, notes, both transcript paths, and timings |
| `<stem>.lows.md` | every low finding, appended per pass; lows never reach the tracker |
| `<stem>.findings.md` | report only mode's findings, appended per pass, with the areas the pass could not reach named first |

A pass also writes its briefs and logs under the state directory, and the feeder records the loop
under one state file key, `test_loop`: the start time, the rounds, one entry per pass, the filed
cards with their generation, area, design flag, and cause file, the patch counts, the stopped
areas, and the stop record. Every pass writes one `test_pass` event carrying its transcript
paths, and the stop writes one `test_loop_stopped`. `feed <manifest> --status` prints the loop's
state: on or off, report only, round and cap, hours used and cap, cards filed per pass, the
generation counts, the stopped areas, and the stop reason.

**One pass by hand.** `relay test` runs one pass without a feeder, under the same sidecar:

```bash
python3 skills/relay/scripts/relay_cli.py test <manifest> --tour --report-only
python3 skills/relay/scripts/relay_cli.py test <manifest> --cards 41 42
```

`--tour` or `--cards ID...` is required. `--report-only` forces report only mode for this pass,
`--stopped-area NAME` skips an area and `--plan-area NAME` files its attended planning card,
each repeatable and each a heading of the tour document, `--budget N` is the cards left in the
loop's budget (the sidecar's `max_cards_total` by default), and `--model NAME` picks the Test
process's model. It prints the pass record's path last. Exit codes: 0 the pass ran, 1 the
manifest, the sidecar, or the command line is wrong and nothing was launched, 2 the pass is
recorded as not run or failed, 3 another runner holds the lease. Run a report only tour by hand
before the first feeder with the loop on, and read the findings file and the pass record.

## 13. Exit codes of a run

Exit codes: 0 the run reached the end of the manifest, 1 the manifest or environment is wrong,
2 the run halted, 3 another runner holds the lease.

Once a task ends in a confirmed usage limit death, a serial run launches no further task on that
model for the rest of that run, always on, with no field to turn it off; the tasks it passes over
keep whatever record they had and the run still ends completed with exit 0, its terminal record
and the summary naming them beside the model. `run --defer <id>`, repeatable, gets the same
treatment by name instead of by evidence, for one listed task, for one run, leaving its record,
branch, and card exactly as they were; an id the manifest does not list refuses the run before the
lease is taken, and a triple manifest refuses the flag outright. `CONCEPTS.md`'s Usage limit entry
names the state machine both this and the feeder's own rules read from.

`validate` reads each listed task's card and makes the runner's launch checks on it. A card whose
text trips the `.claude/` scan is an error, since the runner would skip that task; a card that
cannot be read or already reads done is a warning.

`validate` names the property that failed rather than the exit code, and it never invents a
sentence on your behalf. Read the summary after a halt: it carries the halt class, the cause
line, and the checks a human still has to make, so a transcript is never the place to start.
`skills/relay/SKILL.md` carries the halt class table with what each class means and what to do.

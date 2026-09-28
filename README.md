# Native Relay

Run a list of pre-defined tasks through a native plan, build, review, verify, record pipeline,
one fresh headless process per task, serially by default and unattended. No plugin sits in the loop: the
Task process plans in a message, builds, runs the CLI's built in code review, runs the project's
own verification, records what the project's method says a unit records, and exits. The runner
then runs the project gate, merges, pushes unless the manifest turns pushing off, verifies the
landing, and launches a short Closeout
process that writes the outcome to the tracker and judges whether the task produced a learning
worth keeping.

Each task gets its own context window. Nothing carries between tasks except what landed in git
and on the tracker. The runner verifies that landed state itself before it starts the next task,
and it never trusts a run's own report of success.

Native Relay is a hard fork of `compound-relay`, taken 2026-09-07. That repository drives the same
outer loop through the compound-engineering plugin and stays as it is; this one drives it with
nothing but the CLI.

## Why this exists

Single-task agent pipelines are good and plentiful. Nobody ships the outer loop: take the next
ticket, run it in its own process, confirm it merged, judge whether it produced a learning worth
keeping, take the next one. Relay is that outer loop and nothing more.

## What a project needs before Relay can run against it

1. **Independent tasks.** Each one can be planned, built, reviewed, and merged without the
   others in flight.
2. **Durable state between tasks.** A merge commit on the default branch and a card on a board
   are the only memory. If a task's outcome lives anywhere else, the next task cannot see it.
3. **An external gate that refuses broken changes.** A pre-push hook that runs the test suite,
   or CI that blocks the merge. This is what makes unattended acceptable. A project without one
   gets a gate before it gets Relay.
4. **Its own definition of verification, written down.** The Task process runs inside the
   project's checkout and reads the project's own instructions (`CLAUDE.md` or its equivalent)
   to learn what a unit of work must pass before it counts as done, and what a unit records.
   Relay never defines that; the manifest names only the one gate command the runner itself
   runs before the merge.

## Shape

- **Runner:** a small script. Reads a manifest, computes a conservative schedule when `dispatch`
  was chosen, launches that Task's backend
  with the task's model, effort, and permission allowlist, waits, verifies the landed state, runs
  the closeout as a separate short process on the same backend, advances or halts. It holds no
  project knowledge and never writes to a tracker. `run` is one Task at a time. A normal
  `dispatch` is serial unless the operator selects the conservative `parallel` policy; it can
  schedule tasks on the same CLI. Workers use git worktrees and land in listed order.
- **Manifest:** one file per project. Names the tracker adapter, the task list, the shipping
  mode, any mirror rule, the disallow patterns, and the docs root the closeout may write a
  learning under. Everything project-specific is data here, never code in the runner. One
  shipping mode runs today, `local_merge`, where the runner runs the gate and owns the merge.
  `shipping.push = false` keeps every merge on the local default branch and pushes nothing, so a
  whole run stays on the machine until the operator ships it by hand. `pr_terminal` is named in
  the schema and refused by `validate` until its run loop sequence exists.
- **Task process:** one headless invocation per task, reading a brief the runner renders from a
  template. Its steps are: move the card, branch, plan in a message, build, run the built in
  review, run the project's own verification, record, comment the card, print the return
  envelope. The envelope is a claim; the runner decides landing from git and the tracker.
- **Closeout process:** a second short invocation after each task. Duty one writes the outcome
  to the tracker. Duty two judges whether the task produced a learning and, if so, writes one
  markdown file under the manifest's docs root and commits it inside the allowed paths.
- **Feeder:** optional, for a queue too long or too dependent to list up front. `relay feed`
  is a loop around the runner that appends a few ready cards to the manifest, runs it, reads the
  summary, and repeats, so one manifest can run for a day. See Continuous runs below.
- **Skill:** `/relay`, for Claude Code hosts. Writes the manifest from a conversation, checks the
  properties above, launches the runner. Every step it takes is a runner subcommand an operator
  can run by hand from a shell, which is how a Codex or Grok Build host uses Relay.

## Backends

Native mode runs on `claude`, `grok`, and `codex`. The review step is `/code-review` on Claude,
`/review` on Grok, and the direct foreground command `codex exec review --base <branch>` on
Codex. Relay accepts the Codex step only when its transcript records the exact argv, a zero exit
status, and retained review output. Grok's skip is undetectable: the digest lists
`review_skipped` as not checked.

## Normal-manifest dispatch scheduling

An ordinary manifest has a dispatch policy, separate from the exact three-backend triple profile.
Before an attached normal dispatch, Relay offers the operator two choices: `serial` (the default) or
`parallel`. A noninteractive, detached, or otherwise non-promptable launch defaults to `serial`;
pass `dispatch --policy parallel` only when the operator has chosen it.

`parallel` is a request for safe overlap, not permission to guess. Relay reads the repository and
the task declarations before any worker starts, then prints a pre-launch schedule. A task may
declare narrow repository-relative `declared_paths`; Relay can overlap a pair only when its
deterministic evidence establishes disjoint, bounded work. An absent or broad declaration,
ambiguous evidence, overlapping paths, or a change touching shared configuration, dependency
manifests, migrations, CI, root documentation, or generated output creates a serialized edge.
Read-only semantic analysis may add evidence but never authorizes overlap; uncertainty always
serializes.

Workers in a permitted concurrency group each receive an isolated worktree. Landing remains
serial in manifest order, with the usual gate, hooks, verification, lease, and halt behavior.
Relay displays every serialized edge and its reason before the first worker launches, then follows
that schedule without asking for another authorization.

## Triple board runs

Set `[execution] mode = "triple"` to run exactly three independent GitHub Projects or Jira cards
from one `relay run` command, with one explicit task each for Claude, Grok, and Codex. Relay atomically
claims the three immutable cards and a repository integration fence, launches each
backend in a disconnected independent clone, then imports, gates, merges, pushes, verifies, and
closes out the results in manifest order. It does not use the normal-manifest dispatch-policy prompt or
scheduler.

Triple mode requires `tracker.adapter = "github"` or `"jira"`, `shipping.mode = "local_merge"`,
`shipping.push = true`, three distinct nonexcluded task ids, and each backend exactly once. The
Git host must permit atomic pushes of Relay's custom claim refs. Claims never expire by clock;
after a crashed coordinator, inspect the retained state and worker evidence. Relay deliberately
does not reclaim a remote claim automatically. Custom-ref pushes preserve the target
repository's normal pre-push hooks; their cost or failure is a remote operational constraint, and
a failed release retains exact-token claims for explicit guarded recovery.

Serial Jira pairs with `claude` and `grok`, which write through Atlassian MCP. In a Jira triple,
the coordinator uses its already-scrubbed Jira REST credential for exact claimed-card transitions
and outcome comments; workers, including Codex, receive neither that credential nor Jira write
tools. A Jira triple manifest must explicitly set `tracker.coordinator_rest_writes_authorized = true`
and name `tracker.in_review_transition`; Relay selects that workflow label and verifies that its
target status exactly equals `tracker.in_review_status`. Its `tracker.transition_labels` map must
also name exact labels for the expected in-review, terminal, and possible return statuses. The credential must have Jira
transition/comment permissions. Serial Codex stays refused.

## Continuous runs: the feeder

A run's task list is fixed when the run starts, and a resumed run skips what landed. `relay feed`
turns that into a continuous run by growing the manifest between runs:

```text
   stop file? -> checkout clean and on its default branch? -> pre cycle command
        -> read the READY cards -> take a small batch -> pick a model per card
        -> append [[tasks]] to the manifest -> relay run -> read the summary
        -> exclude what halted twice -> post cycle command -> repeat
```

```bash
python3 skills/relay/scripts/relay_cli.py feed <manifest> --dry-run   # would offer, would hold, or would skip each ready card; writes nothing
python3 skills/relay/scripts/relay_cli.py feed <manifest> --dry-run --detach  # refused: a dry run never detaches
python3 skills/relay/scripts/relay_cli.py feed <manifest> --once      # one cycle, never waits
python3 skills/relay/scripts/relay_cli.py feed <manifest> --detach --notify
python3 skills/relay/scripts/relay_cli.py feed <manifest> --stop      # leave after the current cycle
python3 skills/relay/scripts/relay_cli.py feed <manifest> --release   # clear a failed post cycle hook's hold; starts nothing
python3 skills/relay/scripts/relay_cli.py feed <manifest> --restart --detach --notify
python3 skills/relay/scripts/relay_cli.py feed <manifest> --retry-blocked T-4  # relaunch one blocked task next cycle
python3 skills/relay/scripts/relay_cli.py feed <manifest> --clear-limits  # clear marks, the streak, and deferrals; refused beside a live feeder without --restart
python3 skills/relay/scripts/relay_cli.py feed <manifest> --status    # running? and its last cycle, marks, held tasks, and the streak; --json for data
python3 skills/relay/scripts/relay_cli.py feed <manifest> --follow    # new events as JSON lines until it leaves
```

`--dry-run` previews the next cycle without appending anything: `would offer` for a card it would
add, `would hold` for one routed to a model held back by a usage limit, and `would skip` for one
the runner's own launch time scan would refuse. It reads only the ready source, so a card it
offers is not a guarantee; the markdown adapter's ready read, for one, always returns an empty
description, so a `.claude/` mention sitting in a comment there is invisible to the dry run and
only trips the runner's scan at launch. The launch scan is the one that decides. Outside
`--dry-run`, a scanned card the feeder leaves out of the batch is logged once, naming the card and
the reason, and named again at every feeder start and again if it later trips the scan a second
time.

Three rules carry it. **Only ready cards are appended.** Ready is the tracker's own account that
a card can start now: open issues carrying every configured label on GitHub, the cards a
configured JQL query returns on Jira, every unchecked box in a markdown tracker, or whatever a
project's own ready command prints as JSON. The batch is small, three by default, so a dependency
that lands in one cycle releases its dependants in the next. This is what lets a feeder run a
queue whose cards depend on each other, which a plain manifest must not list. **A task that halts
twice is excluded**, with the reason written into the manifest, because the runner relaunches a
halted task on every run. **A cycle touched by a usage limit** follows the state machine
`CONCEPTS.md`'s Usage limit entry names, marking, moving, holding, or waiting a task rather than
counting an ordinary halt; 429 in a task's log is what is detected, and only the whole cycle wait
still leans on timing alone.

`[models] fallback` and `fallback_hours` in the sidecar (off by default) set the per model side of
that same machine, so a limit on one model moves its tasks to another rather than losing the
whole cycle to it. `CONCEPTS.md` names every transition; `run --defer` and `feed --clear-limits`
are covered below.

Every project fact is data in a sidecar file beside the manifest and named from its stem. For
`queue.toml` the feeder reads `queue.feeder.toml` (settings, all optional), `queue.order`
(priority, one id per line), and `queue.models` (model routing, one `id model` per line, read
fresh each cycle), and writes `queue.feeder.state.json`, `queue.feeder.log`,
`queue.feeder.events.jsonl`, and `queue.feeder.hook.out`.
`docs/examples/feeder/` has one of each. A card's model is the routing file's line, else a
`**Model:** name` line in the card's body, else the sidecar's default, and a name outside the
sidecar's allowed set is ignored and logged.

The sidecar can also name two hooks, plain commands run in the target repository. `pre_cycle`
runs before the ready cards are read, for a board whose ready labels are derived. `post_cycle`
runs after each `relay run` the feeder settles, learning that cycle's landed, halted, blocked, and
skipped ids and the default branch's merge range from its environment, and its output is appended
to `queue.feeder.hook.out`. Blocking, the default, it is waited on and logged; with
`post_cycle_hold` set, a nonzero exit holds the feeder at exit 2 until the operator repairs the
default branch and clears it with `feed <manifest> --release`. Detached, it is started and left to
run, reaped at a later cycle's start.

The feeder never merges, pushes, moves a card, or edits the target repository, and it holds no
way to write to a tracker. It does write the manifest, which no other runner code does: every
edit is parsed back, validated by the same rules `validate` applies, and renamed into place in
one step, so a bad edit never reaches the file. It appends nothing while a runner holds the
lease, stops with exit 1 when the checkout is dirty or off its default branch, or when a run
halts for a cause outside the task such as the remote moving, and reports every task the runner
skipped, with the reason. One manifest has one feeder: a second `feed` exits 3.

**It leaves when the queue is empty.** A cycle with nothing ready, nothing left to run in the
manifest, and no runner holding the lease ends the feeder with exit 0, so no process sits
polling an empty board. Set `idle_waits_max` in the sidecar to wait that many times first, thirty
minutes apart, for a board where a person releases cards through the day. A ready source that
cannot be read is not an empty queue: with nothing left to run the feeder waits and asks again,
and stops with exit 1 after three failed reads in a row. Ready cards that validate refused are
not an empty queue either; the feeder stops with exit 1 and names them, since only a routing
change releases them. Ready cards the launch scan refuses wait the same as an empty queue, but
leave with their own reason, `empty_queue_scanned`, naming the cards, since a watcher reading
`empty_queue` off the last event would otherwise conclude nothing was ever ready. A card still
scanned out is named again at every feeder start, not only the first one, and again whenever it
scans clean and later trips the scan a second time.

**It answers for itself.** `feed <manifest> --status` says whether that manifest's feeder is
running and what its last cycle did: what it appended, what landed, halted, blocked, or was
skipped, and what it is doing now, waiting on a usage limit until a given time for example, or
why it left. It also lists every model marked exhausted with its expiry and where that time came
from, every task held back by one, and the usage limit wait streak; `feed <manifest>
--clear-limits` clears all three in one step, refused beside a live feeder unless paired with
`--restart`. The answer comes from the pid the feeder recorded in its state file, checked
against the manifest's own lock file, so another manifest's feeder or a recycled pid can never
read as this one alive; a process listing cannot tell two boards apart. `status <manifest>`
adds the same answer as one `feeder:` line. For a watcher, the feeder writes one JSON line per
moment to `queue.feeder.events.jsonl`: `started`, `cycle_started` (the ids appended),
`cycle_result` (the run's exit code and the ids by outcome), `post_cycle_started` (a blocking post
cycle hook has begun) and `post_cycle` (a blocking hook's result, including a hold; a detached
hook has none yet, so its `post_cycle` is written when it starts, carrying its pid and no exit
code), `waiting` (a reason and when it ends), `limit` (a mark, a move, or a hold, naming the
model), and `leaving` (the exit code and a reason word such as `stop_file` or `empty_queue`).
`CONCEPTS.md`'s Reason words entry lists every one of those words.
Every line names its manifest and its pid. `feed <manifest> --follow` prints new lines as they
come and ends when the feeder leaves, or with a `not_running` line of its own when the feeder
is gone without saying so; `--events` prints the lines so far. A feeder handing over to
`--restart` leaves with the reason `restart`, and a follow goes on to the new feeder's lines.

It launches the runner from the same tree it was started from. Start it from a pinned extract of
a commit and it drives that extract, whatever happens in your checkout meanwhile. Started from a
git checkout it says so, on the terminal and in its log, because every cycle would then run
whatever that checkout holds. `feed <manifest> --pin` is the one flag that fixes it: it extracts
the default branch's commit, never HEAD, under `~/.relay/extracts/native-relay-<sha>` and starts
the feeder from there with `--restart`, so a running feeder finishes its task and hands over.
The default branch is the manifest's `project.default_branch` when the manifest's repo is this
checkout, else the checkout's `origin/HEAD`; when neither resolves the flag refuses and names
the `git remote set-head` command that sets it, which a checkout made without `git clone` needs
once. It also says when `origin` holds commits the local default branch lacks. A checkout
that is also the run's target sits on a task branch while that task is built, and pinning HEAD
there would run unmerged, ungated work at every later cycle. `<sha>` is always the first 12
characters, what `git rev-parse --short=12 main` prints, so an extract made by hand under that
name is reused. The flag prints the branch beside the sha, and says when the checkout sits on
another branch or holds uncommitted edits, neither of which is in the extract. With `--dry-run`
it says what it would extract and writes nothing. Adding `--detach` to that pair is refused the
same way as plain `--dry-run --detach`: a dry run never detaches, so nothing runs and nothing is
written. For the same
reason, editing the sidecar's settings or cutting a new runner does nothing to a feeder already
running; `--restart` asks the old one to leave, waits for it, and takes its place, and nothing is
killed, so the task in flight finishes and merges normally. The order and routing files are the
exception, since they are read at every cycle.

## Platform

- Python 3.11 or later. The runner is standard library only and reads TOML with `tomllib`,
  which arrived in 3.11.
- macOS or Linux. The lease uses `fcntl`, so Windows is out until that changes.
- CLI versions the pins in `skills/relay/scripts/relay/contracts.py` were observed against:
  `claude` 2.1.250, `codex` 0.149.0, `grok` 1.0.25. A newer CLI usually works; the runner records
  the version it actually ran beside the pinned one in the terminal record so drift is visible.
- For a Jira tracker, two environment variables the manifest names, by default
  `JIRA_API_TOKEN` and `JIRA_EMAIL`. For GitHub Projects, a logged in `gh`.

## Install

Nothing in Relay needs editing to run against a new project: every project specific fact lives in
a manifest outside the target repo.

### As a Claude Code plugin

This repository is its own single plugin marketplace (`.claude-plugin/marketplace.json`):

```bash
claude plugin marketplace add https://github.com/philgutowski/native-relay
claude plugin install native-relay@native-relay
```

The install is a copy, not a link: after changing the skill or the runner, bump `version` in
`.claude-plugin/plugin.json` and run `claude plugin update native-relay@native-relay`. The skill
carries the runner with it, so nothing else is needed.

### By clone, for any host

The runner is the product and the plugin is one thin wrapper over it. From a shell on any host,
including a Codex or Grok Build session:

```bash
git clone https://github.com/philgutowski/native-relay
cd native-relay
python3 skills/relay/scripts/relay_cli.py validate docs/examples/manifest-markdown.toml
```

It will refuse until `project.repo` points at a real checkout, and it names what is missing.
`docs/manifest-authoring.md` is the authoring procedure the `/relay` skill follows, written as a
plain document so it can be followed by hand. The Task processes still launch `claude`, so the
Claude Code CLI has to be installed on the machine and logged in, whichever host you drive the
runner from.

## Use

Copy the example that matches your tracker from `docs/examples/`, point it at your repo, and
answer the four qualifying sentences in it (`docs/manifest-authoring.md` walks through every
field). Then either run `/relay` in a Claude Code session, which reads the tracker, confirms the
list with you, validates, and launches the runner detached, or run the verbs yourself:

```bash
python3 skills/relay/scripts/relay_cli.py validate <manifest> --list
python3 skills/relay/scripts/relay_cli.py run <manifest>
python3 skills/relay/scripts/relay_cli.py run <manifest> --detach --notify
python3 skills/relay/scripts/relay_cli.py run <manifest> --follow --phases --bar
python3 skills/relay/scripts/relay_cli.py run <manifest> --defer T-4   # leave that one listed task alone for this run, repeatable
python3 skills/relay/scripts/relay_cli.py status <manifest>
python3 skills/relay/scripts/relay_cli.py tail <manifest> --bar
python3 skills/relay/scripts/relay_cli.py summary <manifest>
python3 skills/relay/scripts/relay_cli.py audit <manifest>
python3 skills/relay/scripts/relay_cli.py verify <manifest> <task-id>
python3 skills/relay/scripts/relay_cli.py lease <manifest>
```

A first launchd or cron launch of the runner sits outside Terminal's privacy grants. Before that
fire, grant the python binary that job launches access to the folders it will read (the checkout
and the state directory), typically Documents when the checkout lives there, in System Settings,
Privacy and Security, Files and Folders, or grant Full Disk Access. For launchd that path is the
ProgramArguments python. For cron it is the python in the crontab command. The grant is per binary
and holds until that executable's identity changes. Without it the process stalls on a Files and
Folders or Full Disk Access prompt on that Mac, with no halt class and no log line, because nothing
has started yet. Look at the Mac display.

`--notify` on macOS fires a desktop notification as each task's status moves and once at the end
with the run's counts. Each status move says how far along the run is beside the move itself,
`T-3 is now landed; 3 of 8 settled, roughly 40m left`, so one notification is enough to know
whether to come back. The runner carries it, so a run launched with a bare `--detach` from
launchd or cron reaches you with nothing attached to it, and a run you launched with `--follow`
keeps notifying after you stop following.

`--bar` on `run --follow` and `tail` prints a progress bar line, `[#####...............] 2 of 8
settled; 2 landed, 1 running, 5 todo; T-3 running 4m 12s; roughly 40m left`, whenever the counts
move and once a minute in between. It is a new line each time rather than one that redraws, so it
reads the same in a terminal, in a session's tool output, and in `runner.log`. It never notifies.

Under a feeder the manifest's unsettled tasks are only the current cycle, so the estimate on each of
these lines reads `roughly 1h 2m left in this cycle`. It prices the cycle's cards, not the ready
queue behind them. `status --queue` prices that queue on a line of its own, `queue: roughly 21h 4m
for 37 ready card(s) beyond this cycle`, reading the ready source the feeder reads and applying its
deny set. Each card is priced at the mean landed duration of the model it would be routed to, or at
the mean of every landed task when that model has none. It cannot count cards that are not ready
yet or residuals nobody has filed, and says so. A read that fails in any way prints a sentence on
that line and never fails `status`.

The flag is what makes that read happen, because it is not read only. It runs the sidecar's ready
command in the target repository, or reads the tracker, at that moment and beside whatever Task
process is building there, so a command that fetches or writes a cache file does so beside the live
run. The command gets a minute, and at that bound its whole process group is sent SIGTERM, then
SIGKILL for whatever is left five seconds later. Plain `status` under a feeder prints `queue: not
read` and names the flag instead, and the pre cycle hook runs from neither form.

A board stays honest across a run. The task process moves a card to the in review status at its
first step, and a Closeout for a blocked or halted task returns it to the status it read before
the run, since nobody is on it any more. At the end of every run the runner audits every card
against its record and git, writes the result to the state file, and names the count on the
terminal notification; `audit <manifest>` is the same pass on demand, taking no lease and
writing nothing. It reports and never repairs: a card in review with no process on it, a landed
card that was reopened, a closed card nothing landed for, and a card that could not be read.

`status` is the one screen answer to how far along a run is: the same bar, the landed, running,
halted, and todo counts, the elapsed per task and in total, and a rough estimate of what is left drawn from
the mean of the tasks that have already landed. It says it has no estimate rather than guessing
when nothing has landed yet. Like `tail` it takes no lease, and plain `status` reads state only,
running no command and reading no tracker, so it is safe to poll against a live run. `--queue` is
the one form that reaches outside the state directory, as above.

Every launched task's record also carries a snapshot of the host, load average, free and inactive
memory, memory held by the macOS compressor, and cumulative swap counters, read just before the
Task process starts and just after it exits. `summary` prints them as one `host:` line under the
timing, with swap shown as the difference between the two reads. Read that line before blaming a
slow task on the card: rising load, vanishing free and inactive memory, a compressor that grew by
gigabytes, or thousands of swapouts mean the host was starved instead.

`tail` is how you watch a run that is already going. It follows each task's output in order and
prints it decoded, one line per event, instead of the stream json that lands in `runner.log`. It
works before, during, and after a run, takes no lease, and exits when the run reaches a terminal
record.

The run halts rather than continuing past an outcome it cannot confirm, and the summary names the
halt class, its cause, and what a human still has to check. A task that completed without running
the review step lands if the gate passes, and the summary lists it as a diff to review by hand.
Repair by hand, then run again: the runner re-verifies what halted and resumes at the first task
that did not land. A manifest may opt into continuing past a halt contained to one task instead
(`on_halt.continue_past_task_halt`); the summary still lists that task as a check-by-hand item,
and the same repair-and-rerun path applies.

Part of that repair can be the manifest itself. Editing a task's `model` moves any task that has
not landed, and the next run launches it where you sent it, naming the move on its output and on
the task's record. A stranded task branch is still refused the same way, judged against the name
and baseline the record already carries, so the edit does not get a task past it; a blocked task
needs `--retry-blocked` before a reassignment reaches it. `--retry-blocked T-4` retries that task
alone, where the bare flag retries every blocked record.

A run also protects itself. Once a task ends in a confirmed usage limit death, `run` launches no
further task on that model for the rest of that run; the tasks it passes over keep their record
untouched and a later run reaches them like any task it has not tried yet, so a limit on one model
costs at most one task on it, not the whole remaining list. `run --defer T-4` asks for the same
treatment by name instead of by evidence, leaving that one listed task alone for one run; an id
the manifest does not list refuses the run, and a triple manifest refuses the flag outright.
`CONCEPTS.md`'s Usage limit entry names the whole state machine this and the feeder share.

## Where things are

- `CONCEPTS.md`: the vocabulary. Runner, Manifest, Lease, Task process, Closeout process,
  Backend, Review step, Halt class, Cause line, Verify-landed, and for continuous runs Feeder,
  Cycle, Batch, Ready source, Model routing. Read this first.
- `docs/manifest-authoring.md`: how to write a manifest by hand, field by field.
- `docs/plans/2026-09-07-native-mode-plan.md`: the native mode plan and its decisions.
  `docs/plans/2026-08-25-1346-feat-relay-outer-loop-plan.md` is the original outer loop plan,
  including the requirements, the key technical decisions, and the halt class table, as amended
  by the native plan.
- `docs/plans/2026-09-19-feat-generic-feeder-plan.md`: the feeder, its decisions, and the
  live proof it still owes.
- `docs/examples/`: one manifest per adapter, and `docs/examples/feeder/` for a feeder's sidecar,
  order file, and routing file.
- `docs/solutions/`: the learnings store. Problems already solved here, filed by category with
  frontmatter so they can be searched rather than read.
- `skills/relay/scripts/relay/`: the runner package.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Every test runs against a stub `claude` (and stub `codex` and `grok`) on the PATH with a temporary
`HOME`. Nothing in the suite launches a real model, touches a network, or invokes `gh`. The suite
takes several minutes; single modules run from `tests/` with `python3 -m unittest test_<name>`.

## License

MIT, see `LICENSE`.

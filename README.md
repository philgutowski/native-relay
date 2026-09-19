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
        -> exclude what halted twice -> repeat
```

```bash
python3 skills/relay/scripts/relay_cli.py feed <manifest> --dry-run   # what would it append? writes nothing
python3 skills/relay/scripts/relay_cli.py feed <manifest> --once      # one cycle, never waits
python3 skills/relay/scripts/relay_cli.py feed <manifest> --detach --notify
python3 skills/relay/scripts/relay_cli.py feed <manifest> --stop      # leave after the current cycle
python3 skills/relay/scripts/relay_cli.py feed <manifest> --restart --detach --notify
```

Three rules carry it. **Only ready cards are appended.** Ready is the tracker's own account that
a card can start now: open issues carrying every configured label on GitHub, the cards a
configured JQL query returns on Jira, every unchecked box in a markdown tracker, or whatever a
project's own ready command prints as JSON. The batch is small, three by default, so a dependency
that lands in one cycle releases its dependants in the next. This is what lets a feeder run a
queue whose cards depend on each other, which a plain manifest must not list. **A task that halts
twice is excluded**, with the reason written into the manifest, because the runner relaunches a
halted task on every run. **A cycle whose launched tasks all died within ten minutes, with
nothing landed, is read as a usage limit** and waited out for thirty minutes without counting
those halts. That last one is a heuristic, not a detection: Relay has no usage limit handling.

Every project fact is data in a sidecar file beside the manifest and named from its stem. For
`queue.toml` the feeder reads `queue.feeder.toml` (settings, all optional), `queue.order`
(priority, one id per line), and `queue.models` (model routing, one `id model` per line, read
fresh each cycle), and writes `queue.feeder.state.json` and `queue.feeder.log`.
`docs/examples/feeder/` has one of each. A card's model is the routing file's line, else a
`**Model:** name` line in the card's body, else the sidecar's default, and a name outside the
sidecar's allowed set is ignored and logged.

The feeder never merges, pushes, moves a card, or edits the target repository, and it holds no
way to write to a tracker. It does write the manifest, which no other runner code does: every
edit is parsed back, validated by the same rules `validate` applies, and renamed into place in
one step, so a bad edit never reaches the file. It appends nothing while a runner holds the
lease, stops with exit 1 when the checkout is dirty or off its default branch, or when a run
halts for a cause outside the task such as the remote moving, and reports every task the runner
skipped, with the reason. One manifest has one feeder: a second `feed` exits 3.

It launches the runner from the same tree it was started from. Start it from a pinned extract of
a commit and it drives that extract, whatever happens in your checkout meanwhile. For the same
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
when nothing has landed yet. Like `tail` it takes no lease, so it is safe against a live run.

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
needs `--retry-blocked` before a reassignment reaches it.

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

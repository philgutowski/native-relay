# Native Relay

Run a list of pre-defined tasks through a native plan, build, review, verify, record pipeline,
one fresh headless process per task, serially and unattended. No plugin sits in the loop: the
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

- **Runner:** a small script. Reads a manifest, pops the next task, launches that Task's backend
  with the task's model, effort, and permission allowlist, waits, verifies the landed state, runs
  the closeout as a separate short process on the same backend, advances or halts. It holds no
  project knowledge and never writes to a tracker. `run` is one Task at a time. `dispatch` on a
  pair overlaps one claude build with one grok build, each in a git worktree, and still merges in
  the listed order.
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
- **Skill:** `/relay`, for Claude Code hosts. Writes the manifest from a conversation, checks the
  properties above, launches the runner. Every step it takes is a runner subcommand an operator
  can run by hand from a shell, which is how a Codex or Grok Build host uses Relay.

## Backends

Native mode runs on `claude`, `grok`, and `codex`. The review step is `/code-review` on Claude,
`/review` on Grok, and the direct foreground command `codex exec review --base <branch>` on
Codex. Relay accepts the Codex step only when its transcript records the exact argv, a zero exit
status, and retained review output. Grok's skip is undetectable: the digest lists
`review_skipped` as not checked.

## Triple GitHub Projects runs

Set `[execution] mode = "triple"` to run exactly three independent GitHub Projects cards from one
`relay run` command, with one explicit task each for Claude, Grok, and Codex. Relay atomically
claims the three immutable ProjectV2 items and a repository integration fence, launches each
backend in a disconnected independent clone, then imports, gates, merges, pushes, verifies, and
closes out the results in manifest order. A normal manifest remains serial.

Triple mode requires `tracker.adapter = "github"`, `shipping.mode = "local_merge"`,
`shipping.push = true`, three distinct nonexcluded task ids, and each backend exactly once. The
Git host must permit atomic pushes of Relay's custom claim refs. Claims never expire by clock;
after a crashed coordinator, inspect the retained state and worker evidence. Relay deliberately
does not reclaim a remote claim automatically.

Jira pairs with `claude` and `grok`. Both write the card through Atlassian MCP, not through
`JIRA_API_TOKEN` (that token is for the runner's reads, and is scrubbed from every child). Grok
needs its own Atlassian login; Claude's stored token does not travel. `validate` and `run` probe
`grok mcp doctor --json` and refuse until that handshake is healthy. Codex stays refused on Jira.

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
  Backend, Review step, Halt class, Cause line, Verify-landed. Read this first.
- `docs/manifest-authoring.md`: how to write a manifest by hand, field by field.
- `docs/plans/2026-09-07-native-mode-plan.md`: the native mode plan and its decisions.
  `docs/plans/2026-08-25-1346-feat-relay-outer-loop-plan.md` is the original outer loop plan,
  including the requirements, the key technical decisions, and the halt class table, as amended
  by the native plan.
- `docs/examples/`: one manifest per adapter.
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

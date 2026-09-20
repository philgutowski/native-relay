---
title: Generic Feeder and Fable Support Plan
type: feat
date: 2026-09-19
topic: generic-feeder
execution: code
status: built on feat/feeder, not merged, live proof owed
---

# Generic Feeder and Fable Support Plan

## Goal

Turn the one project script that kept a Relay run going on Cratekit into a `feed` verb that does
the same for any manifest and any tracker adapter, with every project fact held as data. Finish
Fable support at the same time: make `validate` refuse `fable` on a backend that does not take
it, and make per card model routing a feature of the feeder rather than of one script.

Built on branch `feat/feeder` in a separate worktree (a second working folder on its own branch
that shares the repository's history), because a live Cratekit run launches the runner from the
primary checkout every cycle and any file changed there becomes the code driving that run.

## Background: what existed

Relay reads its Manifest once per run. The Task list is fixed for that run, and a resumed run
skips what landed. `~/.relay/manifests/cratekit-feeder.py`, written 2026-09-18, turned that into
a continuous run by growing the Manifest between runs. Evidence from its log: 11 cycles, 30 cards
landed, none halted, none excluded, and the usage limit wait never fired. Cratekit's repository,
board, deny list, label rules, and paths were hardcoded throughout.

Fable needed nothing to launch. KTD11 of the backend routing plan lets a model name no backend
claims through `validate`, `tests/test_run.py` has a `FableModel` regression guard, and the alias
`fable` was confirmed headless on 2026-09-19, resolving to `claude-fable-5-1`. Four Cratekit cards
landed on it. The gap was the other direction: because no backend claimed `fable`, a Manifest
sending it to grok or codex was not refused.

## Architecture

Three layers, each unaware of the one above it. Jargon used below: a **sidecar** is a settings
file that sits beside another file and is named after it. An **atomic rename** is a file replace
that either fully happens or does not happen, so no reader ever sees half a file. A **lock** here
is an operating system claim on a file that vanishes by itself when the process holding it exits.

```
                        operator
                           |
                  relay feed <manifest>            cli.py, cmd_feed
                           |
        +------------------v-------------------+
        |  Feeder  (feeder.py)                 |   one long lived process
        |  holds <stem>.feeder.lock            |
        |                                      |
        |  reads   <stem>.feeder.toml          |   sidecar: every project fact
        |          <stem>.order                |   priority, one id per line
        |          <stem>.models               |   routing, read fresh each cycle
        |          <stem>.feeder.stop          |   presence means leave
        |  writes  <stem>.feeder.state.json    |   halt counts, wait counts
        |          <stem>.feeder.log           |
        +---+--------------+---------------+---+
            |              |               |
   ready()  |   append /   |  subprocess,  |  in process read
   read     |   exclude    |  fresh each   |  of the run state
   only     |              |  cycle        |
            v              v               v
     Tracker adapter   manifestedit.py   relay_cli.py run      summary.build
     (github, jira,    text edit, then   from the SAME TREE    the same JSON
      markdown) or     parse check, then the feeder was        `summary --json`
     ready command     validate, then    started from          prints
                       atomic rename
                           |               |
                           v               v
                      the Manifest  --->  Runner: one Task process per task,
                                          gate, merge, verify, Closeout
```

One **Cycle** of the loop:

```
    stop file present? ............ yes -> leave with 0, nothing is killed
            | no
    checkout on its default branch and clean? ... no -> stop with 1 and notify
            | yes
    a live runner holds this manifest's lease? .. yes -> wait, append nothing
            | no
    pre cycle command (optional, in the target repo, failure is logged, not fatal)
            |
    ask the Ready source for cards; drop denied ids, denied labels, ids already listed
            |
    sort by the order file, then by id; take (batch) minus (tasks still unsettled)
            |
    pick a model per card: routing file, then the card's **Model:** line, then the default
            |
    append [[tasks]] blocks through manifestedit; a card that fails validate is left out
            |
    relay run ... exit 3 -> lease held, wait.   exit 1 -> stop with 1 and notify
            |
    read the summary -> landed, halted, blocked, skipped; report every skip and block once
            |
    every halt launched and died quickly, and nothing landed?
            -> read as a usage limit, wait, do not count those halts, give up with 2
            |
    a task on its second halt -> excluded = true and a reason written into the manifest
            |
    repeat. Nothing ready and nothing unsettled -> wait, leave with 0 after a day
```

## Requirements

- R1. A `feed` verb on `relay_cli.py`, standard library only, runs the loop above for any Manifest
  and any of the three Tracker adapters. `--dry-run` prints what the next Cycle would append and
  writes nothing. `--once` runs a single Cycle and never sleeps.
- R2. No project name, path, label, or id appears in code. Settings live in a sidecar named from
  the Manifest stem, with the Cratekit script's values as defaults.
- R3. Commands in the sidecar are argument lists, never shell strings, the rule R9 of the outer
  loop plan applies to `gate.command`.
- R4. The Ready source is read only and belongs to the Tracker adapter: GitHub by configured
  labels, Jira by a configured JQL query, markdown by unchecked boxes, and a ready command that
  prints cards as JSON as the escape hatch.
- R5. Manifest writes are atomic, are refused while the Lease is held, pass the manifest module's
  own `validate` before they reach the file, and are made by helpers tested on their own.
- R6. Every Task the Runner skipped is logged and notified with its reason.
- R7. Notifications go through `notify.py`. Nothing requires macOS: `caffeinate` is used when
  present and skipped when not.
- R8. A restart path with the script's semantics: stop file, wait for the old process to leave,
  start. Nothing is killed.
- R9. `validate` refuses `fable` on grok and on codex.
- R10. Model routing: the routing file wins, then a `**Model:** name` line in the card body, then
  the default. A name outside the allowed set is ignored and logged. Closeout stays on its own
  model from the Manifest.

## Key decisions

- **KTD1. The settings are a sidecar file, not a new Manifest table.** The Feeder rewrites the
  Manifest, and older pinned runners must still load what it writes. A pinned runner meeting a
  table it has never heard of would be within its rights to refuse it. The sidecar is
  `<stem>.feeder.toml`. An unknown key in it is an error, because a typo such as `bacth = 5` that
  was silently ignored would run the default for a day. A missing sidecar means every default.
- **KTD2. Runner code now writes a Manifest. This is new and is recorded here as a decision.**
  Until this plan a Manifest was written by a person or by the `/relay` skill and only ever read
  by the runner package. `manifestedit.py` is the whole of the new write path and the loop never
  edits text itself. The standard library reads TOML and cannot write it, so an edit is a line
  edit that leaves every comment where the operator put it, and no line edit is trusted. Each is
  proven three ways before it reaches the file: the text is parsed before and after and must
  differ by exactly the intended change; the candidate is loaded and validated by the manifest
  module from a temporary file beside the Manifest; and only then is it renamed over the Manifest.
  Rollback is therefore by never having written: a reader, a Runner, or a crash at any moment
  sees the old Manifest or the new one, never a mixture and never an invalid one. If the file
  changed on disk while the edit was computed, the operator's edit wins and the Feeder computes
  again next Cycle.
- **KTD3. `ready` is a ninth method on the Tracker adapter interface, and it reads.** The
  interface was pinned at eight methods by a test, and the GitHub adapter says in a comment why
  triple snapshots were kept out of it: a method only one tracker can honour must not be required
  of all three. `ready` is the opposite case, every tracker can answer it, so it joins the
  interface and the pinning test now says nine. It takes the sidecar's `[ready]` table, so what
  ready means is the project's data. It returns a reason beside an empty list instead of raising,
  because a tracker that could not be read and a tracker with nothing ready send the Feeder down
  different paths. The invariant that the Runner never writes to a tracker on a normal Manifest
  holds for the Feeder too: it holds no write path at all. Cratekit's rule that a residual waits
  on its `Closes with` cards is project policy and belongs in its ready command.
- **KTD4. The Runner is launched as a fresh subprocess, from the tree the Feeder was loaded
  from.** `runner_entry()` derives `relay_cli.py` from the Feeder module's own path. A Feeder
  started from a pinned extract drives that extract, and a checkout somebody is editing is never
  what runs, which is the exact hazard this session worked around. A fresh process per Cycle also
  means each run reads the Manifest the Feeder just wrote. The summary is read in process with
  `summary.build`, the same function `summary --json` prints, so there is no JSON to parse and no
  second shape to drift.
- **KTD5. One Feeder per Manifest, held by a file lock, and the lock replaces `pgrep`.** The
  script's restart found the old process by searching process names. The verb holds an exclusive
  `flock` on `<stem>.feeder.lock` for its life. A second `feed` on the same Manifest leaves with
  exit 3. `--restart` drops the stop file, polls for the lock, removes the stop file, and carries
  on as the new Feeder. The operating system releases the lock when the holder exits however it
  exits, so there is no stale lock and no process to signal. Parallel feeders stay out of scope.
- **KTD6. The usage limit rule is a heuristic and is named as one,** `looks_like_usage_limit`.
  It is true when something halted, nothing landed, and every halt died inside
  `quick_death_seconds`. One deliberate change from the script: a halt with no wall time never
  launched a process, a pre flight refusal on a stale branch for example, and a usage limit cannot
  stop a process that never started. The script read a missing wall time as zero seconds and would
  have waited eight hours on it. Here it counts as a real halt, so rule 2 excludes it on its
  second. A known weakness remains: a record keeps the wall time of an earlier attempt, so a Task
  that ran long once and is refused at pre flight later is judged on the old number. It errs
  toward counting the halt, which is the safe side.
- **KTD7. Skipped is settled for batch room and is always reported.** The script counted a skip
  as settled and told nobody, so a card Relay would never build sat in the Manifest looking
  handled. One known cause is a card whose text contains a literal `.claude/` path. A skip costs
  no session, so it still holds no room, and the Feeder logs and notifies it once with its reason.
  Blocked Tasks are reported once the same way, since a later run does not retry them.
- **KTD8. A card that fails validate is left out alone, and remembered.** When a batch does not
  validate, each card is tried by itself, so one card routed to a model its backend refuses cannot
  starve the two beside it. The refused card is remembered with the model that was refused and is
  offered again only when its routing changes. The allowed set is the first line of defence
  against a typo and `validate`, which routing gets for free through KTD2, is the second.
- **KTD9. The Halt class set is unchanged (KTD6 of the outer loop plan).** The Feeder adds no
  class and no finding. Its exit codes reuse the verb contract: 0 it left on its own terms, 1 a
  person is needed, 2 the usage limit allowance ran out, 3 another Feeder holds the Manifest.
- **KTD10. Notifications are opt in with `--notify`,** like every other verb, which is also what
  keeps the suite unable to fire one. The flag is passed down to each run.
- **KTD12. A run scoped halt stops the Feeder and is never counted.** Found on self review
  and shared by the script. `contracts.RUN_SCOPED_HALT_CLASSES` names the halts whose cause lies
  outside the Task: the remote advanced, the Lease was lost, the Runner hit a defect. Rule 2
  would count such a halt against the Task it happened to fall on, exclude that card on the next
  Cycle, then do the same to the card after it, a cascade of exclusions for a fault that belongs
  to none of them. When the run's own record reads halted with one of those classes, the Feeder
  stops with exit 1, names the class, and counts nothing.
- **KTD11. A Manifest the Feeder grows may start with no tasks.** `manifest.load` gained
  `allow_no_tasks`, used only by the Feeder. `validate` still refuses an empty task list, so
  nothing can run such a Manifest, and the Feeder never launches a run with nothing listed.

## What changed against the Cratekit script

| Script | Verb |
|---|---|
| Cratekit paths, repo, deny set in code | sidecar, all derived from the Manifest stem |
| `gh issue list` and label rules in code | adapter `ready`, or a ready command |
| regex edits, plain `write_text` | `manifestedit`: parse check, validate, atomic rename |
| appends under a held Lease | waits and appends nothing |
| skipped is settled and silent | settled for room, reported once with its reason |
| raw `osascript`, always on | `notify.py`, opt in |
| `caffeinate` required | used when present |
| restart by `pgrep` in a shell script | `--restart` on a file lock |
| missing wall time counts as a quick death | counts as a real halt |
| a failed `gh` read crashes the feeder | logged, nothing new offered, the Cycle still runs |
| `--once` could sleep thirty minutes | `--once` never sleeps |
| runner path hardcoded to the primary checkout | the Feeder's own tree |
| a run scoped halt is counted against its Task | stops the Feeder, counts nothing |
| a bad model in a card body ignored silently | ignored and logged |

## Built

- `skills/relay/scripts/relay/feeder.py`: sidecar loading, the pure helpers, `Feeder`, `Deps`,
  the lock and restart path.
- `skills/relay/scripts/relay/manifestedit.py`: `append_tasks`, `exclude_task`, `commit`,
  `write_atomic`, and the parse checks.
- `skills/relay/scripts/relay/cli.py`: the `feed` verb with `--dry-run`, `--once`, `--stop`,
  `--restart`, `--detach`, `--notify`.
- `skills/relay/scripts/relay/adapters/`: `ready` on all three, `INTERFACE` at nine.
- `skills/relay/scripts/relay/manifest.py`: `load(path, allow_no_tasks=False)`.
- `skills/relay/scripts/relay/backends/claude.py`: `fable` in `known_models`.
- Tests: `tests/test_feeder.py`, `tests/test_manifestedit.py`, `ReadySource` in
  `tests/test_adapters.py`, one case in `BackendModelCoherence`, `ready` on `FakeAdapter`.

## Tests, stub only

Every case uses a temporary `HOME`, a temporary repository, and an injected `sleep`. Covered:
appends only ready and unlisted cards in order file order; batch room shrinks by unsettled
Tasks; the second halt excludes with a reason and the first does not; a Cycle of quick deaths
waits and is not counted, and the wait cap exits 2; a landing in the Cycle means it was not a
usage limit; the stop file is honoured between Cycles; exit 3 waits and exit 1 stops; routing
file beats body line beats default; a bad name is ignored and logged; the routing file is read
fresh; an append that fails validate is rolled back while the rest go in; a skipped card is
reported once; a dirty checkout and an off default checkout each stop the loop; nothing is
appended under a held Lease; a run scoped halt stops the Feeder and is not counted; a state
file that cannot be read is exit 1; a dry run writes nothing; a second Feeder gets 3; restart waits and
kills nothing. One case runs a whole Cycle through the real Runner over the stub `claude`, the
real markdown adapter, and the real summary, which is the only proof the Feeder and the Runner
agree on exit codes and on the summary's shape.

## Owed: the live proof

This repository's `CLAUDE.md` asks for one live task against a throwaway target after a contract
between processes changes. No Brief, Envelope, Closeout line, halt record, or digest key changed
here, so that trigger is not strictly met. Three seams were still proven only against stand ins,
and the stubbed seams learning says that is where defects hide. **Owed, deferred because no live
model run was allowed this session:**

1. `feed --once` against a throwaway repository with a markdown tracker and a real `claude`: the
   Feeder launching a real run and reading a real summary.
2. `feed --dry-run` against a real GitHub board: the `gh issue list` read and its label shapes
   have only ever run against canned JSON.
3. The Jira `ready` JQL read against a real site, before any Jira project is fed.

## Open questions, for later plans

- **Does a usage limit halt a Task or block it?** `CONCEPTS.md` says both, in two places: the
  Halt class entry says an exhausted allowance reads as a crash, and the Blocked entry says a
  process that exits with no Envelope for an outside reason is recorded Blocked. Blocked is
  settled and is not retried, so a usage limit that lands as Blocked is invisible to the
  heuristic and the cards are quietly dropped from the run. The Feeder now reports every Blocked
  Task once, which makes it visible. Deciding it needs a live observation, and real usage limit
  detection is out of scope here.
- **Should the Review step have its own model?** See
  `docs/ideation/2026-09-19-review-step-model.md`. Nothing was built for it.
- **`~/.relay/cut-runner.sh` refuses to cut while `relay_cli.py run` is live.** A Feeder
  between Cycles is `relay_cli.py feed` and does not match, so the guard has a gap once a Feeder
  runs from the pinned extract. That script is outside this repository and was not touched.

## Out of scope

Moving Cratekit onto the verb, which is a separate sitting after its run ends. A separate review
model. Usage limit detection beyond the heuristic. Parallel feeders. Anything in
`compound-relay`.

## Migration notes for the Cratekit sitting

Not done here. The mapping, so that sitting starts oriented:

- `cratekit-parity.order` and `cratekit-parity.models` already carry the names the verb derives.
- The stop file becomes `cratekit-parity.feeder.stop`, the state file
  `cratekit-parity.feeder.state.json`. Copying the old state JSON across keeps the halt counts;
  the verb adds the keys it needs.
- The log moves beside the Manifest as `cratekit-parity.feeder.log`.
- The sidecar carries `[deny] ids = [29, 20, 30, 50, 39, 46]`, `labels = ["attended"]`,
  `[hooks] pre_cycle = [".venv/bin/python", "scripts/board.py", "sync"]` as an absolute path to
  that Python, and a `[ready] command` that prints the unit cards carrying `ready` plus the
  residual cards whose `Closes with` cards are all closed. That second rule is why labels alone
  are not enough for Cratekit.
- Start it from the pinned extract, not the primary checkout.

## Issue text, written here because no issue may be filed this session

**1. feat: `relay feed`, a generic feeder verb.** Status Done on `feat/feeder`. Body: this plan.

**2. fix: `fable` was not refused on grok or codex.** Status Done on `feat/feeder`. `fable` was
missing from the claude backend's `known_models`, so it fell under KTD11's unknown name rule and
a Manifest sending it to another backend passed `validate`. Added, with a coherence test.

**3. owed: live proof of the feeder's three unproven seams.** Status Todo. The list under Owed
above. Do the first before Cratekit moves onto the verb.

**4. chore: move Cratekit onto `relay feed`.** Status Todo, blocked on its current run ending.
Follow the migration notes above, begin with `--dry-run`, and compare its offer with the old
script's dry run before retiring the script.

**5. question: does a usage limit halt or block?** Status Todo. The first open question above.
Needs one observed usage limit with the record read afterwards.

**6. question: a model setting for the Review step.** Status Todo. Points at the ideation note.

**7. chore: `cut-runner.sh` should refuse under a live `relay_cli.py feed` too.** Status Todo.
Outside this repository, in `~/.relay`.

## Merge and cut a new runner, for Phillip, after the Cratekit feeder has left

See the end of session report. The order matters: confirm no feeder and no run is alive, merge
in the primary checkout, run the suite there, delete the branch and the worktree, then cut a new runner.

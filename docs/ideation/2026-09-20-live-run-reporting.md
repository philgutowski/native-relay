# Live run reporting: a `watch` verb, armed by default

Captured 2026-09-20 from a session that had no way to see a run in flight without asking for a
snapshot over and over. Prototyped and proven against the live Cratekit run before writing.

## The problem

A Relay run is unattended by design, and that is the whole economic case. But the operator who
launched it still has a session open, and right now that session is blind between launch and
finish. Three read only verbs exist and none of them fills the gap:

- `status` is a snapshot. It answers only when asked, so seeing a run means asking repeatedly.
- `tail` follows the running process. It shows one task's activity, it does not show the queue
  behind that task or what already settled, and it is a foreground follower.
- `summary --json` is the record, not a feed. Nothing emits when the record changes.

What the operator actually wants is the shape a monitor consumes: one line per thing that
changed, nothing while nothing moves.

## What was prototyped

`~/.relay/bin/relay-watch`, outside the repo, read only, polling `summary --json` and the
feeder log, diffing against the previous poll, emitting a line per change. It ran against the
live Cratekit run and produced what the session wanted:

```
WATCHING cratekit-board.toml
queue: 1 running (#159), 2 waiting (#158, #171), 0 settled (0 landed)
RUNNING  #159 R-221: The queued backup reports its ages and nothing else the run learned  [opus, relay/159]
LANDED   #159 R-221: ...  ref 4f2a91c  2 finding(s)
FEEDER   cycle 1: appending [('96', 'opus'), ('97', 'fable')]
FEEDER   gone. No further cards will be appended.
HALTED   run stopped: class=gate_failed task=171
```

It works, and it should not stay a private script. Every project that invokes the skill wants
this, and a script in one operator's home does not travel.

## Two defects the prototype exposed

These are the reason this is a feature and not a copy of a script into the repo. Both are in
the existing surface and both would be inherited by any consumer.

**1. `summary --json` cannot show the waiting queue.** `summary.py` builds its task list from
`store.records()` and keeps a manifest task only `if task_id in records`. A card the feeder
appended, or a card listed at authoring time, that nothing has started yet has no record, so it
is absent from the summary entirely. The prototype's first version reported `0 waiting` while
two cards sat queued, and the only repair was to re-read the manifest with `tomllib` and
subtract. Every consumer of the summary has to do that, or be wrong the same way. The waiting
queue belongs in the summary, as records with a `todo` status or as a sibling list.

**2. The card title is read and thrown away.** No title is persisted anywhere in the record.
`brief.check_cards` reads every card at launch and the task brief carries the title, so the
runner has it in hand. Because it is not kept, a reporting consumer has to make one tracker read
per card just to name it, which is a network call per card for a cosmetic field, in a tool whose
whole point is to stay read only and cheap. Persisting the title at first read costs nothing.

## Proposal

**A `watch` verb.** Read only, never takes the lease, same as `status` and `audit`. Polls the
record, diffs, emits one line per change on stdout, line buffered. Covers every terminal state,
not only the happy path: a watcher that emits only on success is silent through a halt, and
silence is indistinguishable from still running. Options worth having: `--every SECONDS`,
`--once` for a single snapshot, and `--since` so a re-armed watcher does not replay history.

It stays host agnostic. Line oriented stdout is what a shell, a pipe, or any host can consume,
which is the same reason `README.md` documents running the runner from a shell at all.

**Armed by default from the skill.** `SKILL.md` launches the runner detached today and then
says nothing. It should also arm a monitor on `relay watch` so the session reports progress
without the operator asking. Default on, with a way to decline. This half is Claude Code
specific and belongs in the skill, not in the runner, which keeps the split the repo already
has.

## Open questions

- Does `watch` poll the record, or does the runner write an append only event log that `watch`
  follows? The log is cheaper to consume and survives a re-arm cleanly, but it is a new artifact
  and a new contract between processes, which `CLAUDE.md` warns costs a live run to validate.
- Should the feeder's own log fold into the same stream, or stay a second source the watcher
  reads? The prototype reads both and the seam is visible in its output.
- One monitor per manifest, or one per run? A feeder outlives the runs inside it.
- What does the skill do when a run is already alive at launch time, and when the monitor
  expires before the run ends?

## Not in scope

Nothing here changes what a run does. This is reporting only, no new halt class, no tracker
write, no change to the Task or Closeout briefs.

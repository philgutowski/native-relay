# Concepts

Shared domain vocabulary for this project, entities, named processes, and status concepts with
project-specific meaning. Seeded with core domain vocabulary, then accretes as learnings are
recorded; direct edits are fine. Glossary only, not a spec or catch-all.

## Relationships

A Feeder, when there is one, grows a Manifest between runs and launches a Runner for each Cycle;
a Manifest with a fixed Task list needs none. A Runner reads one Manifest and drives a series of Tasks. Each Task gets its own Task process and,
once it exits, its own Closeout process. The Runner decides a Task's outcome by Verify-landed,
which consults git and the Tracker through a Tracker adapter, never the Task process itself. The
Shipping mode named in the Manifest decides what landing means for that project. A Feeder with
the browser test loop switched on also starts Test passes between runs; each launches a Test
process to find defects in the running app and a Filing process to file the ones its code chose,
as cards the same Feeder then builds.

## The loop

### Runner
The Relay process that drives a Manifest to completion: it selects a dispatch policy when dispatching, computes a
conservative schedule, launches Task processes, decides each outcome, and halts rather than
continuing past an outcome it cannot confirm. `serial` is the default policy. Under `parallel`,
the Runner may overlap only Tasks the schedule establishes as high-confidence independent;
landings remain in Manifest order.

The Runner holds no project knowledge of its own. Everything project-specific reaches it as
Manifest data. On every normal Manifest it reads the Tracker and does not write to it, so a defect
in the Runner can never move a card; every write goes through a Task or Closeout process with the
adapter's instructions.

Triple execution is the one exception, and it is deliberate. A Jira triple's workers hold no Jira
credentials and no Jira write tools at all, so the card writes have nowhere else to live: the
coordinator makes them itself over REST, gated on
`tracker.coordinator_rest_writes_authorized`. It never infers a transition from a target status.
It chooses a configured `transition_labels` label, performs the transition, then reads the card
back and confirms the exact destination status, and a disagreement is a halt rather than a
continue. So the property that survives is not that the Runner cannot move a card, it is that the
Runner cannot move a card anywhere it did not verify it landed.

A Runner reports two of the three Phase event moments: a Task's status moving, and the run
reaching its terminal record, which it announces with the run's counts. It always writes them to
its own output, so a run's log carries them either way, and it also notifies the desktop when the
operator asked for that. A Task's log starting stays the Follower's alone, because only a Follower
reads those files; the Runner's nearest equivalent is the same Task's move into running, which it
writes immediately before it launches the process. Reporting is not participating, so a
notification that fails, or one that hangs, may not change what the run decides: the Runner
swallows what the notifier raises and bounds how long it may take, and the two together are what
replaced keeping the Runner silent.

A Runner is one process for the length of a Manifest, so the code it runs is the code it loaded
when it started. Where Relay drives its own repository, a Task that lands a change to the Runner's
own code does not change the Runner already running, and that run's own record is written by the
older code. What a Task lands becomes visible inside the same run only where the Runner reads
something at the moment it uses it rather than loading it once at the start. A change spanning both
kinds is the case that does more than go unobserved: the run holds the half it loaded at the start
and reads the half it takes at the moment of use, and stops at the first use that needs the two to
agree.

### Lease
The claim a Runner holds while it drives a Manifest, which is what stops two Runners from
interleaving work against one repository. There are two: one over the Manifest, so a second run
of the same Manifest refuses to start, and one over the target repository, so two different
Manifests naming the same repository cannot merge into it at the same time.

A Runner launched to wait for the Lease polls it instead of refusing, and takes it when it
clears or refuses at its own bound, so one run can queue behind another on the same repository
without anything outside the Runner watching a process.

A Lease is renewed on a heartbeat rather than held for the length of the work, so it expires on
its own if a Runner dies. A Lease past its expiry is stale and the next Runner reclaims it,
marking any Task the dead Runner left in flight as halted. Two other endings mark them the same
way, since each leaves a Task in flight with nothing driving it: the operator breaking the Lease
by hand, and a Runner leaving without a terminal record, an interrupt from the keyboard most
often (issue #64). The expiry is deliberately shorter
than any Task timeout, so a crashed Runner never blocks a repository for the length of a Task;
the cost of that choice is that every long operation a Runner performs has to keep renewing, and
one that does not is how a Runner ends up acting without the claim it thinks it holds.

Reclaiming marks only the Task it inherited; it never records the run's own outcome, because at
the moment of reclaim the reclaiming run has not yet decided whether it halts there or continues
past the halt. Only the run's own later conclusion may record that the run itself is over.

### Phase event
One of the small set of moments in a run that are worth telling a person about, as distinct from
the decoded Task activity that fills a log: a Task's log starting, a Task's status moving, and the
run reaching its terminal record. It is a report about a run rather than a part of one, so nothing
that happens to a phase event may change what the run decides.

### Follower
A reader attached to a running Manifest, which decodes the Task processes' output and reports the
run's phase events. It takes no Lease, decides nothing, and writes nothing, so a Follower can be
started, stopped, and started again beside a live Runner without touching it.

A Follower reports on a run rather than participating in one, so a run with no Follower is not
diminished, only unobserved. A Follower launched beside a run starts from a floor taken before
that run began, because the state directory outlives any one run and its Task logs are appended to
rather than replaced.

A Follower's progress bar is not a phase event. It reports counts, how many Tasks the run is done
with out of how many it has, rather than a moment, so it prints on the Follower's own output and
never notifies. The phrase a status move carries beside it, the settled count and the estimate,
is the same report in one clause, and it rides on the phase event rather than being one.

A Follower notifies only for a run somebody else started. One that launched its own run has
already passed the request for notifications down to that Runner, which outlives it, so it prints
its lines and stays quiet on the desktop. That is what keeps one phase event to one notification
while still reaching an operator whose Follower ended at its own bound hours before the run did.

### Manifest
The single file, one per project, carrying every project-specific fact a Runner needs: the Task
list, the Tracker adapter to use, the Shipping mode, the permission allowlist and disallow list,
per-Task timeouts, and how each of the project's qualifying properties is satisfied.

A person or the `/relay` skill writes it, and the Runner only reads it. The one exception is a
Feeder, which appends Tasks and marks a Task excluded between runs, through a write path that
validates before it replaces the file.

### Run policy
The operator's per-launch choice for a normal Manifest dispatch: `serial` or `parallel`. An attached dispatch
offers both choices with `serial` selected by default. A noninteractive or detached run without
an explicit policy uses `serial`. Triple execution is a separate exact profile and has no normal
dispatch-policy choice.

### Schedule
The Runner's pre-launch, deterministic explanation of when normal-manifest Tasks may start. A
parallel schedule is conservative: it contains a serial dependency for every pair whose declared
paths, repository evidence, or read-only semantic analysis does not establish disjoint bounded
work with high confidence. Broad or unknown scope and shared configuration, dependency manifests,
migrations, CI, root documentation, and generated output serialize with peers. The Runner prints
the policy, concurrency groups, and every serial edge with its reason before launching, then
executes that schedule without another approval. A permitted concurrent Task still receives its
own worktree; the gate, verification, and landing sequence remain serial.

### Pair
Two Manifests that partition one Task list across `claude` and `grok`, share the project, tracker,
qualifying sentences, shipping, and gate, and name the original Task order as the merge sequence.
A pair file points at both members. `pair split` writes the three files from a mixed Manifest.
Backend assignment stays with the operator and the rubric; the split only groups what they already
named and keeps their order.

### Dispatch
The coordinator that drives a Pair: one in flight Task process per native backend, each in a git
worktree, merges still in Pair order on the primary checkout. `run` stays one Task at a time.
Dispatch holds the same two Leases `run` does. A halt that does not continue past kills the
sibling build, removes its worktree, deletes its half built branch, and leaves that record
pending so the next dispatch starts it fresh.

An interrupted dispatch, an interrupt from the keyboard most often, does the same to every build
in flight: it ends each Task process, removes its worktree and branch, and returns its record to
pending, wherever the interrupt landed, including during a Closeout on the primary checkout. Only
then does it mark what else was in flight and release the Leases (issues #71 and #79). A build
that would not die inside the bound is released past: its record keeps reading running and the
terminal record names it. A second interrupt during the stop abandons the stop, and the Leases are
released with whatever builds it had not yet ended.

A finished build waiting its merge turn still occupies that backend's slot. Freeing the slot when
the Task process exits would start another Task on the same backend before the previous one has
merged. The merge tail must treat this coordinator's own earlier landings as expected movement of
the default branch, not as a foreign mover. A real concurrent session advancing the default branch
is still a halt.

### Task
One unit of work the operator defined before the run started, identified by a Tracker record. Tasks
in a Manifest are independent of each other by requirement; a Task that depends on another belongs
in a later run. A Task may be marked excluded from unattended runs, with a stated reason, when
something about it needs a human present; that record reads excluded, and only the Manifest lifts
it. A Task the Runner declines at launch, because its card could not be read, was already
terminal, or names a `.claude/` path, reads skipped instead, and every later run checks it again,
so fixing the card is the repair. The same `reason` field is required when a Task's
backend differs from the manifest default. A Task that matches the default needs none.

`declared_paths` is optional repository-relative scheduling evidence on a Task: exact files or
directory prefixes it expects to write. It is neither a tool permission nor a waiver of the
conservative rule. A missing, broad, or ambiguous declaration means the Scheduler serializes the
Task with potentially affected peers.

### Task process
The single headless agent invocation that carries one Task from plan to landing. It starts with an
empty context, knows nothing of any other Task, and its report of its own success is not evidence.
It creates and stays on a branch named the Manifest's `project.branch_prefix` plus the Task id.
The prefix defaults to `relay/` when the Manifest omits the key. An empty prefix is the Task id
alone, which is not the same as omitting the key. Retry of a blocked Task looks at the branch
name stored on that Task's record, so a later prefix edit cannot hide stranded commits. A branch
of that name left behind by a halted or timed out attempt is also in the way: pre flight refuses
to launch the Task while it exists, the Runner never deletes it, and the check is on the name
alone, so the operator moves the branch aside and puts any resume instruction on the card, which
is the only thing a fresh Task process reads.

A Task process owns whatever it spawns. Subagents and gate commands run underneath it and can
outlive it, so the Runner bounds the whole group rather than the one invocation, and a Task
process that has exited is not by itself evidence that its work has stopped.

Its own turn ending and the process exiting are the same event, so a background command the
harness is tracking on its behalf dies with that exit. A child it starts through the shell
instead, detached from the command it is itself waiting on, is tracked by nothing and survives,
outliving the turn, the Task process, and the rest of the run. The Runner's bounding of the whole
group is not the backstop this appears to leave either, because that bounding fires only where a
run has gone wrong, on an operator signal, a lost Lease, or a deadline, and never on an ordinary
exit. So the only Task processes whose leftovers are swept are the ones that failed, and a
leftover has no place in anything the Runner records.

### Backend
The CLI that runs a Task process and that Task's Closeout process, one of `claude`, `codex`, or
`grok`. A Task names its backend. A Manifest may default it. Absence of every backend key means
`claude`. The Runner launches on that CLI. It does not choose or change the backend during a run.
`/relay` itself still runs in Claude Code. Only the launched processes vary.

Native mode runs on `claude` and `grok`, grok admitted 2026-09-11: the Review step is a built in
skill, and only a backend whose Capability record names a verified one can run the Brief.
`validate` refuses a Task naming Codex with a sentence that names the missing step. Codex's
launch seam and evidence reader stay in the Runner, pinned against the CLI version they were
observed on, so that refusal can lift once a review step is verified live. Jira pairs with
`claude` and `grok`: both write the card through Atlassian MCP. Grok needs its own Atlassian
OAuth; Claude's stored token does not travel. Codex on Jira stays refused.

Between runs the Manifest's resolution decides again, so editing a Task's backend or model moves
any Task that has not landed, and the Runner reports the move on its own output and as a finding
on that Task's record rather than taking it silently. What the edit cannot move is the branch a
blocked Task left behind: the stranded branch refusal is judged against the name and baseline
that Task's record already carries, because those point at commits on disk that recomputing could
strand or discard, while a backend points at nothing that survives the attempt. A Manifest that
pairs a backend with a model another backend is known to accept is refused before any Task
launches; a model name Relay does not recognise is allowed through.

### Capability record
The frozen facts the Runner reads about one backend: whether it enforces tool restrictions at
launch, its permission flags and forbidden spellings, the version it was tested against, the
built in review skill the Brief names on it, its credential prefixes and nesting markers,
whether a Jira Closeout can write on it, and whether the session id is runner chosen. The launch
seam, the readiness probe, the Brief inserts, and the classifier all read this record rather
than a second per backend table.

### Task path bound
The commit-scope prefix list a Manifest names for Task branches. On a backend that cannot refuse
tools at launch, the Runner diffs the Task branch against this list before it merges, and refuses
the merge when the commit falls outside it, leaving the branch intact. It is a different set from
the Closeout's own path allowance, and it does not observe which tools the Task invoked.

### Brief
The instruction text a Runner hands a process it launches, rendered for that process alone from a
template plus the Task's own facts. There is one shape for a Task process and one for a Closeout
process.

The Task brief's steps are the native pipeline: move the card, branch, plan in a message, build,
run the Review step, run the project's own verification, record what the project's method says a
unit records, comment the card, print the Envelope. The Task's own facts are the card's title,
description, and the comments on it at launch, the same read that fixes the baseline comment, so
the Task process is told every comment before its run and the Closeout process every one after. The plan is a message in the transcript rather
than a file, and verification is whatever the project's own instructions define, which the Task
process reads because it runs inside that project's checkout. Nothing project specific is in the
template.

A Brief renders deterministically, so the same inputs produce the same text and a re-run after a
halt does not change what a process was told. Its template is read at the moment of rendering rather
than held from the Runner's start, which is what lets a Task change the Brief that later processes in
the same run receive, and equally what lets a template naming a value the running Runner cannot yet
supply stop the run outright.

### Review step
The step of the Task brief that runs the backend's built in code review on the branch's diff and
fixes what it finds. The skill's name comes from the Capability record, so the Brief that asks for
it and the classifier that looks for it cannot disagree. On Claude that name is `/code-review` and
a Skill call is visible, so a Task whose Envelope reads complete and whose transcript holds no
call to that skill gets a `review_skipped` finding: it lands if the gate passes, and the summary
lists its diff as one to review by hand. On grok that name is `/review` and skip is undetectable:
the digest lists `review_skipped` as not checked, and classify does not attach a skip finding. A
backend with no such skill has no native Brief and is refused at validate.

### Envelope
The structured block a Task process prints at the end of its work to report what it did: whether it
completed, was blocked, or failed, the blockers if any, the files it changed, and anything it
judged worth keeping as a learning.

An Envelope is a claim, not evidence. The Runner reads it to classify how a Task process exited, and
never to decide whether the Task landed, which is Verify-landed's job from git and the Tracker
alone. A Task process that exits without one gets its own Halt class rather than the benefit of the
doubt.

### Digest
The record the Runner builds by reading a Task process's transcript once, holding the Envelope
alongside what the Runner itself observed: whether the process timed out, its exit status, how
many tool calls it made, and the Halt class the Runner assigned. The Runner holds it in memory and
renders pieces of it into the Closeout process's own Brief before launching that process; it is
also written once per Task as a durable record, though nothing in the run loop reads that file
back, so the Closeout process itself never sees the Digest directly, only what the Runner rendered
from it.

A Digest is not the same claim as the Envelope it carries. The Envelope is the Task process's own
account of what happened; the Digest is the Runner's account of the process's exit, built without
trusting that account, and the Envelope is only one field inside it. An optional field's presence
in the Digest proves the Runner's transcript parsing found it, never that a later stage, such as a
rendered Brief, actually carries it forward.

### Closeout process
A separate short agent invocation the Runner launches after every Task process exit except a
timeout that left the tree dirty. It has two ordered duties: write the Task's outcome to the Tracker (the closing reference
when Landed, a comment carrying the Runner's blocker digest when Blocked), then the Learning
judgment. It exists because the Runner never writes to the Tracker and the Task process exits
before the landing commit exists, so neither can name it.

For a Blocked or halted Task the first duty also returns the card to the status the record read
before the run, decided 2026-09-08. The Task process moves the card to the in review status at
its first step, so without the return every Task that did not land leaves a card in progress
with nobody on it. The Runner reads the card back afterwards and attaches a `card_left_in_review`
finding when it did not move; it never moves the card itself. No return is asked for when the
status before the run is unknown, when it was already the in review status, or when the record
carries a landing reference, since that card was closed by a landing. An unknown status still gets
the read back: the Closeout is told the Task may have moved the card and there is nowhere known to
send it, and a card still in review is a `card_left_in_review` finding to move by hand.

That status is the baseline the record keeps across a relaunch (issues #51 and #64). A relaunch
of a card an earlier attempt left in review reads in review, and recording that would read as the
operator's staging, which is never undone, so the card would never go back. The Runner tells the
two apart from its own reads, not from an audit: every launch marks the record, because the Task
moves the card at its first step, and only a read of the card that finds it out of review clears
the mark: the read back after a Closeout, or the run end audit. With the mark set, an in review read keeps the earlier baseline; with
it clear, the operator put the card there and it stays. A status other than in review is the
operator's move and wins either way.

On GitHub a landed card has two truths, the issue's state and its project item's status, and a
closed issue is terminal on its own (issue #43). So after a landed Closeout the Runner also reads
the project item when the manifest names a `status_field`, and attaches a
`board_item_not_terminal` finding when the item is on the declared project and reads anything
else, or when the item could not be read, since a read that failed cannot confirm the move. The
card audit makes the same read between runs. Neither moves the item.

Its ending is a contract: the final line of its last message says whether the Learning judgment
wrote a learning or skipped one, and the Runner reads that line from the end of the message, not
the start. A Closeout process that ends any other way is recorded as unfinished, which is a
finding for the operator rather than a halt.

### Learning judgment
The second duty of the Closeout process: judging whether a Task produced a learning worth keeping
and writing it if so, as one markdown file under the Manifest's docs root, committed inside the
Closeout's allowed paths with no plugin involved. It is kept out of the Task process because a
Task process at the end of its context is the worst available judge of its own learning. Runs for
Blocked Tasks too, since a blocker is often the learning. The Task process may also record what
the project's own method tells it to on its branch; the judgment does not write that twice.

### Finding
An observation the Runner attaches to a Task's record without it being that Task's Halt class:
something the run noticed that an operator should know, from a refused tool call, to a Review step
that never ran, to a card the Closeout process failed to comment on.

A finding names a class from the closed Halt class set whenever one fits, so the same name can be a
Task's class and a finding on a Task that landed, and the two mean different things. The class is
the Runner's verdict on why a Task did not land. A finding is one observation about a Task whose
outcome it does not decide. The Envelope settles which a given observation becomes: a Task
reporting itself complete goes to Verify-landed with its findings intact and no class at all, so a
refusal the Task process worked around leaves a finding on a landed record and nothing else. A
`path_gate` finding on a landed record is that complete Envelope case, and the repair is a follow
up edit on a merged commit, not a resume. Some findings reach the operator only through the run
summary's check by hand list, which is why that list is read on a run that reports no halts at all.

### Halt class
The Runner's classification of one Task process exit, drawn from a closed set and decided from the
session transcript plus git and Tracker evidence. Every class carries the evidence its Cause line
needs, so an operator learns why a Task did not land without reading a transcript.

Because a class is decided from what the exit left behind, the set can only name causes the
evidence records. A cause lying outside the Task process leaves the same evidence as a Task fault
and is classified as one, so the Cause line reads as though the Task misbehaved: the account's
model allowance running out mid run reads as a crash, and another session writing an untracked
file into the working tree reads as the Task leaving the tree dirty. The set is closed deliberately, so the answer to such a
cause is a finding attached to the record, or a check made before the run starts, rather than a
new class. The set was amended once, by the native mode plan of 2026-09-07: `skill_substitution`
left it, and `review_skipped` joined the findings.

Three classes are run scoped and always stop the run, named in `contracts.RUN_SCOPED_HALT_CLASSES`:
each puts something outside the failing Task in question, the remote, the Lease, or the Runner
itself. Any other halt can be continued past when the Manifest opts in with
`on_halt.continue_past_task_halt` and the repository, after the Runner returns it to the default
branch, is one the next Task could start from: a clean tree at the remote's head, or, under a
Manifest that does not push, a clean tree whose remote has not diverged from it. The Runner checks
that rather than inferring it from the class, because one class can leave the repository usable or
not. A Task continued past stays halted, is listed by the summary as a check by hand, and is retried
on the next run like any other halt.

Continuation policy reaches only a Task whose process actually raised a halt. An outcome the Runner
records without raising, a Blocked Task above all, is not a halt the Manifest gets to vote on, so
the run continues past it unconditionally and no Manifest field changes that. Being run scoped is
not what stops a run either, since that set is read only on the raised path. A class from it that
reaches a record by the recording route is a label on the outcome and nothing more. The two routes
look alike in a run summary, both naming a class and both saying the Task did not land, so the
distinction is invisible exactly where an operator goes looking for it.

### Cause line
The one sentence a run summary prints to say why a Task did not land, and the only diagnosis an
operator who was not watching gets. Each is a fixed template belonging to a Halt class, filled from
the evidence the Runner recorded when it stopped. Findings that attach to a Task without being its
own class carry one too, so a Cause line is not exclusively a property of a Halt class.

A template and the evidence that fills it are two halves of one contract, written in different
places by different code. Two rules keep them joined. Evidence a template names must be a plain
value, because a structured one cannot be rendered into a sentence and is dropped. And where the
evidence and the Task's own record both carry a field of that name, the evidence wins, since the
record acquired most of its fields after the stop and would otherwise describe the aftermath rather
than the cause.

One Halt class can have more than one raiser, and where two raisers ask the operator for opposite
repairs they write different sentences rather than sharing the class's one. `path_gate` is the case
that established this: the transcript scan raises it for a write the harness refused, where the work
never happened, and the merge tail's `.claude/` backstop raises it for a finished branch the Runner
declined to land. The record carries the merge tail step that refused beside its class, so a reader
can tell which raiser fired without inferring it from the findings, whose provenance is the
transcript and whose phase is earlier than the tail's.

A Cause line is a derived form, and the record keeps the raw sentence it was derived from beside
it: the words the code that stopped the run actually wrote. The summary prints that sentence under
the Cause line whenever the two differ, so a template that fits the class loosely, such as a
refused retry reported under the class for a dirty tree, cannot be the only account of the stop.

## The continuous run

### Feeder
The process that keeps one Manifest running by growing it between runs. A Runner reads its
Manifest once and the Task list is fixed for that run, while a resumed run skips what landed, so
appending Tasks between runs is how one Manifest runs for a day. The Feeder sits outside the
Runner and above it: it launches a fresh Runner each Cycle, from the same tree it was itself
started from, and reads that run's summary afterwards. It never merges, pushes, moves a card, or
edits the target repository. It writes the Manifest, its own state file, its log, its events file,
and its post cycle hook's output, all beside the Manifest, and it holds no way to write to a
Tracker. The state file carries a process record, the pid, host, start time, and current Cycle of
whichever Feeder holds it, stamped with the exit and the reason when it leaves, so a watcher can
tell a live Feeder from a stale one without a process listing.

Three rules carry it. Only cards the Ready source returns are appended, so a unit never launches
before its foundation lands. A Task that halts twice is written into the Manifest as excluded
with its reason, because a Runner relaunches a halted Task on every run for ever. And a Cycle
touched by a usage limit follows the state machine the Usage limit entry below names, marking,
moving, holding, or waiting a Task rather than simply counting an ordinary halt. A halt whose
class is run scoped is never counted either: its cause lies outside the Task, so the Feeder stops
for a person rather than exclude a card for it.

### Usage limit

One question, asked of a death whose process this Cycle actually launched: did the account's
limit for that model end it. The answer comes from the last attempt's own log, read by
`limits.py`, and is one of three words. Confirmed is a 429: the last attempt's `result` line
carries `api_error_status` 429, the CLI's own account limit signal. Refuted is everything else
that leaves evidence: a `result` line with any other status, a death with no wall time at all
because no process ran, or a death that took longer than `quick_death_seconds` with no `result`
line. Unconfirmed is a quick death, one that ended inside `quick_death_seconds`, whose last
attempt printed no `result` line at all, so nothing in the log rules a limit in or out. A record
whose process did not launch this Cycle carries an older attempt's log and is never read. In
order:

| Check | Answer |
|---|---|
| The record's process did not launch this Cycle | not read, an older attempt's evidence |
| The last attempt ends in a `result` line, `api_error_status` 429 | confirmed |
| The last attempt ends in a `result` line, any other status | refuted |
| The last attempt has no `result` line, the death was inside `quick_death_seconds` | unconfirmed |
| The last attempt has no `result` line, the death took longer | refuted |

429 is what is detected. Everything unconfirmed is still inferred from timing alone, and it is
asked to do less than it once did: only the whole Cycle wait below, never a mark on a model.

A model is Marked when a confirmed death sets it an expiry: the CLI's own reset time, read from
a rejected `rate_limit_event` in the same log, when it still lies ahead, otherwise
`fallback_hours` after the death. A later confirmed death on a Marked model replaces the mark
with its own, since the newer death is the newer truth. A Task listed on a Marked model, or
found on one at the start of a Cycle, is Moved to the first open model along its fallback chain;
with none open it is Held, left where it is, taking no room in the batch, and named to the run as
Deferred so it is not launched. A Held Task is Moved once a fallback along its chain opens, and
returns to its own model once that model's mark itself expires. The whole Cycle wait fires
instead of a mark when a death is only Unconfirmed: nothing landed this Cycle, every death in it
was quick, and at least one had no log to confirm or refute it. The Cycle waits
`limit_wait_seconds` under reason `usage_limit`, and the streak `limit_waits` climbs by one; past
`limit_waits_max` waits in a row the Feeder leaves with reason `limit_waits_exhausted`. The
streak survives a restart, and clears on a Cycle where something landed, a death was slow, or
nothing died, and by `feed --clear-limits`.

The states a Task passes through once a limit has touched it, one row per transition:

| From | On | To |
|---|---|---|
| Listed | the run starts, its model unmarked | Launched |
| Listed | the run starts, its model marked, a fallback is open | Moved |
| Listed | the run starts, its model marked, no fallback is open | Held |
| Launched | its verdict passes | Landed |
| Launched | the death reads confirmed | Confirmed |
| Launched | the death reads unconfirmed | Unconfirmed |
| Launched | the death reads refuted, or was slow | Ordinary |
| Confirmed | a fallback model is open | Moved |
| Confirmed | no fallback model is open | Held |
| Moved | the next run | Launched |
| Held | a fallback's mark expires | Moved |
| Held | its own mark expires | Launched |
| Unconfirmed | nothing landed, every death this Cycle was quick | Waited |
| Unconfirmed | anything else | Ordinary |
| Waited | after the wait | Launched |
| Waited | the streak passes `limit_waits_max` | Reported |
| Ordinary | recorded blocked | Reported |
| Ordinary | recorded halted | Counted |
| Counted | `max_halts` reached | Excluded |

The states a model passes through:

| From | On | To |
|---|---|---|
| Open | a confirmed death | Marked |
| Marked | a further confirmed death | Marked, its expiry restamped |
| Marked | `fallback_hours` pass, its CLI reset time passes, or `feed --clear-limits` | Open |

Held describes a model rather than a state of its own: it is Marked with no model along its
fallback chain Open, read fresh from the marks and the table each time it is asked, never stored.

A serial run with no Feeder above it reads the same confirmed signal, narrower. Once a Task ends
in a confirmed limit death, the run launches no further Task on that model for the rest of that
run; the Tasks it passes over keep whatever record they had, named by the terminal record and the
summary alongside the model, and the run still completes with exit 0, since a later run launches
them like any Task it has not yet reached. `run --defer ID` asks for the same treatment by name
instead of by evidence, for one listed Task, for one run, leaving its record, branch, and card
exactly as they were; an id the Manifest does not list refuses the run, and a Manifest whose
execution mode is triple refuses the flag outright.

Every mark, move, and hold this machine makes is one `limit` event, carrying `action`, `model`,
`to`, `tasks`, and `until`. `feed --status` lists every Marked model with its expiry and where
that time came from, every Held Task with the model that holds it, and the streak. `feed
--clear-limits` clears the marks, the streak, and the held snapshot in one step; the retry queue
stays, since a queued retry runs on its own once the mark it waited on is gone. It refuses beside
a live Feeder unless paired with `--restart`.

### Reason words

The complete words a `waiting` or `leaving` event, and a run's own `left_reason`, carry, as they
stand in `feeder.py`. No word here is renamed or removed by this entry; the `limit` event above
is the one addition.

Six waiting words:

- `lease_held`, another Runner holds the Lease on this Manifest or on its target repository.
- `model_held`, everything left to run this Cycle is Held on a model with no fallback Open, so
  the Feeder waits up to the earliest mark's expiry.
- `usage_limit`, nothing landed this Cycle, every death was quick, and at least one was
  Unconfirmed, so the whole Cycle wait covers a possible limit.
- `unreadable_source`, the Ready source failed to read, with nothing left to run, so the Feeder
  asks again.
- `idle_scanned`, nothing is ready except cards the launch scan refuses, so the Feeder waits
  rather than call the queue empty.
- `idle`, nothing is ready and nothing is unsettled, a plain empty queue waited before it leaves.

Nineteen leaving words:

- `state_unreadable`, the Feeder's own state file could not be read at start.
- `interrupted`, the Feeder process was interrupted from the keyboard.
- `crashed`, the Feeder's own loop raised an exception it did not expect.
- `once`, `--once` finished its one Cycle instead of waiting for the next.
- `restart`, a `--restart` or `--pin` is taking over from this Feeder.
- `stop_file`, an operator's stop file, or `--stop`, asked the Feeder to leave after this Cycle.
- `post_cycle_held`, a blocking post cycle hook exited nonzero with `post_cycle_hold` set, or a
  hold it already recorded still stands.
- `manifest_error`, the Manifest failed to load.
- `ready_source`, the sidecar names no ready policy this tracker can answer.
- `checkout`, the target repository's checkout is dirty, or off its default branch.
- `run_refused`, the Runner refused the Manifest or the environment.
- `unreadable_source`, the Ready source failed to read for too many Cycles in a row with nothing
  left to run.
- `all_refused`, every ready card was refused by `validate` for the model it is routed to.
- `empty_queue_scanned`, the queue is empty except for cards the launch scan refuses.
- `empty_queue`, the queue is empty, nothing ready and nothing left to run.
- `run_scoped_halt`, the run halted on a class that puts something outside the Task in question,
  so no halt was counted.
- `exclusion_failed`, a Task that reached `max_halts` could not be written into the Manifest as
  excluded.
- `limit_waits_exhausted`, the whole Cycle wait's streak passed `limit_waits_max`.
- `retry_unknown`, `--retry-blocked` named an id the Manifest does not list.

The Feeder is the one piece of runner code that writes a Manifest. Every write is a text edit
that is parsed back and compared with the change intended, validated by the manifest module from
a temporary file, and only then renamed over the Manifest, so no reader ever sees a half written
or an invalid one. It appends nothing while a Lease is held, and one Manifest has one Feeder,
held by a file lock the operating system releases when the process exits. Everything project
specific reaches it as data in a sidecar file named from the Manifest's stem, never as a Manifest
table, because a pinned older Runner must still load whatever the Feeder writes.

That protection covers only the Manifest. The sidecar itself carries the project facts and is not
backward compatible the same way: a sidecar key an older pinned Runner does not recognise, such as
`hooks.post_cycle` before issue #37, refuses that extract's Feeder to start and refuses `validate`
run from the same extract, both with the config exit. A pinned extract has to be at least as new
as every key the sidecar it feeds uses.

### Cycle
One pass of the Feeder: check the stop file and the checkout, run the `pre_cycle` hook when the
sidecar names one, read the Ready source, append a Batch, launch one run, read its summary, apply
the halt rules, then run the `post_cycle` hook when the sidecar names one. The stop file is
checked only between Cycles, so asking a Feeder to stop never interrupts a Task, and restarting
one means asking, waiting for it to leave, and starting the next. Nothing is killed.

The `post_cycle` hook learns that Cycle's landed, halted, blocked, and skipped ids and the default
branch's merge range from its environment. Blocking, the default, it is waited on and logged; with
`post_cycle_hold` set, a nonzero exit holds the Feeder rather than starting the next Cycle or
waiting, until the operator repairs the default branch and releases it. Detached, it is started
and left to run, reaped at a later Cycle's start.

### Batch
The cards one Cycle appends: the head of the ready list, as long as the room left, which is the
configured batch size minus the Tasks already listed that the next run will still launch. It is
small on purpose. A dependency that lands in one Cycle releases its dependants in the next, where
a fifty Task pass would hold them back for a day. A skipped Task holds no room, since a skip
costs no session, and is reported instead.

### Ready source
Where the Feeder learns which cards can start now, by the Tracker's own account, and the only
thing that may put a card into a Batch. It is the Tracker adapter's `ready` read: open issues
carrying every configured label on GitHub, the cards a configured JQL query returns on Jira, and
every unchecked box in a markdown tracker. A ready command that prints cards as JSON replaces
that read for a project whose rule labels cannot say. What ready means is the project's policy
and lives in the sidecar, never in an adapter.

A Cycle that finds nothing new to append reads as one of four different answers, and the Feeder
tells them apart rather than treating all four as an empty queue. A source with no policy
configured at all, GitHub or Jira with nothing named for what ready means, is refused before a
single Cycle runs, since no amount of waiting configures it. A source that answers but fails to,
a real read error, is logged and offers nothing new that Cycle, waited on and asked again, and
stops for a person only after enough failures in a row, because this one might resolve on its
own. Ready cards that validate refuses outright, every one routed to a model its own Manifest will
not accept, are not an empty queue either: only a person changing the routing releases them, so
the Feeder stops there and names them. Ready cards the launch scan refuses are a fourth answer of
their own, since issue #58: they wait like an empty queue, a card wanting a reword being a lesser
urgency than a routing mismatch, but the Feeder's own leaving reason says `empty_queue_scanned`
rather than `empty_queue` and names the cards, so a watcher never reads a board that in fact held
work as one that was never ready. Only when the source is readable and every ready card clears
both checks, and genuinely nothing new is left, is it a true empty queue: a Cycle with an empty
queue, nothing left to run in the Manifest, and no Lease held ends the Feeder, at once by default,
so no process is left polling an empty board.

### Model routing
How the Feeder chooses a Task's model when it appends one. A routing file beside the Manifest
wins, one line per card, read fresh at every append so it can be edited mid run. Then a body line
on the card of the form `**Model:** name`. Then the sidecar's default. A name outside the
sidecar's allowed set is ignored and logged, because a typo would halt the card twice and get it
excluded, and the append still passes the Manifest's own validation, which refuses a model that
belongs to another Backend. A card refused this way is remembered and left out of every Batch
until its routing changes, since retrying it unchanged would only be refused again. A Cycle whose
only ready cards were all refused this way is not an empty queue: the Feeder stops for a person
and names them, since only a routing change, not a wait, releases them. The Closeout process
stays on the Manifest's closeout model. The Review step runs inside the Task process, so it runs
on whatever model the Task was routed to.

### Test pass
One run of the browser test loop, a Feeder feature the sidecar's `[test_loop]` table switches on
for one Manifest. A pass is one `relay test` invocation: a full tour of the Tour document, or a
check of the cards a Cycle landed. The Feeder starts it as a subprocess between runs, a tour at
the loop's start and whenever the queue drains and a check after each Cycle that landed cards,
and an operator runs the same verb by hand. A pass takes both Leases, runs the sidecar's
`prepare` command to move the app under test to the default branch's commit and confirm it,
launches a Test process, decides in code what to file, launches a Filing process with exactly
that, and leaves a pass record beside the Manifest. Its outcome is the record's own `status`,
`ran`, `not_run`, or `failed`, never a Halt class: a pass is not a Task and writes no Task record,
so the closed set is untouched. The entries of a Test process's report are called findings in
the report's own contract, and they are not the Finding defined above: that is the Runner's
observation on a Task record, while a report finding is a defect or an improvement in the app
under test, which may become a card.

The code between the two processes is the point. The per pass cap, the loop's card budget, the
rule that low findings go to a lows file and never to the Tracker, report only mode, the stopped
areas, and each filed card's Generation are all decided there, before any card is written, so a
cap is prevented rather than detected afterwards. The Feeder holds the loop's state under one
state file key and stops the loop on a rule it checks: a clean tour, a tour whose findings made
no new card, a round cap, a clock cap, or the card budget. A stopped loop starts no further pass,
and the Feeder goes on building what it filed. `docs/manifest-authoring.md` section 12 names
every setting and stop reason.

### Test process
The fresh headless agent invocation a Test pass launches to test the running app. It drives the
app through a headless browser from the shell, using the driver the Tour document names, reads
the code for each defect's cause, and ends with one `relay-test-report` block of JSON findings,
each with a severity, an area, a cause file and line, the steps, and Done when lines. It runs in a
detached worktree of the tested commit, outside the checkout the Runner merges into, and the pass
fails and files nothing when that checkout changed underneath it. It holds no Tracker write tool
and `Bash(gh *)` is denied it, so a tour cannot file, comment, or move a card by any route. It
never types a credential, using the session the operator signed in, and never approves anything
that writes outside the app, stopping at each approval step. When it can reach some areas and
not others, it tests what it can reach and names the rest in the report's `untoured` list, and
the Feeder never reads such a tour as a clean one; a report that reached no area is `not_run`.
Its Brief carries nothing project specific; project facts reach it as sidecar data and from the
Tour document.

### Filing process
The short launched process a Test pass starts after the code has chosen what to file. It gets
the chosen findings, numbered, and the Tracker adapter's filing instructions, which put the
sidecar's loop labels on every card. For each finding it first looks for an open card describing
the same defect and comments there; otherwise it files one card. It ends with one `relay-filed`
block naming each finding's card, and the pass reads every named id back through the adapter,
recording only what the Tracker confirms. It is to a Test pass what the Closeout process is to a
Task: the Feeder and the Runner still never write to a Tracker, and a launched process writes
through the adapter's own instructions. It runs in the checkout on the Manifest's closeout
model, and on a markdown Tracker its commit is bounded to the tracker file by the Closeout's own
scope check. A Filing process that did not complete, one that could not launch, timed out, lost
the Lease, left no readable block, or had its commit reset, fails the pass with that sentence,
so the Feeder reads a filing failure as a failed pass and never as findings that produced no
card. The attended planning card it files for a stopped area is a person's, counted toward
neither the loop's card budget nor a pass's new cards.

### Tour document
The app's own description of what a Test process tests, kept in the app's repository at the path
the sidecar's `test_loop.tour` names. Every markdown heading in it is one area, and a finding
names its area exactly as a heading spells it; a finding naming anything else is recorded as
invalid and never filed, and an area the loop stopped testing is passed by its heading. Under
each heading it says what to check, what counts as a defect, and the approval steps not to pass.
Its opening text, before any heading, names the driver, the storage state file holding the
operator's signed in session, and how to tell the sign in page. It travels to the Test process
inside the Brief's data fence, as data, never as instructions.

### Generation
How far a card the browser test loop filed is from a card a person wrote, and the rule that keeps
a check from feeding itself. A card filed by a full tour, or by checking a card the loop did not
file, is generation 1. A card filed by checking a generation 1 card is generation 2, the last:
when it lands, no check runs on it, and its fix lands on the gate alone. A check pass finding that
names no card the pass sent is taken as generation 2 too. The Feeder records each filed card's
generation in its state file, so the rule survives a restart.

## Outcomes

### Verify-landed
The Runner's own determination, from git and the Tracker alone, of whether a Task actually landed.
It never reads the Task process's exit code, printed result, or claims as evidence, because a
headless run has every incentive to report success.

The landing it recognises does not have to be the Runner's own merge. An operator who finishes a
Task by hand between runs and lands it through the project's own tooling has produced the same
outcome by another means, and Verify-landed accepts it on the same terms, because it reads git and
the Tracker rather than trusting which actor performed the merge.

### Landed
A Task whose work is durably where the Shipping mode says it belongs and whose Tracker record names
the landing. Both halves are required: code that merged while the card stayed put is a partial
landing, not a landing, and it halts the run.

Landed says where the Task's work went, not that the Task did all of the work it set out to do. A
Task refused part of its change that finishes the rest and merges satisfies both halves and is
Landed, with the refusal recorded as a Finding. Completeness is not a property the Runner can read
from git or the Tracker, so no term in this vocabulary asserts it.

### Blocked
A Task whose process stopped deliberately without landing, leaving the repository as it found it. A
Blocked Task is a normal outcome rather than a failure, and the run always continues to the next
Task after one.

A Blocked Task counts as settled, and a later run does not attempt it again unless the operator asks
for that at launch, so a resumed run that skips every Blocked Task still reports itself complete. A
Task process that exits with no return envelope for a reason outside the Task, such as an exhausted
usage window, is also recorded Blocked, so a row of them after one cut off Task is usually one event
rather than many.

A Blocked Task is only legible to an operator who was not watching if the blocker reaches somewhere
outside the run's own transcript. The Closeout process writes that record, as a comment on the
Tracker, and the Runner never does: it reads the Tracker afterwards to confirm a comment appeared
and reports a Blocked Task whose card carries none as a check for the operator to make by hand. So a
Blocked Task the Closeout failed to record is visible in the run summary rather than on the board,
which is the accepted cost of the Runner holding no write path at all.

### Excluded
A Task the Manifest itself keeps out of the run, named with the reason rather than left off the
list. The record stands until the operator edits the Manifest, and no later run reconsiders it.

Naming an excluded Task rather than deleting it keeps the reason where the next operator reads it,
so work that needs an attended session, a paid run, or a decision only the maintainer can make stays
visible in the run's own summary instead of vanishing from the queue.

### Skipped
A Task the Runner declined to launch on what it read at launch time: a card it could not read, a
card already at a done status, or text naming a path the Task process would be refused. It is the
Runner's decision about this moment, where Excluded is the Manifest's standing decision, and where
Blocked is the Task process stopping after it started.

A skip reports what the card said rather than judging the Task, so every later run checks the same
Task again and launches it once the condition clears. That is what makes moving a card the repair
for a skip rather than a problem with one: an operator who lands a Task by hand and moves its card
to a done status turns the next run's launch into a skip, and that skip is the run stepping over
work that is already done.

### Card audit
The Runner's pass over every Task's card at the end of a run, comparing each with its record
and with git, and the same pass an operator can run between runs on demand. It names four
disagreements: a card at the in review status with no process on it, a Landed record whose card
is no longer terminal, a terminal card nothing landed for, and a card that could not be read. It
reports and never repairs, because the Runner holds no way to move a card; each finding says
what to move and where, and the summary lists it as a check by hand. A finding reads the card and
the record alone, so where it names a card to move back it is assuming the Task was abandoned; an
operator who has instead landed that Task by hand wants the card moved forward, and the audit has no
way to tell the two apart. The run end pass writes
under the Lease; the on-demand pass takes none and writes nothing, so it is safe beside a live run, where a
record in flight is a process at work rather than a stale card.

### Shipping mode
The per-project choice of what landing means: a merge to the default branch performed outside the
Task process, or a pull request whose checks have decided. The Manifest names one, and Verify-landed
applies the matching checks. Only the merge mode is implemented; the pull request mode is named in
the schema and refused before a run starts, so no Task can be driven under it today.

Beside the mode, the Manifest says whether the Runner pushes. Pushing is the default. A Manifest
that turns it off keeps the merge mode's whole sequence and drops every push from it, so a landing
is the local default branch and nothing any process in the run does reaches a remote. That changes
what agreement with the remote means rather than removing it: the local default branch runs ahead
by design, and only a remote that has diverged from it stops the run. Such a run's summary ends on
the one command that would ship it, because a run that pushed nothing has to say so.

## Surroundings

### Tracker adapter
The read-side interface between a Runner and whatever holds the Task list and their statuses. Each
adapter exposes the same operations regardless of what sits behind it, and not one of them writes:
listing candidate Tasks, listing the cards that are ready to start for a Feeder, reading one
Task's status and its recent comments, confirming that a
closing reference is present, and supplying what the Task process and the Closeout process need in
order to write to that Tracker themselves. The absence of a write operation is the whole guarantee.
A Runner cannot move a card by mistake because it holds no way to move one at all.

### External gate
The project's own mechanism that refuses a broken change independently of any agent's judgment,
such as a pre-push hook or continuous integration. Relay requires one to exist before it will run
against a project, and verifies its presence rather than providing it.

Those two forms are not interchangeable to the Runner, and the difference is where each spends its
time. A gate wired into a pre-push hook runs inside the Runner's own push, so the gate's whole
runtime is charged to that one command and has to fit whatever bound the push carries. A gate that
runs after the push returns costs the Runner no time at all and is bounded separately, if at all.
A project can therefore satisfy the requirement in either form and hand the Runner a very different
problem.

The pre-push form carries a further cost the other does not: it inherits the git process's own
environment, which can include scoping variables git sets to let the hook find the repository it
is gating. Anything the hook spawns that also shells out to git, such as a suite that builds its
own throwaway repositories to test against, inherits those same variables when git sets them and
can be silently redirected back at the repository the hook is protecting. A gate that runs after
the push, in its own job environment, does not share
this exposure.

### Qualifying properties
The three conditions a project must meet before Relay can run against it: its Tasks are independent,
the state carried between Tasks is durable in git or on the Tracker, and an External gate refuses
broken changes. These describe the project. A Task can still be individually unrunnable while all
three hold.

# Concepts

Shared domain vocabulary for this project, entities, named processes, and status concepts with
project-specific meaning. Seeded with core domain vocabulary, then accretes as learnings are
recorded; direct edits are fine. Glossary only, not a spec or catch-all.

## Relationships

A Runner reads one Manifest and drives a series of Tasks. Each Task gets its own Task process and,
once it exits, its own Closeout process. The Runner decides a Task's outcome by Verify-landed,
which consults git and the Tracker through a Tracker adapter, never the Task process itself. The
Shipping mode named in the Manifest decides what landing means for that project.

## The loop

### Runner
The Relay process that drives a Manifest to completion: it launches one Task process per Task in
order, decides each outcome, and halts rather than continuing past an outcome it cannot confirm.

The Runner holds no project knowledge of its own. Everything project-specific reaches it as
Manifest data. It reads the Tracker but does not write to it, so a defect in the Runner can never
move a card.

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

A Lease is renewed on a heartbeat rather than held for the length of the work, so it expires on
its own if a Runner dies. A Lease past its expiry is stale and the next Runner reclaims it,
marking any Task the dead Runner left in flight as halted. The expiry is deliberately shorter
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

### Task
One unit of work the operator defined before the run started, identified by a Tracker record. Tasks
in a Manifest are independent of each other by requirement; a Task that depends on another belongs
in a later run. A Task may be marked excluded from unattended runs, with a stated reason, when
something about it needs a human present; that record reads excluded, and only the Manifest lifts
it. A Task the Runner declines at launch, because its card could not be read, was already
terminal, or names a `.claude/` path, reads skipped instead, and every later run checks it again,
so fixing the card is the repair. The same `reason` field is required when a Task's
backend differs from the manifest default. A Task that matches the default needs none.

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
unit records, comment the card, print the Envelope. The plan is a message in the transcript rather
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
carries a landing reference, since that card was closed by a landing.

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
refusal the Task process worked around leaves a finding on a landed record and nothing else. Some
findings reach the operator only through the run summary's check by hand list, which is why that
list is read on a run that reports no halts at all.

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

### Card audit
The Runner's pass over every Task's card at the end of a run, comparing each with its record
and with git, and the same pass an operator can run between runs on demand. It names four
disagreements: a card at the in review status with no process on it, a Landed record whose card
is no longer terminal, a terminal card nothing landed for, and a card that could not be read. It
reports and never repairs, because the Runner holds no way to move a card; each finding says
what to move and where, and the summary lists it as a check by hand. The run end pass writes
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
listing candidate Tasks, reading one Task's status and its recent comments, confirming that a
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

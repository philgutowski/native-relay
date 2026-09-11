---
title: A shell backgrounded child outlives the Task that started it, and nothing the runner records can see it
date: 2026-09-11
category: workflow-issues
module: runner
problem_type: workflow_issue
component: runner
severity: high
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [task-process, launcher, contracts, permission-mode, summary, verify-landed]
applies_when:
  - "a Task process runs headless under claude -p in an unattended run with nobody watching"
  - "the Task's verification wants a local server, a preview build, or any process that keeps listening after the command returns"
  - "the Task starts that process with a plain shell ampersand inside an ordinary Bash call"
  - "the Task then exits normally, returning complete or blocked, so no kill trigger fires"
  - "an operator reads status, summary, or the end of run audit expecting them to account for everything the run left behind"
symptoms:
  - "a local server started by one Task is still holding its port hours later, past the Task and past the whole run"
  - "the run finishes clean and the record shows a normal envelope, so nothing marks the Task as having left anything running"
  - "status, summary, and the end of run audit are all silent about the listening socket and about any leftover temp directory"
  - "the only trace is denied_tool findings, and their target text truncates at 120 characters so the teardown command is hard to read back"
  - "the Task's own teardown is refused twice by the runner's deny list, not by anything in the target repository"
tags: [background-process, process-group-kill, unattended-run, task-process, deny-list, port-leak, run-teardown, headless-claude]
---

# A shell backgrounded child outlives the Task that started it, and nothing the Runner records can see it

## Context

The Runner bounds a Task process with a process group kill. The launcher starts every Task in its
own session, `start_new_session=True` in the `Popen` call at
`skills/relay/scripts/relay/launch.py:292` to `:293`, and captures the group id immediately
afterwards at `launch.py:302` to `:305`, because resolving it later fails once the group leader has
been reaped. `_kill_group` at `launch.py:235` SIGTERMs the whole group, waits the grace, then
SIGKILLs what is left.

That kill fires on exactly three triggers, and all three are trouble:

- An operator signal. SIGINT and SIGTERM are captured at `launch.py:314` to `:319`, the `handle`
  function registered at `launch.py:336` calls `_kill_group` at `launch.py:324`, then releases the
  lease and re raises. The child is in its own session, so the operator's Ctrl+C never reaches it
  and the Runner has to pass it on.
- A lost lease. The heartbeat's `lost` flag is checked at `launch.py:358` and the kill runs at
  `launch.py:360`.
- The deadline. Checked at `launch.py:362` against the monotonic deadline computed at
  `launch.py:341`, kill at `launch.py:367`. The comment there says it is deliberately not
  conditioned on the child still running, because a grandchild holding the inherited stdout pipe
  keeps the reader from ever seeing EOF.

The fourth path is the ordinary one, and it has no kill in it. A Task whose process exits normally
leaves the loop through `launch.py:369` to `:376`, which breaks on `proc.poll()` and calls nothing.
So the cleanest finish is the one case where the group is never swept. Anything the Task backgrounded
with a plain shell ampersand, reparented away from the process the Runner is waiting on, survives the
Task and then survives the rest of the run, holding whatever port or temporary directory it was
given.

This is the mirror image of
`docs/solutions/workflow-issues/headless-turn-end-is-exit-backgrounded-command-is-killed.md`, which
records a Task that started a mutation driver as a harness tracked background task and ended its turn
to wait for it. There the CLI killed the background task when the turn ended, because ending the turn
is exiting, and the group kill stood behind that. Here the Task never ended a turn waiting. It ran an
ordinary foreground Bash call whose command text happened to end in `&`, kept working in the same
turn, and finished cleanly. From the observations in this run, a child spawned that way is not a
harness background task at all, so nothing on the CLI side appears to track it or reap it, and the
Runner side, as shown above, has no reason to.

What the Runner does record is git state, tracker state, and the transcript, and nothing else.
`summary.build` at `skills/relay/scripts/relay/summary.py:223` states its own scope, "Reads state
only; acquires nothing and changes nothing." `_pending_checks` at `summary.py:109` to `:180` derives
every check by hand from record statuses, the findings on those records, the card audit, and the
shipping decision. `audit.build` at `skills/relay/scripts/relay/audit.py:32` is described at
`audit.py:12` to `:14` as reading the adapter, the store, and git, taking no lease and writing
nothing. Nowhere in the runner package is there a network socket, a port bind, or a process
listing. The only process control anywhere in it is `os.killpg`, at `launch.py:248` and `:256`
and at `gitwrite.py:44` and `:53`, and the only use of the `socket` module at all is
`socket.gethostname()` at `state.py:97`, labelling the host that holds a Lease. So `status`, `summary`, and the end of run audit are not merely quiet
about a listening socket or a leftover temporary directory, they are structurally incapable of
mentioning one.

The only trace the incident left was two findings of class `denied_tool`
(`skills/relay/scripts/relay/contracts.py:354`), each rendered from the template at
`contracts.py:523`, "{tool} denied by the task's permission posture on {target}", and printed as one
indented `finding:` line under the task entry at `summary.py:323` to `:325`. The target text comes
from `classify._target_of` at `skills/relay/scripts/relay/classify.py:60` to `:70`, which returns a
Bash command truncated to `ARGUMENT_CHARS`, pinned at `classify.py:36` as 120 characters. A long
teardown command cut at 120 characters reads like noise beside the run's other lines, and anything
chained past that cut is not in the record at all.

The sharp part is that the refusal was correct, so this is a trade and not a defect. The same deny
list that keeps an unattended run safe is what refused the Task's own teardown. `KILL_LIKE_TOOLS` at
`contracts.py:270` to `:274` holds `Bash(kill*)`, `Bash(pkill*)`, and `Bash(killall*)`, and the
comment above it at `contracts.py:265` to `:269` records why: in round six, task #40, a Task chasing a
hung unittest child ran `kill -9` over a list of pids and swept in the Runner's own pid and its
`caffeinate` wrapper, with nothing at the permission layer to stop it. `Bash(rm -rf*)` sits in
`DISALLOWED_TOOLS` at `contracts.py:286` and again in the `DESTRUCTIVE_TOOLS` subset at
`contracts.py:309`, where a match on a backend that does not enforce refuses the landing outright.
Both globs are right and both must stay. The consequence is that a Task cannot tear down its own
background process by either of the two most natural spellings, which makes the only safe conclusion
the one about starting it: a process the Task is not permitted to kill is a process the Task must not
start.

### What was observed

This came from watching a run, not from a commit or a pull request, and there is no code change
behind it. That run's state directory lives outside the repository, so the counts below come from
the run itself rather than from anything the tree can confirm. During a run of 14 Tasks on 2026-09-10, one Task set out to verify a rendering defect. It
started a local static server inside an ordinary Bash call, roughly
`python3 -m http.server 8791 > server.log 2>&1 &`, wrote the pid to a file, then rendered screenshots
with headless Chrome. It could not reproduce the defect and returned a blocked envelope, which is a
clean finish rather than a failure.

Its teardown was refused twice, and both refusals came from the Runner's own deny list rather than
from anything in the target repository. A command beginning `kill $(cat ...)` matched `Bash(kill*)`.
A command beginning `rm -rf /tmp/...` matched `Bash(rm -rf*)`, and because the refusal landed on the
whole call, a `pkill` fallback chained inside that same command never ran either. The server held its
port past the end of the Task and past the whole run, 5 hours and 24 minutes. The operator killed it
and cleared the temporary directory by hand afterwards. No later Task in the run collided with it.

## Guidance

**A Task's verification should prefer no long lived local process at all.** The shapes that work
headless are the ones whose lifetime ends inside a single foreground command:

- **Render from the file system when an origin is not required.** One command, nothing left bound.
  A headless browser given a `file://` path writes its screenshot and exits.
- **Make a single request rather than standing a service up.** If the check is "does this endpoint
  return the right bytes", a one shot request inside one command answers it, and there is nothing to
  tear down.
- **Use a test harness that owns the server's lifetime.** A browser test runner, or a unittest or
  pytest fixture that binds in setup and releases in teardown, keeps the bind and the release inside
  the same process that the Task's foreground command is waiting on. When the command returns, the
  socket is already closed. This is the right answer whenever the page genuinely needs an origin,
  because the teardown happens as a library call inside the program, not as a `kill` the permission
  layer will refuse.
- **If a helper process is truly unavoidable, own it from inside one program.** A short script that
  spawns the helper, does the work, and terminates it in a `finally` block runs as one foreground
  command and leaves nothing behind. Note what this is and is not: it is not a way around the kill
  deny rule, it is a way to not need a kill. The rule exists because a Task issuing `kill` at the
  shell has no idea which pids belong to it, and a program terminating the child it just created
  does.

**What a Task should write instead, concretely.** A plan step that reads "start a server, take
screenshots, stop the server" should be rewritten before the run, not repaired during it. Either
"render the page from disk with a headless browser and record the screenshot path", or "run the
project's own browser test command and record its output". If a port must be bound anyway, the Task
should name the port and the temporary directory in its own envelope text, because the Task's prose
is the only channel that survives into the record. The findings will not say it and the summary
cannot.

**When a verification does bind a port, the run needs a check by hand at the end.** The Task cannot
be relied on to remove the process, for the two reasons above, and the Runner will not: the group
kill fires on trouble and this Task finished cleanly. So the operator checks the host after the run
with `lsof -iTCP -sTCP:LISTEN` and looks for the temporary directories the transcript named. That
step belongs to the operator rather than to the Runner because there is no seam to hang it on.
`_pending_checks` at `summary.py:109` builds its list from records, findings, cards, and shipping, so
adding this would mean inventing a host inspection the Runner does not do, and the halt classes are
a closed set at `contracts.py:351` to `:368` with nothing in it that describes a stray listener. This
is guidance for authoring plans and for reading runs, not a code change.

**The brief's existing foreground rule does not cover this case, and should not be assumed to.**
`skills/relay/templates/brief-local-merge.md:19` to `:26` forbids starting work in the background and
ending the turn to wait for it, and forbids ending a turn on a promise to resume. Read literally, it
is about turn boundaries. This Task crossed no turn boundary: it backgrounded a daemon and carried on
working in the same turn, exactly as the rule allows by its letter. The `waiting_last_message`
finding at `contracts.py:381` to `:386` cannot fire here either, because it only looks at runs that
did not end in a complete envelope, and this one returned a clean blocked envelope. If this is ever
pinned in the templates, it belongs beside the foreground rule as a separate sentence about leaving a
process listening, not as a new halt class.

## Why This Matters

The run cost an operator two minutes with `kill` and `rm`, and nothing else. It matters because of
what the same shape does under slightly different conditions, and because of which Tasks it selects.

The selection is the uncomfortable part. The group kill exists for signals, lost leases, and
deadlines, so a Task that hangs, dies, or gets interrupted has its whole group swept. A Task that
does its work and exits cleanly does not. The Tasks whose orphans survive are therefore the
well behaved ones, which is the opposite of where an operator looks after a run.

The failure scales badly on a fixed port. Relay runs Tasks serially in one long process, so a second
Task binding the same port in the same run finds it taken. The loud version of that is a bind error
and a blocked envelope, which is survivable. The quiet version is worse: a second Task's browser
reaches the first Task's server, which is still serving the first Task's files from the first Task's
working tree, and the verification passes or fails against content that has nothing to do with the
change under test. A false verification is the one kind of wrong answer that lands code, and nothing
in the record would contradict it.

Observability is the second cost, and it is a structural one rather than a gap somebody forgot to
fill. Everything the summary and the audit print is derived from state: records, findings, cards, git.
A host level side effect has no representation in that model, so the artifact an operator reads after
an unattended run is silent by construction. The two `denied_tool` findings were the whole signal, and
at a 120 character truncation they read as a Task that bumped into the deny list, which happens.

The third reason is the trade itself. The obvious reflex, loosen the deny list so a Task can clean up
after itself, is a direct regression to round six task #40, where an unbounded `kill -9` took out the
Runner's own process. `Bash(kill*)` and `Bash(rm -rf*)` are both correct and both stay. That leaves
exactly one lever, which is what the Task starts in the first place, and the only place to pull it is
in how verification steps are written.

## When to Apply

- Authoring or reviewing a manifest, plan, or verification step where a Task has to check rendered
  output, a served page, a local API, a database, or anything else that binds a port or holds a
  temporary directory. Choose the shape before the run; there is no repair during it.
- Reading a run's summary where a Task returned blocked or complete on a verification that plausibly
  needed a server, especially with a `denied_tool` finding whose target text is cut off. Check the
  host for listeners before assuming the machine is clean.
- At the end of any unattended run on a machine that will host another one. `lsof -iTCP -sTCP:LISTEN`
  and a look at the temporary paths the transcript named is the whole check.
- Any headless agent running under a deny list that includes kill like tools, in this repo or
  anywhere else. The general rule travels: a process the agent cannot kill is a process it must not
  start.
- Not applicable to a command that binds and releases within itself, or to a test harness whose
  fixture owns the server. Those shapes are the recommendation, not the hazard.

## Examples

### The shape that leaked

The verification step, as the Task actually ran it, in outline:

```
python3 -m http.server 8791 > server.log 2>&1 &
echo $! > /tmp/<work dir>/server.pid
# headless Chrome screenshots against http://127.0.0.1:8791/
```

The teardown, and what happened to it:

```
kill $(cat /tmp/<work dir>/server.pid)        # refused, matched Bash(kill*)
rm -rf /tmp/<work dir> || pkill -f http.server  # refused, matched Bash(rm -rf*)
```

Two things are worth naming here. Both refusals came from the Runner's own list, at
`contracts.py:271` and `contracts.py:286`, and not from anything in the target repository, so a Task
author reading the project's own settings would find no reason for them. And the `||` fallback on the
second line never executed, because the call was refused as a whole before the shell ever saw it: a
recovery path chained behind a command that the permission layer can refuse is not a recovery path.
The server outlived the Task and the run.

### What the same verification should have looked like

With no origin required, one foreground command and no residue:

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --screenshot=/tmp/<work dir>/render.png file:///<repo>/dist/index.html
```

With an origin required, the bind and the release both belong inside the program the command waits
on. A browser test runner does this by default, and a unittest fixture does it explicitly: start the
server in `setUpClass`, shut it down in `tearDownClass`, and let the Task run the test command in the
foreground. The Task issues no `kill`, so nothing is refused, and when the command returns the port
is free because the process that held it is gone.

### The trace it left in the record

What the summary printed, in shape, for a Task that finished cleanly:

```
T-<n>  blocked
    blocked: <the Task's own blocker text>
    finding: Bash denied by the task's permission posture on kill $(cat /tmp/<work dir>/serv
    finding: Bash denied by the task's permission posture on rm -rf /tmp/<work dir> || pkill
    branch left in place: <branch>
```

That is the entire record of the incident. The status is `blocked`, which is an ordinary and correct
outcome for a defect that would not reproduce. The two findings are truncated at 120 characters by
`classify.py:69`, so the `pkill` fallback is only half visible on the second one and the intent
behind either command has to be inferred. Nothing in the run summary, the JSON, or the card audit
mentions a port, a pid file, or a directory, because none of those surfaces has a field for one.

## Related

- `docs/solutions/workflow-issues/headless-turn-end-is-exit-backgrounded-command-is-killed.md`: the
  opposite case, and the one to read first. There a backgrounded command died with the turn, which
  was correct on both sides and cost the run an hour of stranded work. Here a backgrounded command
  survived the turn, the Task, and the run, which is equally correct on both sides. The pair is the
  point: whether a backgrounded child outlives its Task depends on how it was spawned, so a Task
  cannot reason about it safely and should not spawn one.
- `docs/solutions/logic-errors/process-group-kill-resolves-target-lazily.md`: how the group kill
  finds its target, and why the pgid is captured at launch. That kill is the mechanism whose absence
  on the clean exit path is what this learning turns on.
- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`:
  the standing rule that a live run is the only instrument for behaviour the stub cannot produce. The
  stub `claude` in `tests/stub-claude` never backgrounds anything and never binds a port, so no test
  in the suite could have surfaced this.
- `docs/solutions/workflow-issues/headless-dontask-blocks-claude-dir-edits.md`: the first of the
  harness and permission layer walls that only a live unattended run found. Its closing argument, that
  a runner should assume there are more such walls it has not met, holds again here, with the twist
  that this wall is one the project put up on purpose.
- A pre-flight sweep before this learning was written found no existing coverage anywhere in the
  repository, not in `docs/solutions/`, `docs/backlog.md`, `CONCEPTS.md`, the commit messages on any
  ref, or the issue list, and identified the neighbour contradiction above as part of the work rather
  than as a courtesy cross reference (session history).
- No tracker item covers this. Issue #9 is the nearest open one and is adjacent rather than
  overlapping: it is about a verdict the summary omits from evidence it already holds, where this is a
  class of fact the summary holds no evidence for at all.

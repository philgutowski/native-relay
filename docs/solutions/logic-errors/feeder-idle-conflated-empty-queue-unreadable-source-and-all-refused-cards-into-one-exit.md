---
title: The feeder's idle check conflated an empty queue, an unreadable ready source, and all-refused cards into one exit
date: 2026-09-22
category: logic-errors
module: runner
problem_type: logic_error
component: runner
severity: medium
root_cause: missing_validation
resolution_type: code_fix
related_components: [feeder, adapters, manifest]
symptoms:
  - "a github or jira feeder with no [ready] labels or jql configured in the sidecar had that config gap read as a failed read, so it waited and retried before stopping with exit 1 saying the ready source could not be read, instead of refusing the gap immediately"
  - "a cycle where every fresh ready card had been refused by validate for its routed model looked identical to a genuinely empty board, so the feeder exited 0 saying the queue is empty while the board still held work that only a routing change could release"
  - "idle_waits_max defaulting to 0 meant an unattended feeder now exited on the very first cycle that looked empty, ending the run permanently on what could be a misread or an all-refused cycle rather than a true empty queue"
  - "the idle_waits, limit_waits, and unreadable_waits in-a-row counters persisted in <manifest-stem>.feeder.state.json across process restarts, so a feeder that left via the stop file, --restart, a KeyboardInterrupt, or a halt stop handed the next feeder process a partial streak count from a run it was never part of"
  - "the three streak counters were each hand rolled and disagreed on > versus >= and on which exit paths reset them, so the same in-a-row rule was enforced three slightly different ways"
  - "a fourth cause was added later (issue #41, the R41 path scan run at append time): the first pass at it passed every fresh card, scan-refused ones included, into idle()'s fresh_ids, so a card the scan refused and that never reached model routing at all was reported with the all-refused message telling the operator to change the routing"
  - "issue #58: a scan-refused card that fell through to the empty-queue path still left with the plain empty_queue reason, so a watcher reading only the leaving event could not tell a genuinely empty board from one still holding a scanned out card, and report_once's persisted reported table kept the card's scan_skip key for the life of the state file, so a second feeder start, or a --once cron run, saw the same card and stayed silent about it"
tags: [feeder, idle-loop, empty-queue, ready-source, refused-cards, streak-counters, exit-code, code-review-catch, scan-refused-cards, report-once, process-boundary]
---

# The feeder's idle check conflated an empty queue, an unreadable ready source, and all-refused cards into one exit

## Problem

The feeder's empty-queue fast exit (commit `c210090`) treated "nothing appended, nothing
unsettled" as a single condition meaning "the queue is empty." It is actually the visible
symptom of three different states, and the feeder could not tell them apart, so it could exit 0
(leave quietly, no runner watching the manifest afterward) on two states that actually needed a
person's attention.

## Symptoms

- A GitHub or Jira manifest whose feeder sidecar never got a `[ready]` table (no `labels`, no
  `jql`, no `ready.command`) would run cycles anyway, have its adapter's `ready()` report the gap
  as `(cards=[], reason=...)` on every call, and eventually exit 0 through the ordinary idle path
  as if the board were genuinely empty, rather than stopping up front. The adapter's own message
  for this: `"no ready labels are configured; set [ready] labels in the feeder sidecar"`
  (`skills/relay/scripts/relay/adapters/github.py:333`) or `"no ready query is configured; set
  [ready] jql in the feeder sidecar"` (`skills/relay/scripts/relay/adapters/jira.py:265`).
- A manifest where every ready, unlisted card had been refused by `validate()` (routed to a model
  its backend doesn't run) would also exit 0 and leave, logging nothing more informative than the
  ordinary "nothing ready" line, when in fact `self.state["refused"]` held live entries and the
  board had unbuildable-as-routed work sitting on it that only a person editing the routing file
  could release. `skills/relay/scripts/relay/feeder.py:702-726` documents the `refused`
  mechanism `append()` already had before this fix.
- A feeder that left through the stop file, `--restart`, `KeyboardInterrupt`, a dirty-checkout
  stop, a refused-manifest stop, or a run-scoped-halt stop would leave whatever `idle_waits`,
  `limit_waits`, or `unreadable_waits` count it had accumulated sitting in
  `<manifest-stem>.feeder.state.json`. The next feeder process against the same manifest silently
  inherited that count and could hit its "waited N times" threshold and give up sooner than its
  own configured `idle_waits_max` or `UNREADABLE_WAITS_MAX` actually allows, using up a budget it
  never spent.

## What Didn't Work

The feeder's original build (session history, 2026-09-19) added `ready()` as a symmetric,
read-only ninth method across all three adapters, and `manifestedit.py` as the atomic, validated
write path so appending a card can never corrupt a manifest a live run is reading concurrently.
Both were deliberate design choices, not afterthoughts. But the exact ambiguity this doc
documents was already live in that build, and was seen and correctly diagnosed in the moment
without ever becoming a code change (session history, 2026-09-20/21, a ~19-hour live Cratekit
run):

- At `2026-09-21T09:47`, the feeder logged `cycle 11: 0 unsettled in the manifest, 0 ready and
  unlisted, appending []` immediately after its `pre_cycle` hook (`gh project item-edit ...`) had
  exited 1. The operating session's own words at the time: "That `0 ready` is not an empty
  board. It is the ready source failing to read one." The actual cause was a transient GitHub
  API rate limit in `gh project item-list`, and it resolved itself on the next cycle. This is a
  live, concrete instance of the unreadable-source case, correctly diagnosed as distinct from an
  empty queue in the moment, but the diagnosis never turned into a code change (session history).
- The same run also produced a case resembling the all-refused-cards state without resolving it:
  card `#29` stayed ready and unlisted for the rest of the run, the only such card, and was never
  appended. The session flagged it directly: "#29 is the interesting one. It is the only card
  the feeder could legitimately take and it did not, which means the ready source or the deny
  set is holding it back." It was never root-caused as a routing refusal or something else
  (session history).

Then, months later, the `c210090` change (default `idle_waits_max = 0`, leave at once on an
empty cycle, `skills/relay/scripts/relay/feeder.py:119`) turned what had been a slow, forgiving
day-long wait into an immediate, permanent exit on the very first cycle that merely looked
empty. It was itself correct for the real empty-queue case, and its own tests passed. It looked
complete. The gap was not found by manual testing or a second investigation pass; it surfaced
under a deliberate `/code-review` (level `high`) run before the change was pushed anywhere, per
this project's own standing habit of reviewing before shipping.

The reason unit testing did not catch it either time: the suite's fakes make all three states
trivially distinguishable by construction. A fake adapter's `ready()` either returns cards or
doesn't; a fake `append()` either accepts entries or doesn't. Neither fake naturally produces the
ambiguity a real dependency produces, where a real `gh issue list` failure and a permanently
unconfigured sidecar both come back through the adapter as the identical shape,
`(cards=[], reason="...")` (`github.py:333`, `jira.py:265`), and a real `validate()` mismatch
looks, from `idle()`'s point of view, exactly like nothing being ready at all. Passing tests
against a fake ready-source and a fake `append()` is not the same as the read failing for a real
reason, or a real model mismatch tripping the real `validate()`.

## Solution

**(a) `ready_source_problem()` as a pre-cycle guard.** Added as a module-level function
(`skills/relay/scripts/relay/feeder.py:739-751`) and called in `Feeder.cycle()` right alongside
the pre-existing `checkout_problem()` guard:

```python
# feeder.py:508
problem = ready_source_problem(manifest, config) or checkout_problem(manifest)
if problem:
    return self.stop(EXIT_CONFIG, "stopping: %s. Relay owns the default branch while it "
                                  "runs, so this is a person's to look at." % problem)
```

`ready_source_problem` checks the adapter type against `config.ready_source` (or a configured
`ready.command`, which bypasses the check entirely) and returns a sentence like `"no ready labels
are configured; set [ready] labels in the feeder sidecar"` before a single cycle runs, rather than
letting the config gap masquerade as a transient read failure inside `ready_cards()`.

**(b) `idle()` gains `fresh_ids` and a three-way branch.** Before the fix, `idle()` took only
`readable` and could not see whether refused cards existed. Now (`feeder.py:552`, `feeder.py:567`):

```python
elif not unsettled:
    return self.idle(readable, [card["id"] for card in fresh])
```

and inside `idle()` (`feeder.py:567-601`), the branches run in this order:

```python
if not readable:
    ...  # unreadable source: wait, then stop after UNREADABLE_WAITS_MAX in a row
self.state["unreadable_waits"] = 0
if fresh_ids:
    return self.stop(EXIT_CONFIG, "stopping: every ready card was refused with the model "
                                  "it is routed to, ...")
if self.strike("idle_waits", config.idle_waits_max):
    return self.stop(EXIT_OK, "the queue is empty, leaving: ...")
```

Only when `readable` is true and `fresh_ids` is empty does the function fall through to the real
empty-queue path.

**(c) `STREAKS` reset in `Feeder.run()` and the `strike()` helper.** A new module-level tuple
names the three "in a row" counters (`feeder.py:60`, `STREAKS = ("limit_waits", "idle_waits",
"unreadable_waits")`), and `Feeder.run()` resets all three at process start, once, guarded by
`not self.once`:

```python
# feeder.py:458-464
if not self.once:
    for key in STREAKS:
        self.state[key] = 0
    self.save_state()
```

`--once` is excluded on purpose: a cron-driven feeder run one cycle at a time needs the persisted
count to be the mechanism that lets in-a-row failures accumulate across separate process
invocations; resetting there would make its thresholds unreachable. The three counters are also
unified behind one helper, `strike()` (`feeder.py:491-497`), which increments, saves state, and
returns whether the count now exceeds its threshold, replacing three separate hand-written copies
of that logic that had disagreed on `>` versus `>=` and on which exit paths reset them.

## Why This Works

The root cause is a single boolean flag standing in for what is actually a discriminated union
of causes. "Nothing appended, nothing unsettled" was read as if it meant one thing (empty
queue), but it is the union of at least three independent conditions: a genuinely empty board, a
ready source that failed to read (itself split into transient versus permanently unconfigured),
and a board whose ready work was all refused by routing. Each of these three wants a different
response (leave quietly, wait and retry then escalate, stop immediately and name the config gap
before even cycling, stop and name the refused cards), so collapsing them into one flag early
throws away the information needed to choose correctly later. The fix's general shape: keep the
causes distinguishable all the way through to where the response is chosen.
`ready_source_problem()` catches the permanent-gap case before a cycle can even start, and
`fresh_ids` is threaded from `select()` through `cycle()` into `idle()` so the caller (`idle()`)
has enough information to reconstruct which of the three states it is actually in, rather than
being handed a single collapsed "empty" signal.

This is also why the ambiguity survived one full live run (session history, above) without
becoming a fix: a person watching the log can tell the cases apart in the moment ("that `0
ready` is not an empty board"), but the code's own control flow could not, because the signal it
branched on had already thrown that distinction away by the time `idle()` saw it.

## Prevention

1. When a boolean or "nothing happened" signal can arise from more than one root cause, and those
   causes want different responses, keep the causes distinguishable through to the point where
   the response is chosen instead of collapsing them into one flag early. `ready_source_problem()`
   and the `fresh_ids` parameter on `idle()` are the concrete pattern here: pass through enough
   information for the decision point to reconstruct which case it is in, rather than reducing
   everything to `readable: bool` or `appended: bool` before the decision is made.
2. A fake or stub used in tests can structurally prevent a class of bug from ever surfacing in the
   suite. Here, the fake adapter and fake `append()` always return clean, easily-distinguished
   results, so no unit test could reproduce the ambiguity a real `gh issue list` 502 or a real
   `validate()` model mismatch produces, and a live run against a real board (session history)
   surfaced the same ambiguity but did not turn it into a fix either, because nothing forced the
   diagnosis back into the code. When a dependency is faked for good reason (keeping the suite
   fast and offline, per this repo's `Deps` pattern), that is a signal to lean on a `/code-review`
   (or equivalent adversarial review) pass before merging, rather than trying to build a more
   elaborate fake that reproduces every real-world ambiguity, or trusting that a live run alone
   will close the loop.
3. The regression guard going forward is six new cases in `tests/test_feeder.py`:
   `test_ready_cards_that_validate_refused_are_not_an_empty_queue`,
   `test_a_ready_source_that_is_not_configured_is_refused_before_a_cycle`,
   `test_a_feeder_that_leaves_on_the_stop_file_hands_no_partial_count_to_the_next`,
   `test_once_keeps_the_streak_counts_between_cycles`,
   `test_an_empty_queue_ends_the_feeder_at_once_by_default`, and
   `test_idle_waits_max_keeps_an_idle_feeder_waiting_that_many_times`.

## Update (2026-09-27, issue #41): a fourth cause joined the union

The feeder never ran the R41 path scan (`brief.scan`, the check that refuses a card whose text
names a path under `.claude/` because an unattended edit there is refused whatever the allowlist
says) over ready cards before appending them, so a card the runner would skip at launch still
held a batch slot and read as offered in `feed --dry-run`. The fix runs the same scan at append
time (`scanned_ids()`, `skills/relay/scripts/relay/feeder.py`) and keeps a scan-refused card out
of the batch.

The first pass at that fix reintroduced exactly this file's own root cause. `idle()`'s
`fresh_ids` argument was still built from the unfiltered `fresh` list, so a cycle where the only
ready, unlisted card was scan-refused (never routed to a model, never given to `validate()` at
all) still hit the `if fresh_ids:` branch and stopped with "every ready card was refused with the
model it is routed to... Change the routing and start the feeder again." That message is
accurate only for the pre-existing cause it was written for; a card the scan refuses needs a
reworded card, not a routing change, and the message pointed the operator at the wrong repair.
`/code-review` caught it before merge, the same way it caught the original three-way conflation.

The fix follows this file's own prevention rule: `idle()` now receives only the fresh ids the
scan did not refuse (`[card["id"] for card in fresh if card["id"] not in scanned]`), so a
scan-refused card falls through to the ordinary empty-queue path instead of the all-refused one,
and the two causes stay distinguishable at the point where `idle()` chooses its response. Two new
tests guard it: `test_a_card_the_path_scan_refuses_holds_no_room_and_is_logged` and
`test_a_scan_refused_card_alone_reads_as_empty_queue_scanned_not_all_refused` (renamed by issue
#58 below, when the case it names stopped reading as a plain empty queue)
(`tests/test_feeder.py`).

The lesson holds beyond this one case: adding a new branch to a discriminated union that
`idle()` reconstructs from a single list argument is easy to get wrong by construction, because
the list's origin (`fresh`, all ready-unlisted cards) does not itself carry which sub-cause each
id belongs to. The next addition to this decision point should build its filtered id list from
the same per-card verdict dict the other branches already use (`scanned` here), not re-derive it
inline at the call site.

## Update (2026-09-27, issue #58): the same scan-refused card stayed silent across process starts

The 2026-09-27 fix above (issue #41) kept a scan-refused card out of the all-refused branch and
let it fall through to the plain empty-queue path, on purpose: a card the scan refuses needs a
reword, not a routing change. What it left unexamined is what the *state file* did with that
card across more than one cycle, and across more than one feeder process.

`report_once` (`feeder.py`) logs and notifies a message the first time, then remembers it in
`self.state["reported"]`, keyed `scan_skip:<id>`, so the same cycle does not renotify every
ninety minutes for a day. That table is loaded from the state file at every feeder start and
never cleared there, unlike the three streak counters (`STREAKS`), which are. So a card scanned
out in one feeder's life stayed silently "already reported" for the life of the state file: a
second feeder start, or a `--once` invocation from a cron line, read the identical message,
found it already in `reported`, and said nothing. An operator who missed the first process's
line, or who runs the board through a scheduled `--once` rather than a long lived process, never
saw it again. The same table also never cleared a card's key when the card stopped scanning
dirty, so a card reworded clean and later naming a `.claude/` path again read as already
reported too, for the same reason.

Separately, `idle()`'s empty-queue branch (the one issue #41 routed scan-refused cards into) had
no way to say *why* the queue was empty. A watcher reading only the `leaving` event's `reason`,
`empty_queue`, could not tell a genuinely empty board from one still holding a card the scan
refuses, which is exactly the ambiguity this file's root cause section describes, one level
further down the same decision point: `idle()` had regained a fourth cause with no way to
express it in its own exit.

**The fix.** Three changes, all in `feeder.py`:

1. `Feeder.run()` clears every `scan_skip:` key from `self.state["reported"]` at the start of
   every process, unconditionally, not gated by `not self.once` the way `STREAKS` is: a `--once`
   cron start is as much a start as a long lived process is, and the mechanism this file
   documents names that case explicitly.
2. The per-cycle loop that calls `report_once` for a scanned card now also pops
   `scan_skip:<id>` from `state["reported"]` the cycle a card scans clean, the same rule
   `queue_retry` already applies to a retry that blocks again (`state["reported"].pop("blocked:"
   + id, None)`, itself commented "a retry that blocks again is news, even when its sentence is
   the one sent last time").
3. `idle()` gained a `scanned_ids` parameter, threaded from the same `scanned` dict `fresh_ids`
   is already filtered against, so it can tell the fourth cause apart from a true empty queue at
   the point where the exit is chosen, rather than collapsing both into one flag. When the queue
   is only held back by scanned cards, the leaving reason is `empty_queue_scanned`, naming the
   cards, not the plain `empty_queue` a truly empty board leaves with. The exit code stays 0:
   issue #41 already decided a scan refusal is not the same urgency as `all_refused`, and this
   fix does not revisit that, only the reason word a watcher reads afterward.

**Why this is the same lesson, not a new one.** This file's own prevention section already says
it: "the next addition to this decision point should build its filtered id list from the same
per-card verdict dict the other branches already use, not re-derive it inline at the call site."
`scanned_ids` follows that rule directly, built from the same `scanned` dict `fresh_ids` already
filters against. What issue #58 adds beyond that is that a discriminated union does not end at
the exit code; the *reason word* a watcher reads off the `leaving` event is itself a case the
code has to keep distinguishable, and a persisted "already told you" table is its own version of
the same root cause: a boolean-shaped memory (`reported`) standing in for a question that
actually has a lifetime attached to it, a process's, not a state file's.

## Related Issues

- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  is a see-also, not a duplicate: no dimension of the problem, root cause, files, or solution
  overlaps, but its prevention section gestures at the same meta-level shape as this one, a
  single check standing in for more than one underlying case, surfaced only once something
  adversarial (a live run there, a `/code-review` here) exercised it in a shape a same-author
  fixture or a first-pass author never tried.
- GitHub issue #30 on `philgutowski/native-relay` is adjacent in area (the feeder refusing a
  manifest before its first cycle over a missing table) but is a different mechanism: validate,
  status, and lease preflight, not `idle()`'s post-cycle exit reasoning documented here.

---
title: A pass judged its filed cards against a ready read taken before the pre cycle hook derived their labels
date: 2026-09-28
category: logic-errors
module: runner
problem_type: logic_error
component: feeder
severity: medium
root_cause: wrong_ordering
resolution_type: code_fix
related_components: [testloop, testpass]
symptoms:
  - "on a board whose pre_cycle hook derives ready labels, every start tour notified its own cards as not returned by the ready source"
  - "a drain tour filed cards, read them unready in the same breath, and the feeder left on empty_queue with them unbuilt"
  - "a drain tour withheld for a held loop model left on empty_queue with no wait and no notice"
  - "a check whose Test process timed out dropped its landed cards, leaving them to a later tour one generation too early"
tags: [test-loop, pre-cycle-hook, ready-source, pass-points, model-held, carried-check, issue-116]
---

# A pass judged its filed cards against a ready read taken before the pre cycle hook derived their labels

## Problem

`record_pass` read the ready source for the cards each pass filed, to decide whether the drain
tour should go round and to notify cards the source does not return. The feeder's pass points
sit on both sides of the `pre_cycle` hook. The start tour runs before the hook, so that the hook
derives labels for what the tour filed (KTD9). The drain tour runs in `idle`, after this
cycle's hook has already run. Either way, no hook ran between the filing and the read. On a
board whose ready labels are derived, every new card read as unready. The start tour then sent
a false notice, and the drain tour left on the empty queue with its cards unbuilt.

## Fix

A pass no longer reads the ready source at all. Its new unattended cards go into
`test_loop.awaiting_ready`. The cycle judges them in its own ready read, which always follows
its `pre_cycle` hook, and it judges only when that read is readable and the hook ran clean. Any
other read is no evidence, so the list waits. A drain tour that filed any unattended card goes
round without resetting the idle count. The next cycle's read then either builds the card or
sends the notice, and the existing "a tour already ran since the last run" guard makes the
feeder leave without a second tour.

The same review fixed two neighbouring pass point defects:

- `test_pass` returns None for three reasons. The clock and the budget both stop the loop
  first, so a None from a loop that is still running means the loop model is held.
  `drain_tour` turns that into a `Pending` wait under `model_held`, computed by the same
  `held_wait` helper the cycle uses for a held card.
- A `failed` check now carries its cards, but only once each, through `test_loop.retried`.
  Only an exit 1 with no pass record counts as a refusal, the shape `relay test` gives a card
  it cannot read. A verb that was killed or crashed also leaves no record, but it has a
  different exit code and is carried.

## What to remember

- Any reading of "is this card ready" has to happen after the cycle's `pre_cycle` hook, or it
  is not the reading the build will make. The hook is the operator's derivation step, and a
  read taken on the wrong side of it looks right on every board that does not derive labels,
  which is every board the suite had until this fix.
- "No record" is not one outcome. `read_pass_output` gives `record_path` None for a
  refusal, a lease refusal, a signal, and a crash alike, so tell them apart by exit code.
- Left open: a refusal drops every card in the check, even when its sentence names only one of
  them. Parsing the card id out of the sentence would be brittle, so it was left for its own
  issue.

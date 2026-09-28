---
title: Feeder state that a verb validates against a live document wedges every pass when the document changes
date: 2026-09-28
category: logic-errors
module: feeder
problem_type: logic_error
component: feeder
severity: medium
root_cause: missing_precondition
resolution_type: code_fix
related_components: [testpass, testbrief, testloop, cli]
symptoms:
  - "an area at the patch cap is kept in the state file's test_loop.stopped_areas and handed to relay test as --stopped-area at every pass for the life of the loop"
  - "relay test refuses, with exit 1, any --stopped-area or --plan-area that is not a heading of the tour document as the checkout holds it now"
  - "an operator who renames or removes that heading would see every later pass fail, notified once, with no recovery short of the loop's clock cap or a hand edit of the state file"
tags: [browser-test-loop, stopped-areas, tour-document, state-file, verb-refusal, seam]
---

# Feeder state that a verb validates against a live document wedges every pass when the document changes

## Problem

The Feeder runs the browser test loop by launching `relay test` as a subprocess for each pass
(browser test loop plan, U6, KTD2). The loop's stopped areas are Feeder state. They are
written once, when an area reaches the patch cap, and passed on the command line at every
later pass. The verb checks each name against the headings of the tour document as it reads
it at that moment (`testpass._area_problem`), and refuses the whole pass with exit 1 when one
is missing. The Feeder reads exit 1 as a failed pass. So a heading the operator renamed after
the area stopped fails every pass after it. It is notified once, it counts as no round, and it
lasts until the 24 hour clock cap.

The code review of U6 found this. Nothing in the Feeder's code shows that the verb validates
these arguments against a file that can change beneath the state that feeds them.

## Cause

Two owners of one fact, with no rule between them. The Feeder owns which areas are stopped,
and the tour document owns which areas exist. The verb's refusal is correct for a hand run,
where a typo should be refused. For a caller that persists the names, the same refusal turns a
stale name into a permanent failure.

Removing the name from the state file does not fix it. `testloop.area_patches` recounts from
the filed map and the checks on every pass, so the old name reaches the cap again and is added
back.

## Fix

Before each pass, `Feeder.test_pass` reads the tour document's headings from the checkout
through `testbrief.headings`, the same reader the verb uses. It filters what it passes, not
what it stores. A name that is no longer a heading is left off the command line and reported
once, and that area is tested again under its new name. When the document cannot be read, the
Feeder passes everything and lets the verb refuse with its own sentence.

## Next time

Any argument the Feeder persists and hands to `relay test` (or any verb that validates against
a document in the target repository) has to be filtered against that document at call time.
The verb cannot do this for it, because the verb cannot tell a stale name from a typo. Look for
the same shape wherever state outlives the file it names: a card id the tracker has since
deleted is the other one here. A check that failed does not carry its cards forward for that
reason, since `relay test` refuses a card it cannot read.

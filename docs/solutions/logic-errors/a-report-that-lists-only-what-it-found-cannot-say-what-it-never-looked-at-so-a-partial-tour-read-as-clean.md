---
title: A report that lists only what it found cannot say what it never looked at, so a partial tour read as clean
date: 2026-09-28
category: logic-errors
module: runner
problem_type: logic_error
component: testloop
severity: high
root_cause: missing_coverage_field
resolution_type: code_fix
related_components: [testbrief, testpass, feeder, brief-test]
symptoms:
  - "the first live report only tour reached 3 of the tour document's 10 areas behind a sign in, said so in prose and in its reason, and reported ran with two lows"
  - "nothing in the report contract, the pass record, or the findings file named the seven areas it never saw"
  - "testloop.should_stop would have read the tour as clean and stopped the loop under a Feeder"
tags: [test-loop, report-contract, coverage, untoured, stop-rule, clean-stop, fail-open, issue-121, first-live-run]
---

# A report that lists only what it found cannot say what it never looked at, so a partial tour read as clean

## Problem

The first live report only tour of a real web app (2026-09-28, checking U1 and U3 of the
browser test loop plan) met a sign in wall in front of most of the app. The Test process did
the honest thing with the contract it had: it toured the three areas it could reach, reported
`ran`, wrote two low findings, and explained in prose and in the report's `reason` that seven
areas were behind the sign in. The pass recorded `ran`. `testloop.should_stop` reads a tour
that ran with no high or medium finding as the clean stop. Under a Feeder that drain tour
would have ended the loop with "a full tour found nothing above low" about an app it had
barely seen.

## Cause

The report contract carried findings and a free text reason, and nothing that said what the
process had not looked at. Every consumer after the parser read absence of findings as
absence of defects: the stop rule, the pass record, the findings file, the Feeder's notice.
The `not_run` status covered the case where nothing could be reached, and the brief told the
process to use it when the app asked for a sign in, but a process that can reach some areas is
right to report what it found, and the contract gave it no way to bound that report. The
reason field was the only carrier, and code reads nothing from a reason.

The general shape: a report of positives is not a report of coverage. A consumer that turns
"found nothing" into "clean" needs a field that says where the producer looked, checked by
code, or a partial look reads as a full one every time.

## Solution

Issue #121, on `relay/121`.

- The report gains `untoured`, an array of area names, and the brief tells the process to
  test what it can reach, list every area it could not reach under that key by its heading,
  say why in `reason`, and keep `not_run` for the case where no area can be reached.
- `testpass` checks every name against the tour document's headings through the one rule a
  stopped area is checked by at render (`testbrief.check_areas`). A name that is not a
  heading fails the pass rather than being dropped: an invalid finding is dropped because
  filing nothing is safe, and an untoured area cannot be dropped because that reads the tour
  as more complete than the process said. A stopped area on the list is left off after the
  check, since the process was told to skip it. A report that names every area there was to
  reach is recorded `not_run`. The document's title, the first heading when it is the only one
  of its level, is not an area a report has to cover, because no process lists a title.
- `testloop.PassResult` carries `untoured`, and `should_stop` never answers clean for a tour
  with one. It stops on open findings only when the tour had a serious finding, so a partial
  tour with only lows and nothing filed goes on, and the Feeder notifies once per pass kind
  naming the areas until a pass of that kind reaches them all. The notice carries the areas
  and not the reason, which a process rewords each pass.
- The Feeder reads a record whose list is not one of strings as a failed pass, before the
  round is counted, the way it reads an unknown status.

## Prevention

- When a rule turns "found nothing" into a decision, ask what the producer would say if it
  could not look. If the answer is "nothing the code reads", the contract is missing a
  coverage field, and the rule fails open.
- A field a consumer drops on a bad value is fail open when the field's absence means "all
  is well". Refuse the report instead, the way a malformed block is refused.
- Two categories in one document, here the title and the areas under it, need the code to
  name which one a rule is about. `testbrief.headings` is every heading and `testbrief.areas`
  is the ones a report has to cover; the docs say which each rule reads.
- This came from a live run, not the suite: the stub reports whatever the test writes, and no
  fixture had ever reported a partial tour. The live proof unit, U9 (#110), owes the new key
  a real tour against a throwaway target, since a template change is a contract change
  between processes and the stub agrees with it by construction.

## Related

- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  is the rule this is one more instance of: a contract between two processes is checked by
  nothing until a real producer runs.
- `docs/solutions/logic-errors/a-transcript-present-guard-in-the-test-pass-bypassed-the-normalizers-stdout-log-fallback.md`
  is the other defect the same live tour found, one step earlier in the pass.
- `docs/solutions/logic-errors/feeder-state-that-a-verb-validates-against-a-live-document-wedges-every-pass-when-the-document-changes.md`
  is the stopped area seam this change shares its heading check with.

---
title: Stale Cards, the Return and the Audit - Plan
type: feat
date: 2026-09-08
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: conversation 2026-09-08
execution: code
---

# Stale Cards, the Return and the Audit - Plan

## Goal Capsule

- **Objective:** a project board never shows a card as in progress when no process is working
  on it, and an operator learns, from the run summary or one verb, which cards disagree with
  git and state after a run.
- **Means:** the Closeout for a blocked or halted Task returns the card to the status it read
  before the run (KTD1), the Runner confirms the return and attaches a finding when it did not
  happen (KTD2), and a read only audit at run end and on demand names every card that disagrees
  with the record and with git (KTD3).
- **Authority:** the R-IDs win on behavior, the KTDs on mechanism.
- **Execution profile:** four units, one commit, the suite plus one live run the suite cannot
  perform, because the Closeout brief is a contract between processes.
- **Stop conditions:** stop if the return needs the Runner to write to the tracker (it must
  not), or if the audit needs the Lease (it must not).

## Product Contract

### Problem Frame

The Task brief's first step moves the card to `tracker.in_review_status` so the board shows it
as in progress the moment the process launches (the 2026-08-31 plan, R1 there). The Closeout
instructions for a blocked outcome, on both the github and jira adapters, still say "do not
transition the card: a blocked task keeps its current status so the board still shows it as
open", and the halted outcome's say the same. Those sentences were written when the current
status was still todo. Since the early move, the current status is in progress, so every
blocked, halted, timed out, or crashed Task leaves a card reading in progress with nobody on
it. That plan named the case as an accepted residual for one path and did not decide against
moving a card back. This plan reverses "do not transition" for blocked and halted, with the
reason above.

The Runner never writes to a tracker (R19), so the return goes through the Closeout, the
existing write path, and the Runner reads the card back afterwards, the same shape
`closeout.confirm_blocked_comment` already has.

### Requirements

- R1. A Closeout for a blocked or halted outcome instructs the process to return the card to
  the status the record carries as `baseline_tracker_status`, then to comment as it does today.
- R2. No return is instructed when the baseline status is unknown, when it equals the in review
  status (the operator placed it there before the run), or when the record already carries a
  landing reference (the halt came after a landing, and the card was closed by the landed
  Closeout).
- R3. The markdown adapter, which has no in review status, instructs no return.
- R4. After a Closeout that was told to return the card, the Runner reads the card's status. A
  card still reading the in review status attaches a `card_left_in_review` finding to the
  record; the summary lists it as a check by hand. Never a halt.
- R5. At the end of every run that reaches a terminal record other than crashed, the Runner
  audits every Manifest Task's card against its record and git, and writes the findings to the
  state file under `audit`. A failure inside the audit is one finding, never a halt.
- R6. The audit names four disagreements: a card at the in review status whose record is not in
  flight (`card_stale_in_review`), a landed record whose card is not terminal
  (`card_reopened`), a terminal card whose record has not landed and for which no commit on
  the default branch since the record's baseline names the Task (`card_closed_unlanded`), and
  a card that could not be read (`card_unreadable`).
- R7. `relay audit <manifest>` performs the same audit on demand, prints it, takes no Lease,
  and writes nothing, so it is safe beside a live run. Under a live run a record in flight is
  not stale.
- R8. `relay summary` carries the last audit under `audit` and lists each finding under check
  by hand. `relay status` prints the last audit's count and lines.
- R9. The terminal phase event's line names the number of stale cards when there are any, so
  the one notification that ends a run says the board needs a hand.

### Success Criteria

- A blocked Task under the github or jira adapter ends with its card back at its pre run status.
- A run whose Task halted at pre flight, where no Closeout runs, ends with the summary naming
  that card as stale in review and the status to move it to.

### Scope Boundaries

- The Runner writes nothing to any tracker. The audit repairs nothing; it reports.
- No new Halt class. Every outcome here is a finding.
- Cards nobody has touched for a long time are a different question and not this plan.

## Key Technical Decisions

- **KTD1. The return rides on the existing Closeout instruction seam.** `closeout_instructions`
  gains an optional `return_to` keyword; the interface stays eight methods. Each adapter renders
  its own move sentence, so the brief and the adapter cannot disagree about how a card moves.
- **KTD2. The confirmation is a read after the Closeout, in the Runner.** Same shape as
  `confirm_blocked_comment`: a finding on the record, rendered through `HALT_LINES`, so it
  joins `FINDING_CLASSES` and `LINE_CLASSES` and the summary's table test covers it.
- **KTD3. The audit is its own module with a `build` that returns data and a `lines` that
  renders it,** like `progress` and `summary`. It reads the adapter, the store, and git. Its
  sentences are built there rather than through `HALT_LINES`, because they belong to the run
  rather than to a record, and `summary` copies them into pending checks as they are.
- **KTD4. The run end audit writes under the Lease; the verb does not write.** The state file is
  the Runner's, and a reader that wrote it beside a live run would race the Runner.

## Units

- U1. `contracts`, `state`: the finding class, its line, the `audit` block and its writer.
- U2. Adapters and `closeout`: `return_to`, the three adapters' sentences, `confirm_card_returned`.
- U3. `audit` module, `run.py` wiring (return on the blocked and halted routes, the run end
  audit, the terminal line), `summary`, `cli` (`audit` verb, `status`).
- U4. Docs: `SKILL.md`, `README.md`, `CONCEPTS.md`.

## Verification

The suite, then one live run against a target with a real board: the Closeout brief changed, so
by this repo's rule a stub that agrees by construction is not evidence that a real process
returns the card.

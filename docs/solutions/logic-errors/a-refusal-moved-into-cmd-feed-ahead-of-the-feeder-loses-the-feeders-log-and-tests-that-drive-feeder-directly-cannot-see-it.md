---
title: A refusal moved into cmd_feed ahead of the Feeder loses the Feeder's log, and tests that drive Feeder directly cannot see it
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: medium
root_cause: missing_side_effect
resolution_type: code_fix
related_components: [cli, feeder]
symptoms:
  - "issue #53 added a post cycle hold that refuses every feeder start, checked both in cmd_feed and at the top of Feeder.cycle()"
  - "the docs said a cron --once meeting the hold logs each refusal, and a test asserted two refusal lines in the feeder log"
  - "/code-review found the cron path never logged: cmd_feed refused before any Feeder existed and wrote only to stdout, while the passing test built Feeder directly and so only exercised the in-loop check"
tags: [feeder, cmd-feed, precheck, post-cycle-hold, feeder-log, test-seam, code-review-catch]
---

# A refusal moved into cmd_feed ahead of the Feeder loses the Feeder's log, and tests that drive Feeder directly cannot see it

## Problem

A start refusal that must act before `--detach`, `--pin`, or `--restart` has to live in
`cli.cmd_feed`. Anything later is too late. A detached child refuses where nobody reads it, and
a restart has already asked the live feeder to leave. Issue #46's `--retry-blocked` check and
issue #53's post cycle hold both sit there for that reason.

`cmd_feed` runs before any `Feeder` exists, though. Everything a `Feeder` does as a side effect
of stopping is missing there: `Feeder.log` writing `<stem>.feeder.log`, `emit` writing the
`leaving` event, and `stop` notifying. The hold's precheck wrote its refusal to stdout only. For
a cron line running `--once`, stdout is discarded or mailed, so the documented "logs each
refusal" never happened on the one path that was supposed to produce it.

The suite did not catch it. The test that counted refusal lines in the log called
`feeder.Feeder(...).run()` directly, which skips `cmd_feed` and reaches the in-loop check
instead. The only real start that reaches that check is a `--restart` handover.

## Fix

The precheck now appends its refusal to the feeder log itself, through the shared
`feeder.log_line` and `feeder.append_log` helpers. It skips this under `--dry-run`, which writes
nothing. The verb tests (`PostCycle.call`, which goes through `cli.cmd_feed`) count the log
lines. It still writes no `leaving` event, on purpose: no feeder started, and `feed --status`
reads the hold from the state file rather than from the last event.

## Rule

When a feeder check is duplicated into `cmd_feed` so it can run earlier, decide explicitly which
of the Feeder's stop side effects (log, event, notification) the early path needs, and write
each one there. Test it through `cli.cmd_feed`, not through `Feeder`, because a test that builds
`Feeder` directly proves only the late copy.

Related: `feed-retry-blocked-precheck-skipped-manifest-loads-toml-validation.md`, the same
precheck seam, where the early copy skipped a validation the late copy inherited.

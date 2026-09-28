---
title: A transcript_present guard in the Test pass bypassed the normalizer's stdout log fallback, so a valid report was recorded as no transcript
date: 2026-09-28
category: logic-errors
module: runner
problem_type: logic_error
component: testpass
severity: medium
root_cause: duplicated_guard
resolution_type: code_fix
related_components: [testbrief, filing, backends, classify, launch, feeder]
symptoms:
  - "the first live report only tour ended its final message with a valid relay-test-report block and the pass record read failed with the test process left no transcript to read"
  - "no findings file was written for a pass whose stdout log held the whole report"
  - "the transcript sat under the projects folder of the directory CLAUDE_CONFIG_DIR named, not the default one under HOME, so both the predicted path and the glob missed it"
tags: [test-pass, transcript, stdout-log, fallback, claude-config-dir, evidence-opened, duplicated-guard, issue-113]
---

# A transcript_present guard in the Test pass bypassed the normalizer's stdout log fallback, so a valid report was recorded as no transcript

## Problem

The first live Test pass of the browser test loop (a report only tour of a real web app,
2026-09-28, after U5 landed) ran to completion and ended with a valid `relay-test-report`
block. The pass record said `failed` with "the test process left no transcript to read", and
no findings file was written.

The CLI ran with `CLAUDE_CONFIG_DIR` set, so it wrote its transcript under that directory's
projects folder. `launch.find_transcript` predicts the path under the default config directory
in `HOME` and globs the same place, so both missed it and `transcript_present` came back False.

## Cause

Two guards on one fact, in two places, with different answers.

The claude backend's `normalize_transcript` (`backends/claude.py`) already survives exactly
this: when the transcript does not open it reads the run's own stdout log, which under
`--output-format stream-json` holds the same `assistant` records, and names the log in
`Evidence.source`. That fallback was added after the IW run of 2026-09-20 stranded three tasks
the same way. The Runner's Task path and `classify` go through it.

`testpass._pass` checked `launched.transcript_present` before calling `testbrief.parse`, and
returned the failure on False. The parse it was guarding would have read the log. The guard
was written as a defensive check on the launcher's answer, and the launcher's answer is a
prediction, not an observation of what the process said.

## Fix

- The guard is gone. `testbrief.read_final_message` reads through the normalizer and returns a
  `FinalMessage` with the text, the `source` it came from, and whether the transcript opened.
  `Report` and `Filed` carry `source`. The pass fails with "no transcript to read" only when
  `source` is None, which is when neither file held an assistant record.
- The pass record gains `read_from` beside `transcripts`. `transcripts` stays the launcher's
  prediction; `read_from` is the file each block was actually read from, so the two differ
  exactly when the fallback fired. The feeder's `test_pass` event carries both.
- The failure sentence says what was read. A transcript that opened is the process's own
  file and the normalizer reads nothing past it, so the sentence names the transcript then,
  and names the log only when the transcript was not there to open.
- `filing.parse` reads the same way and names its source, and the `elif not
  launch_result.transcript_path` branch in `filing.run` is gone: `transcript_path` is always
  the prediction, so it never fired.

## What to know next time

- **`Evidence.opened` describes the file the lines came from, not the handed path.** On a
  fallback it is whether the log opened, and `source` names the log. `readable()` therefore
  reports a fallback as present, which is what `classify` wants. A reader that needs "did the
  transcript itself open" must compute `opened and not source`. The first draft of the
  `transcript_opened` flag got this wrong and a test caught it.
- **Never guard a parse on `transcript_present`.** It is the launcher's prediction. The
  normalizer is the one place that decides where the evidence is, and every reader of a
  process's final message goes through it: `classify`, `testbrief.read_final_message`, and
  `filing.parse`. A second guard in front of any of them recreates this defect.
- **A subagent line is marked differently in the two files.** The transcript marks it
  `isSidechain`; the stdout log marks it `parent_tool_use_id`, as
  `tests/fixtures/stdout/_make.py` records. `read_final_message` skips both. `classify`'s own
  last message read still skips only `isSidechain`, which is issue #114.
- **The stub stands in for this with a `stream` entry and no `fixture`.** `tests/test_testpass.py`
  `ReadFromTheLog` queues an entry that writes no transcript and echoes the transcript lines
  to stdout. That agrees with the real CLI by construction, the way
  `stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md` warns.
  The Task path has read the log live since 2026-09-20, and the Test pass reads the same file
  with the same reader, but the next live Test pass is still the proof for this seam.

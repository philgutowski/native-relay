---
title: Feeder liveness is the manifest's own lock, not a process match or a pid and hostname
date: 2026-09-27
category: workflow-issues
module: feeder
problem_type: workflow_issue
component: feeder
severity: high
root_cause: missing_workflow_step
resolution_type: code_fix
related_components: [feeder, cli, status]
applies_when:
  - "a watcher, a script, or a person needs to know whether one manifest's feeder is running"
  - "two or more feeders run on one machine for different boards"
  - "code decides liveness from a recorded pid or hostname"
symptoms:
  - "pgrep -f 'relay_cli.py feed' reports a feeder alive while this board's feeder has left on its stop file"
  - "a board sits idle for hours with nothing appended and nobody told"
  - "feed --status reads a live feeder as unknown after a laptop changed networks"
tags: [feeder, liveness, flock, pgrep, hostname, status, events, follow, issue-36]
---

# Feeder liveness is the manifest's own lock, not a process match or a pid and hostname

## Context

Issue #36. A watcher checked a feeder with `pgrep -f "relay_cli.py feed"`. That pattern
matched a second board's feeder, so it reported alive while the first board's feeder had left
on its stop file, and that board sat idle for about five and a half hours. `status <manifest>`
could not help: it reports the run lease, the current batch's `relay run`, not the feeder
around it.

## Guidance

Ask `feed <manifest> --status` (or `--status --json`, key `running`). Never ask the process
table. Watchers that need each cycle follow `feed <manifest> --follow`, JSON lines from
`<stem>.feeder.events.jsonl`, and key on the `event` and `reason` words, not on log sentences.

Inside the code, `feeder.liveness` decides from the lock first:

1. Each manifest has its own `<stem>.feeder.lock`, and only a live feeder holds its `flock`.
   The kernel drops it however the holder dies. That is the one signal that is both per
   manifest and immune to a stale record.
2. The recorded pid only says which process holds it. A pid alone lies after a kill and a
   recycle; `pid_alive` is true for whatever process got the number next.
3. The hostname is not a gate. The first build returned "unknown" whenever the recorded
   hostname differed from `socket.gethostname()`. On macOS the hostname follows the network,
   so a live feeder read as unknown after a Wi-Fi change, and `--follow`, treating unknown as
   alive, never ended over a killed one. Review caught it; the lock answers regardless.

Two seams that are not visible from one file:

- `lock_held` probes with a shared `flock` and drops it. A feeder starting in that instant
  would meet the probe and exit 3 as if another feeder held the manifest, so `acquire_lock`
  retries `LOCK_ATTEMPTS` times, `LOCK_RETRY_SECONDS` apart. Remove the retry and every
  `status` or `--follow` poll becomes a small chance of refusing a real start.
- `--restart` and `--pin` hand over through the same stop file as `--stop`. `wait_for_lock`
  writes `restart` into it, and the leaving feeder reports the reason `restart`, not
  `stop_file`. `--follow` goes on past a `restart` leave and allows twice
  `RESTART_POLL_SECONDS` for the new feeder, because the new one only polls every twenty
  seconds. Without both, a watcher reads every handover as the board abandoned.

## Why this matters

The failure it prevents is silent: an idle board and a watcher that says all is well. Any new
liveness check that is not keyed on this manifest's lock reintroduces it.

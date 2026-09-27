---
title: feed --dry-run --detach started a real feeder, because _detach_feeder built the child's argv from an explicit flag list that never named --dry-run
date: 2026-09-27
category: logic-errors
module: cli
problem_type: logic_error
component: runner
severity: high
root_cause: incomplete_argv_forwarding
resolution_type: code_fix
related_components: [cli, feeder]
symptoms:
  - "feed <manifest> --dry-run --detach started a real detached feeder and opened the manifest's .feeder.out for append, instead of reading one cycle and leaving"
  - "feed <manifest> --pin --dry-run --detach was worse: the pin branch printed 'would pin: ...' and then fell through to the same detach call, starting a real feeder from the checkout that --pin exists to avoid"
  - "cmd_feed checked args.detach and returned _detach_feeder(...) before it ever reached the if args.dry_run: gate a few lines below, so the dry run path was unreachable whenever --detach was also set"
tags: [cli, feeder, dry-run, detach, argv-forwarding, flag-drop, code-review-catch]
---

# feed --dry-run --detach started a real feeder, because _detach_feeder built the child's argv from an explicit flag list that never named --dry-run

## Problem

`_detach_feeder` (`skills/relay/scripts/relay/cli.py`) starts the same `feed` command in its own
session, and builds the child's argv by hand:

```python
command = [sys.executable, "-u", entry, "feed", paths.manifest]
command += [flag for flag, on in (("--once", args.once), ("--restart", args.restart),
                                  ("--notify", args.notify)) if on]
```

`--dry-run` is not in that tuple. `cmd_feed` reaches `if args.detach: return _detach_feeder(...)`
before it ever reaches `if args.dry_run: return loop.run()` a few lines later, so a dry run with
`--detach` never took the dry run path itself. It built a child command that quietly dropped
`--dry-run`, then detached that child, so the child ran a real feeder while the parent's own exit
looked identical to a normal detach.

## Symptoms

- `feed <manifest> --dry-run --detach` printed `feeder detached: pid ...` exactly like a real
  detach, because `_detach_feeder`'s own output does not depend on which flags it forwarded.
- The detached child opened `paths.out` for append and ran a live cycle: real cards got appended
  to the manifest and a real task process launched, with nothing in the parent's output saying so.
- `feed <manifest> --pin --dry-run --detach` was worse than the plain case: the pin branch's dry
  run arm (added for issue #48) printed `would pin: ...` and a note that "the dry run below reads
  the next cycle with this checkout's code", then fell through to the same `if args.detach:` check,
  starting a real feeder from the live checkout, the exact tree `--pin` exists to keep a cycle from
  running out of.

## What Didn't Work

Nothing was attempted before this fix; the defect was found by an independent review of a prior
merge (`10d7fe5`), not by an incident, and it predates issue #48's pin work. The pin case shows why
patching `_detach_feeder`'s argv list to add `--dry-run` would not have been enough on its own: even
carried faithfully, a detached dry run child still starts a whole new process to say something the
parent could say itself, and by the time `_detach_feeder` runs, the pin branch has already decided
whether to treat this as a real pin.

## Solution

`cmd_feed` now refuses the pair outright, before the pin branch and before the `if args.detach:`
check are reached:

```python
if args.dry_run and args.detach:
    out.write("--dry-run and --detach do not combine; a dry run never detaches\n")
    return EXIT_CONFIG
```

Placed after the `--status`/`--events`/`--follow` clash check (so a watcher's own refusal still
wins when both apply) and before every other branch that reads `args.stop`, `args.pin`, or
`args.detach`. Neither `feed --dry-run --detach` nor `feed --pin --dry-run --detach` reaches the pin
plan, `_pin_feeder`, or `_detach_feeder` any more; both are refused with the same message and touch
nothing on disk.

## Why This Works

A dry run's whole contract is "reads only, decides nothing runs." A detach's whole contract is
"start a process the caller stops watching." Those two cannot both hold: forwarding `--dry-run`
faithfully to the child only moves the same tension one process down, and a detached dry run still
has to either do nothing (in which case detaching bought nothing) or print something asynchronously
that a foreground call could have printed directly. Refusing the pair up front, the same way the
watcher clash check above it refuses a flag beside `--status`, keeps `cmd_feed` from ever having to
answer "what does a dry run mean once it can no longer be watched."

## Prevention

- Any helper that builds a child's argv from an explicit `(flag, condition)` tuple list, the same
  shape `_detach_feeder`, `_pin_feeder`, and `cmd_run`'s `detach_command` all use, drops a flag the
  moment a new one is added to the parser but not to that tuple. Adding a CLI flag to `feed` or
  `run` means grepping every such tuple list in `cli.py` for it, not just the flag's own new
  `if args.<flag>:` branch.
- `--pin`'s dry run arm (issue #48) and this fix both had to reason about what happens *after* their
  own branch prints something and falls through. A branch that prints a preview and then continues
  into shared code should be read all the way to the next return, not assumed to end where the
  preview does.
- The same function still has one open instance of this shape: `feed --dry-run --stop` reaches
  `feeder.request_stop`, which writes the stop file, without ever checking `args.dry_run`, because
  `if args.stop:` returns before the `if args.dry_run and args.detach:` guard added here even runs.
  Filed as #66 rather than folded into this fix, since it is a different branch with its own
  semantics to decide (what a dry run even means for `--stop`), not the same dropped-flag mechanism.

## Related Issues

- #48: added `--pin --dry-run`'s own preview arm; this fix's pin case is the same arm falling
  through into the defect this doc describes.
- #66: the same "a flag-triggered early return never checks `args.dry_run`" shape, found while
  reviewing this fix, left open as a separate task.

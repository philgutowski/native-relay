---
title: A new feed --retry-blocked pre-check read the manifest with manifestedit directly, skipping manifest_module.load's TOML-error handling, so a bad manifest crashed instead of refusing cleanly
date: 2026-09-27
category: logic-errors
module: feeder
problem_type: logic_error
component: runner
severity: medium
root_cause: missing_validation
resolution_type: code_fix
related_components: [cli, feeder, manifestedit, manifest]
symptoms:
  - "a new cmd_feed pre-check called feeder.listed_ids(paths), which read the manifest's raw text and passed it straight to manifestedit.task_ids"
  - "ce-code-review, run from four independent angles, all converged on the same reproduced crash: a manifest with invalid TOML plus --retry-blocked raised an uncaught manifestedit.EditError instead of returning EXIT_CONFIG with a readable message"
  - "the same malformed manifest without --retry-blocked already stopped cleanly, because that path only reaches manifestedit.task_ids after Feeder.cycle() has already called manifest_module.load and caught its ManifestError"
tags: [manifest-validation, toml-parsing, editerror-vs-manifesterror, feeder, retry-blocked, code-review-catch, unvalidated-read]
---

# A new feed --retry-blocked pre-check read the manifest with manifestedit directly, skipping manifest_module.load's TOML-error handling, so a bad manifest crashed instead of refusing cleanly

## Problem

Issue #46 asked for `feed --restart --detach --retry-blocked <id>` to check the id against the
manifest before detaching or asking a live feeder to leave, since the only existing check lived
inside `Feeder.cycle()`, which runs after both of those. The fix added `cmd_feed` a pre-check that
read the manifest's ids via a new `feeder.listed_ids(paths)`, modeled on the read `cycle()` already
does at that point: `text = self._read(self.paths.manifest); listed = manifestedit.task_ids(text)`.

That read looked safe in isolation because in `cycle()` it always runs after
`manifest_module.load(self.paths.manifest, allow_no_tasks=True)` has already succeeded a few lines
above it (`feeder.py`, around line 884), and `load` is the one place that turns a `tomllib.TOMLDecodeError`
into a caught `ManifestError`. The new pre-check had no such guard in front of it: it was the first
thing in the whole `feed` invocation to touch the manifest's TOML.

## Symptoms

- `feed <manifest> --retry-blocked 99 --once` against a manifest with a syntax error (a stray
  bracket, a mid-edit save, a bad merge) raised a bare `manifestedit.EditError` all the way out of
  `cli.main()`, which has no surrounding `try`/`except`, printing a raw Python traceback.
- The identical manifest without `--retry-blocked` stopped cleanly with `EXIT_CONFIG` and a
  `"stopping: manifest is not valid TOML: ..."` message, because that path's only read of the broken
  TOML went through `manifest_module.load` first.
- Every other check already in `cmd_feed` (manifest not found, `--json` misuse, a bad sidecar)
  degrades gracefully; this was the one new check that did not.

## What Didn't Work

- Modeling the new pre-check on `cycle()`'s read (`manifestedit.task_ids` over raw text) without
  also carrying over the `manifest_module.load` call that read is only ever reached after. Two
  reviewer passes suggested collapsing `listed_ids` into the existing `Feeder._read` static method
  instead; that would have kept the exact same gap, since `_read` has no TOML validation either, it
  is just a file read.

## Solution

`feeder.listed_ids(paths)` now calls `manifest_module.load(paths.manifest, allow_no_tasks=True)`
and returns `[task.id for task in manifest.tasks]`, the same call and the same field `cmd_run`
already uses for its own id check (`{task.id for task in manifest.tasks}`). `cmd_feed` wraps the
call in `try`/`except manifest_module.ManifestError`, printing `"%s\n" % exc` and returning
`EXIT_CONFIG`, the same pattern the existing `_load` helper already uses elsewhere in `cli.py`.
Landed in the same task as the original fix, before merge.

## Why This Works

`manifest_module.load` is the one function in this codebase that owns turning a `tomllib`
decode failure into `ManifestError`, a caught, documented exception every other manifest-reading
command path already expects. `manifestedit.task_ids`, by contrast, calls `tomllib.loads` directly
through its own `_parse` helper and raises `manifestedit.EditError`, a plain `ValueError` with no
catcher anywhere in `cli.py`. The two modules read the same file for different purposes
(`manifest_module` for a validated `Manifest` object, `manifestedit` for surgical text edits) and
were never meant to be interchangeable entry points into the same manifest.

## Prevention

- Any new manifest read added outside `Feeder.cycle()` must go through `manifest_module.load`
  first, exactly as `cycle()` does, before it reaches `manifestedit` for anything. `manifestedit`
  has no TOML-error handling of its own; it assumes the caller already validated the file.
- When modeling a new check on an existing one, carry over every guard the existing one sits
  behind, not just the specific line that looks relevant. `cycle()`'s `manifestedit.task_ids` call
  reads safely only because of a `load()` call several lines earlier in the same function; copying
  the later line without the earlier one reproduces the read but not its safety.
- A test for a new manifest-reading code path should include a manifest that fails to parse, not
  only one that parses but lacks the id in question; the two new tests this fix landed with did not
  cover that case, and it took a second review pass to add it.

## Related Issues

None yet; the first manifest-reading check added to `cmd_feed` outside the feeder's own loop.

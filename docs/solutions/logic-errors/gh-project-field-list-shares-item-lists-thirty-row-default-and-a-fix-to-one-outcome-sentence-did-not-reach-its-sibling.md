---
title: gh project field-list shares item-list's thirty row default, and the landed sentence's id fix did not reach the return_to sentence beside it
date: 2026-09-27
category: logic-errors
module: adapters
problem_type: logic_error
component: adapters
severity: medium
root_cause: missing_validation
resolution_type: code_fix
related_components: [closeout]
tags: [github-adapter, gh-cli, pagination, default-limit, closeout-instructions, prose-contract, sibling-drift]
---

# gh project field-list shares item-list's thirty row default, and the landed sentence's id fix did not reach the return_to sentence beside it

## Problem

Issue #42 asked for a narrower fix: the landed outcome's `closeout_instructions` sentence named
the wrong source for the project node id (`gh project field-list` and `gh project item-list`
return no project id; `gh project view <number> --owner <owner> --format json` does) and omitted
the owner and project number both commands need. Two things surfaced only while fixing that:

1. `gh project field-list` defaults to a thirty row page exactly like `gh project item-list`
   (`gh project field-list --help`: `-L, --limit int  Maximum number of fields to fetch (default
   30)`). `PROJECT_ITEM_LIMIT = 500` already guards the adapter's own `_items()` read
   (`github.py:94-101`, see `github-adapter-read-only-the-first-thirty-board-items...md`), but
   nothing had ever called `gh project field-list` from this codebase before, so its own default
   trap was untouched by that earlier fix and untouched by this issue's own text, which named the
   limit only for item-list.
2. `closeout_instructions` renders three near-identical sentences from three separate string
   literals: the landed sentence, and the `move` sentence used for a halted or blocked outcome
   with `return_to` set, which also calls `gh project item-edit` and therefore needs the exact
   same owner, project number, and id sourcing. Fixing the landed sentence's missing ids left the
   `move` sentence with the identical gap the issue described, one function down, because nothing
   ties the two together.

## Symptoms

- None yet observed in a real run. This was caught in code review of the fix itself, not in a
  failure: multiple independent review passes over the same diff converged on the `move` sentence
  gap without being told to look for it, which is itself a sign the drift is easy to reintroduce.

## What Didn't Work

- Fixing only the sentence the issue's text quoted. The issue named the landed branch because
  that was the branch #28's merge had just landed; the underlying defect (an id `gh project
  item-edit` needs, sourced from a command that does not return it) is a property of the `gh`
  subcommand, not of which outcome is rendering it, so it was already present in the sibling
  branch before this issue was filed.

## Solution

Factor the three id sourcing sentences (`gh project view`, `gh project field-list --limit`,
`gh project item-list --limit`) into one local string, computed once per call and interpolated by
both the landed and the `move` sentence, rather than writing the sentence into one branch:

```python
ids = (
    "Read the project id from `gh project view %s --owner %s --format json`, the "
    "field and option ids from `gh project field-list %s --owner %s --format json "
    "--limit %d`, and the item id from `gh project item-list %s --owner %s --format "
    "json --limit %d`."
    % (self._project_number, self._owner,
       self._project_number, self._owner, PROJECT_ITEM_LIMIT,
       self._project_number, self._owner, PROJECT_ITEM_LIMIT)
)
```

Both the landed sentence and the `move` sentence (used when `return_to` is set) interpolate `ids`
and separately name the owner and project number in their own prose. A test asserts the same
three commands and the same limit appear for both `("blocked", "halted")` outcomes with
`return_to` set, not just for `"landed"`.

## Why This Works

The three id sourcing commands are a fact about `gh project item-edit`'s own requirements, true
regardless of which outcome is asking for it. Giving that fact one rendering that both call sites
share means a future change to the id sourcing text, a `gh` flag rename, a new required id, only
has one place to change, and a test covering one outcome automatically proves the other has the
same text rather than merely the same shape.

## Prevention

- A `gh project` subcommand new to this codebase gets checked against `--help` for its own
  default page size before it is trusted to return everything; `gh` defaults every list-shaped
  subcommand to 30 rows, not just the one this repo happened to fix first.
- When a Closeout instruction is fixed for one outcome, grep the same file for the other
  `closeout_instructions` branches that call the same tracker write (here, every branch that
  mentions `gh project item-edit`) before calling the fix done. The three outcomes are rendered
  from one function but were, before this fix, three independent string literals with nothing
  forcing them to agree, the same shape `two-instructions-to-two-processes-written-weeks-apart...`
  documents one level up, between the Task brief and the Closeout brief rather than within the
  Closeout brief itself.

## Related Issues

- Issue #42 (fixed).
- `docs/solutions/logic-errors/github-adapter-read-only-the-first-thirty-board-items-so-later-cards-read-as-absent.md`,
  the earlier half of the same trap for `gh project item-list` in the adapter's own Python read
  path rather than in prose read by a separate headless process.
- `docs/solutions/workflow-issues/two-instructions-to-two-processes-written-weeks-apart-disagreed-about-moving-the-card-back-and-the-adapter-couldnt-see-it.md`,
  the same family of drift between sibling instruction sentences, one level up.

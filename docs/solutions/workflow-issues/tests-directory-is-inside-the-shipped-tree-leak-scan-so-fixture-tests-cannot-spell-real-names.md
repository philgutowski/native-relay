---
title: The shipped-tree leak scan covers tests/ too, so a test that checks rendered output for real names cannot spell them itself
date: 2026-09-28
category: workflow-issues
module: test_examples
problem_type: workflow_issue
component: tests
severity: low
root_cause: missing_workflow_step
resolution_type: workflow_improvement
related_components: [testbrief, contracts]
applies_when:
  - "writing a test that asserts a rendered template (a brief, an example manifest) does not leak a real project name, tracker site, or account, per R40"
  - "the fixture data the test itself constructs to prove the negative would otherwise contain one of those real names"
tags: [leak-scan, test-examples, r40, fixture-authoring, shipped-tree]
---

# The shipped-tree leak scan covers tests/ too, so a test that checks rendered output for real names cannot spell them itself

## Problem

U3 (`#104`) added `tests/test_testbrief.py`, which needed a fixture report containing a real
looking finding to prove `testbrief.parse` handles a report past 200 characters. Spelling an
actual project name, tracker key, or account name into that fixture, the obvious way to make it
look real, would fail `tests/test_examples.py`'s own leak scan: `SHIPPED` at
`tests/test_examples.py:36` includes `"tests"` alongside `skills`, `docs/examples`, `README.md`,
`CONCEPTS.md`, `CLAUDE.md`, and `.claude-plugin`, so `LEAK_PATTERNS` (`test_examples.py:29`, R40)
walks the tests directory's own source looking for `IW-[0-9]+`, `support-workbench`,
`pgutowski`, `PhilAI`, and a live Atlassian site.

## Why This Is Easy to Miss

Nothing in `tests/test_testbrief.py` or in `testbrief.py` states that the tests directory is
part of what R40 governs. The rule lives entirely in a constant, `SHIPPED`, inside a different
test module that has no reason to come up while writing a new test elsewhere in the same
directory. A test author checking "does this look like the kind of finding a real report would
carry" has every reason to reach for a familiar-looking project name, and nothing short of
running the full suite (or reading `test_examples.py` first) would catch it before commit.

## Solution

`tests/test_testbrief.py:205` imports `LEAK_PATTERNS` directly from `test_examples` and asserts
its own fixture report contains none of them, rather than trusting eyeballed neutrality:

```python
from test_examples import LEAK_PATTERNS
...
for pattern in LEAK_PATTERNS:
    self.assertNotRegex(fixture_report, pattern)
```

Any fixture text a test constructs to look like a real finding uses only invented, neutral words
of its own after that check, never a real product, company, or account name.

## Prevention

**A test that builds fixture text meant to resemble real project output must check that text
against `test_examples.LEAK_PATTERNS` itself, not assume neutrality by eye.** The scan's scope
(`SHIPPED` including `"tests"`) is a fact about the whole shipped tree, not about
`test_examples.py` alone, so any new test under `tests/` that authors realistic-looking fixture
strings inherits the same constraint. Import the patterns and assert against them directly;
do not re-derive or hand copy the pattern list, since the two would drift.

## Related Issues

- `#111`, opened from this same task's code review, is the neighboring defect in
  `classify.py`'s envelope-block closing fence, unrelated in mechanism but from the same run.

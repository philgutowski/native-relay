---
title: An unanchored fence opener under last opener pairing makes any line ending with the tag the block, so a learning that quotes the fence hides a valid envelope
date: 2026-09-28
category: logic-errors
module: contracts
problem_type: logic_error
component: runner
severity: high
root_cause: missing_anchor
resolution_type: code_fix
related_components: [contracts, classify, testbrief, filing, templates]
symptoms:
  - "a valid envelope whose learnings line ended with ```relay-envelope read as a fragment with no status and the Task halted no_envelope"
  - "a valid envelope followed by prose ending with the tag dropped to the whole message scan and the prose was read as a blocker on a complete status"
  - "a valid Test report followed by prose ending with its tag was not read at all"
tags: [fence-grammar, opener, anchor, last-opener, envelope, test-report, filed-block]
---

# An unanchored fence opener under last opener pairing makes any line ending with the tag the block

## Problem

Issue #124, found by the independent review of #118. `contracts.fence_opener_regex` matched
three or more backticks and the tag at the end of a line, with nothing checked before the
backticks. That leniency was deliberate: it kept the acceptance the pre #118 grammar had for an
opener written after a list marker or indented inside a list item, and the #118 solutions doc
recorded it as a feature. On its own it was harmless, because the old `findall` grammar paired
each opener with the first closer after it and took the last complete pair.

#118 replaced that pairing with "find the last opener, then the first closer after it", so an
earlier draft block could never absorb the valid last one. Under that rule the lenient opener
became a hazard. Any line that merely ended with the tagged fence, a learnings item saying "end
the final message with ```relay-envelope", or a sentence of prose after the closed block, was
now the last opener. Inside the envelope that gave an empty body with no status, so
`classify.parse_envelope` returned None and the Task halted `no_envelope`. After the envelope it
gave an opener with no closer, so the reader returned None, the parser fell to the whole message
scan, and the prose after the keys read as a blocker on a complete status. A Test report followed
by such prose was not read at all.

Every fixture transcript compared identical before and after, because no real capture in the
suite quoted its own fence. The shapes are reachable: Task processes in this repository write
learnings about the envelope grammar, and the review found them by reading the regex, not by
running anything.

## What Didn't Work

Keeping the opener as it was and relying on the brief's instruction to quote fences inline. The
brief already warned about a bare closer line, and the process obeyed it in the shape that
failed: the fence was quoted inline, at the end of a sentence, and the reader still took it.

## Solution

Landed on `relay/124`. The opener is anchored at a line start: optional indentation, at most
one list marker (`-`, `*`, `+`) or ordered list number (`1.`, `1)`) followed by whitespace, then
three or more backticks, the exact tag, trailing whitespace, end of line. The list item and
four backtick forms the #118 tests pin still read. Any other text before the backticks, a
blockquote prefix, or a second marker is not an opener. The closer keeps refusing a marker
before it, on purpose, because Markdown reads `- ```` as a new list item and a closer carries
nothing a process needs to put a marker in front of.

One shape stays open and is documented rather than fixed: a body line that is only the tagged
opener, with or without a list marker, is indistinguishable from a real opener under last
opener pairing. The Task brief's fence sentence now names it beside the closer warning.

A live headless `claude -p` was asked for an envelope whose learnings line ends with the tag,
and wrote exactly that. The new reader gave status complete with the learning intact; the pre
#124 reader from `main` gave an empty body.

## Why This Works

A fence is a line, not a suffix. Anchoring the opener at a line start makes the set of openers
the set of lines a process wrote as fences, so pairing from the last opener pairs from the last
fence the process meant. The leniency the anchor keeps, indentation and one marker, is exactly
the set of places the brief tells a process it may put the fence, and nothing more.

## Prevention

**When a reader's pairing rule changes, re-read every leniency the old rule tolerated.** A
lenient opener was safe under first pair wins and unsafe under last opener wins. The #118 review
checked that the old acceptances still passed, which they did, and did not ask what the new
pairing made of them. A change to how blocks are paired is a change to what an opener may look
like, and the two must be reviewed together.

**Never accept a fence as a suffix.** Any grammar for an opener or closer Relay reads back
anchors at a line start. `tests/test_contracts.py` `FenceReader` holds the mid line shapes that
must not open a block, and a new tag calls `contracts.last_fenced_block` rather than compiling
its own.

## Related Issues

- `docs/solutions/logic-errors/a-findall-fence-grammar-pairs-the-first-unterminated-opener-with-the-last-closer-so-a-draft-block-swallows-the-valid-one.md`
  is the #118 change that made this reachable; its Solution section carries one sentence on the
  #124 anchor so the two records agree.
- Issue #111 made the closer a line of its own for the same reason: a fence inside body text is
  body text. #124 applies the rule to the opener.

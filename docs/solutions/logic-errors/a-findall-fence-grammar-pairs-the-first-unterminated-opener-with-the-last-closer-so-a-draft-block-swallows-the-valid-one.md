---
title: A findall fence grammar pairs the first unterminated opener with the last closer, so a draft block above a valid one swallows it
date: 2026-09-28
category: logic-errors
module: contracts
problem_type: logic_error
component: runner
severity: high
root_cause: wrong_pairing
resolution_type: code_fix
related_components: [contracts, testbrief, filing, classify, testloop]
symptoms:
  - "a Test process's final message held a draft report block above a valid one and the pass recorded 'not valid JSON' with every finding discarded"
  - "an envelope written after an unterminated draft envelope read the draft's status"
  - "a check pass finding with a numeric card id failed the whole report on 'card must be a non empty string'"
tags: [fence-grammar, findall, lazy-body, test-report, envelope, filed-block, card-id]
---

# A findall fence grammar pairs the first unterminated opener with the last closer, so a draft block above a valid one swallows it

## Problem

Issue #118, found by the independent review of U3 of the browser test loop plan. The reader for
every fenced block Relay reads back from a process's final message, the `relay-test-report`
block, the `relay-filed` block, and since issue #111 the `relay-envelope`, was one regex in
`contracts.fence_regex`:

```python
re.compile(r"```%s[ \t]*\r?\n(.*?)^[ \t]*```+[ \t]*\r?$" % re.escape(tag), re.S | re.M)
```

Each reader ran `findall` and took the last body. The pairing is opener, lazy body, closer,
and `findall` scans left to right, so the first opener claims the first closer that follows it.
When the first block's own closer is missing, or is followed by text on the same line so it is
not a closer line, the lazy body runs through the second block's opener and stops at the second
block's closer. `findall` then returns one body, which begins with the draft's text and carries
the second opener inside it, and `json.loads` fails on it. A process that wrote a draft report,
abandoned it, and wrote a valid one below lost every finding to the draft.

The task also named a second loss on the same path: a check pass model copies the id from the
`### Card 12` heading and writes `"card": 12`, and `testloop.validate_finding` refused anything
but a non empty string, which failed the whole report.

## What Didn't Work

Reading the block in the same regex and taking the last of `findall`'s bodies. The last body is
only the last block when every earlier block was well formed, and the reader has no way to say
so. Tightening the closer alone (the #111 change) cannot help, because the defect is in which
opener the closer is paired with, not in what counts as a closer.

## Solution

Landed on `relay/118`. `contracts.last_fenced_block(text, tag)` replaces the regex. It finds
every opener for the tag, takes the last, and then searches for the first closer line after it.
The three readers call it with their own tag and hold no regex of their own. Pairing from the
last opener means an earlier block, however it is broken, is never inside the body that is
read. The opener grammar as landed here checked nothing before the backticks and accepted three
or more, which the old grammar also did, so an opener written after a list marker or with a four
backtick fence still opens the block. Issue #124 then anchored the opener at a line start, after
optional indentation and at most one list marker, because pairing from the last opener made a
prose line that merely ended with the tag the last opener; the list item and four backtick
forms still read. The closer grammar stays the #111 one: any indentation, three or more
backticks, trailing whitespace and carriage return allowed.

`testloop.validate_finding` accepts a card id written as a JSON integer and still refuses a
boolean, and `testloop.with_string_card` is the one place the id becomes a string;
`testbrief.parse_text` passes every finding through it so the pass code and the Filing brief see
one type. The Test brief's example finding shows the card as `"12"` on a check pass and `null`
on a tour, through a `card_example` placeholder, so the example never contradicts the pass
instruction beside it.

The code review on the branch added two more corrections on the same path. The whole message
scan `classify.parse_envelope` falls to when no fenced envelope is found took the last `status:`
line but the first `blockers:` line, so a closed draft above an unterminated final envelope gave
a complete status with the draft's blocker; `_list_after` now reads the last key line, the
same rule the status follows.

## Why This Works

A last block is defined by its own opener, so the reader has to start from the opener and not
from a scan that lets an earlier opener run first. Finding the last opener and then the first
closer after it is the only pairing under which an earlier block cannot reach the last one,
whatever shape the earlier block has. It also gives a truthful answer when the last opener has
no closer at all: no block, rather than an earlier block reported as the last.

## Prevention

**Never read the last of several fenced blocks with `findall` and a lazy body.** Any reader
for a "last block wins" contract must locate the last opener first, then the closer. The same
trap waits for any new block Relay reads back: a fourth block tag must call
`contracts.last_fenced_block` rather than compile a grammar of its own, and
`tests/test_contracts.py` `FenceReader` holds the shapes a new reader must pass.

**A validator on a model written field should accept the JSON type a model will choose.** An
id that looks numeric will be written as a number by some process on some pass; accept it at the
validator and normalise it once, in one helper, rather than fail the whole report on it.

## Related Issues

- Issue #111 put the three readers on one grammar so a triple backtick inside a body could not
  close the block; this task keeps that and fixes the pairing the shared grammar carried.
- `docs/solutions/logic-errors/denial-regex-anchored-immediately-after-tool-name-missed-real-bash-denials.md`
  is the neighbouring lesson that a regex proven only against fixtures its author wrote is
  unproven. Here the fixture transcripts held one block or two well formed ones, and the draft
  above a valid block that a real process writes was never in the suite until this task.

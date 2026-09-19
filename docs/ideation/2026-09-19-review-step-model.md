# Should the Review step have its own model?

Date: 2026-09-19
Status: open question. Nothing is built for it, by decision. It changes a Brief contract, so it
needs its own plan and its own live proof.

## What was observed

From the Cratekit continuous run of 2026-09-18 and 2026-09-19, where the feeder routed some cards
to `fable` and the rest to `opus`. Four cards landed on fable. Evidence to record, not to act on:

| | fable | opus |
|---|---|---|
| Mean wall time per card | 41 min | 23 min |
| Cost per output token | equal | equal |
| Findings raised by the Review step, per card | ten | zero to two |
| Of those, real | six or seven | not measured separately |

The sample is small and the cards were not comparable: fable got the cards Phillip's test picked
out as needing design judgment before any code, which are the harder cards by construction. So the
time gap says little. The review gap is the interesting number, because it is large and it points
one way.

## Why the numbers are tied together today

The Review step runs inside the Task process. It is a step of the Task Brief, so it runs on the
Task's model. There is no seam between "the model that built this" and "the model that reviewed
it". Routing a card to fable today buys a fable build and a fable review together, and there is no
way to buy one without the other.

That leaves two readings of the review gap that the data cannot separate:

1. Fable is a better reviewer, and any diff would get more real findings from it.
2. Fable's builds were on harder cards with more to find, and the reviewer mattered less.

## The question

Would a setting that runs the Review step on a named model, separate from the Task's model, be
worth its cost? The attractive shape is an opus build with a fable review: most of the wall time
saved, most of the findings kept. It is attractive only if reading 1 is the true one.

## Why it is not a small change

- The Review step is a built in skill on claude and grok, `/code-review` and `/review`, invoked
  by the Task process inside its own session. A skill call inherits the session's model. A
  different model means a different process, or a subagent with a model override, and neither is
  what the Brief asks for today.
- The classifier proves the Review step ran by finding the Skill call in the Task's transcript.
  A review in another process leaves its evidence in another transcript, so the `review_skipped`
  finding, the Capability record's `review_skill`, and the codex `review_argv` contract would all
  need a second shape. That is the Brief contract and the classify contract changing together,
  which is exactly the case the stubbed seams learning is about.
- A separate review process would need its own permission posture, its own timeout, and an answer
  to who fixes what it finds: the reviewer, or a third launch of the builder.

## What would settle it cheaply, before any of that

An experiment, not a feature. Take a handful of diffs that already landed from opus builds, run
`/code-review` on each by hand on fable and on opus, and count real findings. If fable finds
markedly more on the same diff, reading 1 holds and a plan is worth writing. If the counts are
close, the gap was the cards, and the routing file already does everything useful.

## Until then

The routing file is the lever. Phillip's test for sending a card to fable: it needs design
judgment before any code, a new seam, a first caller of an engine nobody has driven, a write or
delete or restore path, or a decision still buried in the brief. Wiring and wording stay on the
default.

---
title: A word-list scrubber caught only the private terms it was told about and missed structural leaks in skill listings, git identity, and ls ownership
date: 2026-09-07
category: logic-errors
module: tests/fixtures/backends
problem_type: logic_error
component: runner
severity: high
root_cause: missing_validation
resolution_type: code_fix
related_components: [fixtures, closeout, task-brief]
symptoms:
  - "Claude session captures embedded a skill_listing attachment whose content field held the capturing user's entire installed Claude Code skill catalog verbatim, including private skill names and a Kindle email address, untouched by the scrubber's hand-written word list"
  - "Grok stream captures embedded that same skill catalog a second time as a JSON array inside an available_commands event, in a different serialization the word list never anticipated"
  - "closeout-stdout.jsonl captures carried the real git author and committer name and email on git show and git log output the closeout process ran as part of its own work"
  - "task process explorations captured real ls -l output with the real account username sitting in the owner column"
  - "the leak-detection test (test_examples.py, NoProjectLeakage) only walked skills/, docs/examples/, and README.md, so tests/ itself shipped these leaks with nothing to catch them"
tags: [fixture-scrubbing, personal-data-leak, real-capture, shape-based-matching, regex-scrubber, public-fork, skill-listing, git-identity]
---

# A word-list scrubber caught only the private terms it was told about and missed structural leaks in skill listings, git identity, and ls ownership

## Problem

The twenty six backend fixture files under `tests/fixtures/backends/{claude,codex,grok}/` are real
captured CLI session transcripts and stdout streams from actual headless runs against a throwaway
proof repository (`tests/fixtures/backends/README.md:7-8`), not hand written test data. Because a
captured task can search outward from its target before concluding a search failed, its transcript
can carry file paths and prose from unrelated work on the machine that ran it
(`tests/fixtures/backends/_scrub.py:1-4`). When this private compound-relay fork was prepared for
public release as native-relay (commit `f32ca37`, "Add the MIT license, native plugin metadata,
and neutral fixtures", U1 of `docs/plans/2026-09-07-native-mode-plan.md`), the existing scrubber's
word-list-plus-one-regex approach was not enough to guarantee the committed fixtures were clean.

## Symptoms

Before the fix, `tests/fixtures/backends/_scrub.py` had exactly two path rules, both hardcoded to
one operator's home directory:

```python
(re.compile(r"/Users/pgutowski/(?!Documents/PhilAI/relay)[\w./\-]*"), "/redacted/path"),
(re.compile(r"/Users/pgutowski/Documents/PhilAI/(?!relay)[\w./\-]*"), "/redacted/path"),
```

plus a fixed list of `\b(?:Word)\b` substitutions naming specific private project codenames
directly in the source. This left the fixtures carrying content none of those rules could catch:

1. Claude's session-transcript JSONL includes an attachment `"type":"skill_listing"` whose
   `"content"` field is one giant string holding the operator's entire installed-skills catalog,
   including skill descriptions that can themselves name private things. No fixed word list can
   anticipate arbitrary prose inside a catalog like this.
2. Grok's own stream format carries the equivalent as `"type":"available_commands"` with a
   `"commands"` JSON array, in a different serialization the same word list also could not reach.
3. `git show`/`git log` output captured inside a closeout process's own stdout log carries
   `Author:`/`AuthorDate:` lines with a real name next to a real email.
4. `ls -l` output a task process ran while exploring the repo carries the real account name in the
   owner column.

The companion leak-detection test, `NoProjectLeakage` in `tests/test_examples.py:160-184`, also
did not scan `tests/` itself before this change, so a leak there would not have failed the suite
either.

## What Didn't Work

The prior scrubber's design was lexical: catch specific forbidden words, and catch one person's
home directory by hardcoding the username into the regex. Both strategies fail for content whose
leak risk is structural rather than lexical:

- A skill catalog or a `git log` author line is not a fixed vocabulary. It is a whole payload
  whose content is generated live, differently, on whatever machine happens to capture a fixture.
  Enumerating every word that payload could contain is not tractable.
- Hardcoding one username into the path regex means the scrubber only works for fixtures captured
  on that one machine's account. Any new capture, or any reviewer trying to verify the fixtures
  are clean, has no generic guarantee, only a guarantee scoped to one literal string.
- The private word list lived in the shipped scrubber itself, so the words a reviewer must never
  see were sitting directly in the file meant to remove them, defeating the purpose the moment the
  repo went public.

## Solution

The fix (touching `tests/fixtures/backends/_scrub.py`, `tests/fixtures/backends/README.md`, and
`tests/test_examples.py`, plus re-running the scrubber against all twenty six fixtures) replaces
word matching with shape matching, and moves the private terms out of the shipped file entirely.

**1. A safety net that was already present and is what makes broad regex surgery on JSONL safe.**
`_scrub.py:95-110` checks, for every `.jsonl` file that changed, that the count of lines that
still `json.loads()` successfully is identical before and after the scrub, aborting with
`SystemExit` if not:

```python
if path.endswith(".jsonl"):
    def decodable(text):
        n = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except Exception:
                continue
            n += 1
        return n
    before, after = decodable(original), decodable(cleaned)
    if before != after:
        raise SystemExit("scrub changed decodable line count in %s: %d to %d"
                         % (path, before, after))
```

(Codex prints one non-JSON line onto its own JSON stream, which is why the check is a line count
rather than "every line parses".) This invariant is what lets the new rules replace an entire
`"content":"..."` string or an entire `"commands":[...]` array without fear of producing a line
the per-backend normalizers can no longer parse.

**2. A two-tier, generic path strategy** (`_scrub.py:44-58`), replacing the two hardcoded-username
rules:

```python
SUBS = [
    # The throwaway target, plain path.
    (re.compile(r"/(?:Users|home)/%s/(?:%s/)*%s" % (_USER, _SEGMENT, re.escape(PROOF_TARGET))),
     "%s/%s" % (NEUTRAL_HOME, PROOF_TARGET)),
    # The throwaway target, Claude's project slug.
    (re.compile(r"-(?:Users|home)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*?-relay-proof-target"),
     "-Users-operator-relay-proof-target"),
    # The throwaway target, Grok's percent encoded session directory.
    (re.compile(r"(?:Users|home)%2F[^%\s\"']+(?:%2F[^%\s\"']+)*?%2Frelay-proof%2Ftarget"),
     "Users%2Foperator%2Frelay-proof%2Ftarget"),
    # The Relay checkout the runner ran from, plain path, kept as a path under the neutral home.
    (re.compile(r"/(?:Users|home)/%s/(?:%s/)*%s(?=/|\b)" % (_USER, _SEGMENT, RELAY_CHECKOUT)),
     "%s/%s" % (NEUTRAL_HOME, RELAY_CHECKOUT)),
    # Any other absolute path under a home directory.
    (re.compile(r"/(?:Users|home)/(?!operator/)%s(?:/%s)*" % (_USER, _SEGMENT)), "/redacted/path"),
    ...
```

The two legitimate paths a capture can name, the throwaway proof target (`relay-proof/target`) and
the Relay checkout the runner ran from, are rewritten first, to a stable neutral form under
`/Users/operator`, in each of the three encodings a capturing CLI actually writes: a plain POSIX
path, Claude's dash-slugged project-directory encoding, and Grok's percent-encoded session
directory. Only after those anchored rules run does a generic catch-all fire on any remaining
`/Users/<anyone>/...` or `/home/<anyone>/...` path and stamp it `/redacted/path`. The catch-all's
`(?!operator/)` negative lookahead is what keeps it from re-mangling the neutral paths the
anchored rules just wrote. The ordering is called out explicitly in the module docstring and in an
inline comment: "the two anchored paths are rewritten first so the catch-all home rule below never
sees them."

**3. Structural rules for the two skill-catalog payload shapes**, each replacing the whole payload
rather than scrubbing inside it (`_scrub.py:65-69`):

```python
# Claude's skill listing attachment: the operator's whole skill catalogue, as one JSON string.
(re.compile(r'("type":"skill_listing","content":")(?:[^"\\]|\\.)*(")'),
 r"\1%s\2" % LISTING_REDACTED),
# Grok's available_commands event: the operator's skill names, as one JSON array.
(re.compile(r'("type":"available_commands".*?"commands":\[)[^\]]*(\])'),
 r'\1"%s"\2' % LISTING_REDACTED),
```

`LISTING_REDACTED = "[relay redacted the operator's listing]"`. Verified in the actual fixtures
post-fix: `tests/fixtures/backends/claude/session-transcript-complete.jsonl:6` now carries
`"content":"[relay redacted the operator's listing]"` while keeping the surrounding fields intact,
and `tests/fixtures/backends/grok/stdout-complete.jsonl` carries
`"commands":["[relay redacted the operator's listing]"]` while keeping the sibling `"tools":[...]`
array (generic tool names, not private) untouched.

**4. Structural rules for git identity and `ls -l` ownership** (`_scrub.py:59-63`):

```python
(re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), EMAIL),
# The git identity on author and committer lines, once the email is neutral.
(re.compile(r"(?<=[:\s])[^\s<:\\]+(?: [^\s<:\\]+)*(?= <%s>)" % re.escape(EMAIL)), "Relay Operator"),
# The owner column of an `ls -l` listing the task ran.
(re.compile(r"(\s\d+ )[\w.\-]+(\s+(?:staff|users|wheel)\s)"), r"\1operator\2"),
```

The email rule runs first and rewrites any address to `relay@example.com`; the author-name rule is
keyed on that now-neutral email via a lookahead, so it only strips the name sitting immediately in
front of `<relay@example.com>` on a `git log`/`git show` author-style line. The `ls -l` rule
matches on the specific column shape (a numeric field, then the username field, then a group name
known to be `staff`, `users`, or `wheel`) and rewrites only the username field.

**5. The private word list moved out of the file and into the environment** (`_scrub.py:38-40`):

```python
PRIVATE_TERMS = tuple(term.strip() for term in os.environ.get("RELAY_SCRUB_TERMS", "").split(",")
                      if term.strip())
```

`SUBS` appends one `\bterm\b` rule per entry at import time. The shipped file now carries an empty
tuple by default; a future capture on a different machine supplies its own terms locally via
`RELAY_SCRUB_TERMS` (comma-separated) rather than the fixtures depending on this machine's list
being complete or committed.

**6. The leak-detection test widened its scan surface.** `tests/test_examples.py`'s `SHIPPED`
tuple, which held only `"skills"`, `"docs/examples"`, and `"README.md"` before this fix, now also
includes `".claude-plugin"`, `"tests"`, `"CONCEPTS.md"`, and `"CLAUDE.md"`. Since `LEAK_PATTERNS`
includes literal strings the test file itself must contain in order to check for them,
`NoProjectLeakage.shipped_files` exempts the test's own file with
`os.path.samefile(full, __file__)`.

## Why This Works

The prior approach treated "what's private" as an enumerable set of strings a maintainer could
list in advance. That assumption breaks for content that is generated live by whatever machine
performs a capture: a skill catalog, a git author line, an `ls -l` owner column. None of these are
fixed vocabulary; they are payloads whose content varies by machine and by moment, but whose
*shape* (a specific JSON field name and structure, a specific line format, a specific column
position) is fixed by the producing tool's format and does not vary. By matching the shape and
replacing the whole payload with a fixed placeholder, the scrubber no longer needs to know what
words the payload could contain, only where it lives in the structure.

The decodable-line-count invariant is the second half of why this is safe rather than merely
convenient: the fixtures exist specifically to exercise the per-backend normalizers reading event
shape and structure rather than content, so any rule that changed a JSONL line's parseability
would silently invalidate the very thing being tested. The invariant makes a broad regex free to
replace a whole field's value, but not free to break the line's JSON syntax.

The two-tier path strategy works because it separates "paths this project needs to remain a
readable, believable path" (the proof target, the Relay checkout) from "paths that must simply not
exist in the output" (everything else under a home directory). Running the specific rules first
and excluding their output from the catch-all (`(?!operator/)`) is what prevents the generic rule
from re-redacting the very paths the specific rules just made safe.

## Prevention

- When a fixture is a real captured artifact rather than hand-written data, never assume a
  maintainer-authored word list is a complete leak guard. Ask instead: what does this producing
  tool's format structurally guarantee will appear, regardless of which machine ran it (a catalog
  attachment, an identity line, a listing column), and write a rule keyed to that shape.
- Never hardcode an operator's own username or account into a scrubbing regex. A path rule should
  match the *pattern* of a home directory (`_USER = r"[^/\s\"'%]+"`) so it works unchanged on any
  machine that captures a fixture in the future.
- Keep any private word list out of the committed file. `RELAY_SCRUB_TERMS` is the pattern: the
  list is a property of the capturing machine, supplied at scrub time, never shipped.
- Before trusting a scrub against a new capture, run `_scrub.py` again from the repo root (it is
  idempotent) and grep the tree for anything that should have been caught, the same way a
  whole-tree grep for known personal terms was used to verify this fix.
- When widening a leak-detection test's scan surface, remember the test file itself will now be
  walked if it lives under a newly-included directory, and must exempt itself
  (`os.path.samefile(full, __file__)`) since it necessarily contains the leak patterns it
  searches for.
- Rely on the decodable-line-count check as the enabling safety net any time a new structural rule
  is added to `SUBS` for JSONL content: it is what turns "replace this whole payload with a regex"
  from risky into safe, and any new rule should be exercised against real fixture files, not just
  unit-tested in isolation, so this check actually runs against it.

## Related Issues

- `docs/solutions/logic-errors/stubbed-seams-agree-by-construction-first-live-run-found-five-contract-defects.md`
  shares context on why `tests/fixtures/backends/` holds real CLI captures rather than
  hand-written fixtures ("a fixture written alongside the parser that reads it proves only that
  the two agree"), but solves a different problem family: contract-shape mismatches between a
  stub and a real process, not content leakage.
- `tests/fixtures/backends/README.md` already documents the scrubber's post-fix behavior and cites
  the doc above for the same provenance reason; kept consistent with this fix in the same commit.

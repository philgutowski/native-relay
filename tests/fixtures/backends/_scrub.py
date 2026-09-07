"""Scrub private content out of the captured fixtures without changing their shape.

A captured task can search outward from the throwaway target it was given, so its transcript can
carry file paths and prose from unrelated work on the machine that ran it. These fixtures exist to
exercise the per-backend normalizers, which read event types and structure rather than the content
of a search result, so redacting the payload costs the fixture nothing it is used for.

Every substitution is length-preserving in kind, not in bytes: a redacted path is still a path,
so a parser that walks the structure sees what it saw before.

The rules are generic on purpose. The two paths a capture legitimately names, the throwaway target
the task ran against and the Relay checkout the runner ran from, are rewritten to neutral paths
under a neutral home, in each of the three encodings the CLIs write them in: plain, Claude's
project slug (`-Users-…`), and Grok's percent encoded session directory (`Users%2F…`). Every other
absolute path under a home directory is a leak and becomes `/redacted/path`. Any private word a
machine's captures could carry goes in PRIVATE_TERMS before a new capture is committed; it ships
empty because the list is a property of the machine that captured, not of the fixtures.
"""
import json
import os
import re
import sys

ROOT = "tests/fixtures/backends"
EMAIL = "relay@example.com"
NEUTRAL_HOME = "/Users/operator"
PROOF_TARGET = "relay-proof/target"
RELAY_CHECKOUT = "relay"

# The home directory forms the two CLIs' evidence paths take. `_USER` is one path segment.
_USER = r"[^/\s\"'%]+"
_SEGMENT = r"[\w.\-]+"

# Words specific to the machine that captured a fixture: the operator's name, account, and the
# names of private projects its captures could mention. Supplied locally through
# RELAY_SCRUB_TERMS, comma separated and longest first, before running the scrubber on a new
# capture. The repository ships none, because the list is a property of the capturing machine.
PRIVATE_TERMS = tuple(term.strip() for term in os.environ.get("RELAY_SCRUB_TERMS", "").split(",")
                      if term.strip())
LISTING_REDACTED = "[relay redacted the operator's listing]"

# Order matters: the two anchored paths are rewritten first so the catch-all home rule below
# never sees them, and the catch-all excludes the neutral home it would otherwise re-match.
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
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), EMAIL),
    # The git identity on author and committer lines, once the email is neutral.
    (re.compile(r"(?<=[:\s])[^\s<:\\]+(?: [^\s<:\\]+)*(?= <%s>)" % re.escape(EMAIL)), "Relay Operator"),
    # The owner column of an `ls -l` listing the task ran.
    (re.compile(r"(\s\d+ )[\w.\-]+(\s+(?:staff|users|wheel)\s)"), r"\1operator\2"),
    # Claude's skill listing attachment: the operator's whole skill catalogue, as one JSON string.
    (re.compile(r'("type":"skill_listing","content":")(?:[^"\\]|\\.)*(")'),
     r"\1%s\2" % LISTING_REDACTED),
    # Grok's available_commands event: the operator's skill names, as one JSON array.
    (re.compile(r'("type":"available_commands".*?"commands":\[)[^\]]*(\])'),
     r'\1"%s"\2' % LISTING_REDACTED),
] + [(re.compile(r"\b%s\b" % re.escape(term)), "redacted") for term in PRIVATE_TERMS]


def scrub(text):
    for rx, replacement in SUBS:
        text = rx.sub(replacement, text)
    return text


def main():
    changed = []
    for backend in sorted(os.listdir(ROOT)):
        d = os.path.join(ROOT, backend)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                original = fh.read()
            cleaned = scrub(original)
            if cleaned == original:
                continue
            # A jsonl fixture must still decode exactly as many lines as it did before. Codex
            # prints one non-JSON line onto its own JSON stream, so the test is that the count
            # is unchanged rather than that every line parses.
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
            with open(path, "w", encoding="utf-8") as out:
                out.write(cleaned)
            changed.append(path)
    for path in changed:
        print("scrubbed", path)
    print("\n%d file(s) changed" % len(changed))


if __name__ == "__main__":
    main()

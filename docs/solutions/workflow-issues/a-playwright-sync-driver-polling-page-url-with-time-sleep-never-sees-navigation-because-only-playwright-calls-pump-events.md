---
title: A synchronous Playwright driver polling page.url with time.sleep never sees navigation, because only a Playwright call pumps browser events
date: 2026-09-29
last_updated: 2026-09-29
category: workflow-issues
module: docs/examples/browser-test-loop/drive.py
problem_type: workflow_issue
component: tooling
severity: medium
applies_when:
  - "writing or reviewing a headless browser driver script meant to run unattended, in this repo or any other project"
  - "the driver needs to wait for a page navigation, a state change, or any other async browser event before continuing"
  - "the driver's design assumes an operator interacts with a headed browser window while the script itself waits on a terminal prompt"
  - "the target page holds a long lived connection such as server sent events (SSE) or a websocket, so networkidle never fires"
  - "the script will be run through a passthrough shell such as Claude Code's !, where stdin has no terminal"
root_cause: async_timing
resolution_type: documentation_update
related_components: [browser-test-loop, headless-driver, playwright, storage-state]
tags: [playwright, sync-api, headless, sign-in, storage-state, event-loop, sse, browser-driver]
---

# A synchronous Playwright driver polling page.url with time.sleep never sees navigation, because only a Playwright call pumps browser events

## Context

The browser test loop plan (`docs/plans/2026-09-28-0935-feat-browser-test-loop-plan.md`, unit
U8, merged at `8a23d9a` as of native-relay main `9e58e49`) ships a generic example driver at
`docs/examples/browser-test-loop/drive.py`, meant to be copied into a real project's repository
and adapted (R9, R25). Its `signin` mode is deliberately the one interactive step in an otherwise
unattended pipeline: it opens a headed (visible) browser once, lets a human sign in by hand, and
writes the resulting session to a storage state file the headless Test process reuses for every
later pass (KTD14). As shipped (`drive.py:119` to `:135`), that mode does this:

```python
def signin(args):
    sync_playwright = load_playwright()
    state = os.path.expanduser(args.state)
    os.makedirs(os.path.dirname(state) or ".", exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(args.url)
            input("Sign in in the browser window, then press Enter here to save the session. ")
            context.storage_state(path=state)
        finally:
            browser.close()
    os.chmod(state, 0o600)
    print("wrote %s" % state)
    return EXIT_OK
```

`drive.py:129` blocks on Python's builtin `input()`, waiting for the operator to press Enter in
the same terminal after signing in in the browser window. This works when the script is run from
an ordinary interactive shell. It does not work the way an operator running Relay through Claude
Code actually runs shell commands: the `!` prefix passes a command straight to a shell with no
attached terminal on stdin. `input()` raises `EOFError` the instant it is reached, which in
practice is right after the headed browser window opens, leaving the operator staring at an open
browser and a dead Python process with nothing captured.

This was paid for on 2026-09-28, signing a real web app in for the browser test loop's
report-only tour: the operator's `!` command failed with `EOFError` at `drive.py:129` on the
first attempt. Two increasingly reasonable-looking repairs were then tried and both failed, each
for a distinct reason rooted in how Playwright's synchronous API actually works rather than in
anything about the target page.

**First repair, rejected: wait for `networkidle` instead of a manual Enter.** The idea was to
replace the blocking prompt with Playwright's own `page.wait_for_load_state("networkidle")` right
after the operator's sign in navigates the page. `networkidle` is defined as "no network
connections for at least 500 ms," and the target application's ticket screen holds a live
server-sent-events (SSE) stream, a long-lived HTTP connection the server uses to push updates
without the page polling for them. An open SSE connection is exactly the kind of network activity
`networkidle` is watching for, so on this page it never fires, and the call hangs until its own
timeout.

**Second repair, rejected: poll `page.url` with `time.sleep()`.** The next attempt was a plain
Python loop, checking `page.url` against the sign in path and sleeping a second between checks
with the standard library's `time.sleep()`. This also never worked, and the reason is specific to
Playwright's **synchronous** API (`from playwright.sync_api import sync_playwright`, the form this
driver uses, `drive.py:106`): the sync API drives a real browser over a background connection and
only processes incoming browser events, including navigation events, while a Playwright call
itself is running. Between Playwright calls, nothing pumps that connection. `time.sleep()` blocks
the Python thread without making any Playwright call, so during every sleep the driver is not
listening for the navigation event at all. The operator visibly finished signing in and moved off
the sign in page in the browser window, minutes passed, and `page.url` kept reading the stale sign
in URL for the entire loop, because the one thing that would have delivered the updated URL to the
Python process, a Playwright call, never happened during the wait.

**What worked.** Replace `time.sleep()` in that same polling loop with Playwright's own
`page.wait_for_timeout(1000)`. `wait_for_timeout` is a Playwright API call like any other: it
still pauses for the given duration, but because it is a Playwright call it also pumps the
browser's event queue while it waits, so a navigation that happened during that second is
delivered and reflected in the very next `page.url` read. The loop becomes: call
`page.wait_for_timeout(1000)`, then check whether `page.url` has left the sign in path; repeat
until it has, then call `context.storage_state(path=...)` to save the session and `os.chmod(...,
0o600)` so the file stays operator-only, then print a flushed confirmation line, since there is
no more interactive prompt to tell the operator it saved. This is what a working sign in script
looks like:

```python
# What worked: poll with a Playwright call, not a bare sleep, so the event queue is pumped.
import sys, os

SIGNIN_PATH = "/login"

context = browser.new_context()
page = context.new_page()
page.goto(url)
print("sign in in the browser window; this will save automatically once you leave the sign in page")
while SIGNIN_PATH in page.url:
    page.wait_for_timeout(1000)      # pumps events; time.sleep(1) here would not
context.storage_state(path=state)
os.chmod(state, 0o600)
print("wrote %s" % state, flush=True)
```

The verified working script lived at `~/.relay/scratch/sw-tour/signin_wait.py`, an operator
scratch path outside any repository, and confirmed the session saved on the operator's third
attempt overall (first `EOFError`, second the silent `networkidle` hang, third this pattern).

**As of this run, the shipped recipe still has the `input()` form** at `drive.py:129`. Landing
this fix in `docs/examples/browser-test-loop/drive.py` was out of scope for the run that found it
and is tracked separately as a low in the operator's own (non-repo) lows file, not as a repo
issue. This document exists so the next person who copies this recipe, or writes a similar
attended-once driver step for any other unattended pipeline, does not spend three attempts
rediscovering the same two dead ends.

## Guidance

**A blocking prompt that reads a terminal (`input()`, `getpass()`, anything on stdin) does not
survive a passthrough shell with no attached terminal.** If a script has exactly one interactive
step meant for a human to complete once, detect completion by watching the browser state itself,
never by asking the human to signal back through the same process's stdin. The two shapes to reach
for and their failure modes:

- `page.wait_for_load_state("networkidle")` waits for the *network* to go quiet. It is the wrong
  tool whenever the target page holds any long-lived connection: SSE, a websocket, long polling,
  or a page that fires background analytics or heartbeat requests. Use it only when you have
  confirmed the page's connections all close after the state you are waiting for.
- Polling any Playwright-observed property (`page.url`, `page.title()`, the result of
  `page.query_selector(...)`) in a loop that sleeps with the standard library's `time.sleep()`
  will read a stale value for the entire sleep, no matter how long you wait, because nothing
  pumps Playwright's event queue between calls. This is true of the synchronous API specifically;
  it does not apply to `playwright.async_api`, whose coroutines yield to the event loop on every
  `await`, including inside an `asyncio.sleep()`.

**The fix is always to replace the sleep with a Playwright wait**, so the same call that pauses
also pumps events: `page.wait_for_timeout(ms)` for a plain interval, or a Playwright wait
condition (`page.wait_for_url(...)`, `page.wait_for_selector(...)`, `page.wait_for_function(...)`)
when one exists for exactly the condition you are polling for, which also removes the need to poll
by hand at all. `page.wait_for_url(pattern, timeout=...)` is the more direct call for this exact
sign in case and should be preferred over hand rolling the loop above where either is available.

**Tell the operator the script is unattended-capable, since there is no more prompt to reassure
them.** A flushed `print()` after the state is saved is the only signal left once `input()` is
gone; without it an operator watching through a passthrough shell has no way to tell the sign in
succeeded versus the script still waiting.

## Why This Matters

This is not a defect in the target application or in Playwright; it is Playwright's synchronous
API working exactly as documented, mismatched against a driver script pattern (block on
`input()`, or poll with the standard library's own sleep) that reads as ordinary Python and gives
no error to point at the real cause. `time.sleep()` polling silently reads stale state instead of
raising, which is what makes it costlier to debug than the `EOFError`: the `EOFError` fails loudly
on the first attempt, but the `networkidle` hang and the `time.sleep()` hang both fail by doing
nothing forever, with no exception and no log line to point at the actual mechanism. Anyone who
writes or copies an unattended browser driver, for this project's browser test loop or for
anything else built the same way, an interactive setup step followed by headless reuse of the
saved session, will reach for one of these same two fixes first, because both look correct without
knowing Playwright's event pump contract. Recording the mechanism here, rather than only the
symptom, is what lets the next attempt skip straight to the working pattern instead of re-paying
for the same two dead ends.

## When to Apply

- Writing or reviewing any headless browser driver meant to run unattended, whether copied from
  `docs/examples/browser-test-loop/drive.py` or written independently, in this repo or any other
  project.
- Any driver step that needs to detect a browser side state change, a navigation, a DOM change, a
  new network request, before continuing, especially when that step follows a human-driven action
  in a headed window.
- Before choosing `networkidle` as a wait condition: check first whether the target page holds any
  long-lived connection (SSE, websocket, long polling).
- Before writing any polling loop around a Playwright-observed property with the synchronous API:
  the sleep in that loop must be a Playwright call (`wait_for_timeout`, or a `wait_for_*`
  condition), never `time.sleep()`.
- When adapting `docs/examples/browser-test-loop/drive.py`'s `signin` mode into a real project's
  repository (the IW switch-on steps at `~/.relay/manifests/browser-test-loop.iw-switch-on.md`,
  step 4, name this exact adaptation): replace the `input()` line with the polling pattern above
  before relying on it to run through a passthrough shell.

## Examples

### Before, what fails

```python
# Fails under a passthrough shell with no terminal on stdin: EOFError at input().
page.goto(args.url)
input("Sign in in the browser window, then press Enter here to save the session. ")
context.storage_state(path=state)
```

```python
# Fails silently: networkidle never fires on a page holding an open SSE connection.
page.goto(args.url)
page.wait_for_load_state("networkidle")
context.storage_state(path=state)
```

```python
# Fails silently: time.sleep() never pumps Playwright's event queue, so page.url never
# reflects a navigation that happened during the sleep, no matter how long you wait.
import time
page.goto(args.url)
while "/login" in page.url:
    time.sleep(1)
context.storage_state(path=state)
```

### After, what works

```python
# Works: page.wait_for_timeout() is a Playwright call, so it pumps events, and the next
# page.url read reflects the real navigation.
page.goto(args.url)
print("sign in in the browser window; this saves automatically once you leave the sign in page")
while "/login" in page.url:
    page.wait_for_timeout(1000)
context.storage_state(path=state)
os.chmod(state, 0o600)
print("wrote %s" % state, flush=True)
```

Where a direct Playwright wait condition exists for the exact thing you are polling, prefer it
over hand rolling the loop:

```python
page.goto(args.url)
print("sign in in the browser window; this saves automatically once you leave the sign in page")
page.wait_for_url(lambda url: "/login" not in url, timeout=0)   # timeout=0: wait indefinitely
context.storage_state(path=state)
```

## Related

- `docs/solutions/workflow-issues/headless-turn-end-is-exit-backgrounded-command-is-killed.md`:
  another case where a process shape that is the natural, trained move in an attended session
  (background a long step and wait, or block on a terminal prompt for a human) is exactly the one
  thing that fails once nobody is watching or no terminal is attached. That document's failure is
  about turn lifetime under `claude -p`; this one is about Playwright's own event pump contract
  under `!`. Both are gates a live unattended run found that no stub or unit test could, since
  neither failure mode exists until a real process runs against a real terminal-less shell or a
  real page holding a real SSE connection.

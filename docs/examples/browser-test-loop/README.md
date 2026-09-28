# The browser test loop, an example

The browser test loop is a feeder feature, switched on per manifest, that tests the running web
app after each landing, files what it finds as cards the same feeder builds, retests the fixes,
and stops on its own at a rule it can check. Section 12 of `docs/manifest-authoring.md` is the
reference for every key and rule. This folder is a generic example to copy, naming no real app.

| File | What it is | Where it goes |
|---|---|---|
| `example.feeder.toml` | a sidecar with the loop on and every `[test_loop]` key written out | beside your manifest, renamed to its stem |
| `tour-template.md` | a tour document: one heading per area, what to check there, and the approval steps not to pass | in the app's repository, at the path `test_loop.tour` names |
| `drive.py` | a headless Playwright driver run from the shell, with a one time sign in mode | in the app's repository, where the tour document says |

The app's own repository owns its tour document and its driver. Relay ships neither for a real
app, and its runner never imports `drive.py`: the browser driver is the project's tooling,
installed outside the runner, which stays standard library only.

## What a pass does

The feeder starts a pass as `relay test <manifest>` at three points: a full tour at the first
cycle of the loop, a check of the cards a cycle landed after that cycle settles, and a full tour
each time the queue drains. One pass:

1. refuses to start unless the checkout is clean on its default branch, then takes both leases;
2. runs your `prepare` command, which moves the app to the default branch's commit and confirms
   it serves that commit, or the pass is recorded `not_run` and files nothing;
3. launches a Test process in a detached worktree of that commit, outside the checkout, with no
   tracker write tool; it drives the app through `drive.py`, reads the code for each cause, and
   ends with one `relay-test-report` block;
4. decides in code what to file: high and medium findings up to the per pass cap and the loop's
   budget, lows to the lows file, nothing for a stopped area;
5. launches a Filing process with exactly those findings, which files or comments through the
   tracker adapter's own instructions, and reads each card back to confirm it;
6. writes the pass record and hands it to the feeder, which counts rounds, generations, and
   patches per area, and decides whether the loop stops.

## Switch it on

1. **Give the app a worktree of its own.** The app under test is served from a worktree outside
   the checkout the runner merges into, so a merge never changes what is being served mid pass
   and a server never holds the checkout. `prepare` below makes one on its first run.
2. **Write `prepare`.** It is an argument list the pass runs in the target repository, in its
   own process group, ended whole after `prepare_timeout_seconds`. It reads `RELAY_TEST_COMMIT`
   and `RELAY_TEST_URL` from its environment. It exits 0 only once the app at that url serves
   that commit; anything else records the pass as not run, with the command's last output line
   as the reason. A launched process cannot stop a server, so moving and restarting it lives
   here and nowhere else.
3. **Stub the app's outbound integrations in `prepare`.** Serve the app with mail, payments,
   webhooks, and every other outbound call pointed at a local stub. The Test brief tells the
   Test process never to approve, send, submit, or post anything that leaves the app, and to
   stop at every approval step; the stub is the second line of defense, so a slip in a process
   still writes nowhere real.
4. **Write the tour document** from `tour-template.md`. Every markdown heading in it is an area,
   and a finding names its area exactly as the heading spells it, so keep the opening text free
   of headings. Name the driver, the storage state file, and the sign in marker in that opening
   text, and list every approval step under its area.
5. **Sign in once.** `python3 tools/drive.py signin --url <the sign in page> --state
   <the storage state file>` opens a headed browser; sign in by hand and press Enter in the
   terminal. Keep that file outside the repository: the Test process runs in a fresh worktree
   that would not carry an ignored file, and a session never belongs in git. A Test process
   never types a credential. A missing file or a sign in page is a `not_run` pass, and signing
   in again is the operator's step.
6. **Copy `example.feeder.toml`** beside the manifest, renamed to its stem, and set `tour`,
   `url`, `prepare`, and the labels. The ready source must admit a card carrying the loop's
   `labels`: on GitHub every `[ready] labels` value must also be in `test_loop.labels`, and
   `relay test` refuses the sidecar when one is missing. Under a Jira query or a ready command,
   check by hand that a card carrying those labels is returned, since the feeder notifies once
   and builds nothing when a confirmed card is never offered. Keep `attended` in `[deny]
   labels`, so the loop's planning cards stay a person's.
7. **Run one pass by hand, report only**, before the feeder does:

```bash
python3 skills/relay/scripts/relay_cli.py test <manifest> --tour --report-only
```

   It writes every finding to `<stem>.findings.md` and touches no tracker. Read it, and read the
   pass record it prints last, `<stem>.test/pass-<n>.json`. When the findings look right, start
   the feeder the way section 11 of `docs/manifest-authoring.md` says, pinned.

## A `prepare` to start from

This moves the app's worktree to the commit, restarts the server there with its outbound
integrations stubbed, and waits until the app reports that commit. It assumes the app serves its
commit at `/__version`, which is the health check the loop needs; add one to the app if it has
none. Save it in the app's repository as `tools/prepare_app.py`.

```python
"""Move the app under test to RELAY_TEST_COMMIT and confirm it serves that commit."""
import json, os, signal, subprocess, sys, time, urllib.request

HOME = os.path.expanduser("~/.example-app")
TREE = os.path.join(HOME, "tree")          # the app's own worktree, outside the checkout
PID = os.path.join(HOME, "server.pid")
commit, url = os.environ["RELAY_TEST_COMMIT"], os.environ["RELAY_TEST_URL"]

os.makedirs(HOME, exist_ok=True)
if not os.path.isdir(TREE):
    subprocess.run(["git", "worktree", "add", "--detach", TREE, commit], check=True)
else:
    subprocess.run(["git", "-C", TREE, "checkout", "--detach", commit], check=True)

def ours(pid):
    """True when pid is still the server this script started, not a process that reused it."""
    shown = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                           text=True).stdout
    return "example_app" in shown


def alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


try:                                        # stop the server this script started last time
    with open(PID) as handle:
        old = int(handle.read())
except (OSError, ValueError):
    old = None
if old and ours(old):
    try:
        os.killpg(old, signal.SIGTERM)
        for _ in range(30):                 # wait for it to exit and free the port
            if not alive(old):
                break
            time.sleep(1)
        else:
            os.killpg(old, signal.SIGKILL)
    except ProcessLookupError:
        pass

env = dict(os.environ, EXAMPLE_OUTBOUND="stub")   # mail, payments, webhooks go to local files
with open(os.path.join(HOME, "server.log"), "a") as log:
    server = subprocess.Popen(["python3", "-m", "example_app", "--port", "8765"], cwd=TREE,
                              env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                              start_new_session=True)   # outlives this script, by design
with open(PID, "w") as handle:
    handle.write(str(server.pid))

deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url + "/__version", timeout=5) as response:
            reply = json.load(response)
        served = reply.get("commit") if isinstance(reply, dict) else None
        if served == commit:
            print("serving %s" % commit)
            sys.exit(0)
        print("serving %s, not %s" % (served, commit))
    except (OSError, ValueError) as exc:    # not up yet, or a page that is not the version
        print("not up yet: %s" % exc)
    time.sleep(2)
print("the app did not serve %s within two minutes" % commit)
sys.exit(1)
```

The server is started in a session of its own, so it survives `prepare` exiting 0. That also puts
it outside the process group the pass ends when `prepare` times out or the lease is lost: a
`prepare` ended that way can leave the server running, at the old commit or a half moved one,
and the next `prepare` stops it through the pid file before starting the new one. The pid is
checked against the server's command before it is signalled, so a pid the system has since
given to another process is left alone. The server's output goes to its own log, not to the
prepare log the pass keeps.

## How it stops

After each pass the feeder asks these in this order, and the first that holds stops the loop,
named by its own reason word in the log, the `test_loop_stopped` event, one notice, and
`feed <manifest> --status`:

- `report_only`: a report only loop ran its one full tour.
- `clean`: a full tour found nothing above low.
- `budget`: the loop filed `max_cards_total` cards, 30 by default.
- `open_findings`: a full tour's high and medium findings produced no new card, because each
  went to an open card, a stopped area, or was never confirmed.
- `round_cap`: `max_rounds` full tours ran, six by default.
- `clock_cap`: `max_hours` passed since the loop's first pass, 24 by default.

After it stops the feeder goes on building the cards already filed, under its ordinary rules,
and starts no further pass.

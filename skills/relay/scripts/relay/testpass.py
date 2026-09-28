"""One Test pass end to end (browser test loop plan, U5; KTD1, KTD2, KTD6, KTD7, KTD8, KTD12).

`relay test <manifest>` is one pass: a full tour of the tour document or a check of named landed
cards, filing what it finds or reporting only. The Feeder starts it as a subprocess and reads
the pass record it leaves beside the Manifest (KTD2), and an operator runs the same verb by hand
(R24). Two launched processes do the work and the code between them decides (KTD1): the Test
process finds and reports, holding no tracker write tool; this module applies the per pass cap,
the loop budget, the lows rule, the stopped areas, and report only mode; and only then a Filing
process is launched with exactly the findings chosen, through `filing.run`. Nothing here writes
to a tracker: every tracker write is a sentence handed to that process (R12).

Where each process runs (KTD6). The Test process runs in a detached worktree of the tested
commit, placed under the state directory and removed when the pass ends, never in the checkout
the Runner merges into; the pass records that checkout's HEAD and status before and compares
them after, and fails the pass on any change. The Filing process runs in the checkout like a
Closeout, and its commit is bounded to the tracker file by the Closeout's own scope check, which
runs only while the pass still holds the Lease, as the Closeout's does.

The app is prepared in code (KTD7): the sidecar's `prepare` argument list runs in its own
process group under a timeout with `RELAY_TEST_COMMIT`, `RELAY_TEST_URL`, and `RELAY_TEST_REPO`
in its environment, from a directory under the state directory rather than the checkout, and
anything but exit 0, or a checkout it left changed, records the pass as `not_run`. A launched
process cannot stop a server, so moving and restarting it lives only here.

A pass takes both Leases for its whole length and renews them on the heartbeat, as `run` does
(KTD8). A pass is not a Task and writes no Task record: its outcomes are the pass record's own
`status` words, `ran`, `not_run`, and `failed`, plus notes, so the closed halt class set in
`contracts.py` is untouched (KTD12). `ran` means the Test process reported and, when there was
something to file, the Filing process ended with a readable `relay-filed` block in bounds; a
Filing step that did not complete fails the pass with its own sentence (`filing_failures`), so
the Feeder never reads a filing failure as findings that produced no card.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field, replace

from . import (adapters, contracts, feeder, filing, gitread, gitwrite, launch,
               manifest as manifest_module, state, testbrief, testloop, worktree)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_HALTED = 2
EXIT_LEASE = 3

# The one denial a Test process gets beyond the Manifest's list and the Closeout's push
# spellings (KTD6): the tracker's command line tool, so a tour cannot file, comment, or move a
# card by any route while the pass code has not yet chosen what to file.
TEST_DISALLOWED_EXTRA = contracts.CLOSEOUT_DISALLOWED_EXTRA + ("Bash(gh *)",)

# A pass's own status words, from the rules module (KTD12).
RAN = testloop.RAN
NOT_RUN = testloop.NOT_RUN
FAILED = testloop.FAILED

# One more outcome word beside `testloop`'s: a finding whose area is not a heading of the tour
# document, or whose shape the validator refused, recorded and never filed (KTD4).
OUTCOME_INVALID = "invalid"

# The environment `prepare` reads (KTD7). The checkout's path is there because `prepare` runs
# outside it (issue #117), so a command that needs the repository reaches it by this path.
ENV_COMMIT = "RELAY_TEST_COMMIT"
ENV_URL = "RELAY_TEST_URL"
ENV_REPO = "RELAY_TEST_REPO"

PASS_DIR_SUFFIX = ".test"
LOWS_SUFFIX = ".lows.md"
FINDINGS_SUFFIX = ".findings.md"
PASS_PREFIX = "pass-"
# The working directory `prepare` runs in, under the state directory (issue #117).
PREPARE_DIR = "prepare"

# The planning finding of R19, synthesized for each `--plan-area` outside the cap. Its cause is
# the tour document itself, at the area's heading, and its verdict is intended: nothing is
# broken in the code that a builder should fix, a person is asked to plan.
PLAN_TITLE = "Plan the %s area after repeated patches"
PLAN_STEP = "Read the %s section of the tour document and the loop's cards for that area"
PLAN_EXPECTED = "A person decides what the area should do before any further card is built"
PLAN_DONE_WHEN = "The %s area has a plan a person wrote, and the loop's cards for it are triaged"


@dataclass(frozen=True)
class PassPaths:
    """Where a pass writes beside the Manifest: the pass records under `<stem>.test/`, the lows
    file, and the findings file of report only mode (the plan's Assumptions)."""
    directory: str
    lows: str
    findings: str


def paths_for(manifest_path):
    stem = os.path.splitext(os.path.abspath(manifest_path))[0]
    return PassPaths(directory=stem + PASS_DIR_SUFFIX, lows=stem + LOWS_SUFFIX,
                     findings=stem + FINDINGS_SUFFIX)


def next_number(directory):
    """The next free pass number under `directory`: one past the highest `pass-<n>.json`."""
    highest = 0
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return 1
    for name in names:
        if name.startswith(PASS_PREFIX) and name.endswith(".json"):
            digits = name[len(PASS_PREFIX):-len(".json")]
            if digits.isdigit():
                highest = max(highest, int(digits))
    return highest + 1


@dataclass(frozen=True)
class Request:
    """What one invocation of the verb asks for. `kind` is `testloop.TOUR` or `testloop.CHECK`;
    `cards` the landed ids a check looks at; `budget` the loop's cards left, or None for the
    sidecar's `max_cards_total`; `model` and `effort` override the sidecar's for the Test
    process only."""
    kind: str = testloop.TOUR
    cards: tuple = ()
    report_only: bool = False
    stopped_areas: tuple = ()
    plan_areas: tuple = ()
    budget: int | None = None
    model: str | None = None
    effort: str | None = None


@dataclass
class Outcome:
    """What `run` returns: the exit code, the pass record as written, and its path. `message`
    is the refusal sentence when nothing was written."""
    exit_code: int
    record: dict = field(default_factory=dict)
    path: str | None = None
    message: str | None = None


def backend_for(manifest):
    """The backend the loop's processes run on: the Manifest's `[defaults] backend`, else
    claude. A Test pass runs on claude only in this build (the plan's Assumptions)."""
    defaults = manifest.raw.get("defaults") if isinstance(manifest.raw, dict) else None
    if isinstance(defaults, dict) and "backend" in defaults:
        return str(defaults["backend"])
    return manifest_module.DEFAULT_BACKEND


def refusal(manifest, config, request):
    """The sentence a pass is refused with before it takes a Lease, or None. Every refusal is a
    configuration problem the operator fixes in the sidecar or on the command line, so the
    verb exits 1 on it and writes nothing."""
    loop = config.test_loop
    if not loop.enabled:
        return ("the feeder sidecar beside %s has no [test_loop] table with enabled = true; "
                "relay test runs only under a loop that is switched on" % manifest.path)
    backend = backend_for(manifest)
    if backend != manifest_module.DEFAULT_BACKEND:
        return ("the browser test loop runs its Test and Filing processes on %s only in this "
                "build, and this manifest's default backend is %s"
                % (manifest_module.DEFAULT_BACKEND, backend))
    if manifest.tracker.adapter == "markdown" and manifest_module.pushes(manifest):
        return ("a markdown tracker pairs only with a manifest that does not push: the adapter "
                "reads the tracker at the remote when the manifest pushes, and the loop never "
                "pushes, so every filing would read as unconfirmed; set [shipping] push = false")
    if manifest.tracker.adapter == "github" and not config.ready_command:
        ready_labels = [str(name) for name in config.ready_source.get("labels") or []]
        if not ready_labels:
            return "no ready labels are configured; set [ready] labels in the feeder sidecar"
        missing = [name for name in ready_labels if name not in loop.labels]
        if missing:
            return ("test_loop.labels lacks the [ready] labels value %s, so a card the loop "
                    "files would never reach the ready source" % ", ".join(missing))
    if request.model and request.model not in config.allowed_models:
        return "--model %s is not in models.allowed" % request.model
    if request.kind == testloop.CHECK and not request.cards:
        return "a check pass names at least one landed card with --cards"
    if request.budget is not None and (isinstance(request.budget, bool)
                                       or request.budget < 0):
        return "--budget must be zero or a positive integer"
    # The Filing process runs in the checkout and its commit is bounded by the Closeout's
    # scope check, which resets the tree to bound it (code review). A tree that was dirty
    # before the pass would lose that work to the reset, so the pass asks for what the Runner
    # and the Feeder ask for: the default branch, clean.
    problem = feeder.checkout_problem(manifest)
    if problem:
        return "%s; relay test runs only in a clean checkout on its default branch" % problem
    return None


def _flat(name):
    return " ".join(str(name).split())


def _read_tour(manifest, config):
    """The tour document's text from the checkout, or None with the sentence when it is not
    there. The checkout is clean and on the default branch by the time this runs, so the file
    is the committed one, and the same text a process reading the tested commit would see."""
    path = os.path.join(manifest.project.repo, config.test_loop.tour)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return None, "the tour document %s could not be read: %s" % (config.test_loop.tour, exc)
    if not text.strip():
        return None, "the tour document %s is empty" % config.test_loop.tour
    return text, None


def _area_problem(flag, names, headings):
    """The sentence for a `--stopped-area` or `--plan-area` that is not a heading, or None.
    The names are compared flattened, as `testbrief.headings` flattens the headings."""
    for name in names:
        if _flat(name) not in headings:
            return "%s %r is not a heading of the tour document" % (flag, name)
    return None


def _read_cards(adapter, ids):
    """The landed cards a check pass sends, read through the adapter, or None with the sentence
    naming the first id that could not be read."""
    cards = []
    for card_id in ids:
        try:
            card = adapter.read(str(card_id)) or {}
        except Exception as exc:
            return None, "card %s could not be read: %s" % (card_id, exc)
        if card.get("skipped"):
            return None, "card %s could not be read: %s" % (card_id, card["skipped"])
        cards.append({"id": str(card_id), "title": card.get("title") or "",
                      "description": card.get("description") or ""})
    return cards, None


def _known_ids(adapter):
    """The ids on the tracker before the Filing process runs, for `filing.confirm`'s `known`.
    Best effort: a tracker that cannot be listed leaves existence alone as the check."""
    try:
        return tuple(str(entry.get("id")) for entry in adapter.candidates() or ()
                     if entry.get("id") is not None)
    except Exception:
        return ()


def _last_line(text):
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def prepare(command, repo, commit, url, timeout_seconds, log_path, env, cwd, heartbeat=None,
            heartbeat_interval=contracts.LEASE_HEARTBEAT_SECONDS,
            grace_seconds=launch.SIGKILL_GRACE_SECONDS, tick_seconds=launch.TICK_SECONDS):
    """Run the sidecar's `prepare` (KTD7): its own process group, ended whole at the bound,
    with the commit, the url, and the checkout's path in its environment. Returns (ok,
    sentence, seconds). The whole output goes to `log_path`; the sentence carries the last
    line, which is what a `not_run` record says. The Lease heartbeat runs around it, as it does
    around the gate, and the group is ended the moment the Lease is lost (code review), as
    `launch.launch` ends a process, rather than at the command's own bound: the command is
    moving a server to a commit in a checkout another runner may now own.

    It runs in `cwd`, never in the checkout (issue #117): a relative file it leaves behind, a
    server log or a pid file, would otherwise be blamed on the Filing process by the scope check
    and then refuse every later pass and the Feeder's next cycle as a dirty tree."""
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    os.makedirs(cwd, exist_ok=True)
    # Absolute (code review): a relative Manifest path would resolve against `cwd` here.
    child_env = dict(env, **{ENV_COMMIT: commit, ENV_URL: url, ENV_REPO: os.path.abspath(repo)})
    started = time.monotonic()
    beat = launch._Heartbeat(heartbeat, heartbeat_interval)
    beat.start()
    ending = None
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            try:
                proc = subprocess.Popen(list(command), cwd=cwd, env=child_env,
                                        stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                text = "prepare could not run: %s" % exc
                log.write(text)
                return False, text, time.monotonic() - started
            pgid = proc.pid
            deadline = started + timeout_seconds
            try:
                while proc.poll() is None:
                    if beat.lost:
                        ending = "the lease was lost while prepare ran"
                        break
                    if time.monotonic() >= deadline:
                        ending = "prepare timed out after %d seconds" % timeout_seconds
                        break
                    try:
                        proc.wait(timeout=tick_seconds)
                    except subprocess.TimeoutExpired:
                        pass
            except BaseException:
                launch._kill_group(proc, grace_seconds, pgid)
                raise
            if ending:
                launch._kill_group(proc, grace_seconds, pgid)
    finally:
        beat.stop()
    seconds = time.monotonic() - started
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        captured = handle.read()
    last = _last_line(captured) or "(none)"
    if ending:
        return False, "%s; last output: %s" % (ending, last), seconds
    if proc.returncode != 0:
        return False, "prepare exited %d; last output: %s" % (proc.returncode, last), seconds
    return True, "", seconds


def _rev_parse(repo, ref):
    """`gitread.rev_parse`, which answers None for a ref that does not resolve, and here also
    None when git itself fails, so every caller has one shape to check (code review)."""
    try:
        return gitread.rev_parse(repo, ref)
    except gitread.GitError:
        return None


def _checkout_state(repo):
    return {"head": _rev_parse(repo, "HEAD"),
            "status": gitread.status_porcelain(repo)}


def plan_finding(area, tour, tour_path):
    """The attended planning finding for one area at the patch cap (R19), marked with
    `filing.ATTENDED_KEY` so the Filing brief tells the process to label it."""
    line = 1
    for number, text in enumerate(tour.splitlines(), 1):
        if area in testbrief.headings(text):
            line = number
            break
    return {
        "title": PLAN_TITLE % area,
        "severity": testloop.HIGH,
        "kind": "improvement",
        "area": area,
        "design": False,
        "cause": {"file": tour_path, "line": line, "verdict": "intended"},
        "steps": [PLAN_STEP % area],
        "expected": PLAN_EXPECTED,
        "observed": "",
        "done_when": [PLAN_DONE_WHEN % area],
        "card": None,
        filing.ATTENDED_KEY: True,
    }


def check_findings(findings, headings, kind, sent):
    """KTD4's checks in code, on the findings of a parsed report, whose shape
    `testbrief.parse` has already checked. Returns (valid, invalid), where each invalid entry
    is `(finding, problem)`. A finding names an area exactly as a heading spells it, flattened
    to one line; on a check pass a finding names one of the cards sent, and one that does not
    is kept, marked, and takes the last generation downstream through
    `testloop.generation_for`, which the Feeder applies. The mark is `card_sent` on the finding
    itself, so the record carries what the pass could tell."""
    valid, invalid = [], []
    sent_ids = {str(card) for card in sent}
    for finding in findings:
        area = _flat(finding.get("area"))
        if area not in headings:
            invalid.append((finding, "area %r is not a heading of the tour document"
                            % finding.get("area")))
            continue
        checked = dict(finding, area=area)
        if kind == testloop.CHECK:
            card = finding.get("card")
            checked["card_sent"] = card is not None and str(card) in sent_ids
        valid.append(checked)
    return valid, invalid


def _finding_entry(finding, outcome, problem=None):
    entry = {"title": finding.get("title"), "severity": finding.get("severity"),
             "kind": finding.get("kind"), "area": finding.get("area"),
             "design": finding.get("design"), "card": finding.get("card"),
             "cause_file": (finding.get("cause") or {}).get("file")
             if isinstance(finding.get("cause"), dict) else None,
             "attended": finding.get(filing.ATTENDED_KEY) is True,
             "outcome": outcome}
    if "card_sent" in finding:
        entry["card_sent"] = finding["card_sent"]
    if problem:
        entry["problem"] = problem
    return entry


def _lows_text(number, kind, commit, lows):
    lines = ["## Pass %d, %s of %s" % (number, kind, commit[:12]), ""]
    for finding in lows:
        cause = finding.get("cause") or {}
        lines.append("- %s (%s, %s line %s)" % (finding.get("title"), finding.get("area"),
                                                cause.get("file"), cause.get("line")))
    return "\n".join(lines) + "\n\n"


def _findings_text(number, kind, commit, entries):
    lines = ["## Pass %d, %s of %s, report only" % (number, kind, commit[:12]), ""]
    for finding, outcome in entries:
        cause = finding.get("cause") or {}
        lines.append("- [%s] %s: %s (%s, %s line %s, %s)"
                     % (finding.get("severity"), outcome, finding.get("title"),
                        finding.get("area"), cause.get("file"), cause.get("line"),
                        cause.get("verdict")))
        for step in finding.get("steps") or ():
            lines.append("  - step: %s" % step)
        lines.append("  - expected: %s" % finding.get("expected"))
        lines.append("  - observed: %s" % " ".join(str(finding.get("observed") or "").split()))
        for line in finding.get("done_when") or ():
            lines.append("  - done when: %s" % line)
    return "\n".join(lines) + "\n\n"


def _append(path, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def filing_failures(result, scope, allowed, pre_head, timeout_seconds):
    """The sentences for every way the Filing step did not complete, most telling first, or an
    empty list when a readable `relay-filed` block was read from a process that ran to its end
    in bounds (issue #115). Five causes: the Lease was lost while it ran, the process could not
    be launched, it timed out, its block could not be read, or the scope check reset its
    commit. The first is the pass's reason and the rest are its notes, so a reset of the
    checkout is on the record even when a timeout is the headline (code review). Each is a
    filing failure and not an account of the findings: a pass recorded `ran` with no new card
    on one of these would stop the loop on open findings that no card ever answered, so the
    pass is `failed`, which the Feeder notifies once and counts as no round. `scope` is None
    when the Lease was lost, since no scope check runs then (issue #117), so a lost Lease
    headlines as the reason the checkout went unchecked."""
    launched = result.launch_result
    sentences = []
    if launched.lease_lost:
        sentences.append("the lease was lost while the filing process ran")
    if launched.launch_error:
        sentences.append("the filing process could not be launched: %s" % launched.launch_error)
    if launched.timed_out:
        sentences.append("the filing process timed out after %d seconds" % timeout_seconds)
    if not result.filed.ok and not launched.timed_out:
        # A timeout writes itself as the block's error, so that sentence is the timeout's.
        sentences.append("the filing process's block could not be read: %s" % result.filed.error)
    if scope is not None and not scope.ok:
        # Two shapes, as `_run_closeout` reads them: a path outside the bound, or a change
        # inside it left uncommitted. Both reset, and the sentence says which (code review).
        if scope.offending:
            what = "changed %s in the checkout, outside %s" % (
                ", ".join(scope.offending), ", ".join(allowed) or "any path")
        else:
            what = "left %s changed and uncommitted in the checkout" % (
                ", ".join(scope.changed) or "the tree")
        sentences.append("the filing process %s; the checkout was reset to %s and nothing it "
                         "filed there counts" % (what, pre_head[:12]))
    return sentences


def run(manifest, config, request, env, out=None, home=None, adapter=None, now=time.time,
        launch_kwargs=None, prepare_kwargs=None, timeout_overrides=None):
    """One pass. Returns an `Outcome`; never raises for anything the pass record can say.

    `env` is the run level environment for git, the adapter, and `prepare`; the launched
    processes get their own copy through `launch.child_env`. `launch_kwargs` reach both
    launches, the way `run.py` passes its own through, `prepare_kwargs` reach `prepare`, and
    `timeout_overrides` may carry `test_seconds` in place of the sidecar's minutes and
    `filing_seconds` in place of the Manifest's closeout minutes; all three are the suite's
    way in."""
    stream = (lambda line: out.write(line + "\n")) if out is not None else (lambda line: None)
    launch_kwargs = dict(launch_kwargs or {})
    prepare_kwargs = dict(prepare_kwargs or {})
    repo = manifest.project.repo
    # Area names are flattened once here, so the rules, the brief, and the record all compare
    # the spelling `testbrief.headings` uses (code review). The sidecar's report only setting
    # and the flag are one switch (R23).
    request = replace(request,
                      stopped_areas=tuple(_flat(name) for name in request.stopped_areas),
                      plan_areas=tuple(_flat(name) for name in request.plan_areas),
                      report_only=bool(request.report_only or config.test_loop.report_only))

    sentence = refusal(manifest, config, request)
    if sentence:
        return Outcome(EXIT_CONFIG, message=sentence)
    try:
        adapter = adapter or adapters.build(manifest, env=env)
    except adapters.ConfigurationError as exc:
        return Outcome(EXIT_CONFIG, message=str(exc))
    tour, sentence = _read_tour(manifest, config)
    if sentence:
        return Outcome(EXIT_CONFIG, message=sentence)
    headings = testbrief.headings(tour)
    sentence = (_area_problem("--stopped-area", request.stopped_areas, headings)
                or _area_problem("--plan-area", request.plan_areas, headings))
    if sentence:
        return Outcome(EXIT_CONFIG, message=sentence)
    cards = ()
    if request.kind == testloop.CHECK:
        cards, sentence = _read_cards(adapter, request.cards)
        if sentence:
            return Outcome(EXIT_CONFIG, message=sentence)

    store = state.StateStore(manifest.path, repo, home=home)
    acquired = store.acquire()
    if acquired.code == state.LOCKED:
        holder = acquired.holder or {}
        return Outcome(EXIT_LEASE, message=(
            "another runner holds the lease: pid %s on %s, manifest %s, heartbeat %.0f "
            "seconds old" % (holder.get("holder_pid"), holder.get("hostname"),
                             acquired.other_manifest or holder.get("manifest"),
                             acquired.age_seconds or 0)))
    try:
        return _pass(manifest, config, request, env, stream, home, adapter, store, tour,
                     headings, cards, now, launch_kwargs, prepare_kwargs,
                     dict(timeout_overrides or {}))
    finally:
        store.release()


def _pass(manifest, config, request, env, stream, home, adapter, store, tour, headings, cards,
          now, launch_kwargs, prepare_kwargs, overrides):
    loop = config.test_loop
    repo = manifest.project.repo
    test_seconds = overrides.get("test_seconds") or loop.timeout_minutes * 60
    paths = paths_for(manifest.path)
    os.makedirs(paths.directory, exist_ok=True)
    number = next_number(paths.directory)
    pass_id = "%s%d" % (PASS_PREFIX, number)
    record_path = os.path.join(paths.directory, pass_id + ".json")
    default = feeder.default_branch_of(manifest)
    started_at = now()
    sent = tuple(card["id"] for card in cards)
    record = {
        "pass": number, "id": pass_id, "kind": request.kind, "cards": list(sent),
        "url": loop.url, "commit": None, "status": None, "reason": "",
        "report_only": bool(request.report_only), "stopped_areas": list(request.stopped_areas),
        "plan_areas": list(request.plan_areas), "budget": request.budget,
        "findings": [], "filed": [], "commented": [], "over_cap": [], "over_budget": [],
        "dropped": [], "lows": [], "invalid": [], "planning": [], "approval_steps": [],
        "notes": [], "transcripts": {"test": None, "filing": None},
        # The file each process's block was actually read from: its transcript, or its stdout
        # log when the transcript was not at the predicted path (issue #113). `transcripts`
        # stays the launcher's answer, so the two can differ and a reader can see the fallback.
        "read_from": {"test": None, "filing": None},
        "briefs": {"test": None, "filing": None}, "checkout": {}, "worktree_removed": None,
        "timings": {"started_at": state._iso(started_at), "ended_at": None,
                    "prepare_seconds": None, "test_wall_seconds": None,
                    "test_active_seconds": None, "filing_wall_seconds": None},
    }

    def finish(status, reason="", exit_code=None):
        record["status"] = status
        record["reason"] = reason
        record["timings"]["ended_at"] = state._iso(now())
        with open(record_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        stream("pass %d %s%s" % (number, status, (": " + reason) if reason else ""))
        stream(record_path)
        if exit_code is None:
            exit_code = EXIT_OK if status == RAN else EXIT_HALTED
        return Outcome(exit_code, record=record, path=record_path)

    commit = _rev_parse(repo, default)
    if commit is None:
        return finish(FAILED, "the default branch %s does not resolve to a commit" % default)
    record["commit"] = commit

    # KTD7. The app is moved to the commit and confirmed by the operator's own command, run
    # from a directory under the state directory rather than the checkout (issue #117).
    ok, sentence, seconds = prepare(loop.prepare, repo, commit, loop.url,
                                    loop.prepare_timeout_seconds,
                                    store.path("logs", pass_id + ".prepare.log"), env,
                                    store.path(PREPARE_DIR),
                                    heartbeat=store.heartbeat,
                                    heartbeat_interval=launch_kwargs.get(
                                        "heartbeat_interval", contracts.LEASE_HEARTBEAT_SECONDS),
                                    **prepare_kwargs)
    record["timings"]["prepare_seconds"] = round(seconds, 3)
    if not ok:
        return finish(NOT_RUN, sentence)
    # The refusal saw a clean tree on the default branch at `commit`, so anything different now
    # is `prepare`'s, reached through `RELAY_TEST_REPO`. Caught here, before the snapshot, it is
    # named as `prepare`'s and not blamed on the Filing process by the scope check, whose reset
    # would leave an untracked file behind, nor filed onto a branch the Runner never merges.
    try:
        branch = gitread.current_branch(repo)
        head = _rev_parse(repo, "HEAD")
        changed, _ = gitread.status_paths(repo)
    except gitread.GitError as exc:
        return finish(FAILED, "the checkout could not be read after prepare: %s" % exc)
    if branch != default or head != commit:
        return finish(NOT_RUN, "prepare moved the checkout to %s at %s, from %s at %s; prepare "
                               "must leave the checkout where it found it"
                               % (branch, str(head)[:12], default, commit[:12]))
    if changed:
        return finish(NOT_RUN, "prepare left the checkout changed at %s; the pass runs only in a "
                               "clean checkout, so remove it and have prepare write outside the "
                               "checkout" % changed[0])

    # KTD6. The checkout is recorded, the Test process runs in a worktree of its own, and the
    # checkout is compared after.
    try:
        before = _checkout_state(repo)
    except gitread.GitError as exc:
        return finish(FAILED, "the checkout could not be read: %s" % exc)
    record["checkout"] = {"head_before": before["head"], "status_before": before["status"],
                          "head_after": None, "status_after": None}
    try:
        text = testbrief.render(request.kind, loop.url, commit, tour, cards=cards,
                                stopped_areas=request.stopped_areas)
    except ValueError as exc:
        return finish(FAILED, "the test brief could not be rendered: %s" % exc)
    brief_path = store.path("briefs", pass_id + ".test.md")
    with open(brief_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(brief_path, 0o600)
    record["briefs"]["test"] = brief_path

    task = manifest_module.Task(id=pass_id, model=request.model or config.test_model,
                                effort=request.effort or config.test_effort, excluded=False,
                                reason=None, backend=manifest_module.DEFAULT_BACKEND)
    # Both launches heartbeat on the pass's own Lease and release nothing on an interrupt:
    # the `finally` in `run` releases once, after the worktree is gone.
    kwargs = dict(launch_kwargs, heartbeat=store.heartbeat, on_release=None)
    dest = worktree.path_for(store, pass_id)
    launched, not_added = None, None
    try:
        try:
            worktree.add(repo, dest, commit, env=env)
        except worktree.WorktreeError as exc:
            not_added = str(exc)
        else:
            launched = launch.launch(
                manifest, task, text, store.path("logs", pass_id + ".test.stdout.log"),
                test_seconds, allowed=tuple(loop.allowed_tools),
                disallowed=TEST_DISALLOWED_EXTRA, cwd=dest, home=home, base_env=env,
                stream=stream, **dict(kwargs, host_probe=None))
    finally:
        # Before any record is written (code review): the record the Feeder reads from disk
        # has to say whether the worktree is gone, and a note here has to reach it.
        removed = True
        try:
            worktree.remove(repo, dest, env=env)
        except worktree.WorktreeError as exc:
            removed = False
            record["notes"].append("the worktree at %s could not be removed: %s" % (dest, exc))
        record["worktree_removed"] = removed
    if not_added:
        return finish(FAILED, not_added)
    record["transcripts"]["test"] = launched.transcript_path
    record["timings"]["test_wall_seconds"] = round(launched.wall_seconds, 3)
    record["timings"]["test_active_seconds"] = round(launched.active_seconds, 3)

    try:
        after = _checkout_state(repo)
    except gitread.GitError as exc:
        return finish(FAILED, "the checkout could not be read after the test process: %s" % exc)
    record["checkout"]["head_after"] = after["head"]
    record["checkout"]["status_after"] = after["status"]
    if after != before:
        return finish(FAILED, "the checkout changed while the test process ran: HEAD %s to %s, "
                              "status %r to %r" % (str(before["head"])[:12],
                                                   str(after["head"])[:12],
                                                   before["status"], after["status"]))
    if launched.lease_lost:
        return finish(FAILED, "the lease was lost while the test process ran")
    if launched.launch_error:
        return finish(FAILED, launched.launch_error)
    if launched.timed_out:
        return finish(FAILED, "the test process timed out after %d seconds" % test_seconds)
    # No guard on `launched.transcript_present` here (issue #113): a CLI running under
    # `CLAUDE_CONFIG_DIR` writes its transcript where neither the prediction nor the glob
    # looks, and the stdout log holds the same final message. `testbrief.parse` reads
    # whichever file has it, and `source` is None only when neither does.
    report = testbrief.parse(launched.transcript_path, backend=task.backend,
                             log_path=launched.log_path)
    record["read_from"]["test"] = report.source
    if report.source is None:
        return finish(FAILED, "the test process left no transcript to read: %s" % report.error)
    if not report.ok:
        return finish(FAILED, report.error)
    record["approval_steps"] = list(report.approval_steps)
    if report.status == NOT_RUN:
        return finish(NOT_RUN, report.reason)

    # KTD4's checks, then the rules (KTD1, KTD3).
    valid, invalid = check_findings(report.findings, headings, request.kind, sent)
    for finding, problem in invalid:
        record["findings"].append(_finding_entry(finding, OUTCOME_INVALID, problem))
        record["invalid"].append(finding.get("title") if isinstance(finding, dict) else str(finding))
    budget = loop.max_cards_total if request.budget is None else request.budget
    selection = testloop.select_findings(valid, loop.max_cards_per_pass, budget,
                                         request.stopped_areas)
    for finding, outcome in zip(valid, selection.outcomes):
        record["findings"].append(_finding_entry(finding, outcome))
    record["over_cap"] = [item["title"] for item in selection.over_cap]
    record["over_budget"] = [item["title"] for item in selection.over_budget]
    record["dropped"] = [item["title"] for item in selection.dropped]
    record["lows"] = [item["title"] for item in selection.lows]
    planning = [plan_finding(area, tour, loop.tour) for area in request.plan_areas]
    for finding in planning:
        record["findings"].append(_finding_entry(finding, testloop.FILE))
    record["planning"] = list(request.plan_areas)
    to_file = list(selection.to_file) + planning

    if selection.lows:
        _append(paths.lows, _lows_text(number, request.kind, commit, selection.lows))
    if request.report_only:
        entries = ([(finding, outcome) for finding, outcome in zip(valid, selection.outcomes)]
                   + [(finding, testloop.FILE) for finding in planning])
        _append(paths.findings, _findings_text(number, request.kind, commit, entries))
        return finish(RAN)
    if not to_file:
        return finish(RAN)

    # KTD5. The Filing process, in the checkout, bounded to the tracker file.
    known = _known_ids(adapter)
    pre_head = _rev_parse(repo, "HEAD")
    if pre_head is None:
        return finish(FAILED, "the checkout's HEAD does not resolve to a commit before filing")
    filing_seconds = overrides.get("filing_seconds") or manifest.timeouts.closeout_minutes * 60
    result = filing.run(manifest, to_file, adapter, store, task.backend, pass_id,
                        labels=loop.labels, design_note=loop.design_note, home=home,
                        base_env=env, stream=stream, timeout_seconds=filing_seconds, **kwargs)
    record["transcripts"]["filing"] = result.launch_result.transcript_path
    record["read_from"]["filing"] = result.filed.source
    record["briefs"]["filing"] = result.brief_path
    record["timings"]["filing_wall_seconds"] = round(result.launch_result.wall_seconds, 3)
    for finding in result.findings:
        record["notes"].append("filing process: %s" % json.dumps(finding, sort_keys=True))
    allowed = ([manifest.tracker.file] if manifest.tracker.adapter == "markdown"
               and manifest.tracker.file else [])
    # The scope check runs whatever the process did, so a commit outside the bound is reset
    # before anything is read back or recorded. Except on a lost Lease, which is read first,
    # as `run.py`'s Closeout path reads it (issue #117): another runner may have merged into
    # the checkout meanwhile, the diff from `pre_head` would name its paths as out of scope,
    # and the reset would remove its merge. The checkout is left for whoever holds the Lease.
    lease_lost = result.launch_result.lease_lost
    scope = None
    if lease_lost:
        record["notes"].append("the checkout was left as the filing process ended, with no scope "
                               "check and no reset, since another runner may hold it now")
    else:
        scope = gitwrite.closeout_scope_check(repo, pre_head, allowed, ops=store,
                                              task_id=pass_id, env=env)
    failures = filing_failures(result, scope, allowed, pre_head, filing_seconds)
    record["notes"].extend(failures[1:])
    # The confirmation runs on a failed filing too, over whatever entries were read. Only the
    # scope reset leaves a readable block, since the other causes end the process before its
    # final message; on a tracker outside the checkout the cards that block names exist, and
    # the record and the Feeder's filed map say so rather than letting the next tour file
    # them again.
    confirmation = filing.confirm(result.filed.entries, adapter, known=known)
    record["notes"].extend(confirmation.notes)
    by_number = {index: finding for index, finding in enumerate(to_file, 1)}
    for entry in confirmation.filed:
        finding = by_number.get(entry["finding"], {})
        record["filed"].append({
            "id": entry["id"], "finding": entry["finding"], "area": finding.get("area"),
            "design": finding.get("design") is True,
            "cause_file": (finding.get("cause") or {}).get("file"),
            "card": finding.get("card"), "card_sent": finding.get("card_sent"),
            "attended": finding.get(filing.ATTENDED_KEY) is True,
        })
    for entry in confirmation.commented:
        record["commented"].append({"id": entry["id"], "finding": entry["finding"]})
    for item in record["findings"]:
        if item["outcome"] != testloop.FILE:
            continue
        for entry in confirmation.filed + confirmation.commented:
            if by_number.get(entry["finding"], {}).get("title") == item["title"]:
                item["action"] = entry["action"]
                item["filed_id"] = entry["id"]

    # KTD5's last step: one ready read, and a note for every confirmed card it does not return.
    if record["filed"]:
        record["notes"].extend(_ready_notes(manifest, config, env, adapter,
                                            [entry["id"] for entry in record["filed"]]))
    # `ran` only when a readable block was confirmed (issue #115): a pass that filed nothing
    # because its Filing step did not complete is not a pass whose findings went unanswered.
    if failures:
        return finish(FAILED, failures[0])
    return finish(RAN)


def _ready_notes(manifest, config, env, adapter, filed_ids):
    """The confirmed cards the ready source does not return, as one note each, or one note when
    the source could not be read. The Feeder's own read, through `feeder.read_ready`, so the
    pass and the Feeder cannot disagree about what the board offers."""
    problem = feeder.ready_source_problem(manifest, config)
    if problem:
        return ["the ready source could not be read after filing: %s" % problem]
    deps = feeder.build_deps(config, env)
    deps.build_adapter = lambda _manifest: adapter
    cards, reason = feeder.read_ready(manifest, config, deps,
                                      timeout=feeder.STATUS_READY_TIMEOUT_SECONDS)
    if reason is not None:
        return ["the ready source could not be read after filing: %s" % reason]
    ready_ids = {str(card.get("id")) for card in cards}
    return ["card %s was filed and confirmed, but the ready source does not return it; check "
            "that the source admits a card carrying the loop's labels" % card_id
            for card_id in filed_ids if str(card_id) not in ready_ids]

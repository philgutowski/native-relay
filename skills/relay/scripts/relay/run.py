"""The run loop (U10): one manifest, tasks in a fixed sequence of R50.

`run` is still one Task process at a time. `dispatch` overlaps one claude process with one
grok process, each in a worktree, and still merges in Manifest order.

Everything else in Relay is a piece; this is where they are ordered, and the order is the
product. The sequence after a task process exits is not negotiable and not conditional on what
that process said about itself: classify the exit from the transcript, gate the branch head,
merge, push, verify the code scope, run the closeout, check what it committed, push that, mirror,
verify the full scope, and only then delete the branch and move on. A task is landed when the
runner's own verify says so and never before (R20). Under `shipping.push = false` (issue #15)
every push in that sequence is skipped and the landing is the local default branch; each site
reads `manifest.pushes`, and the no push plan's read site table names them.

Three properties of the loop are worth naming because they are easy to lose in a refactor.

Nothing crosses between tasks except the manifest, git, and the tracker (R15). No transcript, no
summary, and no memory of a prior task reaches a later brief, and there is no variable in this
module that carries one.

Every stop is a named class with evidence, not an exception (R25, R44). The operator repairs by
hand and re-runs; the loop resumes at the first record that did not land (R32) and re-verifies
the ones that halted (R48), so a repair made between runs is picked up rather than redone.

The runner never writes to the tracker (R19). Every tracker write in this file happens inside a
closeout process the runner launched; the runner reads the result back and decides from it.
"""
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field

from . import (adapters, audit, backends, brief, classify, closeout, contracts, gitread,
               gitwrite, launch, limits, manifest as manifest_module, progress, scheduler, state,
               summary, verify, worktree)
from .adapters import github as github_adapter
from .adapters import jira as jira_adapter

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_HALTED = 2
EXIT_LEASE = 3

# Issue #71. How long past the SIGKILL a dispatch waits for a flight's thread to finish and its
# process group to empty before it names the flight as a survivor and leaves anyway.
FLIGHT_EXIT_SECONDS = 10


@dataclass
class _Run:
    """The values that are fixed for a whole run. Gathered once so a per task function takes a
    task rather than a parameter list, and so the tail's context is built by expanding this."""
    manifest: object
    adapter: object
    store: object
    repo: str
    default: str
    env: dict
    base_env: dict
    home: str
    stream: object
    retry_blocked: object      # True for every blocked record, else the ids to retry (#39)
    overrides: dict
    launch_kwargs: dict
    now: object
    allowed_paths: tuple
    used_backends: set = field(default_factory=set)
    # `--defer ID` (KTD6, R10): listed tasks this run leaves exactly as they were. Wins over
    # `retry_blocked`, which names the blocked records to launch.
    defer: frozenset = frozenset()
    # Dispatch only. SHA of the default branch after the last landing this coordinator made.
    # The merge tail compares against this so a sibling landing is not a foreign mover.
    expected_default: str | None = None
    # Dispatch-only frozen, read-only scheduling evidence.  The loop treats every edge as a
    # full-settlement dependency, not merely a build-order hint.
    schedule: object = None
    # Dispatch only (issue #71). Flights still alive when the coordinator gave up waiting for
    # them, as `{task, process_group}`. Named in the terminal record, never marked crashed.
    surviving_flights: list = field(default_factory=list)
    # Serial run only (usage limit plan, R11, KTD7). `launched_ids` is the ids `_one_task` began
    # and launched in this run; `limited_models` the models a Task among them died on with a
    # confirmed usage limit; `limit_passed_over` the listed Tasks `_begin_task` left unlaunched
    # for it, each `{task, model}`. Dispatch never fills the first two, so it passes nothing over.
    # Not `launched`: `_Context` extends this class and already has a field by that name.
    launched_ids: set = field(default_factory=set)
    limited_models: set = field(default_factory=set)
    limit_passed_over: list = field(default_factory=list)

    @property
    def release_on_interrupt(self):
        """Whether a launch on the main thread releases the lease from its interrupt handler.
        Serial `run` does. Dispatch, the one run with a schedule, does not (issue #79): its builds
        in flight are still alive when the Closeout's handler runs, so the release waits for
        `_concurrent_loop` to stop them first."""
        return self.schedule is None


@dataclass
class RunOutcome:
    exit_code: int
    halt_task: str | None = None
    halt_class: str | None = None
    message: str | None = None
    store: object = None
    records: dict = field(default_factory=dict)


class _Halt(Exception):
    """A named stop. Carries the task, the class, and the line the summary prints."""

    def __init__(self, task_id, halt_class, message, evidence=None):
        super().__init__(message)
        self.task_id = task_id
        self.halt_class = halt_class
        self.message = message
        self.evidence = evidence or {}


def _routable(manifest, adapter, digest, repo, branch, baseline_sha):
    """KTD6's second route: a missing envelope is still routable when git and the tracker carry
    a stronger completion signal than the last paragraph of a long context, which is commits on
    the task branch plus the card sitting in the manifest's in review status. A missing envelope
    with an unmoved card is stranded, never merged."""
    has_commits = (gitread.branch_exists(repo, branch)
                   and bool(gitread.log_oneline(repo, baseline_sha, branch)))
    if digest.get("routable"):
        # A complete envelope is still only a claim (R20). With nothing on the branch there is
        # nothing to merge, and the runner says so rather than trusting the claim.
        return has_commits, (None if has_commits else
                             "the envelope read complete but the branch carries no commits")
    if digest.get("findings_unavailable"):
        # R20, KTD5. The route reads the card and the branch as a stronger signal than a silent
        # process. Evidence the runner could not read is not a silent process, it is a runner
        # fault, and classifying it as one is not enough on its own: the check has to be here
        # too, or a card someone moved by hand merges work nobody ever observed.
        return False, "the evidence could not be read, so the no envelope route does not apply"
    if digest.get("halt_class") != contracts.HALT_NO_ENVELOPE:
        return False, None
    if not has_commits:
        return False, "no envelope and no commits on the branch"
    wanted = manifest.tracker.in_review_status
    try:
        card = adapter.status(digest.get("task_id") or "") or {}
    except Exception:
        card = {}
    if wanted and str(card.get("status") or "").lower() == str(wanted).lower():
        return True, "no envelope, routed on commits plus the card in %s" % wanted
    return False, "no envelope and the card did not move"


def _envelope_verdict(digest, repo, branch, baseline_sha, tree=None, tree_repo=None):
    """Issue #9: what the Task process claimed on the way out, beside what git shows it left.

    Stamped into the digest and copied onto the record, so the summary reads it from there and
    never re-parses a log. The claim is the fastest way to tell an unfinished task from a
    finished one the runner declined to merge: on the live run behind this, the findings read as
    a task that died mid work while the envelope said complete with three commits and a clean
    tree.

    `tree` is the dispatch path's snapshot, taken before its worktree was removed; without one
    the tree is read from `tree_repo`, the checkout the process ran in. A git failure costs the
    count or the tree, never the digest."""
    envelope = digest.get("envelope") or {}
    commits = current_tree = None
    try:
        if repo and branch and baseline_sha:
            # A branch the process never created carries nothing past the baseline.
            commits = (len(gitread.log_oneline(repo, baseline_sha, branch))
                       if gitread.branch_exists(repo, branch) else 0)
    except gitread.GitError:
        pass
    if tree is not None:
        current_tree = tree
    elif tree_repo:
        try:
            current_tree = "clean" if gitread.is_clean(tree_repo) else "dirty"
        except gitread.GitError:
            pass
    return {"status": envelope.get("status"), "commits": commits, "tree": current_tree,
            "evidence_read": bool(digest.get("transcript_present"))}


def _announcer(stream, notifier):
    """One phase event: a line, and a notification when the operator asked for them (R2).

    Both come from one call so the printed line and the notification cannot drift, which is the
    same shape `tail.follow` uses for the Follower's side of the same events.

    Everything is swallowed. This is a report about a run, not a part of one, so a desktop that
    refuses a notification and a stream whose pipe has closed must both leave the run's outcome
    exactly where it was (R4, KTD5). The other half of that isolation is the time bound in
    `notify.send`: an `osascript` that never returns is a failure no guard here could catch.
    """
    def announce(text):
        try:
            if stream is not None:
                stream(text)
        except Exception:
            pass
        if notifier is not None:
            try:
                notifier(text)
            except Exception:
                pass

    return announce


def _moved_line(manifest, store, task_id, after):
    """One status move as a sentence: the move, then the progress phrase, so a notification on
    the desktop says how far along the run is and not only which card moved.

    The phrase is read from the store after the move was written, which the observer's contract
    guarantees: it fires outside the lock, after `_write_locked`. Its failure costs the phrase and
    never the event, the same isolation `_announcer` gives the stream and the notifier, because
    this runs inside `store.upsert` on the run's own path and a reader that raised here would
    surface as an unexpected error on the task.
    """
    line = "%s is now %s" % (task_id, after)
    try:
        return "%s; %s" % (line, progress.phrase(progress.build(manifest, store)))
    except Exception:
        return line


def _counts_line(store, run_status):
    """The terminal record's phase event (R3). Read from the records rather than from a tally the
    loop keeps, so a status another path wrote is counted too, and short enough to read inside a
    notification body.

    Rendered through `progress.format_counts` so this line and the one `relay status` prints
    cannot drift into two vocabularies for one fact. The populations differ on purpose: this
    counts every record in the store, while `status` counts the manifest's own tasks.
    """
    counts = {}
    for record in store.records().values():
        # Same guard `state._statuses` applies. This line is built outside `announce`, so a
        # record shaped unlike the others would escape as a traceback from the run's own last
        # act rather than costing a notification.
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if status is None:
            continue
        counts[status] = counts.get(status, 0) + 1
    tally = progress.format_counts(counts)
    line = "run %s: %s" % (run_status, tally) if tally else "run %s" % run_status
    # Stale cards, R9: the one notification that ends a run says the board needs a hand. Read
    # from the store, where `_audit_cards` wrote it just before the terminal record.
    stale = ((store.audit() or {}).get("count") or 0) if hasattr(store, "audit") else 0
    if stale:
        line += "; %d stale card(s)" % stale
    return line


def _audit_cards(cfg):
    """The run end card audit (stale cards, R5). Reads every Manifest Task's card once and
    writes the findings under the Lease. Nothing here may stop the run: a failure inside the
    audit is one finding on the run, and a failure writing it costs the record and not the
    terminal record that follows."""
    observed = {}
    item_seen = {}
    try:
        findings = audit.build(cfg.manifest, cfg.store, cfg.adapter, env=cfg.env, live=False,
                               observed=observed, item_seen=item_seen)
    except Exception as exc:
        findings = [{"class": contracts.AUDIT_FAILED, "task": None,
                     "text": "the card audit failed: %s" % exc,
                     "card_status": None, "record_status": None}]
    try:
        cfg.store.write_audit(findings)
    except Exception:
        pass
    # The audit still reads and reports a deferred task's card; only the two record repairs below
    # pass it over, since a run with `--defer` leaves that record as it was (R10).
    _clear_seen_out_of_review(cfg, {task_id: status for task_id, status in observed.items()
                                    if task_id not in cfg.defer})
    _retire_confirmed_item_findings(cfg, {task_id: seen for task_id, seen in item_seen.items()
                                          if task_id not in cfg.defer})
    if cfg.stream is not None:
        for line in audit.lines(findings):
            try:
                cfg.stream(line)
            except Exception:
                pass


def _retire_confirmed_item_findings(cfg, item_seen):
    """Issue #61: `closeout.confirm_board_terminal` writes its finding once, at the landed
    Closeout, and nothing before this cleared it once the operator moved the item by hand. The
    run end audit's own read of the same item is the read that retires it, the same shape #64
    already gave the in review mark: a card the audit has now seen at the terminal status is
    one the record should stop naming. Nothing here may stop the run."""
    for task_id, seen in item_seen.items():
        if not seen:
            continue
        try:
            record = cfg.store.get(task_id)
            findings = (record or {}).get("findings") or []
            kept = [finding for finding in findings
                   if finding.get("class") != contracts.BOARD_ITEM_NOT_TERMINAL]
            if len(kept) != len(findings):
                cfg.store.upsert(task_id, findings=kept)
        except Exception:
            pass


def _clear_seen_out_of_review(cfg, observed):
    """Issue #64: the audit's read of a card is a read of the runner's own, and a card it finds
    out of review is one the runner no longer holds there, whoever moved it: a Closeout whose read
    back failed, or the operator repairing a `card_left_in_review` by hand. Clearing the mark here
    is what lets a later deliberate staging of that card stand. Nothing here may stop the run.
    `observed` holds readable cards only, so a None in it is a card read with no status, a
    GitHub item with no column (issue #78), and that is out of review too."""
    in_review = cfg.manifest.tracker.in_review_status
    for task_id, status in observed.items():
        if closeout._same(status, in_review):
            continue
        try:
            record = cfg.store.get(task_id)
            if record and record.get("card_in_review_by_run"):
                cfg.store.upsert(task_id, card_in_review_by_run=False)
        except Exception:
            pass


LEASE_POLL_SECONDS = 60


@dataclass
class _TripleWorker:
    """One immutable allocation and the private workspace/process it owns."""
    task: object
    card: dict
    expected_card: dict
    branch: str
    baseline_sha: str
    baseline_comment_id: object
    worker: object = None
    brief_text: str | None = None
    brief_sha: str | None = None
    launched: object = None
    digest: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)
    collision: dict | None = None


def _triple_halt(task_id, halt_class, message, store, env=None):
    """Write the one safe terminal shape for a batch refusal."""
    if task_id:
        store.upsert(task_id, status=contracts.STATUS_HALTED, halt_class=halt_class,
                     halt_message=message)
    _write_terminal(store, env or {}, contracts.RUN_HALTED, task_id, halt_class)
    return RunOutcome(EXIT_HALTED, task_id, halt_class, message, store, store.records())


def _triple_release(repo, leases, store, env):
    """Release every exact remote fence only after the coordinator is done with it."""
    if not leases:
        return None
    result = gitwrite.release_remote_leases(repo, leases, ops=store, env=env)
    if result.ok:
        store.clear_remote_leases()
    return result


def _triple_tracker(manifest):
    """The optional tracker-specific snapshot transport; generic adapters stay eight-method."""
    return jira_adapter if manifest.tracker.adapter == "jira" else github_adapter


def _triple_jira_start(cfg, tracker, expected):
    """Move claimed Jira cards before workers exist, checking each narrow write immediately."""
    cards = list(expected)
    for index, card in enumerate(cards):
        ok, reason = cfg.adapter._triple_transition(
            card, cfg.manifest.tracker.in_review_transition,
            cfg.manifest.tracker.in_review_status)
        if not ok:
            return None, "could not transition %s using Jira workflow label %r: %s" % (
                card["id"], cfg.manifest.tracker.in_review_transition, reason)
        observed = tracker.read_triple_snapshot(cfg.adapter, [task.id for task in cfg.manifest.tasks])
        if observed["reason"]:
            return None, observed["reason"]
        actual = observed["snapshot"]["cards"]
        wanted = dict(card)
        wanted["status"] = cfg.manifest.tracker.in_review_status
        for position, candidate in enumerate(actual):
            baseline = wanted if position == index else cards[position]
            if github_adapter.collision_evidence(baseline, candidate):
                return None, "Jira changed while coordinator transitioned %s" % card["id"]
        cards = actual
    return cards, None


def _triple_jira_label(manifest, expected_status):
    """A coordinator can never infer a Jira transition label from its target status."""
    label = dict(manifest.tracker.transition_labels).get(str(expected_status))
    if not label:
        return None, "Jira triple has no configured transition label for expected status %r" % expected_status
    return label, None


def _triple_jira_comment(cfg, tracker, item, text):
    ok, reason = cfg.adapter._triple_comment(item.expected_card, text)
    if not ok:
        return None, reason
    observed = tracker.read_triple_snapshot(cfg.adapter, [task.id for task in cfg.manifest.tasks])
    if observed["reason"]:
        return None, observed["reason"]
    card = observed["snapshot"]["cards"][[task.id for task in cfg.manifest.tasks].index(item.task.id)]
    return card, None


def _triple_worker_root(store):
    """State is outside the canonical checkout, so a worker cannot mutate it through git."""
    return store.path("workers")


def _triple_workers_stopped(workers):
    """Remote claims may be released only after every started worker group is gone."""
    for item in workers:
        launched = getattr(item, "launched", None)
        if (launched is None or not getattr(launched, "pid", None)
                or not getattr(launched, "process_group_id", None)):
            # A failed Popen has no child group.  A partial identity is unsafe: retain the
            # remote claims rather than guessing whether a descendant still owns the clone.
            if launched is not None and getattr(launched, "pid", None):
                return False
            continue
        process = gitwrite.WorkerProcess(launched.pid, launched.process_group_id)
        stopped, _reason = gitwrite.worker_process_stopped(process)
        if not stopped:
            return False
    return True


def _triple_launch_worker(cfg, item):
    """Thread target.  launch itself is synchronous; three threads make starts concurrent."""
    item.launched = launch.launch(
        cfg.manifest, item.task, item.brief_text,
        cfg.store.path("logs", item.task.id + ".stdout.log"),
        cfg.overrides.get("task_seconds") or cfg.manifest.timeouts.task_minutes * 60,
        home=cfg.home, base_env=cfg.base_env, stream=cfg.stream,
        # A lane losing its own process is not authority to release the coordinator lease:
        # two sibling lanes may still be running and the remote fence is still held.  Only the
        # coordinator's finally block releases either lease after all worker groups stop.
        heartbeat=cfg.store.heartbeat, on_release=None,
        repo=item.worker.path, **cfg.launch_kwargs)
    if not item.launched.launch_error:
        cfg.used_backends.add(item.task.backend)


def _triple_classify(cfg, item):
    """Classify from each worker's own evidence, never from the canonical checkout."""
    launched = item.launched
    if launched is None:
        item.collision = {"kind": "worker", "reason": "worker_not_launched"}
        return
    capability = backends.build(item.task.backend).CAPABILITY
    disallow = (manifest_module.resolved_disallowed(cfg.manifest)
                if not capability.enforces_at_launch else None)
    digest = classify.classify(launched.transcript_path, launched,
                               cfg.adapter.write_tool_patterns(), backend=item.task.backend,
                               disallow_patterns=disallow,
                               review_base=cfg.manifest.project.default_branch)
    digest["task_id"] = item.task.id
    # The worker's own checkout: the canonical repo has not imported its branch yet.
    worker_path = getattr(item.worker, "path", None)
    digest["envelope_verdict"] = _envelope_verdict(
        digest, worker_path, item.branch, item.baseline_sha, tree_repo=worker_path)
    item.digest = digest
    item.findings = list(digest.get("findings") or [])
    classify.write_digest(digest, cfg.store.path("digests", item.task.id + ".json"))
    cfg.store.upsert(item.task.id, session_id=launched.session_id,
                     transcript_path=launched.transcript_path,
                     wall_seconds=launched.wall_seconds, active_seconds=launched.active_seconds,
                     host_at_start=launched.host_at_start, host_at_end=launched.host_at_end,
                     findings=item.findings, binary_path=launched.binary_path, args=launched.args,
                     envelope_verdict=digest["envelope_verdict"])
    if launched.launch_error:
        item.collision = {"kind": "worker", "reason": "launch_error",
                          "detail": launched.launch_error}
    elif launched.lease_lost:
        item.collision = {"kind": "worker", "reason": "local_lease_lost"}


def _triple_integrate(cfg, item, integration_lease, expected_remote):
    """Import, rebase, gate and land one completed worker under the remote integration fence."""
    if not item.launched or not item.launched.pid or not item.launched.process_group_id:
        return None, expected_remote, _Halt(item.task.id, contracts.HALT_UNCLEAN_EXIT,
            "worker process identity is unavailable for %s" % item.task.id,
            {"branch": item.branch, "reason": "worker_process_identity_missing"})
    if not backends.build(item.task.backend).CAPABILITY.enforces_at_launch:
        allowed = manifest_module.task_allowed_paths(cfg.manifest)
        if allowed is not None:
            offenders = gitwrite.task_scope_offenders(item.worker.path, item.baseline_sha,
                                                       item.branch, allowed)
            if offenders:
                return None, expected_remote, _Halt(item.task.id, contracts.HALT_PATH_GATE,
                    "worker branch touched paths outside its declared bound",
                    {"branch": item.branch, "paths": ", ".join(offenders)})
    process = gitwrite.WorkerProcess(item.launched.pid, item.launched.process_group_id)
    imported = gitwrite.import_worker_branch(cfg.repo, item.worker, process, ops=cfg.store,
                                              task_id=item.task.id, env=cfg.env)
    if not imported.ok:
        return None, expected_remote, _Halt(item.task.id, contracts.HALT_UNCLEAN_EXIT,
            "could not safely import %s: %s" % (item.task.id, imported.reason),
            {"branch": item.branch, "reason": imported.reason})
    rebased = gitwrite.rebase_worker_branch(cfg.repo, item.branch, cfg.default, ops=cfg.store,
                                             task_id=item.task.id, env=cfg.env)
    if not rebased.ok:
        return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
            "could not rebase %s onto the current landing base" % item.task.id,
            {"branch": item.branch, "merge_output": rebased.output[-2000:]})
    landing_base = gitread.rev_parse(cfg.repo, cfg.default)
    tail = gitwrite.local_merge_tail(
        cfg.repo, item.task.id, cfg.default, landing_base, list(cfg.manifest.gate.command),
        cfg.store.path("gate", item.task.id + ".log"), ops=cfg.store, env=cfg.env,
        gate_timeout_seconds=cfg.overrides.get("gate_seconds"),
        still_ours=cfg.store.heartbeat, branch=item.branch, pushes=False)
    if not tail.ok:
        return None, expected_remote, _Halt(item.task.id, tail.halt_class,
            summary.cause_line(tail.halt_class, tail.evidence), tail.evidence)
    pushed = gitwrite.renew_integration_lease_and_push(
        cfg.repo, cfg.default, expected_remote, integration_lease, ops=cfg.store,
        task_id=item.task.id, env=cfg.env, timeout=cfg.overrides.get("gate_seconds"))
    if not pushed.ok:
        return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
            "guarded landing push refused for %s: %s" % (item.task.id, pushed.reason),
            {"branch": cfg.default, "reason": pushed.reason, "push_output": pushed.output[-2000:]})
    cfg.store.update_integration_lease(pushed.integration_lease)
    cfg.store.upsert(item.task.id, landing_ref=tail.merge_sha)
    ctx = _Context(task=item.task, card=item.card, branch=item.branch,
                   baseline_sha=landing_base, baseline_comment_id=item.baseline_comment_id,
                   digest=item.digest, launched=item.launched, findings=item.findings, **vars(cfg))
    closeout_push = {}

    def guarded_closeout_push():
        result = gitwrite.renew_integration_lease_and_push(
            cfg.repo, cfg.default, tail.merge_sha, pushed.integration_lease, ops=cfg.store,
            task_id=item.task.id, env=cfg.env, timeout=cfg.overrides.get("gate_seconds"))
        if result.ok:
            closeout_push["lease"] = result.integration_lease
            cfg.store.update_integration_lease(result.integration_lease)
        return result

    _run_closeout(ctx, closeout.OUTCOME_LANDED, landing_ref=tail.merge_sha,
                  commit_range="%s..%s" % (landing_base[:7], (tail.merge_sha or "")[:7]),
                  gate={"ok": True, "returncode": 0,
                        "log": cfg.store.path("gate", item.task.id + ".log")},
                  guarded_push=guarded_closeout_push)
    if cfg.manifest.tracker.adapter == "jira":
        ok, reason = cfg.adapter._triple_comment(
            item.expected_card, "Relay triple landed at %s" % tail.merge_sha)
        if not ok:
            return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                "Jira landing comment refused: %s" % reason, {"branch": cfg.default})
        terminal = cfg.manifest.tracker.done_statuses[0]
        label, reason = _triple_jira_label(cfg.manifest, terminal)
        if label is None:
            return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                reason, {"branch": cfg.default})
        ok, reason = cfg.adapter._triple_transition(item.expected_card, label, terminal)
        if not ok:
            return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                "Jira terminal transition refused: %s" % reason, {"branch": cfg.default})
    final = verify.verify(cfg.manifest, cfg.store.get(item.task.id), cfg.adapter,
                          scope=verify.SCOPE_FULL, do_fetch=True, env=cfg.env, now=cfg.now)
    cfg.store.upsert(item.task.id, verify=final.as_dict())
    if not final.landed:
        return None, expected_remote, _Halt(item.task.id,
            final.halt_class or contracts.HALT_PARTIAL_LANDING,
            "%s did not verify as landed" % item.task.id,
            {"branch": cfg.default, "checks": final.checks})
    # A Closeout normally commits documentation or tracker evidence.  If it did not create a
    # commit, the task landing's token remains current; otherwise guarded_closeout_push rotated it.
    next_lease = closeout_push.get("lease", pushed.integration_lease)
    finding = closeout.confirm_board_terminal(cfg.adapter, cfg.manifest, item.task.id,
                                              now=cfg.now)
    if finding:
        item.findings.append(finding)
    cfg.store.upsert(item.task.id, status=contracts.STATUS_LANDED,
                     halt_class=contracts.HALT_LANDED, branch=None, findings=item.findings)
    return next_lease, gitread.rev_parse(cfg.repo, cfg.default), None


def _triple_close_blocked(cfg, item, integration_lease, expected_remote):
    """Preserve a stopped blocked branch and run its tracker-only Closeout under the fence."""
    if not item.launched or not item.launched.pid or not item.launched.process_group_id:
        return None, expected_remote, _Halt(item.task.id, contracts.HALT_UNCLEAN_EXIT,
            "worker process identity is unavailable for %s" % item.task.id,
            {"branch": item.branch, "reason": "worker_process_identity_missing"})
    process = gitwrite.WorkerProcess(item.launched.pid, item.launched.process_group_id)
    imported = gitwrite.import_worker_branch(cfg.repo, item.worker, process, ops=cfg.store,
                                              task_id=item.task.id, env=cfg.env)
    if not imported.ok:
        return None, expected_remote, _Halt(item.task.id, contracts.HALT_UNCLEAN_EXIT,
            "could not preserve blocked worker %s: %s" % (item.task.id, imported.reason),
            {"branch": item.branch, "reason": imported.reason})
    stranded = gitwrite.blocked_path(cfg.repo, cfg.default, item.branch, ops=cfg.store,
                                     task_id=item.task.id, env=cfg.env)
    ctx = _Context(task=item.task, card=item.card, branch=item.branch,
                   baseline_sha=item.baseline_sha, baseline_comment_id=item.baseline_comment_id,
                   digest=item.digest, launched=item.launched, findings=item.findings, **vars(cfg))
    pushed = {}

    def guarded_closeout_push():
        result = gitwrite.renew_integration_lease_and_push(
            cfg.repo, cfg.default, expected_remote, integration_lease, ops=cfg.store,
            task_id=item.task.id, env=cfg.env, timeout=cfg.overrides.get("gate_seconds"))
        if result.ok:
            pushed["lease"] = result.integration_lease
            cfg.store.update_integration_lease(result.integration_lease)
        return result

    record = cfg.store.get(item.task.id) or {}
    return_to = closeout.return_to_for(cfg.manifest, record)
    unknown = closeout.baseline_unknown(cfg.manifest, record)
    _run_closeout(ctx, closeout.OUTCOME_BLOCKED, branch=stranded["branch"],
                  return_to=return_to, baseline_unknown=unknown,
                  guarded_push=guarded_closeout_push)
    if cfg.manifest.tracker.adapter == "jira":
        ok, reason = cfg.adapter._triple_comment(
            item.expected_card, "Relay triple blocked: %s" %
            (item.digest.get("halt_class") or contracts.HALT_BLOCKED_ENVELOPE))
        if not ok:
            return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                "Jira blocker comment refused: %s" % reason, {"branch": stranded["branch"]})
        if return_to:
            label, reason = _triple_jira_label(cfg.manifest, return_to)
            if label is None:
                return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                    reason, {"branch": stranded["branch"]})
            ok, reason = cfg.adapter._triple_transition(item.expected_card, label, return_to)
            if not ok:
                return None, expected_remote, _Halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                    "Jira return transition refused: %s" % reason,
                    {"branch": stranded["branch"]})
    finding = closeout.confirm_blocked_comment(cfg.adapter, item.task.id, item.baseline_comment_id)
    if finding:
        item.findings.append(finding)
    returned = {}
    if return_to or unknown:
        finding, out = closeout.read_back(cfg.adapter, cfg.manifest, item.task.id, return_to)
        if finding:
            item.findings.append(finding)
        if out:
            returned["card_in_review_by_run"] = False
    cfg.store.upsert(item.task.id, status=contracts.STATUS_BLOCKED,
                     halt_class=item.digest.get("halt_class") or contracts.HALT_BLOCKED_ENVELOPE,
                     branch=stranded["branch"], findings=item.findings, **returned)
    return pushed.get("lease", integration_lease), gitread.rev_parse(cfg.repo, cfg.default), None


def run_triple(manifest, adapter=None, store=None, home=None, base_env=None, stream=print,
               retry_blocked=False, timeout_overrides=None, launch_kwargs=None, now=time.time,
               notifier=None, wait_for_lease_seconds=None, lease_poll_seconds=LEASE_POLL_SECONDS,
               sleep=time.sleep, clock=time.monotonic, defer=frozenset()):
    """Run exactly three declared cards concurrently, then integrate them in manifest order.

    This is intentionally a coordinator rather than a parallel version of ``run``: the workers
    never share a Git directory and never receive a remote.  GitHub is read before and after the
    atomic remote claim; all subsequent repository writes are serial and atomically rotate the
    remote integration token.  A refusal leaves evidence and branches in place instead of trying
    a best-effort cleanup that could erase an active worker's work.

    `defer` is taken only to refuse it: a triple is three cards run together, so leaving one
    alone is not a triple. The refusal comes before the lease, like every configuration exit.
    """
    if manifest.execution.mode != "triple":
        return run(manifest, adapter=adapter, store=store, home=home, base_env=base_env,
                   stream=stream, retry_blocked=retry_blocked,
                   timeout_overrides=timeout_overrides, launch_kwargs=launch_kwargs, now=now,
                   notifier=notifier, wait_for_lease_seconds=wait_for_lease_seconds,
                   lease_poll_seconds=lease_poll_seconds, sleep=sleep, clock=clock, defer=defer)
    if defer:
        return RunOutcome(EXIT_CONFIG, message=TRIPLE_DEFER_REFUSAL)
    repo, default = manifest.project.repo, verify.default_branch_of(manifest)
    overrides, launch_kwargs = timeout_overrides or {}, dict(launch_kwargs or {})
    env = launch.child_env(manifest, base_env, home)
    try:
        adapter = adapter or adapters.build(manifest, env=base_env)
    except adapters.ConfigurationError as exc:
        return RunOutcome(EXIT_CONFIG, message=str(exc))
    store = store or state.StateStore(manifest.path, repo, home=home)
    acquired = _acquire(store, stream, wait_for_lease_seconds, lease_poll_seconds, sleep, clock)
    if acquired.code == state.LOCKED:
        return RunOutcome(EXIT_LEASE, message="another runner holds the lease")
    cfg = _Run(manifest, adapter, store, repo, default, env, base_env, home, stream,
               retry_blocked, overrides, launch_kwargs, now,
               tuple(manifest_module.completed_allowed_paths(manifest)))
    leases = ()
    workers = ()
    wrote_terminal = False
    # `_triple_halt` writes its terminal and returns without setting `wrote_terminal`, so the
    # `finally` tells this run's terminal from an earlier run's by comparing against this one.
    prior_terminal = (store.read() or {}).get("terminal")
    tracker = _triple_tracker(manifest)
    try:
        # The pre-read is retained as durable evidence, but only the exact re-read after the
        # atomic claims authorizes launches.  A client-side card write cannot race past a ref CAS.
        before = tracker.read_triple_snapshot(adapter, [task.id for task in manifest.tasks])
        if before["reason"]:
            return _triple_halt(None, contracts.HALT_UNEXPECTED_ERROR, before["reason"], store)
        snapshot = before["snapshot"]
        baseline = gitread.rev_parse(repo, default)
        remote_baseline = gitread.rev_parse(repo, "origin/" + default)
        if not baseline or baseline != remote_baseline:
            return _triple_halt(None, contracts.HALT_REMOTE_ADVANCED,
                               "canonical default is not exactly at origin/%s" % default, store)
        for task in manifest.tasks:
            preflight = gitwrite.preflight(repo, default,
                                            gitwrite.task_branch_for(task.id, manifest.project.branch_prefix),
                                            env=env, pushes=True)
            if not preflight.ok:
                return _triple_halt(task.id, contracts.HALT_UNCLEAN_EXIT,
                                   "triple preflight refused on %s" % preflight.failed, store)
        claimed = gitwrite.acquire_remote_leases(
            repo, snapshot["repository_id"], snapshot["project_id"],
            [card["item_id"] for card in snapshot["cards"]], ops=store, env=env)
        if not claimed.ok:
            return _triple_halt(None, contracts.HALT_REMOTE_ADVANCED,
                               "triple remote claim refused: %s" % claimed.reason, store)
        leases = claimed.card_leases + (claimed.integration_lease,)
        store.write_remote_leases(claimed.claim_key, claimed.card_leases, claimed.integration_lease)
        after = tracker.read_triple_snapshot(adapter, [task.id for task in manifest.tasks])
        if after["reason"]:
            return _triple_halt(None, contracts.HALT_UNEXPECTED_ERROR, after["reason"], store)
        for expected, observed in zip(snapshot["cards"], after["snapshot"]["cards"]):
            collision = github_adapter.collision_evidence(expected, observed)
            if collision:
                return _triple_halt(expected["id"], contracts.HALT_REMOTE_ADVANCED,
                                   "board changed while triple claims were acquired", store)

        if manifest.tracker.adapter == "jira":
            scoped, reason = adapter._authorize_triple_writes(after["snapshot"])
            if not scoped:
                return _triple_halt(None, contracts.HALT_REMOTE_ADVANCED, reason, store)
            snapshot, reason = _triple_jira_start(cfg, tracker, after["snapshot"]["cards"])
            if snapshot is None:
                return _triple_halt(None, contracts.HALT_REMOTE_ADVANCED, reason, store)
        else:
            snapshot = snapshot["cards"]
        workers = []
        # Issue #51: the baseline is the status each card read before any start transition. On
        # Jira the coordinator has already moved every card to in review by here, so `snapshot`
        # reads that, and recording it would make every blocked card look staged.
        last_audit = store.audit()
        for task, card, before_start in zip(manifest.tasks, snapshot, after["snapshot"]["cards"]):
            branch = gitwrite.task_branch_for(task.id, manifest.project.branch_prefix)
            comments = card.get("comments") or []
            item = _TripleWorker(task, dict(card, comments=comments), card, branch, baseline,
                                 comments[-1]["id"] if comments else None)
            item.brief_text = brief.render(manifest, task, item.card, branch=branch)
            hits = brief.scan(item.card, item.brief_text)
            if hits:
                return _triple_halt(task.id, contracts.HALT_UNCLEAN_EXIT,
                                   "triple brief refused: %s" % brief.exclusion_reason(hits), store)
            _path, item.brief_sha = brief.write(store, task.id, item.brief_text)
            made = gitwrite.create_worker_clone(repo, _triple_worker_root(store), task.id, branch,
                                                 baseline, default, ops=store, env=env)
            if not made.ok:
                return _triple_halt(task.id, contracts.HALT_UNCLEAN_EXIT,
                                   "could not create isolated worker: %s" % made.reason, store)
            item.worker = made.worker
            capability = backends.build(task.backend).CAPABILITY
            store.upsert(task.id, status=contracts.STATUS_RUNNING, baseline_sha=baseline,
                         baseline_tracker_status=closeout.launch_baseline(
                             manifest, store.get(task.id) or {}, before_start.get("status"),
                             last_audit),
                         baseline_comment_id=item.baseline_comment_id, branch=branch,
                         brief_sha256=item.brief_sha, findings=[], backend=task.backend,
                         model=task.model, halt_class=None, halt_stage=None,
                         halt_message=None, halt_evidence=None, envelope_verdict=None,
                         host_at_start=None, host_at_end=None, card_in_review_by_run=True,
                         unenforced_restrictions=(_unenforced_scalar(manifest, capability)
                                                  if not capability.enforces_at_launch else None))
            workers.append(item)

        threads = [threading.Thread(target=_triple_launch_worker, args=(cfg, item), daemon=True)
                   for item in workers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for item in workers:
            _triple_classify(cfg, item)
            if item.collision:
                return _triple_halt(item.task.id, contracts.HALT_UNEXPECTED_ERROR,
                                   "triple worker refused: %s" % item.collision["reason"], store)

        observed = tracker.read_triple_snapshot(adapter, [task.id for task in manifest.tasks])
        if observed["reason"]:
            return _triple_halt(None, contracts.HALT_UNEXPECTED_ERROR, observed["reason"], store)
        integration = claimed.integration_lease
        expected_remote = remote_baseline
        for item, card in zip(workers, observed["snapshot"]["cards"]):
            if manifest.tracker.adapter == "jira":
                next_expected, collision = item.expected_card, github_adapter.collision_evidence(
                    item.expected_card, card)
                if not collision:
                    updated, reason = _triple_jira_comment(cfg, tracker, item, item.branch)
                    if updated is None:
                        return _triple_halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                                           "Jira branch comment refused: %s" % reason, store)
                    next_expected, collision = github_adapter.capture_task_delta(
                        item.expected_card, updated, manifest.tracker.in_review_status, item.branch)
            else:
                next_expected, collision = github_adapter.capture_task_delta(
                    item.expected_card, card, manifest.tracker.in_review_status, item.branch)
            if collision:
                return _triple_halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                                   "board task transition was not exclusive", store)
            item.expected_card = next_expected
            if not item.digest.get("routable"):
                integration, expected_remote, halt = _triple_close_blocked(
                    cfg, item, integration, expected_remote)
                if halt:
                    return _triple_halt(halt.task_id, halt.halt_class, halt.message, store)
                post_closeout = tracker.read_triple_snapshot(
                    adapter, [task.id for task in manifest.tasks])
                if post_closeout["reason"]:
                    return _triple_halt(item.task.id, contracts.HALT_UNEXPECTED_ERROR,
                                       post_closeout["reason"], store)
                blocked_card = post_closeout["snapshot"]["cards"][workers.index(item)]
                return_to = closeout.return_to_for(manifest, store.get(item.task.id) or {})
                next_expected, collision = github_adapter.capture_closeout_delta(
                    item.expected_card, blocked_card, closeout.OUTCOME_BLOCKED,
                    return_to=return_to)
                if collision:
                    return _triple_halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                                       "board Closeout transition was not exclusive", store)
                item.expected_card = next_expected
                leases = claimed.card_leases + (integration,)
                continue
            integration, expected_remote, halt = _triple_integrate(
                cfg, item, integration, expected_remote)
            if halt:
                return _triple_halt(halt.task_id, halt.halt_class, halt.message, store)
            post_closeout = tracker.read_triple_snapshot(adapter,
                                                                 [task.id for task in manifest.tasks])
            if post_closeout["reason"]:
                return _triple_halt(item.task.id, contracts.HALT_UNEXPECTED_ERROR,
                                   post_closeout["reason"], store)
            landed_card = post_closeout["snapshot"]["cards"][workers.index(item)]
            next_expected, collision = github_adapter.capture_closeout_delta(
                item.expected_card, landed_card, closeout.OUTCOME_LANDED,
                landing_ref=store.get(item.task.id).get("landing_ref"))
            if collision:
                return _triple_halt(item.task.id, contracts.HALT_REMOTE_ADVANCED,
                                   "board Closeout transition was not exclusive", store)
            item.expected_card = next_expected
            # The card now has durable Closeout proof and the default has passed full verify.
            # Only at that point may a coordinator delete its imported branch or its private
            # clone.  A failed cleanup is intentionally non-fatal and leaves inspection data.
            if gitread.branch_exists(repo, item.branch):
                gitwrite.delete_branch(repo, item.branch, ops=store, task_id=item.task.id, env=env)
            gitwrite.cleanup_worker_clone(
                item.worker,
                gitwrite.WorkerProcess(item.launched.pid, item.launched.process_group_id))
            # The next guarded push must delete/rotate the new token, never the token acquired
            # before any landings.  Card claims never rotate during one batch.
            leases = claimed.card_leases + (integration,)
        # A run is not complete until its exact remote claims are gone.  Do this before the
        # completed terminal record so a deletion race or server refusal cannot be reported as
        # a successful batch while blocking future coordinators indefinitely.
        if not _triple_workers_stopped(workers):
            return _triple_halt(None, contracts.HALT_RUNNER_CRASHED,
                               "a triple worker process group survived its coordinator", store)
        released = _triple_release(repo, leases, store, env)
        if released is None or not released.ok:
            return _triple_halt(None, contracts.HALT_REMOTE_ADVANCED,
                               "triple remote lease could not be released", store)
        leases = ()
        _write_terminal(store, env, contracts.RUN_COMPLETED, used_backends=cfg.used_backends)
        wrote_terminal = True
        return RunOutcome(EXIT_OK, store=store, records=store.records())
    finally:
        # A failed release is a collision boundary: retain its state and report the run halt;
        # never delete local evidence or manufacture a stale-success terminal.
        if leases and _triple_workers_stopped(workers):
            released = _triple_release(repo, leases, store, env)
            if released is not None and not released.ok and stream is not None:
                stream("triple remote lease retained: %s" % released.reason)
        elif leases and stream is not None:
            stream("triple remote lease retained: a worker process group may still be running")
        if not wrote_terminal and (store.read() or {}).get("terminal") == prior_terminal:
            try:
                _write_terminal(store, env, contracts.RUN_CRASHED)
            except Exception:
                pass
            # A worker that may still be running is still driving its record.
            if _triple_workers_stopped(workers):
                _mark_in_flight_crashed(store)
        store.release()


def _mark_in_flight_crashed(store, spare=()):
    """Issue #64: a Runner leaving without a terminal record, an interrupt from the keyboard most
    often, drives nothing it launched any more. Its records in flight are marked the way a
    reclaim marks them, so none is left reading running with no process behind it. Called from a
    `finally`, where the exception on its way out names the cause. Best effort, like the terminal
    write before it: that exception is the one to keep.

    `spare` is the task ids whose process is still alive (issue #71), which are left reading
    running because something still drives them."""
    leaving = sys.exc_info()[1]
    cause = (state.CRASH_INTERRUPTED if isinstance(leaving, KeyboardInterrupt)
             else state.CRASH_EXITED)
    try:
        store.mark_in_flight_crashed(cause, spare=spare)
    except Exception:
        pass


def _holder_phrase(acquired):
    holder = acquired.holder or {}
    return ("pid %s on %s, manifest %s"
            % (holder.get("holder_pid"), holder.get("hostname"),
               acquired.other_manifest or holder.get("manifest")))


def _acquire(store, stream, wait_seconds, poll_seconds, sleep, clock):
    """Take the leases, polling while a live runner holds either one when the operator asked to
    queue (issue #23). Polling is `acquire` itself rather than a read of the lease, because there
    are two leases and only `acquire` checks both; a refused `acquire` changes no state, and one
    refused on the repo lease gives back the manifest lease it took. A holder that dies is
    reclaimed at its expiry exactly as an unqueued run would reclaim it."""
    acquired = store.acquire()
    if acquired.code != state.LOCKED or not wait_seconds:
        return acquired
    started = clock()
    deadline = started + wait_seconds
    if stream is not None:
        stream("waiting for the lease held by %s; polling every %ds for up to %d minute(s)"
               % (_holder_phrase(acquired), poll_seconds, round(wait_seconds / 60)))
    while acquired.code == state.LOCKED and clock() < deadline:
        sleep(max(0, min(poll_seconds, deadline - clock())))
        acquired = store.acquire()
    if stream is not None:
        if acquired.code == state.LOCKED:
            stream("gave up waiting for the lease after %d minute(s)" % round(wait_seconds / 60))
        else:
            stream("the lease cleared after %ds of waiting; starting the run"
                   % round(clock() - started))
    return acquired


def run(manifest, adapter=None, store=None, home=None, base_env=None, stream=print,
        retry_blocked=False, timeout_overrides=None, launch_kwargs=None, now=time.time,
        notifier=None, wait_for_lease_seconds=None, lease_poll_seconds=LEASE_POLL_SECONDS,
        sleep=time.sleep, clock=time.monotonic, defer=frozenset()):
    """Drive one manifest to completion or to a named halt. Returns a RunOutcome; never raises
    for a task level failure, because every one of those is a class an operator can act on.

    `wait_for_lease_seconds` (issue #23) queues this run behind a live holder of either lease
    instead of refusing at once, up to that bound. The refusal after the bound is the same one an
    unqueued run gets.

    `defer` (R10) is the ids of listed tasks this run passes over. The caller has checked each is
    listed; `cmd_run` refuses one that is not, before the lease, as it does for `retry_blocked`."""
    repo = manifest.project.repo
    default = verify.default_branch_of(manifest)
    overrides = timeout_overrides or {}
    launch_kwargs = dict(launch_kwargs or {})
    env = launch.child_env(manifest, base_env, home)

    try:
        adapter = adapter or adapters.build(manifest, env=base_env)
    except adapters.ConfigurationError as exc:
        return RunOutcome(EXIT_CONFIG, message=str(exc))
    store = store or state.StateStore(manifest.path, repo, home=home)

    announce = _announcer(stream, notifier)
    # Attached before `acquire()`, which is what makes a stale lease reclaim visible: the records
    # it marks halted are written by `_mark_crashed` inside that call, and it is the strongest
    # signal an operator who is not watching can receive.
    store.observer = lambda task_id, _before, after: announce(_moved_line(manifest, store,
                                                                          task_id, after))

    acquired = _acquire(store, stream, wait_for_lease_seconds, lease_poll_seconds, sleep, clock)
    if acquired.code == state.LOCKED:
        holder = acquired.holder or {}
        where = acquired.other_manifest or holder.get("manifest")
        return RunOutcome(EXIT_LEASE, message=(
            "another runner holds the lease: pid %s on %s, manifest %s, heartbeat %.0f seconds old"
            % (holder.get("holder_pid"), holder.get("hostname"), where, acquired.age_seconds or 0)))

    if stream is not None and acquired.code == state.STALE_RECLAIMED:
        stream("reclaimed a stale lease from pid %s; %d record(s) marked %s"
               % ((acquired.previous_holder or {}).get("holder_pid"),
                  len(acquired.reclaimed_ids), contracts.HALT_RUNNER_CRASHED))

    # Resolved once, here, from the repo as it stands before any task has touched it. Reading
    # it per closeout would let a task's own merge move the bound its closeout is checked
    # against (R53, KTD15).
    allowed_paths = tuple(manifest_module.completed_allowed_paths(manifest))
    config = _Run(manifest, adapter, store, repo, default, env, base_env, home, stream,
                  retry_blocked, overrides, launch_kwargs, now, allowed_paths,
                  defer=frozenset(defer))
    outcome = RunOutcome(EXIT_OK, store=store)
    wrote_terminal = False
    try:
        store.validate()
        verify.startup_reverify(manifest, _undeferred(store, config.defer), adapter, env=env,
                                now=now)
        for index, task in enumerate(manifest.tasks):
            try:
                _one_task(config, task)
            except _Halt as exc:
                # Rebound deliberately: Python unbinds the `as` name at the end of an except
                # block, so the handler below could not see it.
                halt = exc
            except gitread.GitError as exc:
                halt = _Halt(task.id, contracts.HALT_UNCLEAN_EXIT,
                             "a git command failed while handling %s: %s" % (task.id, exc),
                             {"task": task.id,
                              "branch": gitwrite.task_branch_for(task.id, config.manifest.project.branch_prefix),
                              **_git_error_fields(exc)})
            except Exception as exc:
                # A defect or an unanticipated library error. It still stops the way every
                # other stop does, because an operator cannot act on a traceback.
                halt = _Halt(task.id, contracts.HALT_UNEXPECTED_ERROR,
                             "the runner hit an unexpected %s on %s: %s"
                             % (type(exc).__name__, task.id, exc),
                             {"task": task.id, "error_type": type(exc).__name__,
                              "error": str(exc)[:500]})
            else:
                _note_usage_limit(config, task)
                store.set_cursor(index + 1)
                continue
            # Issue #15: a halt contained to one task need not stop the rest. Decided from
            # the repo, not the class, because the same class covers a failed gate command
            # (default untouched) and a failed push after the merge (default ahead of origin).
            continued = _continue_past(config, halt)
            # The message is the raiser's own sentence, kept beside the class's template
            # line. First live run: a retry refused under R48 halted as unclean_exit and the
            # summary said "left the tree dirty" about a clean tree, because the sentence that
            # explained the refusal was printed to stdout and never written down.
            # Fill the routing only when the record has none, and never overwrite it. Three
            # raise sites reach here before anything launches: the pre flight refusal, the R48
            # stranded branch refusal, and a git or adapter error. On those the record still
            # describes the previous attempt, whose args, binary, and transcript all name the
            # backend that actually ran, so writing this run's manifest value would leave the
            # record naming a CLI that never launched, with no finding to explain it (#58).
            # Both halves come from one source, never OR'd independently: a record predating the
            # model field carries a backend and no model, and filling each from whichever source
            # is non empty would pair the previous attempt's CLI with this run's model.
            previous = store.get(halt.task_id) or {}
            kept = previous if previous.get("backend") else {"backend": task.backend,
                                                             "model": task.model}
            store.upsert(halt.task_id, status=contracts.STATUS_HALTED,
                         halt_class=halt.halt_class, halt_evidence=halt.evidence,
                         halt_message=halt.message, continued_past=continued,
                         backend=kept.get("backend"), model=kept.get("model"))
            if continued:
                if stream is not None:
                    stream("%s halted with class %s; continuing past it"
                           % (halt.task_id, halt.halt_class))
                _note_usage_limit(config, task)
                store.set_cursor(index + 1)
                continue
            _audit_cards(config)
            _write_terminal(store, env, contracts.RUN_HALTED, halt.task_id, halt.halt_class,
                            config.used_backends, announce=announce,
                            limit_passed_over=config.limit_passed_over)
            wrote_terminal = True
            return RunOutcome(EXIT_HALTED, halt.task_id, halt.halt_class, halt.message,
                              store, store.records())
        _audit_cards(config)
        _write_terminal(store, env, contracts.RUN_COMPLETED,
                        used_backends=config.used_backends, announce=announce,
                        limit_passed_over=config.limit_passed_over)
        wrote_terminal = True
        outcome.records = store.records()
        return outcome
    finally:
        # The lease is released on the way out either way, and status_word reads a crash from a
        # surviving lease. So a run that reached neither terminal write has to say so itself,
        # or `relay status` reports the previous run's outcome as if it were this one.
        if not wrote_terminal:
            try:
                _write_terminal(store, env, contracts.RUN_CRASHED,
                                used_backends=config.used_backends, announce=announce,
                                limit_passed_over=config.limit_passed_over)
            except Exception:
                pass
            _mark_in_flight_crashed(store)
        store.release()


def dispatch(manifest, adapter=None, store=None, home=None, base_env=None, stream=print,
             retry_blocked=False, timeout_overrides=None, launch_kwargs=None, now=time.time,
             notifier=None, wait_for_lease_seconds=None, lease_poll_seconds=LEASE_POLL_SECONDS,
             sleep=time.sleep, clock=time.monotonic, policy="serial", defer=frozenset()):
    """Drive a manifest under a frozen conservative serial or parallel schedule.

    Parallel is opt-in and still fail-closed: only a scheduler wave with no conflict edge may
    build together.  Merges stay in manifest order, so hooks, gates, and verification are
    unchanged.  Each build runs in a distinct git worktree.
    """
    repo = manifest.project.repo
    default = verify.default_branch_of(manifest)
    overrides = timeout_overrides or {}
    launch_kwargs = dict(launch_kwargs or {})
    env = launch.child_env(manifest, base_env, home)

    try:
        adapter = adapter or adapters.build(manifest, env=base_env)
    except adapters.ConfigurationError as exc:
        return RunOutcome(EXIT_CONFIG, message=str(exc))
    store = store or state.StateStore(manifest.path, repo, home=home)

    announce = _announcer(stream, notifier)
    store.observer = lambda task_id, _before, after: announce(_moved_line(manifest, store,
                                                                          task_id, after))

    acquired = _acquire(store, stream, wait_for_lease_seconds, lease_poll_seconds, sleep, clock)
    if acquired.code == state.LOCKED:
        holder = acquired.holder or {}
        where = acquired.other_manifest or holder.get("manifest")
        return RunOutcome(EXIT_LEASE, message=(
            "another runner holds the lease: pid %s on %s, manifest %s, heartbeat %.0f seconds old"
            % (holder.get("holder_pid"), holder.get("hostname"), where, acquired.age_seconds or 0)))

    if stream is not None and acquired.code == state.STALE_RECLAIMED:
        stream("reclaimed a stale lease from pid %s; %d record(s) marked %s"
               % ((acquired.previous_holder or {}).get("holder_pid"),
                  len(acquired.reclaimed_ids), contracts.HALT_RUNNER_CRASHED))

    allowed_paths = tuple(manifest_module.completed_allowed_paths(manifest))
    if policy not in scheduler.POLICIES:
        return RunOutcome(EXIT_CONFIG, message="run policy must be serial or parallel")
    defer = frozenset(defer)
    # A deferred task is left out of the schedule, so its card is not read and the plan printed
    # below does not show it building. The drive still walks every manifest task, reads only the
    # schedule's edges, and settles a deferred one through `_begin_task`'s early return.
    scheduled = tuple(task for task in manifest.tasks if task.id not in defer)
    # This is the entire pre-launch repository/card inspection.  It is intentionally read-only;
    # a missing card text becomes an uncertainty edge and the ordinary launch read rechecks it.
    task_text = {}
    for task in scheduled:
        try:
            card = adapter.read(task.id)
        except Exception:
            card = None
        if not isinstance(card, dict) or card.get("skipped"):
            task_text[task.id] = None
        else:
            comments = card.get("comments") or ()
            task_text[task.id] = "\n".join(str(part) for part in (
                card.get("title", ""), card.get("description", ""),
                *(entry.get("body", "") for entry in comments if isinstance(entry, dict))))
    schedule = scheduler.build_schedule(scheduled, repo, task_text, policy=policy)
    schedule_base = gitread.rev_parse(repo, default)
    store.write_schedule({
        "policy": schedule.policy,
        "repo_head": schedule_base,
        "task_ids": list(schedule.task_ids),
        "waves": [list(wave) for wave in schedule.waves],
        "edges": [{"first": edge.first, "second": edge.second, "reason": edge.reason}
                  for edge in schedule.edges],
    })
    if stream is not None:
        for line in scheduler.render(schedule).splitlines():
            stream(line)
    config = _Run(manifest, adapter, store, repo, default, env, base_env, home, stream,
                  retry_blocked, overrides, launch_kwargs, now, allowed_paths, schedule=schedule,
                  defer=defer)
    outcome = RunOutcome(EXIT_OK, store=store)
    wrote_terminal = False
    try:
        store.validate()
        verify.startup_reverify(manifest, _undeferred(store, config.defer), adapter, env=env,
                                now=now)
        halted = _concurrent_loop(config, announce)
        if halted is not None:
            wrote_terminal = True
            return halted
        _audit_cards(config)
        _write_terminal(store, env, contracts.RUN_COMPLETED,
                        used_backends=config.used_backends, announce=announce)
        wrote_terminal = True
        outcome.records = store.records()
        return outcome
    except gitread.GitError as exc:
        halt = _Halt("dispatch", contracts.HALT_UNCLEAN_EXIT,
                     "a git command failed while dispatching: %s" % exc,
                     _git_error_fields(exc))
        _audit_cards(config)
        _write_terminal(store, env, contracts.RUN_HALTED, halt.task_id, halt.halt_class,
                        config.used_backends, announce=announce,
                        surviving_flights=config.surviving_flights)
        wrote_terminal = True
        return RunOutcome(EXIT_HALTED, halt.task_id, halt.halt_class, halt.message,
                          store, store.records())
    except Exception as exc:
        halt = _Halt("dispatch", contracts.HALT_UNEXPECTED_ERROR,
                     "the runner hit an unexpected %s while dispatching: %s"
                     % (type(exc).__name__, exc),
                     {"error_type": type(exc).__name__, "error": str(exc)[:500]})
        _audit_cards(config)
        _write_terminal(store, env, contracts.RUN_HALTED, halt.task_id, halt.halt_class,
                        config.used_backends, announce=announce,
                        surviving_flights=config.surviving_flights)
        wrote_terminal = True
        return RunOutcome(EXIT_HALTED, halt.task_id, halt.halt_class, halt.message,
                          store, store.records())
    finally:
        if not wrote_terminal:
            try:
                _write_terminal(store, env, contracts.RUN_CRASHED,
                                used_backends=config.used_backends, announce=announce,
                                surviving_flights=config.surviving_flights)
            except Exception:
                pass
            _mark_in_flight_crashed(store, spare={entry["task"]
                                                  for entry in config.surviving_flights})
        store.release()


def _git_error_fields(exc):
    """The evidence a GitError contributes wherever one is recorded."""
    return {"args": exc.args_list, "returncode": exc.returncode,
            "stderr": (exc.stderr or "")[-2000:]}


def _write_terminal(store, env, run_status, halt_task=None, halt_class=None, used_backends=(),
                    announce=None, surviving_flights=(), limit_passed_over=()):
    """Write terminal version evidence for only the CLIs this invocation actually launched.

    The run's last phase event goes out from here rather than from each of the three call sites,
    so a fourth ending added later cannot forget to announce itself. The counts are read after the
    record is written, so they describe the run the record just closed.

    `surviving_flights` names the dispatch builds still alive when the run left (issue #71).
    `limit_passed_over` names the Tasks a serial run left unlaunched on a model that reported its
    usage limit (R11).
    """
    used = sorted(used_backends)
    pinned = {name: backends.build(name).CAPABILITY.version_tested for name in used}
    observed = {name: launch.cli_version(env, backend=name) for name in used}
    record = store.write_terminal(run_status, halt_task, halt_class, pinned, observed,
                                  surviving_flights=surviving_flights,
                                  limit_passed_over=limit_passed_over)
    if announce is not None:
        line = _counts_line(store, run_status)
        if halt_task:
            line += "; halted on %s with class %s" % (halt_task, halt_class)
        announce(line)
    return record


def _continue_past(cfg, halt):
    """Whether the run goes on past this halt (issue #15). True only when the manifest opted
    in, the class is not run scoped, and the repo, returned to the default branch, is one the
    next task's pre flight would accept. A refusal is recorded on the halt's evidence under
    `resume` so the record says why the run stopped rather than continuing.

    A failure inside the disposition is itself a stop: the evidence names it beside the
    original halt, and the class stays the original's, because that is what the operator has
    to repair first. Any exception, not only GitError: this runs outside the per task handler
    that turns the unexpected into a named class, and a hung checkout raising TimeoutExpired
    here would otherwise escape the loop as a traceback.

    Two refusals never reach the disposition. A `no_task_branch` pre-flight refusal is this
    same task's own branch from an earlier continued-past halt still in the way; nothing here
    deletes it (Scope Boundaries), so allowing continuation would repeat the identical refusal
    on every later run while the record keeps reading `continued_past` and the run keeps
    reading `completed` -- a review finding on the first draft of this function caught it
    passing on that exact case. And a lease already lost means the checkout below would mutate
    a repo another runner may hold, the one mutation in this path with no heartbeat guard the
    way `_merge_route`'s tail already has one."""
    if not cfg.manifest.on_halt.continue_past_task_halt:
        return False
    if halt.halt_class in contracts.RUN_SCOPED_HALT_CLASSES:
        return False
    if halt.evidence.get("check") == "no_task_branch":
        halt.evidence["resume"] = {"check": "no_task_branch"}
        return False
    if not cfg.store.heartbeat():
        halt.evidence["resume"] = {"check": "lease_lost"}
        return False
    try:
        result = gitwrite.resume_disposition(cfg.repo, cfg.default, ops=cfg.store,
                                             task_id=halt.task_id, env=cfg.env,
                                             pushes=manifest_module.pushes(cfg.manifest))
    except gitread.GitError as exc:
        halt.evidence["resume"] = {"check": "git_error", **_git_error_fields(exc)}
        return False
    except Exception as exc:
        halt.evidence["resume"] = {"check": "unexpected_error",
                                   "error_type": type(exc).__name__, "error": str(exc)[:500]}
        return False
    if result.ok:
        return True
    halt.evidence["resume"] = dict(result.evidence, check=result.failed)
    return False


# Round eight #54, from relay proof T-65. The Brief forbids these operations "however this CLI
# spells it", and the audit after exit matches command spellings. T-65 ran a disallowed operation
# by a spelling the pattern does not match, and the record said only that the operations went
# unenforced, next to an empty findings list. Read together those two read as a clean run.
#
# Every clause below is load bearing, and a code review earned each one.
#
# Present tense, because the scalar is written off `enforces_at_launch` alone, before the launch
# error and timeout branches. A past tense sentence would claim an audit ran for a Task whose
# binary was missing or that was killed at the timeout with no readable evidence.
#
# "the Task process's own log recorded", because the audit walks only completed calls the log
# decoded. The Closeout is a second contributor to the same findings list and `closeout.run`
# passes no disallow patterns at all, so the sentence names the process it actually describes.
#
# "a restriction naming a tool other than a command", because `classify` reads `input.command`
# and skips a tool_use without one. A manifest may disallow an `Edit(...)` or `Write(...)`
# pattern, and for that entry the honest word is unaudited rather than bounded by spelling.
#
# The destructive clause, because `_destructive_finding` filters the findings
# `classify.matches_disallow_pattern` produced. The refusal an operator trusts most rests on the
# same match this sentence has just called evadable, so it cannot be left implied.
#
# Positive form, because "no finding is not proof" inverts on a fast read, and defeating a fast
# misread is the whole job.
UNENFORCED_BOUND = (
    ". The Brief carries these to the Task process as instructions naming operations, and the "
    "audit after exit matches command spellings against the commands that process's own log "
    "recorded. An absent finding therefore does not prove the operation was avoided. Another "
    "spelling, a restriction naming a tool other than a command, and a call the log never "
    "recorded all reach the same empty result, and the refusal of a destructive landing rests "
    "on that same match."
)


def _unenforced_scalar(manifest, capability):
    """One plain string naming the unenforced disallow patterns, the bound on what the audit that
    follows them can prove, and any sandbox network grant the backend launches with.

    The network clause is chosen off the capability's own `grants_network`, so a backend that
    enforces nothing and reaches no network never inherits a sentence that is false for it. The
    whole scalar is still written only for a backend that does not enforce at launch, because
    that is what the record key means; `test_no_backend_grants_network_while_enforcing_at_launch`
    is what keeps the two conditions from drifting apart into a silently lost disclosure.

    It belongs on the record and not only in `SKILL.md`, because the skill speaks when a manifest
    is authored: an operator running a manifest written before the grant existed would otherwise
    never be told, and `validate` only checks that `permissions.unenforced_acceptance` is non
    empty (issue #51).

    Single line, and the newline ban is not a style preference: `summary.line_fields` hoists
    every non-container record field into the namespace `cause_line` formats halt templates
    against, so this value has to stay Cause-line-safe even though no template names it today.
    """
    inners = []
    for pattern in manifest_module.resolved_disallowed(manifest):
        inner = contracts.disallow_inner(pattern)
        if inner not in inners:
            inners.append(inner)
    scalar = "disallowed tools not enforced at launch: " + ", ".join(inners) + UNENFORCED_BOUND
    if capability.grants_network:
        scalar += (" This Task also launches with its sandbox network turned on, so it reaches "
                   "every host and not only the tracker, because the sandbox takes no host "
                   "allowlist. It reaches them holding whatever credentials the child env "
                   "carries, which for the github adapter is the operator's own gh login, scoped "
                   "to their account rather than to this card, and no argv restriction narrows "
                   "that.")
    return scalar


def _destructive_finding(findings):
    for finding in findings:
        if (finding.get("class") == contracts.UNENFORCED_DISALLOWED
                and finding.get("pattern") in contracts.DESTRUCTIVE_TOOLS):
            return finding
    return None


def _reassignment(record, task):
    """The finding for a task the Manifest now routes somewhere other than where it last ran, or
    None (issue #58).

    Compared per field, and only where the record actually carries a value. A record written
    before `model` joined `RECORD_FIELDS` has no key at all, and a record that halted before its
    first launch has no backend, so an absent value is not a changed value. Without that rule
    every older record would report a move on its first relaunch.
    """
    was_backend, was_model = record.get("backend"), record.get("model")
    moved_backend = bool(was_backend) and was_backend != task.backend
    moved_model = bool(was_model) and was_model != task.model
    if not (moved_backend or moved_model):
        return None
    # The recorded halves pass through as they are, never defaulted to this run's values. A
    # record that predates the model field carries a backend and no model, so filling the gap
    # from the manifest would report a previous attempt that never happened, on a pair validate
    # itself refuses. An absent half renders as the line's own placeholder instead.
    return {"class": contracts.BACKEND_REASSIGNED,
            "from_backend": was_backend, "from_model": was_model,
            "to_backend": task.backend, "to_model": task.model}


def _skip(cfg, task_id, reason):
    """A runner decided skip (issue #19): its own status and reason field, so status, summary,
    and the phase event can tell it from the manifest's exclusion, and no early return on a
    later run, so the check that wrote it is made again."""
    cfg.store.upsert(task_id, status=contracts.STATUS_SKIPPED, skip_reason=reason,
                     excluded_reason=None)
    if cfg.stream is not None:
        cfg.stream("%s skipped: %s" % (task_id, reason))


@dataclass
class _Begun:
    """Everything `_begin_task` gathered before launch, so dispatch can launch off the
    main thread and still complete on it."""
    task: object
    card: dict
    branch: str
    baseline_sha: str
    baseline_comment_id: object
    brief_text: str
    log_path: str
    capability: object
    reassignment: object
    worktree: str | None = None
    tree_at_exit: str | None = None
    current_at_exit: str | None = None


@dataclass
class _Flight:
    begun: object
    worktree: str
    thread: object
    pgid: list
    box: list
    # Issue #71. Set once the coordinator has begun ending this build, so a process whose launch
    # reaches Popen only afterwards is killed as it starts rather than left running untracked.
    stopping: object = field(default_factory=threading.Event)


def retries_blocked(retry_blocked, task_id):
    """Whether a blocked record for `task_id` is retried this run. `--retry-blocked` alone is
    True and retries every one; with ids it is a frozenset, and only those are retried (issue
    #39), so one blocked task can be relaunched without reviving every older blocked record."""
    if isinstance(retry_blocked, frozenset):
        return task_id in retry_blocked
    return bool(retry_blocked)


def retry_blocked_argv(retry_blocked):
    """The flags that carry `retry_blocked` to a child `run`, in the one shape `retries_blocked`
    reads: one `--retry-blocked ID` per named id, the bare flag only for True, and nothing for
    an empty set, which must never widen into the bare flag and retry every blocked record."""
    if isinstance(retry_blocked, frozenset):
        return [part for task_id in sorted(retry_blocked) for part in ("--retry-blocked", task_id)]
    return ["--retry-blocked"] if retry_blocked else []


def defer_argv(defer):
    """The flags that carry `defer` to a child `run`: one `--defer ID` per id, in a stable
    order, and nothing for an empty set. Unlike `--retry-blocked` there is no bare form."""
    return [part for task_id in sorted(defer) for part in ("--defer", task_id)]


# One sentence for both refusals, `cmd_run`'s before a detach and `run_triple`'s before the lease.
TRIPLE_DEFER_REFUSAL = ("--defer is unavailable under execution.mode triple, which runs its "
                        "three cards together; run without it")


class _Undeferred:
    """The store as the startup re-verify sees it on a run with `--defer`: no deferred record is
    listed or found, and every other call is passed straight through. The re-verify runs before any
    task is begun and restamps each halted record's `verify` and reads its card, so the early
    return in `_begin_task` alone would still leave a deferred halted record rewritten (R10)."""

    def __init__(self, store, defer):
        self._store, self._defer = store, defer

    def records(self):
        return {task_id: record for task_id, record in self._store.records().items()
                if task_id not in self._defer}

    def get(self, task_id):
        return None if task_id in self._defer else self._store.get(task_id)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _undeferred(store, defer):
    return _Undeferred(store, defer) if defer else store


def _one_task(cfg, task):
    begun = _begin_task(cfg, task)
    if begun is None:
        return
    cfg.launched_ids.add(task.id)
    launched = _launch_begun(cfg, begun, cwd=cfg.repo)
    return _complete_task(cfg, begun, launched, tree_repo=cfg.repo)


def _note_usage_limit(cfg, task):
    """R11, KTD7. After a Task this serial run launched ends blocked or halted, read its death
    from its log (KTD3). A confirmed usage limit adds the model it ran on to the run's set, and
    `_begin_task` launches nothing more on that model in this run.

    Confirmed only: the Runner has no `quick_death_seconds`, so the reading is taken with none,
    and a death with no `result` line reads refuted rather than unconfirmed. A Task that did not
    launch in this run is never read, since its record and log describe an earlier attempt
    (KTD9). The breaker must never stop the run itself, so a failure to read costs the reading."""
    if task.id not in cfg.launched_ids:
        return
    try:
        record = cfg.store.get(task.id) or {}
        if record.get("status") not in (contracts.STATUS_BLOCKED, contracts.STATUS_HALTED):
            return
        tail = limits.log_tail(cfg.store.path("logs", task.id + ".stdout.log"))
        reading, _resets_at = limits.read_death(record, tail, 0)
    except Exception:
        return
    model = record.get("model") or task.model
    if reading != limits.CONFIRMED or model in cfg.limited_models:
        return
    cfg.limited_models.add(model)
    if cfg.stream is not None:
        cfg.stream("%s died of a usage limit on %s; this run launches nothing more on %s"
                   % (task.id, model, model))


def _begin_task(cfg, task):
    """Pre flight, baseline, brief, running upsert. None means skip/exclude/already done."""
    manifest, adapter, store = cfg.manifest, cfg.adapter, cfg.store
    repo, default, env = cfg.repo, cfg.default, cfg.env
    stream = cfg.stream
    record = store.get(task.id) or state.new_record(task.id)
    status = record.get("status")

    # Issue #19. Only the manifest's exclusion is decided before anything is read, and it is
    # decided afresh every run, so un-excluding a task in the manifest launches it. A runner
    # decided skip returns early from nowhere: every check that wrote one runs again below, so
    # a card the operator fixed between runs launches rather than staying skipped for good.
    if task.excluded:
        store.upsert(task.id, status=contracts.STATUS_EXCLUDED, excluded_reason=task.reason,
                     skip_reason=None)
        return
    if status == contracts.STATUS_LANDED:
        return
    # KTD6, R10. Before anything is read or written for it: no upsert, no branch, no card read,
    # so the record, the branch, and the card are what they were. Ahead of the blocked check, so
    # an id also given to `--retry-blocked` is deferred. Past the landed check, so a landed task
    # the run would not have touched anyway is not announced as held.
    if task.id in cfg.defer:
        if stream is not None:
            stream("%s deferred by --defer; left as it was for this run" % task.id)
        return
    branch = gitwrite.task_branch_for(task.id, cfg.manifest.project.branch_prefix)

    if status == contracts.STATUS_BLOCKED and not retries_blocked(cfg.retry_blocked, task.id):
        return
    # R11, KTD7. A model that reported its usage limit earlier in this run launches nothing more
    # in it. Returned from with nothing read or written, the way `--defer` returns, so the Task
    # keeps whatever record it had and a later run launches it like any Task never reached. Past
    # the blocked check, so only a Task this run would otherwise have launched is named as passed
    # over: a blocked record nobody asked to retry would not launch in a later run either.
    if task.model in cfg.limited_models:
        cfg.limit_passed_over.append({"task": task.id, "model": task.model})
        if stream is not None:
            stream("%s passed over: %s reported its usage limit earlier in this run"
                   % (task.id, task.model))
        return

    # Issue #58. The manifest's resolution decides where a relaunch goes, so a task the operator
    # moved lands on the CLI they moved it to. Computed past every early return, so a task that
    # will not relaunch never reports a move, and announced ahead of the stranded branch check
    # and pre-flight, the two refusals an operator most needs it beside: both leave the move
    # unperformed on the repair path SKILL.md sends them down, and a run that never reaches the
    # launch would otherwise say nothing at all. The sentence is intent, not history, because
    # those refusals are still ahead of it.
    reassignment = _reassignment(record, task)
    if reassignment and stream is not None:
        stream("%s will relaunch on %s"
               % (task.id, summary.cause_line(contracts.BACKEND_REASSIGNED, reassignment)))

    if status == contracts.STATUS_BLOCKED:
        # Prefer the name recorded when the task blocked. A later prefix edit must not hide
        # a stranded branch that still carries commits.
        _clear_blocked_branch(store, task, repo, record, env, record.get("branch") or branch)

    # Pre-flight (R16). A failure here is a halt: the repo is not in the state a task process
    # can start from, and no launch may happen until the operator has looked.
    preflight = gitwrite.preflight(repo, default, branch, env=env,
                                   pushes=manifest_module.pushes(manifest))
    if not preflight.ok:
        message = ("pre flight refused before launching %s on check %s"
                   % (task.id, preflight.failed))
        if preflight.failed == "no_task_branch":
            message += ". %s already exists. %s" % (branch, gitwrite.keep_and_free_hint(branch))
        raise _Halt(task.id, contracts.HALT_UNCLEAN_EXIT, message,
                    {"branch": branch, "check": preflight.failed,
                     "evidence": preflight.evidence})

    # Baseline (R17): what the runner will compare against when it decides landing.
    card = adapter.read(task.id)
    if card.get("skipped"):
        _skip(cfg, task.id, "the tracker card could not be read: %s" % card["skipped"])
        return
    baseline_sha = gitread.rev_parse(repo, default)
    card_status = adapter.status(task.id)
    if card_status.get("terminal"):
        # Startup re-verify runs before this and promotes a task that landed by hand. A card
        # that is terminal and was not promoted was closed elsewhere, and a task process given
        # it has nothing to do. The first Cratekit run relaunched a closed issue this way.
        _skip(cfg, task.id, "the card already reads %s, which is terminal; nothing to run"
              % card_status.get("status"))
        return
    # Issue #22. One read serves both the brief, which carries every comment on the card now, and
    # the baseline, which is the newest of them, so the task and the closeout see a clean split.
    # The markdown adapter numbers its comments from one, so its newest id is also its count and
    # the same rule works for all three adapters (R17).
    comments = brief.launch_comments(adapter, task.id)
    card = dict(card, comments=comments)
    baseline_comment_id = comments[-1]["id"] if comments else None

    # Brief and the pre-flight scan (R7, R41, R43).
    brief_text = brief.render(manifest, task, card)
    hits = brief.scan(card, brief_text)
    if hits:
        _skip(cfg, task.id, brief.exclusion_reason(hits))
        return
    brief_path, brief_sha = brief.write(store, task.id, brief_text)

    capability = backends.build(task.backend).CAPABILITY
    # Supplied afresh by this attempt rather than cleared and rewritten after the launch. `upsert`
    # merges, so nothing else resets it, and while the record pinned the backend a stale one was
    # impossible. Clearing it here and restoring it later would leave the whole task process
    # lifetime, hours in the shipped examples, during which a crash strands a record that ran on a
    # CLI which cannot refuse tools while disclosing nothing about it.
    unenforced = (_unenforced_scalar(manifest, capability)
                  if not capability.enforces_at_launch else None)
    log_path = store.path("logs", task.id + ".stdout.log")
    if reassignment:
        # The log is per task and appended to by every attempt, so a move leaves one file holding
        # two backends' output. That is not cosmetic: `codex.readable` counts the file's own lines
        # and would pass on the previous CLI's, and the follower decodes the whole file with the
        # manifest's current grammar. Start the reassigned attempt on an empty one.
        with open(log_path, "w", encoding="utf-8"):
            pass

    # Every halt field clears here, not just the class. `halt_evidence` feeds the Cause line last
    # and wins over the fresh record, so a leftover key would name a previous attempt's sha or
    # branch inside a well formed sentence. The host snapshots clear for the same reason: an
    # attempt that never reaches its own launch must not print the last one's host line.
    # Issue #51: a relaunch can read the in review status its last attempt left behind, and
    # `launch_baseline` keeps the status the card read before that attempt instead. Issue #64:
    # this attempt's Task moves the card at its first step, so the record says so from here
    # until a read back after a Closeout finds the card out of review.
    baseline_status = closeout.launch_baseline(manifest, record, card_status.get("status"),
                                               store.audit())
    store.upsert(task.id, status=contracts.STATUS_RUNNING, baseline_sha=baseline_sha,
                 baseline_tracker_status=baseline_status,
                 baseline_comment_id=baseline_comment_id, branch=branch,
                 brief_sha256=brief_sha, halt_class=None, halt_stage=None,
                 halt_message=None, halt_evidence=None, envelope_verdict=None,
                 host_at_start=None, host_at_end=None,
                 excluded_reason=None, skip_reason=None,
                 findings=[reassignment] if reassignment else [],
                 continued_past=False, backend=task.backend, model=task.model,
                 unenforced_restrictions=unenforced, card_in_review_by_run=True)

    return _Begun(task=task, card=card, branch=branch, baseline_sha=baseline_sha,
                  baseline_comment_id=baseline_comment_id, brief_text=brief_text,
                  log_path=log_path, capability=capability, reassignment=reassignment)


def _launch_begun(cfg, begun, cwd, on_started=None):
    """Blocking launch of a begun task on the main thread, which serial run uses on the repo.
    Dispatch launches its builds through `_spawn_flight` instead."""
    launched = launch.launch(
        cfg.manifest, begun.task, begun.brief_text, begun.log_path,
        cfg.overrides.get("task_seconds") or cfg.manifest.timeouts.task_minutes * 60,
        home=cfg.home, base_env=cfg.base_env, stream=cfg.stream, heartbeat=cfg.store.heartbeat,
        on_release=cfg.store.release if cfg.release_on_interrupt else None, cwd=cwd,
        on_started=on_started, **cfg.launch_kwargs)
    if not launched.launch_error:
        cfg.used_backends.add(begun.task.backend)
    return launched


def _complete_task(cfg, begun, launched, tree_repo=None):
    """Classify and take the merge, blocked, or halt route. Always on the main thread.

    `tree_repo` is the checkout the process ran in when the runner can still read it, which is
    the repo itself on a serial run. Dispatch passes none: its worktree is already gone, and
    `begun.tree_at_exit` carries the tree it read before removing it."""
    manifest, adapter, store = cfg.manifest, cfg.adapter, cfg.store
    task, capability = begun.task, begun.capability
    reassignment = begun.reassignment
    branch, baseline_sha = begun.branch, begun.baseline_sha
    stream = cfg.stream

    disallow = (manifest_module.resolved_disallowed(manifest)
                if not capability.enforces_at_launch else None)
    digest = classify.classify(launched.transcript_path, launched,
                               adapter.write_tool_patterns(), backend=task.backend,
                               disallow_patterns=disallow,
                               review_base=manifest.project.default_branch)
    digest["task_id"] = task.id
    raw_findings = digest.get("findings")
    if raw_findings is not None:
        digest["findings"] = list(raw_findings)
    # The record's list, deliberately not the digest's object. A reassignment is a routing note
    # about the operator's own edit, and the digest is what closeout renders as Other findings
    # bullets in the Closeout brief, so sharing the list would have the Closeout process comment
    # a tracker card about a routing change (issue #58).
    findings = ([reassignment] if reassignment else []) + list(raw_findings or [])
    digest["envelope_verdict"] = _envelope_verdict(
        digest, cfg.repo, branch, baseline_sha, tree=begun.tree_at_exit, tree_repo=tree_repo)
    classify.write_digest(digest, store.path("digests", task.id + ".json"))
    # `unenforced_restrictions` is not rewritten here: the running upsert above already supplied
    # it from this attempt's own capability, before the launch rather than after it.
    store.upsert(task.id, session_id=launched.session_id,
                 transcript_path=launched.transcript_path, wall_seconds=launched.wall_seconds,
                 active_seconds=launched.active_seconds, findings=findings,
                 host_at_start=launched.host_at_start, host_at_end=launched.host_at_end,
                 binary_path=launched.binary_path, args=launched.args,
                 envelope_verdict=digest["envelope_verdict"])

    context = _Context(task=task, card=begun.card, branch=branch, baseline_sha=baseline_sha,
                       baseline_comment_id=begun.baseline_comment_id, digest=digest,
                       launched=launched, findings=findings,
                       tree_at_exit=begun.tree_at_exit, current_at_exit=begun.current_at_exit,
                       **vars(cfg))

    # From here on, every raise is a halt on a task whose process has already launched and whose
    # brief already told it to move the card (R1). The wrap makes that halt visible on the card
    # too (R4): _note_halt runs once, right where the halt is classified, and never changes what
    # gets raised (KTD4).
    try:
        if launched.launch_error:
            raise _Halt(task.id, contracts.HALT_UNEXPECTED_ERROR,
                        "%s could not be launched: %s" % (task.id, launched.launch_error),
                        {"task": task.id, "error": launched.launch_error,
                         "error_type": "launch failure"})

        if launched.lease_lost:
            raise _Halt(task.id, contracts.HALT_RUNNER_CRASHED,
                        "the lease was lost while %s was running; another runner may hold it"
                        % task.id,
                        {"status_before": contracts.STATUS_RUNNING, "branch": branch})

        destructive = _destructive_finding(findings)
        if destructive is not None:
            line = summary.cause_line(contracts.UNENFORCED_DISALLOWED, destructive)
            raise _Halt(task.id, contracts.HALT_UNEXPECTED_ERROR, line,
                        {"task": task.id, "error_type": "destructive_call", "error": line})

        if not capability.enforces_at_launch and digest.get("findings_unavailable"):
            raise _Halt(task.id, contracts.HALT_UNEXPECTED_ERROR,
                        "%s evidence could not be read; unenforced restrictions were not audited"
                        % task.id,
                        {"task": task.id, "error_type": "findings_unavailable",
                         "error": "unenforced restrictions were not audited"})

        if digest.get("halt_class") == contracts.HALT_TIMEOUT:
            return _timeout_route(context)

        routable, note = _routable(manifest, adapter, digest, cfg.repo, branch, baseline_sha)
        if note and stream is not None:
            stream("%s: %s" % (task.id, note))
        if routable:
            return _merge_route(context)
        if digest.get("routable"):
            # The process claimed complete and produced nothing the runner can merge. That is not
            # a blocked task, which leaves the repo as it found it deliberately; it is an exit the
            # runner cannot act on, so it halts for a human.
            raise _Halt(task.id, contracts.HALT_UNCLEAN_EXIT,
                        "%s reported status complete but left no commits on %s" % (task.id, branch),
                        {"branch": branch, "baseline_sha": baseline_sha})
        return _blocked_route(context, digest.get("halt_class") or contracts.HALT_BLOCKED_ENVELOPE)
    except _Halt as halt:
        _note_halt(context, halt)
        raise


def _snapshot_and_remove(cfg, dest):
    """Read the worktree's tree state, then remove it so the merge tail can check the branch out."""
    tree = current = None
    if dest and os.path.isdir(dest):
        try:
            tree = "clean" if gitread.is_clean(dest) else "dirty"
            current = gitread.current_branch(dest)
        except gitread.GitError:
            tree, current = "dirty", None
    worktree.remove(cfg.repo, dest, env=cfg.env)
    return tree, current


def _spawn_flight(cfg, begun, dest):
    pgid_box = []
    result_box = []
    stopping = threading.Event()

    def on_started(_pid, group_id):
        # No group id means the process was gone before its group could be read (issue #79).
        # Nothing is left to signal, so the flight stays groupless and `_flight_exited` goes by
        # its thread instead.
        if group_id is None:
            return
        # The group first and the check second, the reverse of `_stop_flights`, so one of the
        # two always sees the other and a late start is never missed.
        pgid_box.append(group_id)
        if stopping.is_set():
            _signal_group(group_id, signal.SIGKILL)

    def worker():
        try:
            # No release callback (issue #79). Only `dispatch` releases, after every flight is
            # stopped; a thread cannot install the handler that would call it anyway.
            launched = launch.launch(
                cfg.manifest, begun.task, begun.brief_text, begun.log_path,
                cfg.overrides.get("task_seconds") or cfg.manifest.timeouts.task_minutes * 60,
                home=cfg.home, base_env=cfg.base_env, stream=None,
                heartbeat=cfg.store.heartbeat, on_release=None, cwd=dest,
                on_started=on_started, **cfg.launch_kwargs)
            result_box.append(("ok", launched))
        except Exception as exc:
            result_box.append(("err", exc))

    thread = threading.Thread(target=worker, name="relay-build-%s" % begun.task.id, daemon=True)
    thread.start()
    return _Flight(begun=begun, worktree=dest, thread=thread, pgid=pgid_box, box=result_box,
                   stopping=stopping)


def _wait_any_flight(slots, timeout=0.1):
    for backend, flight in list(slots.items()):
        flight.thread.join(timeout=timeout)
        if not flight.thread.is_alive():
            return backend, flight
    return None, None


def _abandon_build(cfg, task_id, branch, dest=None):
    """Kill a sibling's leftover: worktree gone, branch gone, record pending for a fresh start."""
    if dest:
        try:
            worktree.remove(cfg.repo, dest, env=cfg.env)
        except worktree.WorktreeError:
            pass
    if branch and gitread.branch_exists(cfg.repo, branch):
        try:
            gitwrite.delete_branch(cfg.repo, branch, ops=cfg.store, task_id=task_id, env=cfg.env)
        except gitread.GitError:
            pass
    cfg.store.upsert(task_id, status=contracts.STATUS_PENDING, session_id=None,
                     transcript_path=None, halt_class=None, halt_stage=None,
                     halt_message=None, halt_evidence=None, skip_reason=None,
                     envelope_verdict=None, host_at_start=None, host_at_end=None)


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except OSError:
        pass


def _flight_exited(flight):
    """No process is left in the flight's group. A flight with no group, because its process has
    not started yet or was gone before launch could read its group, has exited only once its
    thread has returned. The thread is not waited on once the group is
    empty: a descendant that left the group can hold the pipe open and keep the launch's reader
    waiting long past the bound, and nothing of this build is running in the meantime."""
    if not flight.pgid:
        return not flight.thread.is_alive()
    try:
        os.killpg(flight.pgid[0], 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _stop_flights(cfg, flights):
    """Issue #71. End every flight's process group and wait for each to exit, the way the triple
    route waits in `_triple_workers_stopped`. The build threads run `launch.launch` off the main
    thread, where it cannot install its signal handlers, and each Task process sits in its own
    session, so nothing else passes a stop on to them. Every group gets SIGTERM together, then
    SIGKILL once the grace has passed, and a flight whose process starts during the wait is
    signalled when its group appears. Returns the flights still alive at the bound."""
    for flight in flights:
        flight.stopping.set()
    grace = cfg.launch_kwargs.get("sigkill_grace_seconds", launch.SIGKILL_GRACE_SECONDS)
    if flights and cfg.stream is not None:
        # Said before the wait, which can run the whole grace, so a silent pause does not draw a
        # second interrupt that would leave the stop half done.
        cfg.stream("stopping %d build(s): %s; up to %ds"
                   % (len(flights), ", ".join(f.begun.task.id for f in flights),
                      round(grace + FLIGHT_EXIT_SECONDS)))
    kill_at = time.monotonic() + grace
    give_up_at = kill_at + FLIGHT_EXIT_SECONDS
    sent = {}
    while True:
        alive = [flight for flight in flights if not _flight_exited(flight)]
        now = time.monotonic()
        if not alive or now >= give_up_at:
            return alive
        signum = signal.SIGTERM if now < kill_at else signal.SIGKILL
        for flight in alive:
            if flight.pgid and sent.get(id(flight)) != signum:
                _signal_group(flight.pgid[0], signum)
                sent[id(flight)] = signum
        time.sleep(0.05)


def _abort_siblings(cfg, slots, waiting, keep_id):
    """On a halt that does not continue past: drop every other in flight or waiting build.

    A flight still alive after `_stop_flights` keeps its worktree, branch, and running record,
    because a process is still using them, and is named on `cfg.surviving_flights` for the
    terminal record. Every flight is named there until the stop has said otherwise, so a raise
    partway through leaves none unnamed. `keep_id` of None drops every build, which is an
    interrupted dispatch."""
    flights = [flight for flight in slots.values() if flight.begun.task.id != keep_id]
    # A survivor an earlier abort named has already left `slots`, so it is carried rather than
    # lost when a halt's abort is followed by the interrupt's.
    earlier = [entry for entry in cfg.surviving_flights
               if entry["task"] not in {flight.begun.task.id for flight in flights}]
    cfg.surviving_flights = earlier + _named_flights(flights)
    survivors = _stop_flights(cfg, flights)
    cfg.surviving_flights = earlier + _named_flights(survivors)
    surviving_ids = {flight.begun.task.id for flight in survivors}
    for task_id, flight in list(slots.items()):
        if task_id == keep_id:
            continue
        del slots[task_id]
        if task_id not in surviving_ids:
            _abandon_build(cfg, task_id, flight.begun.branch, flight.worktree)
    for task_id, (begun, _launched) in list(waiting.items()):
        if task_id == keep_id:
            continue
        _abandon_build(cfg, task_id, begun.branch)
        del waiting[task_id]
    if cfg.stream is not None:
        for entry in _named_flights(survivors):
            cfg.stream("%s: its Task process group %s was still alive after the stop; left "
                       "running with its worktree" % (entry["task"], entry["process_group"]))


def _named_flights(flights):
    return [{"task": flight.begun.task.id,
             "process_group": flight.pgid[0] if flight.pgid else None} for flight in flights]


def _record_halt(cfg, halt, task):
    """The same halt upsert `run` does, so dispatch and serial cannot disagree."""
    continued = _continue_past(cfg, halt)
    previous = cfg.store.get(halt.task_id) or {}
    kept = previous if previous.get("backend") else {"backend": task.backend,
                                                     "model": task.model}
    cfg.store.upsert(halt.task_id, status=contracts.STATUS_HALTED,
                     halt_class=halt.halt_class, halt_evidence=halt.evidence,
                     halt_message=halt.message, continued_past=continued,
                     backend=kept.get("backend"), model=kept.get("model"))
    if continued and cfg.stream is not None:
        cfg.stream("%s halted with class %s; continuing past it"
                   % (halt.task_id, halt.halt_class))
    return continued


def _concurrent_loop(cfg, announce):
    """Launch ready schedule waves, then merge strictly in manifest order.

    Issue #71. Anything leaving the loop by an exception, an interrupt from the keyboard most
    often, ends every build in flight first and abandons it the way a halt that does not continue
    past does, so dispatch's own handlers mark records and release the lease only once no Task
    process is left behind them. That holds for an interrupt landing in the Closeout on the main
    thread too: its handler ends the Closeout and raises, and releases nothing (issue #79). A
    build that would not die is kept on `cfg.surviving_flights`
    for the terminal record. A build already finished and waiting its merge is left alone: its
    process has exited, and its branch is completed work the next pre flight will name, not a
    half built one to discard."""
    slots = {}
    waiting = {}
    try:
        return _concurrent_drive(cfg, announce, slots, waiting)
    except BaseException:
        try:
            _abort_siblings(cfg, slots, {}, None)
        except Exception:
            # Best effort, like every write on the way out: the exception already leaving is
            # the one to keep.
            pass
        raise


def _concurrent_drive(cfg, announce, slots, waiting):
    tasks = list(cfg.manifest.tasks)
    n = len(tasks)
    by_id = {task.id: task for task in tasks}
    settled = set()
    next_merge = 0
    predecessors = {task.id: set() for task in tasks}
    for edge in getattr(cfg.schedule, "edges", ()):
        predecessors[edge.second].add(edge.first)

    def in_play(task_id):
        if task_id in settled or task_id in waiting:
            return True
        return any(flight.begun.task.id == task_id for flight in slots.values())

    def fill():
        for task in tasks:
            if in_play(task.id):
                continue
            # An edge means the earlier task must have fully settled: its merge, gate, push,
            # closeout, and verification all complete before the successor reads its baseline.
            if not predecessors[task.id].issubset(settled):
                continue
            begun = _begin_task(cfg, task)
            if begun is None:
                settled.add(task.id)
                continue
            dest = worktree.path_for(cfg.store, task.id)
            try:
                worktree.add(cfg.repo, dest, begun.baseline_sha, env=cfg.env)
            except worktree.WorktreeError as exc:
                raise _Halt(task.id, contracts.HALT_UNEXPECTED_ERROR,
                            "could not create a worktree for %s: %s" % (task.id, exc),
                            {"task": task.id, "error_type": "worktree", "error": str(exc)[:500]})
            begun.worktree = dest
            slots[task.id] = _spawn_flight(cfg, begun, dest)
            if cfg.stream is not None:
                cfg.stream("%s building on %s in a worktree" % (task.id, task.backend))

    def drain():
        nonlocal next_merge
        while next_merge < n:
            task = tasks[next_merge]
            if task.id in settled:
                next_merge += 1
                cfg.store.set_cursor(next_merge)
                continue
            if task.id not in waiting:
                return
            begun, launched = waiting.pop(task.id)
            _complete_task(cfg, begun, launched)
            settled.add(task.id)
            cfg.expected_default = gitread.rev_parse(cfg.repo, cfg.default)
            next_merge += 1
            cfg.store.set_cursor(next_merge)

    def handle_halt(halt, task):
        continued = _record_halt(cfg, halt, task)
        if continued:
            settled.add(halt.task_id)
            if halt.task_id in waiting:
                del waiting[halt.task_id]
            cfg.expected_default = gitread.rev_parse(cfg.repo, cfg.default)
            return None
        _abort_siblings(cfg, slots, waiting, halt.task_id)
        _audit_cards(cfg)
        _write_terminal(cfg.store, cfg.env, contracts.RUN_HALTED, halt.task_id, halt.halt_class,
                        cfg.used_backends, announce=announce,
                        surviving_flights=cfg.surviving_flights)
        return RunOutcome(EXIT_HALTED, halt.task_id, halt.halt_class, halt.message,
                          cfg.store, cfg.store.records())

    try:
        fill()
        drain()
    except _Halt as halt:
        outcome = handle_halt(halt, by_id.get(halt.task_id) or tasks[0])
        if outcome is not None:
            return outcome

    while slots or waiting:
        backend, flight = _wait_any_flight(slots)
        if flight is None:
            continue
        del slots[backend]
        if not flight.box:
            halt = _Halt(flight.begun.task.id, contracts.HALT_UNEXPECTED_ERROR,
                         "%s build thread ended with no result" % flight.begun.task.id,
                         {"task": flight.begun.task.id, "error_type": "empty_flight"})
            outcome = handle_halt(halt, flight.begun.task)
            if outcome is not None:
                return outcome
            try:
                fill()
            except _Halt as halt:
                outcome = handle_halt(halt, by_id.get(halt.task_id) or flight.begun.task)
                if outcome is not None:
                    return outcome
            continue
        kind, payload = flight.box[0]
        tree, current = _snapshot_and_remove(cfg, flight.worktree)
        flight.begun.tree_at_exit = tree
        flight.begun.current_at_exit = current
        if kind == "err":
            halt = _Halt(flight.begun.task.id, contracts.HALT_UNEXPECTED_ERROR,
                         "the runner hit an unexpected %s on %s: %s"
                         % (type(payload).__name__, flight.begun.task.id, payload),
                         {"task": flight.begun.task.id, "error_type": type(payload).__name__,
                          "error": str(payload)[:500]})
            outcome = handle_halt(halt, flight.begun.task)
            if outcome is not None:
                return outcome
        else:
            launched = payload
            if not launched.launch_error:
                cfg.used_backends.add(flight.begun.task.backend)
            waiting[flight.begun.task.id] = (flight.begun, launched)
        try:
            drain()
            fill()
        except _Halt as halt:
            outcome = handle_halt(halt, by_id.get(halt.task_id) or flight.begun.task)
            if outcome is not None:
                return outcome
    return None


@dataclass
class _Context(_Run):
    """One task's tail: the run wide values plus what this task produced, so each route below
    reads as the sequence R50 names rather than as parameter threading."""
    task: object = None
    card: dict = None
    branch: str = None
    baseline_sha: str = None
    baseline_comment_id: object = None
    digest: dict = None
    launched: object = None
    findings: list = None
    tree_at_exit: str | None = None
    current_at_exit: str | None = None


def _clear_blocked_branch(store, task, repo, record, env, branch):
    """R48: `--retry-blocked` may delete a stranded branch only when it carries nothing past the
    baseline. Work that exists only on that branch is the operator's to keep or discard."""
    if not gitread.branch_exists(repo, branch):
        return
    baseline = record.get("baseline_sha")
    if baseline and gitread.log_oneline(repo, baseline, branch):
        raise _Halt(task.id, contracts.HALT_UNCLEAN_EXIT,
                    "retry refused: %s carries commits past the baseline; keep or discard them "
                    "by hand first. %s" % (branch, gitwrite.keep_and_free_hint(branch)),
                    {"branch": branch, "baseline_sha": baseline})
    gitwrite.delete_branch(repo, branch, ops=store, task_id=task.id, env=env)


def _timeout_route(ctx):
    """R35 and R50. A clean tree takes the blocked path with a digest naming the timeout, so the
    run continues past a task that ran long. A dirty tree halts, because nobody can tell from
    here whether the half written state is safe to build on."""
    disposition = gitwrite.timeout_disposition(
        ctx.repo, ctx.default, ctx.branch,
        tree=ctx.tree_at_exit, current=ctx.current_at_exit)
    # Both units on purpose. The seconds are the measurement and the minutes are what the
    # cause line names; deriving the minutes at render time would put a unit conversion in the
    # summary, which is the one place that must not compute anything.
    ctx.digest["timeout"] = {
        "tree": disposition.tree, "branch": disposition.branch,
        "active_seconds": ctx.launched.active_seconds, "wall_seconds": ctx.launched.wall_seconds,
        "active_minutes": round((ctx.launched.active_seconds or 0) / 60.0),
        "wall_minutes": round((ctx.launched.wall_seconds or 0) / 60.0),
    }
    if disposition.action == "halt":
        verdict = verify.verify(ctx.manifest, ctx.store.get(ctx.task.id), ctx.adapter,
                                scope=verify.SCOPE_CODE, env=ctx.env, now=ctx.now)
        ctx.store.upsert(ctx.task.id, verify=verdict.as_dict())
        raise _Halt(ctx.task.id, contracts.HALT_TIMEOUT,
                    "%s timed out after %.0f active seconds and left the tree dirty on %s"
                    % (ctx.task.id, ctx.launched.active_seconds, disposition.branch),
                    ctx.digest["timeout"])
    return _blocked_route(ctx, contracts.HALT_TIMEOUT)


def _merge_route(ctx):
    """R50, local merge, routable to merge. Every step before the closeout is the runner's own,
    and every one of them can refuse."""
    if ctx.manifest.shipping_mode in manifest_module.UNIMPLEMENTED_SHIPPING_MODES:
        # Unreachable through the CLI, which validates first and refuses the mode there. Kept as
        # a backstop for a caller that builds a manifest by hand, and deliberately not
        # ci_undecided: that class tells an operator to wait for CI, and there is no pull
        # request being checked.
        raise _Halt(ctx.task.id, contracts.HALT_UNEXPECTED_ERROR,
                    "shipping.mode %s is not implemented" % ctx.manifest.shipping_mode,
                    {"task": ctx.task.id, "error_type": "unimplemented shipping mode",
                     "error": "%s has no sequence in the run loop; relay validate refuses it"
                              % ctx.manifest.shipping_mode})

    if not backends.build(ctx.task.backend).CAPABILITY.enforces_at_launch:
        allowed = manifest_module.task_allowed_paths(ctx.manifest)
        if allowed is not None:
            offenders = gitwrite.task_scope_offenders(
                ctx.repo, ctx.baseline_sha, ctx.branch, allowed)
            if offenders:
                detail = "commit on %s touched %s outside the Task path bound" % (
                    ctx.branch, ", ".join(offenders))
                evidence = {"detail": detail, "branch": ctx.branch,
                            "paths": ", ".join(offenders)}
                raise _Halt(ctx.task.id, contracts.HALT_PATH_GATE, detail, evidence)

    ctx.store.upsert(ctx.task.id, status=contracts.STATUS_MERGING)
    # The gate is the longest thing the runner does without a child process to heartbeat for
    # it, so the tail carries its own heartbeat and refuses to merge or push once the lease is
    # no longer ours (R31, R47).
    beat = launch._Heartbeat(ctx.store.heartbeat,
                             ctx.launch_kwargs.get("heartbeat_interval",
                                                   contracts.LEASE_HEARTBEAT_SECONDS))
    beat.start()
    try:
        tail = gitwrite.local_merge_tail(
            ctx.repo, ctx.task.id, ctx.default, ctx.baseline_sha, list(ctx.manifest.gate.command),
            ctx.store.path("gate", ctx.task.id + ".log"), ops=ctx.store, env=ctx.env,
            gate_timeout_seconds=ctx.overrides.get("gate_seconds"),
            still_ours=lambda: not beat.lost,
            branch=ctx.branch, pushes=manifest_module.pushes(ctx.manifest),
            expected_default=ctx.expected_default)
    finally:
        beat.stop()
    if not tail.ok:
        # `halt_stage` alongside the evidence (issue #8). Two tail refusals can carry the same
        # halt class and mean different repairs, path_gate being the pair the record could not
        # tell apart: the backstop stage is a finished branch the Runner declined to land, while
        # the same class from classify's transcript scan is work that never happened.
        ctx.store.upsert(ctx.task.id, halt_evidence=tail.evidence, halt_stage=tail.stage)
        raise _Halt(ctx.task.id, tail.halt_class,
                    summary.cause_line(tail.halt_class, tail.evidence),
                    tail.evidence)

    ctx.store.upsert(ctx.task.id, landing_ref=tail.merge_sha)
    verdict = verify.verify(ctx.manifest, ctx.store.get(ctx.task.id), ctx.adapter,
                            scope=verify.SCOPE_CODE, env=ctx.env, now=ctx.now)
    ctx.store.upsert(ctx.task.id, verify=verdict.as_dict())
    if verdict.failed():
        raise _Halt(ctx.task.id, contracts.HALT_GATE_REFUSED,
                    "the code scope verify failed for %s on %s"
                    % (ctx.task.id, ", ".join(verdict.failed())),
                    {"branch": ctx.default, "sha": tail.merge_sha,
                     "log": ctx.store.path("gate", ctx.task.id + ".log"),
                     "checks": verdict.checks})

    gate_summary = {"ok": True, "returncode": 0,
                    "log": ctx.store.path("gate", ctx.task.id + ".log")}
    _run_closeout(ctx, closeout.OUTCOME_LANDED, landing_ref=tail.merge_sha,
                  commit_range="%s..%s" % (ctx.baseline_sha[:7], (tail.merge_sha or "")[:7]),
                  gate=gate_summary)

    # A mirror is a push by another name. validate refuses a mirror under shipping.push = false;
    # this is the backstop for a manifest built by hand past validate (KTD4 of the no push plan).
    if ctx.manifest.project.mirror and manifest_module.pushes(ctx.manifest):
        pushed = gitwrite.mirror_push(ctx.repo, list(ctx.manifest.project.mirror), ops=ctx.store,
                                      task_id=ctx.task.id, env=ctx.env,
                                      timeout=ctx.overrides.get("gate_seconds"))
        if not pushed.ok:
            raise _Halt(ctx.task.id, contracts.HALT_GATE_REFUSED,
                        "the mirror push was refused for %s" % ctx.task.id,
                        {"branch": ctx.default, "sha": tail.merge_sha,
                         "log": ctx.store.path("gate", ctx.task.id + ".log"),
                         "push_output": pushed.output})

    final = verify.verify(ctx.manifest, ctx.store.get(ctx.task.id), ctx.adapter,
                          scope=verify.SCOPE_FULL, do_fetch=True, env=ctx.env, now=ctx.now)
    ctx.store.upsert(ctx.task.id, verify=final.as_dict())
    if not final.landed:
        raise _Halt(ctx.task.id, final.halt_class or contracts.HALT_PARTIAL_LANDING,
                    "%s did not verify as landed: %s"
                    % (ctx.task.id, ", ".join(final.failed() + final.blocking_skips())),
                    {"sha": tail.merge_sha, "card_status": verify.card_status_of(final),
                     "branch": ctx.default, "checks": final.checks})

    # Issue #43: a closed issue verifies as landed on its own, so the item the Closeout was also
    # told to move is read here, after the landing is settled, and a lag is a finding only.
    finding = closeout.confirm_board_terminal(ctx.adapter, ctx.manifest, ctx.task.id,
                                              now=ctx.now)
    if finding:
        ctx.findings.append(finding)
    if gitread.branch_exists(ctx.repo, ctx.branch):
        gitwrite.delete_branch(ctx.repo, ctx.branch, ops=ctx.store, task_id=ctx.task.id,
                               env=ctx.env)
    ctx.store.upsert(ctx.task.id, status=contracts.STATUS_LANDED,
                     halt_class=contracts.HALT_LANDED, branch=None, findings=ctx.findings)


def _blocked_route(ctx, halt_class):
    """R50, blocked. The branch is stranded rather than merged, the closeout comments the card,
    and the run continues. A blocked task is a normal outcome (R23)."""
    stranded = gitwrite.blocked_path(ctx.repo, ctx.default, ctx.branch, ops=ctx.store,
                                     task_id=ctx.task.id)
    # Stale cards, R1: the task process moved the card to in review at its first step, and a
    # blocked task leaves nobody on it. The Closeout is told where the card came from; the
    # runner reads it back below and never moves it itself.
    # Issue #51: with no baseline the Closeout is told the card may be in review with nowhere
    # known to go, and the read back below still runs, so a card left there is a check by hand.
    record = ctx.store.get(ctx.task.id) or {}
    return_to = closeout.return_to_for(ctx.manifest, record)
    unknown = closeout.baseline_unknown(ctx.manifest, record)
    _run_closeout(ctx, closeout.OUTCOME_BLOCKED, branch=stranded["branch"], return_to=return_to,
                  baseline_unknown=unknown)

    finding = closeout.confirm_blocked_comment(ctx.adapter, ctx.task.id, ctx.baseline_comment_id)
    if finding:
        ctx.findings.append(finding)
    # Issue #64: a read back that finds the card out of review is the runner's last word on it,
    # and it is what lets the next launch tell the operator's staging from this run's leftover.
    returned = {}
    if return_to or unknown:
        finding, out = closeout.read_back(ctx.adapter, ctx.manifest, ctx.task.id, return_to)
        if finding:
            ctx.findings.append(finding)
        if out:
            returned["card_in_review_by_run"] = False
    # The class arrives from the digest, so the evidence has to cover every class that can
    # reach here: blocked_envelope wants the blocker, no_envelope the last message, timeout the
    # tree and the minutes. Recording only the stranded head left each of them a placeholder.
    envelope = ctx.digest.get("envelope") or {}
    blockers = envelope.get("blockers") or []
    evidence = {
        "stranded_head": stranded["head"],
        "branch": stranded["branch"] or ctx.branch,
        "blocker": blockers[0] if blockers else "no blocker text in the envelope",
        "last_message": ctx.digest.get("last_message") or "(no final message)",
    }
    if ctx.digest.get("findings_unavailable"):
        # unexpected_error reaches here now that unreadable evidence is a runner fault (KTD5),
        # and its line asks for fields no transcript could have supplied.
        evidence.update({
            "task": ctx.task.id,
            "error_type": "unreadable evidence",
            "error": "the transcript at %s could not be read"
                     % (ctx.digest.get("transcript_path") or "an unknown path"),
        })
    evidence.update(ctx.digest.get("timeout") or {})
    ctx.store.upsert(ctx.task.id, status=contracts.STATUS_BLOCKED, halt_class=halt_class,
                     branch=stranded["branch"], findings=ctx.findings,
                     halt_evidence=evidence, **returned)


def _run_closeout(ctx, outcome, landing_ref=None, branch=None, commit_range=None, gate=None,
                  halt_class=None, cause_line=None, return_to=None, baseline_unknown=False,
                  guarded_push=None):
    """Launch the closeout, then bound what it committed before anything is pushed (R53).

    The order matters: the check runs against the local head before the push, so a commit
    outside the allowed paths is reset rather than reported after the fact (KTD15).

    `halt_class`/`cause_line` are set only for `closeout.OUTCOME_HALTED`, from `_note_halt`.
    """
    allowed_paths = list(ctx.allowed_paths)
    pre_closeout_head = gitread.rev_parse(ctx.repo, "HEAD")
    try:
        comments = ctx.adapter.comments_since(ctx.task.id, ctx.baseline_comment_id)
    except Exception:
        comments = []
    envelope = ctx.digest.get("envelope") or {}

    result = closeout.run(
        ctx.manifest, ctx.card, outcome, ctx.digest, comments, ctx.adapter, ctx.store,
        allowed_paths, backend=ctx.task.backend, task_model=ctx.task.model,
        landing_ref=landing_ref, branch=branch or ctx.branch,
        commit_range=commit_range, gate=gate,
        wall_seconds=ctx.launched.wall_seconds, active_seconds=ctx.launched.active_seconds,
        halt_class=halt_class, cause_line=cause_line, return_to=return_to,
        baseline_unknown=baseline_unknown, timeout_seconds=ctx.overrides.get("closeout_seconds"),
        home=ctx.home, base_env=ctx.base_env, stream=ctx.stream, heartbeat=ctx.store.heartbeat,
        on_release=ctx.store.release if ctx.release_on_interrupt else None, **ctx.launch_kwargs)
    ctx.findings.extend(result.findings)
    ctx.store.upsert(ctx.task.id, closeout=result.result, findings=ctx.findings)

    if getattr(result.launch_result, "lease_lost", False):
        raise _Halt(ctx.task.id, contracts.HALT_RUNNER_CRASHED,
                    "the lease was lost while the closeout for %s was running; nothing was "
                    "pushed" % ctx.task.id,
                    {"stage": "closeout", "status_before": contracts.STATUS_MERGING,
                     "branch": ctx.branch})

    scope = gitwrite.closeout_scope_check(ctx.repo, pre_closeout_head, allowed_paths,
                                          ops=ctx.store, task_id=ctx.task.id, env=ctx.env)
    if not scope.ok:
        # Two classes reach here. A path outside the allowed set is out of scope; a tree the
        # closeout left dirty entirely inside the allowed set is an unclean exit. Both reset to
        # the pre closeout head, and neither pushes.
        evidence = {"branch": ctx.default, "reset_to": scope.reset_to,
                    "offending": scope.offending, "untracked": scope.untracked,
                    "path": ", ".join(scope.offending) or "nothing outside the allowed paths",
                    "allowed": ", ".join(allowed_paths) or "nothing outside the default branch"}
        raise _Halt(ctx.task.id, scope.halt_class,
                    summary.cause_line(scope.halt_class, evidence), evidence)

    # Under shipping.push = false the closeout's commit stays on the local default branch with
    # the landing it records; the scope check above still bounded it.
    if (manifest_module.pushes(ctx.manifest)
            and gitread.rev_parse(ctx.repo, "HEAD") != pre_closeout_head):
        pushed = (guarded_push() if guarded_push is not None else
                  gitwrite.push(ctx.repo, ["origin", ctx.default], ops=ctx.store,
                                task_id=ctx.task.id, env=ctx.env,
                                timeout=ctx.overrides.get("gate_seconds")))
        if not pushed.ok:
            raise _Halt(ctx.task.id, contracts.HALT_GATE_REFUSED,
                         "the push of the closeout commit was refused for %s" % ctx.task.id,
                        {"branch": ctx.default, "sha": gitread.rev_parse(ctx.repo, "HEAD"),
                         "log": ctx.store.path("gate", ctx.task.id + ".log"),
                         "push_output": pushed.output})
    return result


def _note_halt(ctx, halt):
    """R4: make a halt visible on the tracker card without changing its status, by launching the
    Closeout with `outcome=halted`, the same mechanism a landed or blocked outcome already uses.
    Best effort: everything below is wrapped so a failure anywhere in it, one of the checks, the
    checkout, or the Closeout launch itself, is logged and swallowed rather than propagating,
    leaving the halt already raised (`halt`) as the run's only record of what happened, per KTD4
    (this repo's own CLAUDE.md: "every stop is a named class with evidence, not an exception").
    An unguarded check that raised would otherwise escape `_one_task`'s `except _Halt` handler
    and reach `run()`'s own except-Exception path, which fabricates a new halt from whatever
    broke, exactly the masking this function exists to prevent. This is the same reason
    `_continue_past` wraps its own mutation in a `try` rather than trusting each check to stay
    side-effect free.

    Gate 1 also excludes a halt whose evidence carries `reset_to`: that key is unique to
    `gitwrite.closeout_scope_check`'s own `HALT_UNCLEAN_EXIT` raise (a Closeout that left
    in-scope work uncommitted), which already reset the tree before raising, so the tree-clean
    check alone would not catch it, and `HALT_UNCLEAN_EXIT` is too general a class to exclude
    outright (most of its raise sites are unrelated to Closeout and still want the comment).

    Four checks gate the launch, each closing a specific gap KTD3 names:

    1. The halt class is run-scoped, or means the Closeout mechanism itself just misbehaved
       (`CLOSEOUT_MISBEHAVED_HALT_CLASSES`, or the `reset_to` case above): the repository, a
       second runner, or Closeout's own trustworthiness is uncertain, so no second process is
       launched onto it.
    2. The tree is dirty: that state is the operator's own evidence to inspect, not a workspace
       to launch a process into (mirrors R16's pre-flight refusal for the task process).
    3. The default branch is not in sync with `origin/<default>` (`gitwrite.head_equals_remote`,
       the same check pre-flight and the resume disposition already share): `local_merge_tail`'s
       push step can fail after the merge already applied locally, and a commit the halted
       closeout makes on top would otherwise carry that unverified merge to origin alongside it.
       Under `shipping.push = false` nothing is carried anywhere and local runs ahead by design,
       so the same seam, `gitwrite.default_in_sync`, asks only that the remote not have
       diverged, which is what pre flight and the resume disposition ask too.
    4. The lease can no longer be confirmed as this runner's: the same freshness check
       `_continue_past` already applies before its own repository mutation.

    When this task's record already carries a `landing_ref` (a halt raised after its own landed
    closeout already ran, from a mirror push refusal or a failing final verify), that reference
    is passed through so the rendered comment reads as "landed, then this later step failed"
    instead of an undifferentiated halt on a card that already shows landed.
    """
    if (halt.halt_class in contracts.RUN_SCOPED_HALT_CLASSES
            or halt.halt_class in contracts.CLOSEOUT_MISBEHAVED_HALT_CLASSES
            or halt.evidence.get("reset_to") is not None):
        return
    try:
        if not gitread.is_clean(ctx.repo):
            if ctx.stream is not None:
                ctx.stream("%s: tree left dirty on %s; skipping the halt comment"
                           % (halt.task_id, gitread.current_branch(ctx.repo)))
            return
        in_sync, _check = gitwrite.default_in_sync(ctx.repo, ctx.default,
                                                   manifest_module.pushes(ctx.manifest), {})
        if not in_sync:
            if ctx.stream is not None:
                ctx.stream("%s: %s is not in sync with origin/%s; skipping the halt comment"
                           % (halt.task_id, ctx.default, ctx.default))
            return
        if not ctx.store.heartbeat():
            if ctx.stream is not None:
                ctx.stream("%s: lease no longer confirmed; skipping the halt comment"
                           % halt.task_id)
            return
        gitwrite.blocked_path(ctx.repo, ctx.default, ctx.branch, ops=ctx.store,
                              task_id=halt.task_id, env=ctx.env)
        record = ctx.store.get(halt.task_id) or {}
        # Stale cards, R1 and R2. `return_to_for` refuses when the record carries a landing
        # reference, which is exactly the halt-after-landing case the docstring above names:
        # that card is closed, and moving it back would undo a landing.
        return_to = closeout.return_to_for(ctx.manifest, record)
        unknown = closeout.baseline_unknown(ctx.manifest, record)
        _run_closeout(ctx, closeout.OUTCOME_HALTED, landing_ref=record.get("landing_ref"),
                     halt_class=halt.halt_class, cause_line=halt.message, return_to=return_to,
                     baseline_unknown=unknown)
        if return_to or unknown:
            finding, out = closeout.read_back(ctx.adapter, ctx.manifest, halt.task_id, return_to)
            if finding:
                ctx.findings.append(finding)
                ctx.store.upsert(halt.task_id, findings=ctx.findings)
            if out:
                ctx.store.upsert(halt.task_id, card_in_review_by_run=False)
    except Exception as exc:
        if ctx.stream is not None:
            ctx.stream("%s: could not comment halt %s on the tracker: %s"
                       % (halt.task_id, halt.halt_class, exc))

"""The operator interface (U10, R45).

Every verb is a subcommand.  Dispatch alone may ask a terminal operator to choose its run policy;
automation supplies `--policy` and detached children receive the resolved choice, so they never
wait on stdin.

Exit codes are the contract, since a detached runner is read by its exit status before anyone
reads its log: 0 fine, 1 the manifest or the environment is wrong, 2 the run halted and needs a
hand, 3 another runner holds the lease.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

from . import (adapters, audit as audit_module, brief as brief_module, contracts,
               feeder as feeder_module, manifest as manifest_module, notify, pair as pair_module,
               progress, run as run_module, state, summary, tail as tail_module, verify)

EXIT_OK = run_module.EXIT_OK
EXIT_CONFIG = run_module.EXIT_CONFIG
EXIT_HALTED = run_module.EXIT_HALTED
EXIT_LEASE = run_module.EXIT_LEASE


# Issue #23. A day: long enough to queue behind a whole manifest of hour long tasks, short enough
# that a queue behind a wedged runner still ends on its own.
DEFAULT_LEASE_WAIT_MINUTES = 1440


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which is Relay's halted code. A bad command line is a
    configuration problem, so it exits 1 like every other one."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        raise SystemExit(EXIT_CONFIG)


def _add_follow_options(parser):
    """The four options both following paths share, added in one place so `run --follow` and
    `tail` cannot drift apart. `--for` names its destination because `for` is a Python keyword
    and argparse's default destination would be unreachable."""
    parser.add_argument("--phases", action="store_true",
                        help="print phase events only, without the decoded task activity")
    parser.add_argument("--for", type=int, dest="for_seconds", metavar="SECONDS",
                        help="stop following after this many seconds; the run continues")
    parser.add_argument("--notify", action="store_true",
                        help="fire a macOS notification on each phase event, from the runner "
                             "itself on `run` and from the follower on `tail`")
    parser.add_argument("--bar", action="store_true",
                        help="print a progress bar line whenever the counts move, and once a "
                             "minute in between; never notifies")


def build_parser():
    parser = _Parser(prog="relay", description="Run a manifest of independent tasks unattended.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    validate = verbs.add_parser("validate", help="check a manifest and its target repo")
    validate.add_argument("manifest")
    validate.add_argument("--list", action="store_true", dest="list_candidates",
                          help="also print the tracker's candidate tasks")

    run_verb = verbs.add_parser("run", help="run the manifest to completion or to a halt")
    run_verb.add_argument("manifest")
    run_verb.add_argument("--retry-blocked", action="store_true",
                          help="retry tasks whose records read blocked")
    run_verb.add_argument("--detach", action="store_true",
                          help="start the run in its own session, logging to the state "
                               "directory, and return at once")
    run_verb.add_argument("--wait-for-lease", type=int, nargs="?", const=DEFAULT_LEASE_WAIT_MINUTES,
                          dest="wait_for_lease", metavar="MINUTES",
                          help="when another runner holds the lease on this manifest or its "
                               "repo, wait for it to clear and then run, polling once a minute "
                               "for up to MINUTES (default %d); with --detach the detached "
                               "runner does the waiting" % DEFAULT_LEASE_WAIT_MINUTES)
    run_verb.add_argument("--follow", action="store_true",
                          help="detach, then follow this run in the foreground; implies --detach")
    _add_follow_options(run_verb)

    status = verbs.add_parser("status", help="print the run status without taking the lease")
    status.add_argument("manifest")

    tail_verb = verbs.add_parser("tail", help="follow the running task's activity, decoded")
    tail_verb.add_argument("manifest")
    _add_follow_options(tail_verb)

    summary_verb = verbs.add_parser("summary", help="print the run summary")
    summary_verb.add_argument("manifest")
    summary_verb.add_argument("--json", action="store_true", dest="as_json")

    audit_verb = verbs.add_parser("audit", help="list the cards that disagree with the "
                                                "record and with git; never takes the lease")
    audit_verb.add_argument("manifest")

    verify_verb = verbs.add_parser("verify", help="re-run the landing verdict for one task")
    verify_verb.add_argument("manifest")
    verify_verb.add_argument("task_id")

    lease = verbs.add_parser("lease", help="inspect or break the lease")
    lease.add_argument("manifest")
    lease.add_argument("--break", action="store_true", dest="break_lease")

    pair_verb = verbs.add_parser("pair", help="split a mixed manifest into claude and grok members")
    pair_sub = pair_verb.add_subparsers(dest="pair_verb", required=True)
    split_verb = pair_sub.add_parser("split", help="write sibling manifests and a pair file")
    split_verb.add_argument("manifest")
    split_verb.add_argument("--out-dir", dest="out_dir",
                            help="directory for the pair files; default is next to the source")
    pair_validate = pair_sub.add_parser("validate", help="check a pair file and both members")
    pair_validate.add_argument("pair")

    dispatch_verb = verbs.add_parser("dispatch",
                                     help="run claude and grok tasks at once, merging in order")
    dispatch_verb.add_argument("target", help="a pair file, or a mixed manifest")
    dispatch_verb.add_argument("--retry-blocked", action="store_true",
                               help="retry tasks whose records read blocked")
    dispatch_verb.add_argument("--policy", choices=("serial", "parallel"),
                               help="run policy; default is an interactive serial-default choice, or serial without a TTY")
    dispatch_verb.add_argument("--detach", action="store_true",
                               help="start the dispatch in its own session, logging to the state "
                                    "directory, and return at once")
    dispatch_verb.add_argument("--wait-for-lease", type=int, nargs="?",
                               const=DEFAULT_LEASE_WAIT_MINUTES,
                               dest="wait_for_lease", metavar="MINUTES",
                               help="when another runner holds the lease, wait for it to clear")
    dispatch_verb.add_argument("--follow", action="store_true",
                               help="detach, then follow this dispatch in the foreground")
    _add_follow_options(dispatch_verb)

    feed_verb = verbs.add_parser("feed", help="keep a manifest running: append ready cards a "
                                              "few at a time, run, read the summary, repeat")
    feed_verb.add_argument("manifest")
    feed_verb.add_argument("--dry-run", action="store_true", dest="dry_run",
                           help="print what the next cycle would append and leave; writes "
                                "nothing and runs nothing")
    feed_verb.add_argument("--once", action="store_true",
                           help="run a single cycle and leave, without waiting")
    feed_verb.add_argument("--stop", action="store_true",
                           help="ask the running feeder to leave after its current cycle; "
                                "nothing is killed")
    feed_verb.add_argument("--restart", action="store_true",
                           help="ask the running feeder to leave, wait for it, then take its "
                                "place; nothing is killed")
    feed_verb.add_argument("--detach", action="store_true",
                           help="start the feeder in its own session, logging beside the "
                                "manifest, and return at once")
    feed_verb.add_argument("--notify", action="store_true",
                           help="fire a macOS notification when the feeder stops, excludes a "
                                "task, or meets a skipped card, and pass --notify to each run")
    return parser


def _load(path, out):
    try:
        return manifest_module.load(path), None
    except manifest_module.ManifestError as exc:
        out.write("%s\n" % exc)
        return None, EXIT_CONFIG


def _store_for(manifest, env):
    return state.StateStore(manifest.path, manifest.project.repo, home=env.get("HOME"))


def _adapter_for(manifest, env, out):
    try:
        return adapters.build(manifest, env=env), None
    except adapters.ConfigurationError as exc:
        out.write("%s\n" % exc)
        return None, EXIT_CONFIG


def cmd_validate(args, env, out):
    if pair_module.is_pair_file(args.manifest):
        return _validate_pair_path(args.manifest, env, out)
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    result = manifest_module.validate(manifest, check_environment=True, env=env)
    errors, warnings = list(result.errors), list(result.warnings)
    adapter = None
    if result.ok:
        # Issue #20. The runner reads every card at launch and skips a task whose text trips the
        # R41 scan; validate has the same cards in reach, so it makes the same checks first.
        adapter, failure = _adapter_for(manifest, env, out)
        if failure:
            return failure
        card_errors, card_warnings = brief_module.check_cards(manifest, adapter)
        errors += card_errors
        warnings += card_warnings
    for applied in result.defaults_applied:
        out.write("default applied: %s\n" % applied)
    for warning in warnings:
        out.write("warning: %s\n" % warning)
    for error in errors:
        out.write("error: %s\n" % error)
    if errors:
        if args.list_candidates:
            # Issue #24. A manifest refused for a missing qualifying sentence is exactly the one
            # whose author needs the card list to write that sentence.
            _list_candidates(manifest, adapter, env, out)
        out.write("%s is not valid: %d error(s)\n" % (args.manifest, len(errors)))
        return EXIT_CONFIG
    out.write("%s is valid: %d task(s), %s adapter, %s mode%s\n"
              % (args.manifest, len(manifest.tasks), manifest.tracker.adapter, manifest.shipping_mode,
                 "" if manifest_module.pushes(manifest) else ", push off: nothing will be pushed"))
    out.write("closeout may touch: %s\n" % ", ".join(result.allowed_paths))
    if args.list_candidates:
        _list_candidates(manifest, adapter, env, out)
    return EXIT_OK


def _list_candidates(manifest, adapter, env, out):
    """The tracker's open cards (issue #24): anything reading one of the manifest's done statuses,
    or the github board status it names as terminal, is left out, since none of those can be a
    task. Builds the adapter itself when validation stopped before one existed, and a failure
    to build one is a printed line, never a second exit code."""
    if adapter is None:
        adapter, failure = _adapter_for(manifest, env, out)
        if failure:
            return
    done = {str(name).lower() for name in manifest.tracker.done_statuses}
    if manifest.tracker.status_field:
        done.add(str(manifest.tracker.status_field).lower())
    candidates = [entry for entry in adapter.candidates()
                  if str(entry.get("status") or "").lower() not in done]
    if not candidates:
        out.write("no candidate tasks read from the tracker\n")
    for entry in candidates:
        out.write("candidate: %s  %s  [%s]\n"
                  % (entry.get("id"), entry.get("title"), entry.get("status")))


def cmd_run(args, env, out):
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    result = manifest_module.validate(manifest, check_environment=True, env=env,
                                      check_branches=False)
    if not result.ok:
        for error in result.errors:
            out.write("error: %s\n" % error)
        out.write("refusing to run an invalid manifest; fix it and run validate again\n")
        return EXIT_CONFIG
    adapter, failure = _adapter_for(manifest, env, out)
    if failure:
        return failure
    if getattr(args, "detach", False) or getattr(args, "follow", False):
        # `--follow` implies `--detach`: a foreground run is already in the foreground, so there
        # would be nothing to follow.
        return _detach(args, manifest, env, out)
    run_kwargs = {
        "adapter": adapter,
        "home": env.get("HOME"),
        "base_env": env,
        "retry_blocked": args.retry_blocked,
        "wait_for_lease_seconds": _wait_seconds(args),
        "stream": lambda line: out.write(line + "\n"),
        "notifier": notify.build(getattr(args, "notify", False)),
    }
    # U1's routing seam is intentionally this narrow: U4 supplies the coordinator while the
    # serial runner and all of its call arguments remain byte-for-byte the established path.
    runner = run_module.run_triple if manifest.execution.mode == "triple" else run_module.run
    outcome = runner(manifest, **run_kwargs)
    if outcome.message:
        out.write("%s\n" % outcome.message)
    if outcome.store is not None:
        out.write(summary.render(summary.build(manifest, outcome.store)) + "\n")
    return outcome.exit_code


def _wait_seconds(args):
    minutes = getattr(args, "wait_for_lease", None)
    return minutes * 60 if minutes else None


def detach_command(entry, manifest_path, retry_blocked, notify_on=False, wait_minutes=None,
                   verb="run", policy=None):
    """The argv for a detached runner.

    `-u` is load-bearing. The child's stdout is `runner.log`, and a block buffered Python writes
    nothing to a file until 8KB or exit, so without it the log SKILL.md calls followable stays
    empty for the length of the run.

    `--notify` is the whole channel by which a detached runner learns the operator wants
    notifications (issue #44). The child is an ordinary `run` with no `--detach`, so it takes the
    same code path a foreground `run --notify` takes and there is no second mechanism to keep in
    step.

    The parameter is `notify_on` rather than mirroring the flag name the way `retry_blocked`
    does, because `notify` is a module this file imports and calls twice elsewhere; a bool of
    that name here would shadow it for anyone later reaching for `notify.available()` inside
    this function.
    """
    command = [sys.executable, "-u", entry, verb, manifest_path]
    if retry_blocked:
        command.append("--retry-blocked")
    if notify_on:
        command.append("--notify")
    if wait_minutes:
        # Issue #23. The child waits, so the operator's shell returns at once and the queue
        # outlives it, which is what a hand written watcher loop was standing in for.
        command += ["--wait-for-lease", str(wait_minutes)]
    if policy:
        command += ["--policy", policy]
    return command


def _detach(args, manifest, env, out, verb="run"):
    """Start the same `run` in its own session and return, or follow it when asked. `setsid` does
    not exist on macOS, so the /relay skill had to improvise a wrapper on the first Cratekit run;
    `start_new_session` is the portable form. `caffeinate -i` keeps a Mac awake for the run when
    it is available."""
    store = _store_for(manifest, env)
    log_path = store.path("runner.log")
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay_cli.py")
    command = detach_command(entry, os.path.abspath(args.manifest), args.retry_blocked,
                             notify_on=getattr(args, "notify", False),
                             wait_minutes=getattr(args, "wait_for_lease", None),
                             verb=verb, policy=getattr(args, "policy", None))
    if shutil.which("caffeinate"):
        command = ["caffeinate", "-i"] + command
    following = getattr(args, "follow", False)
    # Read the floor before the child can write anything. That is what makes a follower's "only
    # what this launch produced" exact rather than a race against process startup.
    floor = tail_module.read_floor(manifest, store) if following else None
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env)
    out.write("runner detached: pid %d\n" % proc.pid)
    out.write("state: %s\n" % store.dir)
    out.write("runner log: %s\n" % log_path)
    if not following:
        return EXIT_OK
    return _follow(args, manifest, store, out, floor=floor, proc=proc)


def cmd_status(args, env, out):
    """Reads state and nothing else. It never acquires the lease, so an operator can ask what a
    live run is doing without disturbing it.

    It answers two questions now (issue #44). Where the run is, which is the cursor, the lease,
    and the terminal record it always printed. And how far along it is, which is the counts, the
    elapsed, and the rough remaining estimate `progress` derives from the record stamps.
    """
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    store = _store_for(manifest, env)
    raw = store.read()
    if raw is None:
        out.write("no state for %s yet\n" % args.manifest)
        return EXIT_OK
    word = store.status_word()
    # The progress view is built from the read above rather than taking its own, so the counts,
    # the durations, and the per task lines below all describe one moment. `live` is what stops a
    # crashed run's last record from counting to the present: without it the same screen would
    # print `status: crashed` beside a task that has been "running" for eight hours.
    view = progress.build(manifest, store, raw=raw, live=(word == "running"))
    out.write("status: %s\n" % word)
    cursor = raw.get("cursor", 0)
    out.write("cursor: %d of %d task(s)\n" % (cursor, len(manifest.tasks)))
    for line in progress.lines(view):
        out.write(line + "\n")
    # The state directory is keyed on the manifest's real path, so editing the manifest in place
    # keeps the directory and everything the previous run left in it. Say so rather than clamping
    # the number: the cursor and the terminal record are true facts, about a run this manifest no
    # longer describes. "different" rather than "longer", because a manifest whose tasks were
    # swapped for others of the same count reaches here too.
    ids = {task.id for task in manifest.tasks}
    records = store.records()
    stale = cursor > len(manifest.tasks) or any(task_id not in ids for task_id in records)
    if stale:
        out.write("stale state: this directory is keyed on the manifest path and holds a run of "
                  "a different manifest from the one loaded now\n")
    lease = store.lease()
    if lease:
        out.write("lease: pid %s on %s\n" % (lease.get("holder_pid"), lease.get("hostname")))
    terminal = store.terminal()
    if terminal:
        out.write("terminal record: %s%s\n"
                  % (terminal.get("run_status"), " (of that previous run)" if stale else ""))
        if terminal.get("halt_task"):
            out.write("halted on %s with class %s\n"
                      % (terminal.get("halt_task"), terminal.get("halt_class")))
    card_audit = raw.get("audit")
    if card_audit:
        # The last run end audit, as the state file carries it. `status` reads state and
        # nothing else, so this is what the runner found when it finished, not a fresh read;
        # `relay audit` is the fresh one.
        for line in audit_module.lines(card_audit.get("findings") or [],
                                       at=card_audit.get("at")):
            out.write(line + "\n")
    # Manifest order, then whatever the state directory still holds from a different list, which
    # is the ordering `summary.build` already uses for the same inputs. Sorting by id put the
    # tasks in an order the run never followed.
    for entry in view["tasks"]:
        out.write("  %s %s%s\n" % (entry["id"], progress.task_line(entry),
                                   "  (not in this manifest)" if not entry["in_manifest"] else ""))
    out.write("state: %s\n" % store.dir)
    return EXIT_OK


def _follow(args, manifest, store, out, floor=None, proc=None):
    """The one following path, shared by `tail` and `run --follow`.

    `proc` is the run this call launched, and it is what separates the two callers: only a caller
    that launched a run can watch that process, and only it owes the operator the summary a
    foreground `run` would have printed. `tail` follows a run somebody else started and passes
    none.

    It also decides who notifies (KTD4). A follower that launched its own run has already passed
    `--notify` down to that child, which notifies for the whole run rather than only until this
    follower's `--for` bound, so this one stays quiet and each phase event notifies exactly once.
    `tail` launched nothing, so it is the notifier.

    Four endings. A run status maps to an exit code. `None` is the `--for` bound, which is not a
    failure: the run continues. `GONE` is a launched process that exited without a record. An
    interrupt is the operator, which is an ordinary ending too.
    """
    launched = proc is not None

    def keep_following():
        out.write("state: %s\n" % store.dir)
        out.write("follow it again: relay tail %s\n" % args.manifest)

    try:
        outcome = tail_module.follow(
            manifest, store, lambda line: out.write(line + "\n"), floor=floor,
            deadline_seconds=getattr(args, "for_seconds", None),
            phases_only=getattr(args, "phases", False),
            notifier=notify.build(getattr(args, "notify", False) and not launched),
            runner_alive=(lambda: proc.poll() is None) if launched else None,
            bar=getattr(args, "bar", False))
    except KeyboardInterrupt:
        # The operator stopping a follower is an ordinary ending, not a fault, and the runner is
        # in its own session so this never reached it (R49).
        out.write("\n")
        out.write("stopped following; the run continues\n")
        keep_following()
        return EXIT_OK
    if outcome is tail_module.GONE:
        out.write("the runner exited without writing a terminal record; read %s\n"
                  % store.path("runner.log"))
        return proc.returncode or EXIT_CONFIG
    if outcome is None:
        out.write("still running after %s second(s); the run continues\n" % args.for_seconds)
        keep_following()
        return EXIT_OK
    if launched:
        out.write(summary.render(summary.build(manifest, store)) + "\n")
    return EXIT_HALTED if outcome == contracts.RUN_HALTED else EXIT_OK


def cmd_tail(args, env, out):
    """Follows the run's task logs and prints them decoded, one line per event. Reads state and
    the log files and nothing else, so like `status` it never acquires the lease and can run
    beside a live runner.

    Takes no floor: `tail` is for a run somebody else launched, so everything on disk is in
    scope. The manifest is loaded but not validated: a reader should still be able to watch a run
    whose manifest was edited since it started, and `_load` already refuses one it cannot parse.
    """
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    store = _store_for(manifest, env)
    return _follow(args, manifest, store, out)


def cmd_audit(args, env, out):
    """The card audit on demand (stale cards, R7). Reads the tracker, the state file, and git,
    and prints which cards disagree. Takes no lease and writes nothing, so it is safe beside a
    live run; under one, a record in flight is a process at work rather than a stale card. The
    runner performs the same audit at run end and writes that one, which `status` and
    `summary` then show."""
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    adapter, failure = _adapter_for(manifest, env, out)
    if failure:
        return failure
    store = _store_for(manifest, env)
    live = store.status_word() == "running"
    findings = audit_module.build(manifest, store, adapter, env=env, live=live)
    for line in audit_module.lines(findings):
        out.write(line + "\n")
    return EXIT_OK


def cmd_summary(args, env, out):
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    store = _store_for(manifest, env)
    data = summary.build(manifest, store)
    if args.as_json:
        out.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
    else:
        out.write(summary.render(data) + "\n")
    return EXIT_HALTED if data["run_status"] == contracts.RUN_HALTED else EXIT_OK


def cmd_verify(args, env, out):
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    adapter, failure = _adapter_for(manifest, env, out)
    if failure:
        return failure
    store = _store_for(manifest, env)
    record = store.get(args.task_id)
    if record is None:
        out.write("no record for %s in %s\n" % (args.task_id, store.dir))
        return EXIT_CONFIG
    verdict = verify.verify(manifest, record, adapter, scope=verify.SCOPE_FULL, do_fetch=True)
    for name, check in verdict.checks.items():
        out.write("  %-26s %s  %s\n" % (name, check["result"], json.dumps(check["evidence"], sort_keys=True)))
    out.write("%s: %s\n" % (args.task_id, "landed" if verdict.landed else "not landed"))
    return EXIT_OK if verdict.landed else EXIT_HALTED


def cmd_lease(args, env, out):
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    store = _store_for(manifest, env)
    lease = store.lease()
    if args.break_lease:
        store.break_lease()
        out.write("lease broken; it was %s\n" % (json.dumps(lease, sort_keys=True) if lease else "free"))
        return EXIT_OK
    if not lease:
        out.write("lease: free\n")
        return EXIT_OK
    out.write("lease: pid %s on %s, manifest %s, heartbeat %s\n"
              % (lease.get("holder_pid"), lease.get("hostname"), lease.get("manifest"),
                 lease.get("heartbeat_at")))
    return EXIT_LEASE


def _validate_pair_path(path, env, out):
    try:
        loaded = pair_module.load(path)
    except pair_module.PairError as exc:
        out.write("%s\n" % exc)
        return EXIT_CONFIG
    errors = pair_module.validate(loaded, env=env)
    for item in errors:
        out.write("error: %s\n" % item)
    if errors:
        out.write("%s is not a valid pair: %d error(s)\n" % (path, len(errors)))
        return EXIT_CONFIG
    grouped = pair_module.queues(pair_module.combine(loaded))
    out.write("%s is a valid pair: %d claude task(s), %d grok task(s), merge order is %s\n"
              % (path, len(grouped["claude"]), len(grouped["grok"]),
                 " ".join(grouped["order"])))
    return EXIT_OK


def _load_dispatch_target(path, env, out):
    """A pair file remains compatible; any ordinary validated manifest may dispatch."""
    if pair_module.is_pair_file(path):
        try:
            loaded = pair_module.load(path)
        except pair_module.PairError as exc:
            out.write("%s\n" % exc)
            return None, EXIT_CONFIG
        errors = pair_module.validate(loaded, env=env)
        if errors:
            for item in errors:
                out.write("error: %s\n" % item)
            out.write("refusing to dispatch an invalid pair; fix it and run pair validate again\n")
            return None, EXIT_CONFIG
        try:
            return pair_module.combine(loaded), None
        except pair_module.PairError as exc:
            out.write("%s\n" % exc)
            return None, EXIT_CONFIG
    manifest, failure = _load(path, out)
    if failure:
        return None, failure
    result = manifest_module.validate(manifest, check_environment=True, env=env,
                                      check_branches=False)
    if not result.ok:
        for error in result.errors:
            out.write("error: %s\n" % error)
        out.write("refusing to dispatch an invalid manifest; fix it and run validate again\n")
        return None, EXIT_CONFIG
    if manifest.execution.mode == "triple":
        out.write("execution.mode triple has its own exact coordinator; use relay run\n")
        return None, EXIT_CONFIG
    return manifest, None


def cmd_pair(args, env, out):
    if args.pair_verb == "validate":
        return _validate_pair_path(args.pair, env, out)
    manifest, failure = _load(args.manifest, out)
    if failure:
        return failure
    result = manifest_module.validate(manifest, check_environment=True, env=env,
                                      check_branches=False)
    if not result.ok:
        for error in result.errors:
            out.write("error: %s\n" % error)
        out.write("refusing to split an invalid manifest; fix it and run validate again\n")
        return EXIT_CONFIG
    try:
        loaded = pair_module.split(manifest, out_dir=args.out_dir)
    except pair_module.PairError as exc:
        out.write("%s\n" % exc)
        return EXIT_CONFIG
    grouped = pair_module.queues(pair_module.combine(loaded))
    out.write("wrote pair %s\n" % loaded.path)
    out.write("  claude: %s (%d task(s))\n" % (loaded.claude.path, len(grouped["claude"])))
    out.write("  grok: %s (%d task(s))\n" % (loaded.grok.path, len(grouped["grok"])))
    out.write("  order: %s\n" % " ".join(grouped["order"]))
    return EXIT_OK


def cmd_dispatch(args, env, out):
    manifest, failure = _load_dispatch_target(args.target, env, out)
    if failure:
        return failure
    adapter, failure = _adapter_for(manifest, env, out)
    if failure:
        return failure
    if manifest.execution.mode == "triple" and getattr(args, "policy", None):
        out.write("execution.mode triple owns its exact coordinator schedule; --policy is unavailable\n")
        return EXIT_CONFIG
    policy = getattr(args, "policy", None) or _choose_dispatch_policy(out)
    args.policy = policy
    if getattr(args, "detach", False) or getattr(args, "follow", False):
        args.manifest = args.target
        return _detach(args, manifest, env, out, verb="dispatch")
    outcome = run_module.dispatch(manifest, adapter=adapter, home=env.get("HOME"), base_env=env,
                                  retry_blocked=args.retry_blocked,
                                  policy=policy,
                                  wait_for_lease_seconds=_wait_seconds(args),
                                  stream=lambda line: out.write(line + "\n"),
                                  notifier=notify.build(getattr(args, "notify", False)))
    if outcome.message:
        out.write("%s\n" % outcome.message)
    if outcome.store is not None:
        out.write(summary.render(summary.build(manifest, outcome.store)) + "\n")
    return outcome.exit_code


def cmd_feed(args, env, out, deps=None):
    """The feeder (feeder plan). Exit codes keep the contract every verb has: 0 the feeder left
    on its own terms (the stop file, a day with nothing ready, `--once`, `--dry-run`), 1 the
    manifest, the sidecar, or the checkout needs a person, 2 every task died quickly for the
    whole usage limit allowance, 3 another feeder holds this manifest.

    `deps` is the suite's way in; an operator never passes it.
    """
    paths = feeder_module.paths_for(args.manifest)
    if args.stop:
        feeder_module.request_stop(paths)
        out.write("stop requested: %s\nthe feeder leaves after its current cycle\n" % paths.stop)
        return EXIT_OK
    if not os.path.isfile(paths.manifest):
        out.write("manifest not found: %s\n" % paths.manifest)
        return EXIT_CONFIG
    try:
        config = feeder_module.load_config(paths.config)
        if args.detach:
            return _detach_feeder(args, paths, config, env, out)
        if deps is None:
            deps = feeder_module.build_deps(config, env, notifier=notify.build(args.notify),
                                            notify_on=args.notify)
        loop = feeder_module.Feeder(paths, config, deps, env, out, dry_run=args.dry_run,
                                    once=args.once)
    except feeder_module.ConfigError as exc:
        out.write("%s\n" % exc)
        return EXIT_CONFIG
    if args.dry_run:
        # Reads only, so it takes no lock and is safe beside a live feeder.
        return loop.run()
    if args.restart:
        lock = feeder_module.wait_for_lock(paths, deps.sleep, loop.log)
    else:
        lock = feeder_module.acquire_lock(paths)
    if lock is None:
        out.write("another feeder holds %s; use --restart to take its place or --stop to end "
                  "it\n" % paths.lock)
        return EXIT_LEASE
    try:
        return loop.run()
    finally:
        lock.close()


def _detach_feeder(args, paths, config, env, out):
    """The same `feed` in its own session, its output appended beside the manifest. `-u` for
    the reason `detach_command` gives: a block buffered child writes nothing until it exits."""
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "relay_cli.py")
    command = [sys.executable, "-u", entry, "feed", paths.manifest]
    command += [flag for flag, on in (("--once", args.once), ("--restart", args.restart),
                                      ("--notify", args.notify)) if on]
    if config.caffeinate and shutil.which("caffeinate", path=env.get("PATH")):
        command = ["caffeinate", "-i"] + command
    with open(paths.out, "ab") as log:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True, env=env)
    out.write("feeder detached: pid %d\n" % proc.pid)
    out.write("feeder log: %s\n" % paths.log)
    out.write("feeder output: %s\n" % paths.out)
    out.write("stop it with: relay feed %s --stop\n" % paths.manifest)
    return EXIT_OK


def _choose_dispatch_policy(out, input_fn=input, stdin=None):
    """Ask only a real terminal operator.  EOF and every noninteractive path are serial."""
    source = sys.stdin if stdin is None else stdin
    if not getattr(source, "isatty", lambda: False)():
        out.write("run policy: serial (noninteractive default)\n")
        return "serial"
    out.write("Run policy:\n  1. Serial (default) — one task settles before the next\n"
              "  2. Parallel — Relay overlaps only proven-independent tasks\n")
    try:
        choice = input_fn("Choose [1]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        choice = ""
    policy = "parallel" if choice in ("2", "parallel") else "serial"
    out.write("run policy: %s\n" % policy)
    return policy


VERBS = {
    "validate": cmd_validate,
    "run": cmd_run,
    "status": cmd_status,
    "tail": cmd_tail,
    "summary": cmd_summary,
    "audit": cmd_audit,
    "verify": cmd_verify,
    "lease": cmd_lease,
    "pair": cmd_pair,
    "dispatch": cmd_dispatch,
    "feed": cmd_feed,
}


def main(argv=None, env=None, out=None):
    env = dict(os.environ if env is None else env)
    out = out or sys.stdout
    try:
        args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_CONFIG
    return VERBS[args.verb](args, env, out)

"""The card audit (stale cards, 2026-09-08): which cards disagree with the record and with git.

A board goes stale in one way the run loop cannot see from inside a task: the card's status is
right for a moment the run has moved past. The Task process moves a card to the in review status
at its first step, and a Task that then blocks, halts, times out, or dies with the runner leaves
that status behind. The Closeout for a blocked or halted Task now returns the card, but no
Closeout runs after a dirty timeout, a run scoped halt, or a crash, and nothing at all runs
when the operator closes or reopens a card by hand between runs. This module is the pass that
reads every card once and says which ones disagree.

Shaped like `progress` and `summary`: a `build` that returns data and a `lines` that renders it.
It reads the adapter, the store, and git, takes no Lease, and writes nothing; the Runner writes
what `build` returned under its own Lease at run end, and the `audit` verb prints the same view
without writing, because a reader beside a live run would race the Runner for the state file.

Four disagreements, each a class from `contracts.AUDIT_CLASSES`, and every one is a report
rather than a repair. The Runner never writes to a tracker, so the fix is the operator's, and
each sentence says what to move and where.
"""
from . import contracts, gitread, verify


def _same(a, b):
    return bool(a) and bool(b) and str(a).lower() == str(b).lower()


def _finding(klass, task_id, text, card_status=None, record_status=None):
    return {"class": klass, "task": task_id, "text": text,
            "card_status": card_status, "record_status": record_status}


def build(manifest, store, adapter, env=None, live=False, records=None):
    """The audit as data: a list of findings, empty when every card agrees.

    `live` is whether a Runner is driving this manifest now. Under a live run a record in flight
    is a process at work, and its card reading in review is right. With no live run, a record
    still reading running or merging belongs to a runner that died, and its card is as stale as a
    blocked one's. The Runner passes False at run end, where nothing is in flight; the verb reads
    the lease.

    `records` lets a caller hand in the store's records rather than take a second read. A
    failure reading git or the store is one finding on the run, never an exception out of here:
    an audit that raised would turn a report into a halt.
    """
    try:
        records = dict(records if records is not None else store.records())
    except Exception as exc:
        return [_finding(contracts.AUDIT_FAILED, None,
                         "the audit could not read the state file: %s" % exc)]
    repo = manifest.project.repo
    default = verify.default_branch_of(manifest)
    in_review = manifest.tracker.in_review_status
    try:
        head = gitread.rev_parse(repo, default)
    except Exception:
        head = None

    findings = []
    for task in manifest.tasks:
        task_id = task.id
        record = records.get(task_id) or {}
        record_status = record.get("status") or "todo"
        try:
            card = adapter.status(task_id) or {}
        except Exception as exc:
            card = {"skipped": "adapter.status raised: %s" % exc}
        if card.get("skipped"):
            findings.append(_finding(
                contracts.AUDIT_UNREADABLE, task_id,
                "%s's card could not be read: %s" % (task_id, card["skipped"]),
                None, record_status))
            continue
        status = card.get("status")
        terminal = bool(card.get("terminal"))
        in_flight = record_status in contracts.IN_FLIGHT_STATUSES

        if _same(status, in_review) and not (live and in_flight):
            baseline = record.get("baseline_tracker_status")
            back = ("`%s`" % baseline if baseline and not _same(baseline, in_review)
                    else "its todo status")
            findings.append(_finding(
                contracts.AUDIT_STALE_IN_REVIEW, task_id,
                "%s's card reads %s but its record reads %s; no process is working on it. "
                "Move it to %s by hand." % (task_id, status, record_status, back),
                status, record_status))
        elif record_status == contracts.STATUS_LANDED and not terminal:
            findings.append(_finding(
                contracts.AUDIT_REOPENED, task_id,
                "%s landed at %s but its card reads %s; it was reopened or the close did not "
                "stick. Check the card by hand."
                % (task_id, (record.get("landing_ref") or "an unrecorded reference")[:12],
                   status), status, record_status))
        elif terminal and record_status != contracts.STATUS_LANDED:
            if _landed_by_hand(repo, record, head, task_id):
                continue
            findings.append(_finding(
                contracts.AUDIT_CLOSED_UNLANDED, task_id,
                "%s's card reads %s but nothing landed for it; the next run will skip it. "
                "Confirm the close was deliberate, or reopen the card."
                % (task_id, status), status, record_status))
    return findings


def _landed_by_hand(repo, record, head, task_id):
    """A commit on the default branch since the record's baseline that names the Task: the
    close is honest, and `verify.startup_reverify` promotes the record on the next run. With no
    baseline there is nothing to compare against, and the close is reported for the operator."""
    baseline = record.get("baseline_sha")
    if not baseline or not head:
        return False
    try:
        return verify.hand_landing(repo, baseline, head, task_id) is not None
    except Exception:
        return False


def lines(findings, at=None):
    """The text form. One line for the count, then one per finding, so the same words reach the
    operator from the verb, from `status`, and from the summary's checks by hand."""
    if not findings:
        return ["cards: every card agrees with its record" + (" (audited %s)" % at if at else "")]
    out = ["cards: %d stale card(s)%s" % (len(findings), " (audited %s)" % at if at else "")]
    # Four spaces, not two: `status` prints its per task lines at two, and a finding that opens
    # with a task id would otherwise read as one of them to a reader splitting on that prefix.
    for finding in findings:
        out.append("    %s" % finding["text"])
    return out


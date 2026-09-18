"""Closeout process (U9, KTD4).

One short process runs after every task process exit except a dirty timeout, with two ordered
duties: write the task's outcome to the tracker, then judge whether the task produced a learning
worth keeping and write it if so.

It is a separate process for two reasons that are not interchangeable. The runner cannot do duty
one because the runner never writes to a tracker (R19), and that rule is what makes a runner
defect unable to move a card. The Task process cannot do it either, because it exits before the
merge commit that duty one has to name. Duty two is here rather than in the task process because
a process at the end of a long context is the worst available judge of its own learning, and
because a blocked task deserves the same pass; the 2026-08-25 proof run's best learning came from
its blocker, not from its code.

The closeout is also the one process that commits without the runner's local gate in front of it,
so the brief names the paths it may touch and forbids a push. The runner checks the commit
against that list before pushing (U8's scope check, R53, KTD15). A bound checked before the push
is a guard; the same bound checked after is a report.

Duty two is native: the process judges, and writes the learning itself as one markdown file
under the manifest's docs root, inside the same allowed paths. No plugin is in the loop.

Its ending is a contract, not a judgement call: the last line is `Documentation complete` or
`Documentation skipped`, and anything else is a finding on the record rather than a halt, because
the runner's own verify decides landing and does not need this process's opinion.
"""
import os
import string
from dataclasses import dataclass, field

from . import brief, classify, contracts, launch, manifest as manifest_module, state

OUTCOME_LANDED = "landed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_HALTED = "halted"

RESULT_COMPLETE = "complete"
RESULT_SKIPPED = "skipped"
RESULT_UNFINISHED = "unfinished"

# The closeout's own allowlist floor. Narrower than a task's: it reads, writes docs, and commits.
# The adapter adds what its tracker write needs and the manifest may add more.
BASE_TOOLS = ("Read", "Edit", "Write", "Bash", "Grep", "Glob")

TEMPLATE = "brief-closeout.md"

DATA_HEADER = brief.DATA_HEADER
DATA_BEGIN = brief.DATA_BEGIN
DATA_END = brief.DATA_END

NONE_LINE = "none"

# Where duty two writes, relative to the repository root: the manifest's docs root plus this
# conventional subdirectory, which the brief offers as the default and the project's own
# instructions may override inside the allowed paths.
LEARNINGS_SUBDIR = "solutions"


@dataclass
class CloseoutResult:
    result: str
    findings: list = field(default_factory=list)
    digest: dict = field(default_factory=dict)
    launch_result: object = None
    brief_path: str | None = None
    brief_sha256: str | None = None


def allowed_tools(manifest, adapter, backend=None):
    """The base set, plus what the adapter's tracker write needs, plus the manifest's additions.
    Order is stable and duplicates are dropped, so the same manifest renders the same flag.
    `backend` selects the adapter's grok tool spelling when the Closeout is not on claude."""
    tools = list(BASE_TOOLS)
    extras = adapter.closeout_allowed_tools(backend=backend)
    for extra in tuple(extras) + tuple(manifest.closeout.allowed_tools):
        if extra not in tools:
            tools.append(extra)
    return tuple(tools)


def _bullets(items, empty=NONE_LINE):
    """One item per line, defanged and flattened. Flattening matters: a denied Bash command or
    a last message can contain a newline, and a bullet that spans lines is a bullet a reader
    (or a model) can mistake for the surrounding instructions."""
    flattened = []
    for item in items:
        one_line = " ".join(str(item).split())
        if one_line:
            flattened.append(brief.defang(one_line))
    if not flattened:
        return empty
    return "\n".join("- " + item for item in flattened)


def _denial_lines(digest):
    lines = []
    for finding in (digest or {}).get("findings") or []:
        if finding.get("class") not in (contracts.HALT_DENIED_TOOL, contracts.HALT_PATH_GATE,
                                        contracts.HALT_TRACKER_WRITE_DENIED):
            continue
        lines.append("%s denied on %s" % (finding.get("tool") or "?", finding.get("target") or "?"))
    return lines


def _other_findings(digest):
    lines = []
    for finding in (digest or {}).get("findings") or []:
        if finding.get("class") in (contracts.HALT_DENIED_TOOL, contracts.HALT_PATH_GATE,
                                    contracts.HALT_TRACKER_WRITE_DENIED):
            continue
        lines.append(classify.finding_line(finding))
    return lines


def _comment_lines(comments):
    return [("%s: %s" % (entry.get("id"), entry.get("body", ""))).strip()
            for entry in comments or []]


def _gate_line(gate):
    if not gate:
        return "not run for this outcome"
    return "%s (exit %s), output in %s" % ("passed" if gate.get("ok") else "refused",
                                           gate.get("returncode"), gate.get("log"))


def _timing_line(digest, wall_seconds=None, active_seconds=None):
    if wall_seconds is None and active_seconds is None:
        return "not recorded"
    return "%.0f seconds active, %.0f seconds wall" % (active_seconds or 0, wall_seconds or 0)


def learnings_dir(manifest):
    """The directory duty two writes under: the docs root the manifest names plus
    LEARNINGS_SUBDIR, as a repository relative path with a trailing slash."""
    root = manifest.closeout.docs_root.strip().rstrip("/")
    return "%s/%s/" % (root, LEARNINGS_SUBDIR)


def render(manifest, card, outcome, digest, comments, adapter, allowed_paths, backend,
           landing_ref=None, branch=None, commit_range=None, gate=None,
           wall_seconds=None, active_seconds=None, halt_class=None, cause_line=None,
           return_to=None):
    """The closeout brief. Deterministic from its inputs, like the task brief, and it never
    receives the task process transcript (R27), only the digest the runner composed from it.

    `backend` is required, not defaulted (backends KTD2): only `run()` may default it, because a
    second independent default here is how the brief and the CLI that reads it drift apart
    (backends KTD15). `halt_class`/`cause_line`
    are set only for `OUTCOME_HALTED`, the runner's own values for the halt already raised (R4);
    `cause_line` is defanged because, unlike a landing sha, it can carry task-influenced text (a
    denied call's captured argument, a dirty tree's file list) that must not close the data block
    or forge a runner instruction (R56). `landing_ref` is also passed for `OUTCOME_HALTED` when
    the task's own landed closeout already ran before this halt (a mirror push refusal or a
    failing final verify), naming it keeps the comment from reading as an undifferentiated halt
    on a card the runner already moved to a terminal status. `return_to` (stale cards,
    2026-09-08) is the status the runner wants a blocked or halted card returned to, or None;
    the adapter renders the move sentence, so the brief and the adapter cannot disagree."""
    task_id = card.get("id")
    envelope = (digest or {}).get("envelope") or {}
    landing_line = ""
    if outcome == OUTCOME_HALTED:
        cause_text = "Halt class: %s\nCause: %s" % (
            halt_class or "unknown", brief.defang(cause_line or "no cause line recorded"))
        landing_line = ("Landed at %s, but the run then halted.\n%s" % (landing_ref, cause_text)
                        if landing_ref else cause_text)
    elif outcome == OUTCOME_LANDED and landing_ref:
        landing_line = "Landing reference: %s" % landing_ref
    elif outcome == OUTCOME_LANDED:
        landing_line = "Landing reference: not recorded"
    else:
        landing_line = "No landing reference: this task did not land."
    if commit_range:
        landing_line += "\nCommit range: %s" % commit_range

    values = {
        "task_id": task_id,
        "outcome": outcome,
        "landing_line": landing_line,
        "branch": branch or "none",
        "timing": _timing_line(digest, wall_seconds, active_seconds),
        "gate": _gate_line(gate),
        "blockers": _bullets(envelope.get("blockers") or []),
        "learnings": _bullets(envelope.get("learnings") or []),
        "denials": _bullets(_denial_lines(digest)),
        "findings": _bullets(_other_findings(digest)),
        "data_header": DATA_HEADER,
        "data_begin": DATA_BEGIN,
        "data_end": DATA_END,
        "title": brief.defang(str(card.get("title") or "")).strip(),
        "description": brief.defang(str(card.get("description") or "")).strip(),
        "comments": brief.defang(_bullets(_comment_lines(comments))),
        "duty_one": adapter.closeout_instructions(outcome, return_to=return_to, backend=backend),
        "learnings_dir": learnings_dir(manifest),
        "allowed_paths": _bullets(allowed_paths),
        "complete_line": contracts.CLOSEOUT_COMPLETE_LINE,
        "skipped_line": contracts.CLOSEOUT_SKIPPED_LINE,
    }
    path = os.path.join(brief.TEMPLATE_DIR, TEMPLATE)
    try:
        with open(path, encoding="utf-8") as handle:
            template = string.Template(handle.read())
    except OSError as exc:
        raise brief.BriefError("closeout template could not be read: %s" % exc)
    try:
        return template.substitute(values)
    except KeyError as exc:
        raise brief.BriefError("closeout template names an unknown placeholder %s" % exc)


def parse(last_message):
    """The ending contract. The terminal line must be the last thing in the message, so a run
    that kept going after printing it is unfinished rather than complete."""
    text = (last_message or "").strip()
    if not text:
        return RESULT_UNFINISHED
    last = ""
    for line in reversed(text.splitlines()):
        if line.strip():
            last = line.strip().strip("*`_ ")
            break
    if last == contracts.CLOSEOUT_COMPLETE_LINE:
        return RESULT_COMPLETE
    if last == contracts.CLOSEOUT_SKIPPED_LINE:
        return RESULT_SKIPPED
    return RESULT_UNFINISHED


def _closeout_task(manifest, task_id, backend, task_model=None):
    """A task record shaped for the launcher, carrying the closeout's own model and effort
    (R29): two bounded jobs that need judgement, not depth. `backend` is required for the same
    reason `render`'s is.

    The manifest's closeout model is claude vocabulary. On any other backend it is not a model
    that CLI serves (U14 found codex refusing `sonnet` with a 400 and its Closeout dying without
    a terminal line), so a non claude Closeout runs on the Task's own model, which the operator
    already chose for that backend."""
    model = manifest.closeout.model
    if backend != manifest_module.DEFAULT_BACKEND and task_model:
        model = task_model
    return manifest_module.Task(id=task_id, model=model,
                                effort=manifest.closeout.effort, excluded=False, reason=None,
                                backend=backend)


def run(manifest, card, outcome, digest, comments, adapter, store, allowed_paths,
        backend, task_model=None,
        landing_ref=None, branch=None, commit_range=None, gate=None,
        wall_seconds=None, active_seconds=None, halt_class=None, cause_line=None,
        timeout_seconds=None, return_to=None,
        **launch_kwargs):
    """Render, launch, and read the ending. Returns what happened; it changes no git state and
    writes nothing to the tracker itself. The caller runs the scope check and the push.

    The caller supplies the Task backend. It feeds all three consumers: the
    rendered brief, the launched CLI, and the normalizer that reads what that CLI wrote."""
    task_id = card.get("id")
    text = render(manifest, card, outcome, digest, comments, adapter, allowed_paths, backend,
                  landing_ref=landing_ref, branch=branch, commit_range=commit_range,
                  gate=gate, wall_seconds=wall_seconds,
                  active_seconds=active_seconds, halt_class=halt_class, cause_line=cause_line,
                  return_to=return_to)
    brief_path = store.path("briefs", task_id + ".closeout.md")
    with open(brief_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(brief_path, 0o600)

    if timeout_seconds is None:
        timeout_seconds = manifest.timeouts.closeout_minutes * 60
    launch_result = launch.launch(
        manifest, _closeout_task(manifest, task_id, backend, task_model=task_model), text,
        store.path("logs", task_id + ".closeout.stdout.log"), timeout_seconds,
        allowed=allowed_tools(manifest, adapter, backend=backend),
        disallowed=contracts.CLOSEOUT_DISALLOWED_EXTRA, **launch_kwargs)

    # U7 runs over the closeout transcript too (R44). AE1's denied tracker write most often
    # happens here, after the code has already merged. The backend goes through: a closeout
    # launched on one CLI whose evidence is normalized as another decodes nothing, so `parse()`
    # sees no terminal line and every run appends a CLOSEOUT_UNFINISHED finding.
    closeout_digest = classify.classify(launch_result.transcript_path, launch_result,
                                        adapter.write_tool_patterns(), backend=backend,
                                        review_required=False)
    findings = [finding for finding in closeout_digest.get("findings") or []
                if finding.get("class") != contracts.HALT_NO_ENVELOPE]
    result = RESULT_UNFINISHED if launch_result.timed_out else parse(closeout_digest.get("last_message_tail"))
    if result == RESULT_UNFINISHED:
        findings.append({
            "class": contracts.CLOSEOUT_UNFINISHED,
            "task": task_id,
            "last_message": closeout_digest.get("last_message") or "(no final message)",
        })
    return CloseoutResult(result, findings, closeout_digest, launch_result,
                          brief_path, state.sha256_of(text))


def confirm_blocked_comment(adapter, task_id, baseline_comment_id):
    """R42: a blocked task is only legible to an operator who was not watching if the blocker
    reached the card. Returns a finding when it did not, or when the tracker could not be read,
    so the summary prints the card to check by hand. Never a halt: the run continues."""
    try:
        newer = adapter.comments_since(task_id, baseline_comment_id)
    except Exception as exc:
        return {"class": contracts.BLOCKED_UNRECORDED, "task": task_id,
                "evidence": "the tracker could not be read to confirm the comment: %s" % exc}
    if newer:
        return None
    return {"class": contracts.BLOCKED_UNRECORDED, "task": task_id,
            "evidence": "no comment newer than %r after the closeout" % baseline_comment_id}


def return_to_for(manifest, record):
    """The status a blocked or halted card goes back to, or None when nothing should move
    (stale cards, 2026-09-08). Three refusals, each a reason rather than a gap. No baseline: the
    card could not be read before the run, so there is nowhere known to return it to. Baseline
    equal to the in review status: the operator placed it there before the run, and the runner
    does not second guess that placement. A landing reference on the record: the halt came after
    a landing whose own Closeout already closed the card, and moving a closed card back to todo
    would undo a landing."""
    baseline = record.get("baseline_tracker_status")
    in_review = manifest.tracker.in_review_status
    if not baseline or record.get("landing_ref"):
        return None
    if in_review and str(baseline).lower() == str(in_review).lower():
        return None
    return baseline


def confirm_card_returned(adapter, manifest, task_id, return_to):
    """R4 of the stale cards plan: after a Closeout told to return the card, read it back. A
    finding when it still reads the in review status, or when the read failed, so the summary
    lists the card to move by hand. Never a halt: the run continues, and the runner never moves
    the card itself."""
    in_review = manifest.tracker.in_review_status
    try:
        card = adapter.status(task_id) or {}
    except Exception as exc:
        return {"class": contracts.CARD_LEFT_IN_REVIEW, "task": task_id,
                "card_status": "unreadable", "return_to": return_to,
                "evidence": "the tracker could not be read to confirm the return: %s" % exc}
    if card.get("skipped"):
        return {"class": contracts.CARD_LEFT_IN_REVIEW, "task": task_id,
                "card_status": "unreadable", "return_to": return_to,
                "evidence": "the tracker could not be read to confirm the return: %s"
                            % card["skipped"]}
    status = card.get("status")
    if in_review and status and str(status).lower() == str(in_review).lower():
        return {"class": contracts.CARD_LEFT_IN_REVIEW, "task": task_id,
                "card_status": status, "return_to": return_to,
                "evidence": "the card reads %s after the closeout" % status}
    return None

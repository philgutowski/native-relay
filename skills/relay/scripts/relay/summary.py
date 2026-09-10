"""The run summary (U10, R36, R46).

The JSON is the summary; the text is rendered from it, never the other way round. That direction
is the whole point of R46: a later session, the `/relay` skill, and the operator's own eye all
read the same facts, and the text can never say something the JSON does not carry.

Every line the text prints names the JSON field it came from, which `lines()` returns alongside
it, so the two cannot drift without a test noticing.

What a summary is for: an operator who was not watching should learn why a task did not land
without opening a transcript. So every task line carries a class and a cause built from the
evidence on the record, and the checks a human still has to make by hand are listed separately
rather than buried in prose. It points at a class, a cause, and the state directory, and never
at a machine readable file the operator would have to parse to learn anything.
"""
import shlex
import string

from . import audit, contracts, gitread, manifest as manifest_module, verify

SCHEMA_VERSION = 1


def _template_fields():
    """Every field name any halt line template can ask for, defaulted to a placeholder.

    Derived from the templates rather than listed by hand. A hand written list goes stale the
    moment a template gains a field, and it fails in the wrong direction when it does: the
    missing key raises inside `format`, the except below swallows it, and the operator gets the
    raw template with its braces still in it instead of a sentence.
    """
    fields = {}
    for template in contracts.HALT_LINES.values():
        for _, field, _, _ in string.Formatter().parse(template):
            if field:
                fields[field] = "?"
    return fields


LINE_FIELD_DEFAULTS = _template_fields()


def line_fields(*sources):
    fields = dict(LINE_FIELD_DEFAULTS)
    for source in sources:
        for key, value in (source or {}).items():
            if value is not None and not isinstance(value, (dict, list)):
                fields[key] = value
    return fields


def cause_line(halt_class, *evidence):
    """The halt class's sentence, filled from whatever evidence the record carries."""
    if not halt_class:
        return None
    template = contracts.HALT_LINES.get(halt_class, halt_class)
    try:
        return template.format(**line_fields(*evidence))
    except (KeyError, IndexError, ValueError):
        return template


def _task_entry(store, record):
    task_id = record.get("id")
    evidence = record.get("halt_evidence") or {}
    landing = {"ref": record.get("landing_ref")} if record.get("landing_ref") else {}
    findings = []
    for finding in record.get("findings") or []:
        findings.append({
            "class": finding.get("class"),
            "line": cause_line(finding.get("class"), finding),
        })
    verify_result = record.get("verify") or {}
    failed = [name for name, check in (verify_result.get("checks") or {}).items()
              if check.get("result") == "fail"]
    return {
        "id": task_id,
        "status": record.get("status"),
        "class": record.get("halt_class"),
        # Issue #8: which step of the merge tail refused, on a record whose class alone cannot
        # say. Reset with `halt_class` at every launch, so a stage here describes this attempt.
        "halt_stage": record.get("halt_stage"),
        # Weakest source first. The record carries fields a template may also name, most
        # of them written after the halt, so the evidence the raiser recorded has to win.
        "cause": cause_line(record.get("halt_class"), record, landing, evidence),
        "halt_message": record.get("halt_message"),
        "backend": record.get("backend"),
        # Issue #58. The other half of a routing choice the operator can edit between runs.
        # `.get`, because a record written before the field joined RECORD_FIELDS has no key.
        "model": record.get("model"),
        "landing_ref": record.get("landing_ref"),
        "branch": record.get("branch"),
        "closeout": record.get("closeout"),
        "excluded_reason": record.get("excluded_reason"),
        "continued_past": bool(record.get("continued_past")),
        "wall_seconds": record.get("wall_seconds"),
        "active_seconds": record.get("active_seconds"),
        "verify_failed": failed,
        # Round eight #54: beside `findings`, not as a Cause line. The empty findings list is
        # what gets misread on a backend that enforces nothing at launch, and an operator who
        # only reads the summary would otherwise never meet the bound run._unenforced_scalar
        # records. None on a backend that refuses a denied call itself.
        "unenforced_restrictions": record.get("unenforced_restrictions"),
        "findings": findings,
        "log_path": store.path("logs", "%s.stdout.log" % task_id) if task_id else None,
    }


def _pending_checks(entries, run_status, halt_task, halt_class, state_dir, card_audit=None):
    """R36's last column: what a human still has to do. Each entry is a kind and a sentence, so
    the skill can group them and the text can print them as a list.

    `card_audit` is the run end card audit the state file carries (stale cards, R8). Each of its
    findings is copied in as it is: `audit.build` already wrote the sentence, and rewriting it
    here would be a second place for the words to drift."""
    checks = []
    for entry in entries:
        task_id = entry["id"]
        if entry["status"] == contracts.STATUS_EXCLUDED:
            checks.append({"kind": "excluded", "task": task_id,
                           "text": "%s was skipped: %s. Run it attended."
                                   % (task_id, entry["excluded_reason"] or "no reason recorded")})
        if entry["status"] == contracts.STATUS_HALTED and entry["continued_past"]:
            # Issue #15. The run stepped over this task, so the halted line at the bottom
            # of this list never names it; it needs its own. The retry the record promises
            # needs the branch named here too: it is what a rerun's own pre-flight will
            # refuse on until the operator deletes it (the resume disposition never does).
            branch_note = (" %s is still in place; delete it first." % entry["branch"]
                          if entry["branch"] else "")
            checks.append({"kind": "continued_past", "task": task_id,
                           "text": "%s halted with class %s and the run continued past it."
                                   "%s Repair by hand, then run again to resume."
                                   % (task_id, entry["class"], branch_note)})
        if entry["status"] == contracts.STATUS_BLOCKED and entry["branch"]:
            checks.append({"kind": "stranded_branch", "task": task_id,
                           "text": "%s left %s in place. Keep or delete it by hand."
                                   % (task_id, entry["branch"])})
        if (entry["class"] == contracts.HALT_PATH_GATE
                and entry["halt_stage"] == contracts.TAIL_STAGE_BACKSTOP):
            # Issue #8. The other raiser of this class, and the opposite repair: the Task's work
            # is done and the Runner would not land it, so this asks for a merge rather than for
            # the work. The cause line is reused verbatim rather than restated, because it
            # already names the branch and the paths its raiser recorded.
            checks.append({"kind": "path_gate_backstop", "task": task_id,
                           "text": "%s: %s" % (task_id, entry["cause"]
                                               or contracts.PATH_GATE_CLAUDE_DIR_BACKSTOP)})
        for finding in entry["findings"]:
            if finding["class"] == contracts.BLOCKED_UNRECORDED:
                checks.append({"kind": "unrecorded_blocker", "task": task_id,
                               "text": "%s blocked and its card carries no new comment. Check the "
                                       "card by hand." % task_id})
            elif finding["class"] == contracts.REVIEW_SKIPPED:
                checks.append({"kind": "review_skipped", "task": task_id,
                               "text": "%s completed without running %s. Review the diff by "
                                       "hand." % (task_id, finding.get("review") or "the review step")})
            elif finding["class"] == contracts.CLOSEOUT_UNFINISHED:
                checks.append({"kind": "closeout_unfinished", "task": task_id,
                               "text": "%s: the closeout did not print a terminal line. Its "
                                       "tracker write may be incomplete." % task_id})
            elif finding["class"] == contracts.HALT_PATH_GATE:
                # The transcript raiser, named as such (issue #8). A finding is what the Task
                # met while working, so this line can appear on a record whose halt class came
                # from the merge tail minutes later; saying which raiser it is keeps the two
                # provenances apart. `line`, not `detail`: `_task_entry` keeps a finding's
                # class and its rendered line only, so `detail` was always the fallback here.
                checks.append({"kind": "path_gate_denial", "task": task_id,
                               "text": "%s: in the transcript, %s"
                                       % (task_id, finding["line"]
                                          or contracts.PATH_GATE_CLAUDE_DIR)})
            elif finding["class"] == contracts.CARD_LEFT_IN_REVIEW:
                checks.append({"kind": "card_left_in_review", "task": task_id,
                               "text": "%s: %s" % (task_id, finding["line"])})
    for finding in (card_audit or {}).get("findings") or []:
        checks.append({"kind": finding.get("class"), "task": finding.get("task"),
                       "text": finding.get("text") or ""})
    if run_status == contracts.RUN_HALTED:
        checks.append({"kind": "halted", "task": halt_task,
                       "text": "the run halted on %s with class %s. Repair by hand, then run "
                               "again to resume. State is in %s" % (halt_task, halt_class, state_dir)})
    return checks


def _shipping(manifest):
    """What the run did with the remote (issue #15, R9 of the no push plan). Under push true it
    is the one fact. Under push false it carries the command that ships the default branch, so
    the summary can end on it: a run that pushed nothing has to say so, and say how, rather than
    leave the operator to infer it from a remote that did not move.

    Two git reads, both read only, and a failure in either costs the detail and not the summary."""
    if manifest_module.pushes(manifest):
        return {"push": True}
    repo = manifest.project.repo
    try:
        default = verify.default_branch_of(manifest)
    except Exception:
        default = manifest.project.default_branch or "main"
    try:
        remote = "origin" if "origin" in gitread.remotes(repo) else None
    except Exception:
        remote = None
    return {"push": False, "remote": remote, "default_branch": default,
            "command": "git -C %s push origin %s" % (shlex.quote(repo), shlex.quote(default))}


def _unpushed_check(shipping):
    """The last check by hand of a push false run. Last on purpose: everything above it
    describes what the run did, and this is the one decision left to the operator."""
    if shipping["remote"]:
        text = ("nothing was pushed: shipping.push is false, so every landing above is on the "
                "local %s only. To ship it: %s" % (shipping["default_branch"], shipping["command"]))
    else:
        text = ("nothing was pushed: shipping.push is false, and the repo has no origin remote. "
                "Add one, then ship the local %s with: %s"
                % (shipping["default_branch"], shipping["command"]))
    return {"kind": "unpushed", "task": None, "text": text}


def build(manifest, store):
    """The summary as data. Reads state only; acquires nothing and changes nothing."""
    raw = store.read() or {}
    terminal = raw.get("terminal") or {}
    records = store.records()
    order = [task.id for task in manifest.tasks]
    ordered = [records[task_id] for task_id in order if task_id in records]
    ordered += [record for task_id, record in sorted(records.items()) if task_id not in order]
    entries = [_task_entry(store, record) for record in ordered]
    run_status = store.status_word()
    data = {
        "schema_version": SCHEMA_VERSION,
        "manifest": manifest.path,
        "repo": manifest.project.repo,
        "state_dir": store.dir,
        "run_status": run_status,
        "halt_task": terminal.get("halt_task"),
        "halt_class": terminal.get("halt_class"),
        "cli_version": terminal.get("cli_version"),
        "cli_version_observed": terminal.get("cli_version_observed"),
        "cursor": raw.get("cursor", 0),
        "counts": _counts(entries),
        "tasks": entries,
        # The last run end card audit as the state file carries it, or None. Additive to the
        # schema: a reader of version 1 that never asks for it sees nothing new.
        "audit": raw.get("audit"),
    }
    data["pending_checks"] = _pending_checks(entries, run_status, data["halt_task"],
                                             data["halt_class"], store.dir,
                                             card_audit=data["audit"])
    # Additive to schema version 1, like `audit`.
    data["shipping"] = _shipping(manifest)
    if not data["shipping"]["push"]:
        data["pending_checks"].append(_unpushed_check(data["shipping"]))
    return data


def _counts(entries):
    counts = {}
    for entry in entries:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return counts


def _seconds(entry):
    active = entry.get("active_seconds")
    wall = entry.get("wall_seconds")
    if active is None and wall is None:
        return "not run"
    return "%.0fs active, %.0fs wall" % (active or 0, wall or 0)


def lines(data):
    """The text form as (line, source) pairs. `source` names the JSON field the line came from,
    which is how R46's one direction is kept honest."""
    out = [("relay run %s" % data["run_status"], "run_status")]
    out.append(("manifest: %s" % data["manifest"], "manifest"))
    out.append(("state: %s" % data["state_dir"], "state_dir"))
    if data["halt_class"]:
        out.append(("halted on %s with class %s" % (data["halt_task"], data["halt_class"]),
                    "halt_class"))
    if data.get("audit"):
        # The count only. Each finding's own sentence is already a pending check below.
        out.append((audit.lines(data["audit"].get("findings") or [],
                                at=data["audit"].get("at"))[0], "audit.count"))
    out.append(("", "run_status"))
    for index, entry in enumerate(data["tasks"]):
        source = "tasks[%d]" % index
        head = "%s  %s" % (entry["id"], entry["status"])
        if entry["class"]:
            head += "  [%s]" % entry["class"]
        if entry["backend"]:
            # Both halves of the routing when the record carries both, the CLI alone when it
            # does not. A record that never launched has neither and stays untagged.
            head += "  (%s)" % " ".join(filter(None, (entry["backend"], entry["model"])))
        out.append((head, source + ".status"))
        if entry["cause"]:
            out.append(("    %s" % entry["cause"], source + ".cause"))
        # Directly under the cause, because the cause is the sentence it qualifies. Named rather
        # than silent (issue #8), so a class with two raisers cannot reach an operator
        # unattributed.
        if entry["halt_stage"]:
            out.append(("    refused at the %s step of the merge tail" % entry["halt_stage"],
                        source + ".halt_stage"))
        if entry["halt_message"] and entry["halt_message"] != entry["cause"]:
            out.append(("    %s" % entry["halt_message"], source + ".halt_message"))
        if entry["excluded_reason"]:
            out.append(("    %s" % entry["excluded_reason"], source + ".excluded_reason"))
        # A landed task's cause line already names the ref; printing it twice was the first
        # live run's summary.
        if entry["landing_ref"] and entry["class"] != contracts.HALT_LANDED:
            out.append(("    landed at %s" % entry["landing_ref"], source + ".landing_ref"))
        if entry["branch"]:
            out.append(("    branch left in place: %s" % entry["branch"], source + ".branch"))
        if entry["closeout"]:
            out.append(("    closeout: %s" % entry["closeout"], source + ".closeout"))
        if entry["verify_failed"]:
            out.append(("    verify failed: %s" % ", ".join(entry["verify_failed"]),
                        source + ".verify_failed"))
        for finding_index, finding in enumerate(entry["findings"]):
            out.append(("    finding: %s" % finding["line"],
                        "%s.findings[%d].line" % (source, finding_index)))
        # Last of the per task lines, directly under the findings, because an empty findings
        # list is the thing it qualifies. A JSON key with no line here would be invisible on
        # every default CLI path, which is the R46 direction this module exists to keep.
        if entry["unenforced_restrictions"]:
            out.append(("    %s" % entry["unenforced_restrictions"],
                        source + ".unenforced_restrictions"))
        out.append(("    %s" % _seconds(entry), source + ".active_seconds"))
        out.append(("    output: %s" % entry["log_path"], source + ".log_path"))
        out.append(("", source + ".id"))
    if data["pending_checks"]:
        out.append(("check by hand:", "pending_checks"))
        for index, check in enumerate(data["pending_checks"]):
            out.append(("  %s" % check["text"], "pending_checks[%d].text" % index))
    return out


def render(data):
    return "\n".join(line for line, _ in lines(data))

"""Markdown adapter, read side (U4, R51).

The tracker is a file in the target repo, and the adapter reads it at the remote default branch
head rather than in the working tree. That is the whole point: the closeout process edits the
line and commits it, the runner pushes that commit, and the runner then confirms the close by
reading what actually reached the remote. Reading the working tree would confirm a local edit
nobody else can see.

Under `shipping.push = false` (issue #15) nothing reaches the remote, so the landing and the
closeout's commit both live on the local default branch, and the adapter reads the file at that
branch's head instead. Still a committed state and never the working tree.

The grammar, from the plan:

    - [ ] T-1 Add the brief renderer            an open task
      - 2026-08-24 picked this up               a comment, indented under its task
    - [x] T-2 Wire the run loop (abc1234)       a closed task and its closing reference

A comment id is its position in the task's comment list, so the baseline the runner records
before launch is a count and `comments_since` is everything past it.

Two states only, and the consequence (finding 20, decided 2026-08-26). A line is open or closed,
so this adapter never reports a card in `in_review_status`, and KTD6's route that merges a task
which exited without an envelope on the strength of commits plus a moved card cannot fire here.
Such a task is always treated as blocked. A third mark would not repair that on its own, because
the adapter reads the file at the remote default branch head and a task branch never reaches it
before the merge. `manifest.validate` warns about this so the operator is not waiting for a route
that cannot come. Choosing `in_review_status = "open"` is worse, not better: it would route every
unclosed card.
"""
import re

from .. import gitread, manifest as manifest_module
from . import OUTCOME_HALTED, OUTCOME_LANDED, reference_hit, skipped

TASK_RE = re.compile(r"^-\s*\[(?P<mark>[ xX])\]\s+(?P<id>\S+)\s*(?P<rest>.*?)\s*$")
COMMENT_RE = re.compile(r"^\s+-\s+(?P<body>.*?)\s*$")
REFERENCE_RE = re.compile(r"\((?P<ref>[^()\s]+)\)\s*$")

CLOSEOUT_TOOLS = ("Bash",)


def parse(text):
    """The file as {task id: {"title", "closed", "reference", "comments": [...]}}, in order."""
    tasks = {}
    current = None
    for line in (text or "").splitlines():
        match = TASK_RE.match(line)
        if match:
            rest = match.group("rest")
            reference = None
            found = REFERENCE_RE.search(rest)
            if found:
                reference = found.group("ref")
                rest = rest[:found.start()].strip()
            current = match.group("id")
            tasks[current] = {
                "title": rest,
                "closed": match.group("mark").lower() == "x",
                "reference": reference,
                "comments": [],
            }
            continue
        comment = COMMENT_RE.match(line)
        if comment and current:
            entry = tasks[current]["comments"]
            entry.append({"id": len(entry) + 1, "body": comment.group("body"), "created": None})
            continue
        if line.strip() == "":
            continue
        current = None
    return tasks


class MarkdownAdapter:
    def __init__(self, manifest, read=None):
        self._repo = manifest.project.repo
        self._file = manifest.tracker.file
        self._default_branch = (manifest.project.default_branch
                                or gitread.default_branch(self._repo)
                                or "main")
        self._pushes = manifest_module.pushes(manifest)
        self._ref = ("origin/" + self._default_branch if self._pushes
                     else self._default_branch)
        self._read = read or self._show

    def _show(self):
        return gitread.show(self._repo, self._ref, self._file)

    def _tasks(self):
        """Returns (tasks, None) or ({}, reason). A file that is not on the default branch the
        adapter reads is a reason, not a crash: the operator may not have pushed it yet."""
        try:
            text = self._read()
        except gitread.GitError as exc:
            return {}, "could not read %s at %s: %s" % (self._file, self._ref, exc)
        if text is None:
            return {}, "%s does not exist at %s" % (self._file, self._ref)
        return parse(text), None

    # Interface.
    def candidates(self):
        tasks, _ = self._tasks()
        return [{"id": task_id, "title": entry["title"], "description": "", "status": "open"}
                for task_id, entry in tasks.items() if not entry["closed"]]

    def ready(self, source):
        """Every unchecked box. The file has no way to say one task waits on another, so under
        this adapter ready means open, and `source` is accepted for the interface and unused."""
        tasks, reason = self._tasks()
        if reason:
            return [], reason
        return [{"id": task_id, "title": entry["title"], "description": "", "labels": ()}
                for task_id, entry in tasks.items() if not entry["closed"]], None

    def read(self, task_id):
        tasks, reason = self._tasks()
        entry = tasks.get(task_id)
        if entry is None:
            return {"id": task_id, "title": "", "description": "", "status": None,
                    "skipped": reason or "no line for %s in %s" % (task_id, self._file)}
        return {
            "id": task_id,
            "title": entry["title"],
            "description": "",
            "status": "closed" if entry["closed"] else "open",
        }

    def status(self, task_id):
        tasks, reason = self._tasks()
        entry = tasks.get(task_id)
        if entry is None:
            return skipped(reason or "no line for %s in %s" % (task_id, self._file))
        return {
            "status": "closed" if entry["closed"] else "open",
            "terminal": entry["closed"],
            "reference": entry["reference"] if entry["closed"] else None,
            "skipped": None,
        }

    def comments_since(self, task_id, baseline_comment_id):
        tasks, _ = self._tasks()
        entry = tasks.get(task_id)
        if entry is None:
            return []
        if baseline_comment_id is None:
            return list(entry["comments"])
        try:
            count = int(baseline_comment_id)
        except (TypeError, ValueError):
            return list(entry["comments"])
        return entry["comments"][count:]

    def closing_reference(self, task_id, ref):
        """The line's own reference is the closing reference, so the id it returns is the task
        id rather than a comment id: this tracker has one place to write the landing."""
        tasks, _ = self._tasks()
        entry = tasks.get(task_id)
        if entry is None or not entry["closed"] or not entry["reference"]:
            return None
        return task_id if reference_hit(entry["reference"], ref) else None

    def write_tool_patterns(self):
        return {"tools": (), "bash": (), "paths": (self._file,)}

    def closeout_allowed_tools(self, backend=None):
        return CLOSEOUT_TOOLS

    def closeout_instructions(self, outcome, return_to=None, backend=None):
        """`return_to` is accepted for the interface and ignored: a markdown tracker has only an
        open box and a checked one, and the task process never moves it."""
        if outcome == OUTCOME_LANDED:
            # Under shipping.push = false the runner pushes nothing, so the instruction does not
            # promise that it will.
            after = ("do not push; the runner pushes it under the gate." if self._pushes
                     else "do not push.")
            return ("Edit the task's line in %s: change `[ ]` to `[x]` and append the landing "
                    "reference below in parentheses at the end of the line. Commit that file and "
                    "%s" % (self._file, after))
        if outcome == OUTCOME_HALTED:
            return ("Append one indented comment line under the task's line in %s naming the halt "
                    "class and the cause line below, in the form `  - <date> <text>`. Leave the "
                    "`[ ]` box unchecked. Commit that file and do not push." % self._file)
        return ("Append one indented comment line under the task's line in %s carrying the blocker "
                "digest below, in the form `  - <date> <text>`. Leave the `[ ]` box unchecked. "
                "Commit that file and do not push." % self._file)
